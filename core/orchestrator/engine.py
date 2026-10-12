import asyncio
from uuid import UUID

from agents.base import BaseSlaveAgent
from core.incidents.models import Incident, IncidentStatus
from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum
from shared.time import utc_now


class Orchestrator:
    """Coordinates processing steps for incidents."""

    def __init__(self, agent_timeout: float = 10.0):
        if agent_timeout <= 0:
            raise ValueError("agent_timeout must be greater than zero")

        self.storage: dict[UUID, Incident] = {}
        self.agent_timeout = agent_timeout

        self.agents: dict[RoleEnum, BaseSlaveAgent] = {
            RoleEnum.TRIAGE: BaseSlaveAgent(RoleEnum.TRIAGE),
        }

        # Prevent concurrent processing of the same incident.
        self._locks: dict[UUID, asyncio.Lock] = {}

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
            # This initial version implements the Triage step only.
            if incident.status != IncidentStatus.NEW:
                return

            incident.update_status(IncidentStatus.INVESTIGATING)

            message = AgentMessage(
                incident_id=incident.incident_id,
                receiver=RoleEnum.TRIAGE,
                task_description=(
                    "Проанализируй описание инцидента "
                    "и выдели ключевые метрики."
                ),
                payload={"text": incident.description},
            )

            caller = asyncio.current_task()
            cancel_requests = caller.cancelling() if caller is not None else 0

            def caller_was_cancelled() -> bool:
                return caller is not None and caller.cancelling() > cancel_requests

            try:
                agent = self.agents[RoleEnum.TRIAGE]
                deadline = asyncio.timeout(self.agent_timeout)
                async with deadline:
                    # Isolate the caller's cancellation counter from the agent.
                    result = await asyncio.create_task(agent.process(message))

                if caller_was_cancelled():
                    raise asyncio.CancelledError
                if deadline.expired():
                    # An agent can suppress timeout cancellation as well.
                    raise TimeoutError("Agent processing timed out")

                result = self._validate_result(message, result)
                self._handle_agent_result(incident, result)

            except asyncio.CancelledError:
                # The step has no committed result; release the lock with a
                # retryable state, but preserve cancellation for the caller.
                incident.update_status(IncidentStatus.NEW)
                raise

            except Exception as exc:
                if caller_was_cancelled():
                    # Cleanup errors must not turn an interrupted step into a
                    # permanent failure or swallow the caller's cancellation.
                    incident.update_status(IncidentStatus.NEW)
                    raise asyncio.CancelledError from exc
                # Agent implementations may raise arbitrary exceptions.
                # Do not leave the incident in an intermediate state.
                incident.update_status(IncidentStatus.FAILED)
                print(
                    f"[Orchestrator] Ошибка обработки "
                    f"{incident_id}: {exc}"
                )

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
