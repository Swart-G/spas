import asyncio
from math import isfinite
from uuid import UUID

from agents.base import BaseSlaveAgent
from core.incidents.models import Incident, IncidentStatus
from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum
from shared.time import utc_now


class Orchestrator:
    """Coordinates processing steps for incidents."""

    def __init__(self, agent_timeout: float = 10.0, *, cleanup_timeout: float = 10.0):
        self.storage: dict[UUID, Incident] = {}
        self.agent_timeout = agent_timeout
        self.cleanup_timeout = cleanup_timeout

        self.agents: dict[RoleEnum, BaseSlaveAgent] = {
            RoleEnum.TRIAGE: BaseSlaveAgent(RoleEnum.TRIAGE),
        }

        # Prevent concurrent processing of the same incident.
        self._locks: dict[UUID, asyncio.Lock] = {}
        # Keep unfinished agents alive and prevent overlap after a cleanup timeout.
        self._active_agents: dict[UUID, asyncio.Task[AgentResult]] = {}

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

    async def create_incident(
        self, title: str, description: str
    ) -> Incident:
        incident = Incident(title=title, description=description)
        self.storage[incident.incident_id] = incident
        self._locks[incident.incident_id] = asyncio.Lock()
        return incident

    async def execute_next_step(self, incident_id: UUID) -> None:
        incident = self.storage.get(incident_id)
        if incident is None:
            raise ValueError(f"Incident not found: {incident_id}")

        async with self._locks[incident_id]:
            active_agent = self._active_agents.get(incident_id)
            if active_agent is not None and not active_agent.done():
                return
            # This initial version implements the Triage step only.
            if incident.status != IncidentStatus.NEW:
                return

            incident.update_status(IncidentStatus.INVESTIGATING)

            caller = asyncio.current_task()
            cancel_requests = caller.cancelling() if caller is not None else 0

            def caller_was_cancelled() -> bool:
                return caller is not None and caller.cancelling() > cancel_requests

            agent_task = None
            try:
                message = AgentMessage(
                    incident_id=incident.incident_id,
                    receiver=RoleEnum.TRIAGE,
                    task_description=(
                        "Проанализируй описание инцидента "
                        "и выдели ключевые метрики."
                    ),
                    payload={"text": incident.description},
                )
                agent = self.agents[RoleEnum.TRIAGE]
                loop = asyncio.get_running_loop()
                execution_deadline = loop.time() + self.agent_timeout
                deadline = asyncio.timeout_at(execution_deadline)
                async with deadline:
                    # Eager task factories may execute the agent during creation.
                    agent_task = asyncio.create_task(agent.process(message))
                    self._active_agents[incident_id] = agent_task
                    agent_task.add_done_callback(
                        lambda task: self._agent_finished(incident_id, task)
                    )
                    # Caller/deadline cancellation interrupts only this wait.
                    # The agent is cancelled once, outside the execution timer.
                    await asyncio.wait({agent_task})
                    result = agent_task.result()

                if caller_was_cancelled():
                    raise asyncio.CancelledError
                if deadline.expired() or loop.time() >= execution_deadline:
                    # Never accept a result after the execution deadline expired.
                    raise TimeoutError("Agent processing timed out")

                result = self._validate_result(message, result)
                self._handle_agent_result(incident, result)

            except asyncio.CancelledError:
                completed = await self._stop_agent(agent_task)
                incident.update_status(
                    IncidentStatus.NEW if completed else IncidentStatus.FAILED
                )
                raise

            except Exception as exc:
                cancelled = caller_was_cancelled()
                completed = await self._stop_agent(agent_task)
                incident.update_status(
                    IncidentStatus.NEW if cancelled and completed else IncidentStatus.FAILED
                )
                if caller_was_cancelled():
                    # Cancellation received during cleanup must also propagate.
                    raise asyncio.CancelledError from exc
                print(
                    f"[Orchestrator] Ошибка обработки "
                    f"{incident_id}: {exc}"
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
    def _validate_result(
        message: AgentMessage, result: AgentResult
    ) -> AgentResult:
        if not isinstance(result, AgentResult):
            raise TypeError("Agent result must be an AgentResult")

        # Revalidate a detached copy: model_copy(update=...) and model_construct
        # can bypass Pydantic field validation even on an otherwise frozen model.
        result = AgentResult.model_validate(result.model_dump())

        if result.message_id != message.message_id:
            raise ValueError("Agent result message_id mismatch")

        if result.incident_id != message.incident_id:
            raise ValueError("Agent result incident_id mismatch")

        if result.sender != message.receiver:
            raise ValueError("Unexpected agent role in result")

        if result.receiver != RoleEnum.ORCHESTRATOR:
            raise ValueError("Unexpected result receiver")

        return result

    @staticmethod
    def _handle_agent_result(
        incident: Incident, result: AgentResult
    ) -> None:
        incident.history.append(result)

        if not result.is_success:
            incident.update_status(IncidentStatus.FAILED)
            return

        if result.sender == RoleEnum.TRIAGE:
            incident.metrics_log = {**incident.metrics_log, **result.output_data}

            # Successful triage does not mean that the incident is resolved.
            incident.updated_at = utc_now()


async def main() -> None:
    orchestrator = Orchestrator(agent_timeout=5.0)

    incident = await orchestrator.create_incident(
        title="Alert: DB_TIMEOUT",
        description=(
            "Production database is dropping connections "
            "with timeout 5000ms"
        ),
    )

    print(f"Created: {incident.status.value}")
    await orchestrator.execute_next_step(incident.incident_id)

    updated = orchestrator.storage[incident.incident_id]
    print(f"Status: {updated.status.value}")
    print(f"Metrics: {updated.metrics_log}")
    print(f"History entries: {len(updated.history)}")


if __name__ == "__main__":
    asyncio.run(main())
