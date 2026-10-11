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

            try:
                agent = self.agents[RoleEnum.TRIAGE]
                result = await asyncio.wait_for(
                    agent.process(message),
                    timeout=self.agent_timeout,
                )

                self._validate_result(message, result)
                self._handle_agent_result(incident, result)

            except Exception as exc:
                # Do not leave the incident in an intermediate state.
                incident.update_status(IncidentStatus.FAILED)
                print(
                    f"[Orchestrator] Ошибка обработки "
                    f"{incident_id}: {exc}"
                )

    @staticmethod
    def _validate_result(
        message: AgentMessage, result: AgentResult
    ) -> None:
        if result.message_id != message.message_id:
            raise ValueError("Agent result message_id mismatch")

        if result.incident_id != message.incident_id:
            raise ValueError("Agent result incident_id mismatch")

        if result.sender != message.receiver:
            raise ValueError("Unexpected agent role in result")

        if result.receiver != RoleEnum.ORCHESTRATOR:
            raise ValueError("Unexpected result receiver")

    @staticmethod
    def _handle_agent_result(
        incident: Incident, result: AgentResult
    ) -> None:
        incident.history.append(result)

        if not result.is_success:
            incident.update_status(IncidentStatus.FAILED)
            return

        if result.sender == RoleEnum.TRIAGE:
            incident.metrics_log.update(result.output_data)

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
