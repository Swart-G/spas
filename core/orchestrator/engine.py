import asyncio
import logging
from collections.abc import Mapping
from inspect import iscoroutinefunction
from math import isfinite
from types import MappingProxyType
from uuid import UUID

from core.incidents.models import Incident, IncidentStatus
from shared.contracts.agents import Agent, validate_agent_result
from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum, StageEnum
from shared.contracts.workflow import DiagnosisData, RemediationData, VerificationData
from shared.time import utc_now

logger = logging.getLogger(__name__)


class Orchestrator:
    """Coordinates processing steps for incidents."""

    def __init__(
        self,
        agent_timeout: float = 10.0,
        *,
        cleanup_timeout: float = 10.0,
        agents: Mapping[RoleEnum, Agent] | None = None,
    ):
        self.storage: dict[UUID, Incident] = {}
        self.agent_timeout = agent_timeout
        self.cleanup_timeout = cleanup_timeout

        self._agents: dict[RoleEnum, Agent] = {}
        if agents is not None:
            for role, agent in agents.items():
                self._validate_agent(role, agent)
                self._agents[role] = agent

        # Prevent concurrent processing of the same incident.
        self._locks: dict[UUID, asyncio.Lock] = {}
        # Keep unfinished agents alive and prevent overlap after a cleanup timeout.
        self._active_agents: dict[UUID, asyncio.Task[AgentResult]] = {}

    @property
    def agents(self) -> Mapping[RoleEnum, Agent]:
        """Registered agents; modifications go through the validated registration API."""
        return MappingProxyType(self._agents)

    @staticmethod
    def _validate_agent(role: RoleEnum, agent: Agent) -> None:
        if not isinstance(role, RoleEnum) or role == RoleEnum.ORCHESTRATOR:
            raise ValueError("Agent role must be TRIAGE, DIAGNOSTIC or REMEDIATION")
        agent_role = getattr(agent, "role", None)
        if not isinstance(agent_role, RoleEnum) or agent_role != role:
            raise ValueError("Agent role does not match its registration")
        if not iscoroutinefunction(getattr(agent, "process", None)):
            raise TypeError("Agent must implement async process(AgentMessage) -> AgentResult")

    def register_agent(self, agent: Agent, *, replace: bool = False) -> None:
        """Register a structural implementation; replacing a role must be explicit."""
        role = getattr(agent, "role", None)
        self._validate_agent(role, agent)
        if role in self._agents and not replace:
            raise ValueError(f"Agent already registered for {role.value}")
        self._agents[role] = agent

    def unregister_agent(self, role: RoleEnum) -> Agent:
        """Remove future routing; an already running task still belongs to its agent."""
        return self._agents.pop(role)

    @property
    def agent_timeout(self) -> float:
        return self._agent_timeout

    @agent_timeout.setter
    def agent_timeout(self, value: float) -> None:
        if not isfinite(value) or value <= 0:
            raise ValueError("agent_timeout must be finite and greater than zero")
        self._agent_timeout = value

    @property
    def cleanup_timeout(self) -> float:
        return self._cleanup_timeout

    @cleanup_timeout.setter
    def cleanup_timeout(self, value: float) -> None:
        if not isfinite(value) or value <= 0:
            raise ValueError("cleanup_timeout must be finite and greater than zero")
        self._cleanup_timeout = value

    async def create_incident(self, title: str, description: str) -> Incident:
        incident = Incident(title=title, description=description)
        self.storage[incident.incident_id] = incident
        self._locks[incident.incident_id] = asyncio.Lock()
        return incident

    async def execute_next_step(self, incident_id: UUID) -> None:
        incident = self.storage.get(incident_id)
        if incident is None:
            raise ValueError(f"Incident not found: {incident_id}")

        async with self._locks[incident_id]:
            await self._execute_step(incident)

    async def process_incident(self, incident_id: UUID) -> Incident:
        """Run remaining stages once, stopping on failure or cancellation."""
        incident = self.storage.get(incident_id)
        if incident is None:
            raise ValueError(f"Incident not found: {incident_id}")
        async with self._locks[incident_id]:
            # A bounded pipeline: verification failure never reruns remediation.
            for _ in StageEnum:
                if not await self._execute_step(incident):
                    break
        return incident

    async def _execute_step(self, incident: Incident) -> bool:
        incident_id = incident.incident_id
        active_agent = self._active_agents.get(incident_id)
        if active_agent is not None and not active_agent.done():
            return False
        if incident.status in (IncidentStatus.RESOLVED, IncidentStatus.FAILED):
            return False

        stage = incident.next_stage
        previous_status = incident.status

        caller = asyncio.current_task()
        cancel_requests = caller.cancelling() if caller is not None else 0

        def caller_was_cancelled() -> bool:
            return caller is not None and caller.cancelling() > cancel_requests

        agent_task = None
        try:
            message = self._prepare_message(incident)
            if stage == StageEnum.TRIAGE:
                incident.update_status(IncidentStatus.INVESTIGATING)
            incident.last_error = None
            agent = self._agents.get(message.receiver)
            if agent is None:
                raise ValueError(f"No agent registered for {message.receiver.value}")
            self._validate_agent(message.receiver, agent)
            loop = asyncio.get_running_loop()
            execution_deadline = loop.time() + self.agent_timeout
            deadline = asyncio.timeout_at(execution_deadline)
            async with deadline:
                # Eager task factories may execute the agent during creation.
                agent_task = asyncio.create_task(agent.process(message))
                self._active_agents[incident_id] = agent_task
                agent_task.add_done_callback(lambda task: self._agent_finished(incident_id, task))
                # Caller/deadline cancellation interrupts only this wait.
                # The agent is cancelled once, outside the execution timer.
                await asyncio.wait({agent_task})
                result = agent_task.result()

            if caller_was_cancelled():
                raise asyncio.CancelledError
            if deadline.expired() or loop.time() >= execution_deadline:
                # Never accept a result after the execution deadline expired.
                raise TimeoutError("Agent processing timed out")

            result = validate_agent_result(message, result)
            self._handle_agent_result(incident, result)
            return incident.status != IncidentStatus.FAILED

        except asyncio.CancelledError:
            completed = await self._stop_agent(agent_task)
            retryable = completed and stage != StageEnum.REMEDIATION
            incident.update_status(previous_status if retryable else IncidentStatus.FAILED)
            incident.last_error = (
                None if retryable else "Agent cancellation left cleanup or remediation unconfirmed"
            )
            raise

        except Exception as exc:
            cancelled = caller_was_cancelled()
            completed = await self._stop_agent(agent_task)
            incident.update_status(
                previous_status
                if cancelled and completed and stage != StageEnum.REMEDIATION
                else IncidentStatus.FAILED
            )
            incident.last_error = str(exc) or type(exc).__name__
            if caller_was_cancelled():
                # Cancellation received during cleanup must also propagate.
                raise asyncio.CancelledError from exc
            logger.error("Incident %s failed: %s", incident_id, incident.last_error)
            return False

    @staticmethod
    def _prepare_message(incident: Incident) -> AgentMessage:
        config = {
            StageEnum.TRIAGE: (
                IncidentStatus.NEW,
                RoleEnum.TRIAGE,
                "Выдели ключевые метрики инцидента.",
            ),
            StageEnum.DIAGNOSIS: (
                IncidentStatus.INVESTIGATING,
                RoleEnum.DIAGNOSTIC,
                "Определи причину сбоя.",
            ),
            StageEnum.REMEDIATION: (
                IncidentStatus.REMEDIATING,
                RoleEnum.REMEDIATION,
                "Выполни исправление по результатам диагностики.",
            ),
            StageEnum.VERIFICATION: (
                IncidentStatus.VERIFYING,
                RoleEnum.DIAGNOSTIC,
                "Проверь результат исправления и состояние сервиса.",
            ),
        }
        if incident.next_stage is None:
            raise ValueError("Non-terminal incident has no pending stage")
        expected, receiver, description = config[incident.next_stage]
        if incident.status != expected:
            raise ValueError("Incident status does not match its next stage")
        if incident.next_stage in (
            StageEnum.REMEDIATION,
            StageEnum.VERIFICATION,
        ) and not (incident.root_cause and incident.root_cause.strip()):
            raise ValueError("A diagnosis is required before remediation or verification")
        if incident.next_stage == StageEnum.VERIFICATION and not (
            incident.action_taken and incident.action_taken.strip()
        ):
            raise ValueError("A recorded action is required before verification")
        return AgentMessage(
            incident_id=incident.incident_id,
            receiver=receiver,
            stage=incident.next_stage,
            task_description=description,
            payload={
                "text": incident.description,
                "title": incident.title,
                "metrics": incident.metrics_log,
                "root_cause": incident.root_cause,
                "action_taken": incident.action_taken,
                "history": [item.model_dump(mode="json") for item in incident.history],
            },
        )

    async def _stop_agent(self, task: asyncio.Task[AgentResult] | None) -> bool:
        """Confirm error-free termination within a separate cleanup deadline."""
        if task is None:
            return True
        if task.done():
            return self._completed_without_error(task)
        if not task.cancelling():
            task.cancel()

        # A fixed deadline bounds cleanup even if the caller is cancelled again.
        # asyncio.wait's timeout/cancellation never propagates into its tasks.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cleanup_timeout
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                done, _ = await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                continue
            if not done:
                return False
        # The event loop may have been blocked while the agent finished. A done
        # task is not proof that cleanup was confirmed within the allowed time.
        return loop.time() <= deadline and self._completed_without_error(task)

    @staticmethod
    def _completed_without_error(task: asyncio.Task[AgentResult]) -> bool:
        # An exception during teardown leaves resource release unconfirmed.
        return task.cancelled() or task.exception() is None

    def _agent_finished(self, incident_id: UUID, task: asyncio.Task[AgentResult]) -> None:
        if self._active_agents.get(incident_id) is task:
            del self._active_agents[incident_id]
        if not task.cancelled():
            # Observe late failures after cleanup timeout; never commit late results.
            task.exception()

    @staticmethod
    def _handle_agent_result(incident: Incident, result: AgentResult) -> None:
        if not result.is_success:
            incident.history.append(result)
            incident.last_error = result.error_message or "Agent reported a failed result"
            incident.update_status(IncidentStatus.FAILED)
            return

        # Validate stage-specific output before committing any history or fields.
        if result.stage == StageEnum.TRIAGE:
            incident.metrics_log = {**incident.metrics_log, **result.output_data}
            incident.next_stage = StageEnum.DIAGNOSIS
        elif result.stage == StageEnum.DIAGNOSIS:
            data = DiagnosisData.model_validate(dict(result.output_data))
            incident.metrics_log = {**incident.metrics_log, **data.metrics}
            incident.root_cause = data.root_cause
            incident.update_status(IncidentStatus.REMEDIATING)
            incident.next_stage = StageEnum.REMEDIATION
        elif result.stage == StageEnum.REMEDIATION:
            data = RemediationData.model_validate(dict(result.output_data))
            incident.metrics_log = {**incident.metrics_log, **data.metrics}
            incident.action_taken = data.action_taken
            incident.update_status(IncidentStatus.VERIFYING)
            incident.next_stage = StageEnum.VERIFICATION
        elif result.stage == StageEnum.VERIFICATION:
            data = VerificationData.model_validate(dict(result.output_data))
            incident.metrics_log = {**incident.metrics_log, **data.metrics}
            incident.verification = data.model_dump()
            incident.update_status(
                IncidentStatus.RESOLVED if data.resolved else IncidentStatus.FAILED
            )
            if data.resolved:
                incident.next_stage = None
            else:
                incident.last_error = data.details
        incident.history.append(result)
        incident.updated_at = utc_now()
