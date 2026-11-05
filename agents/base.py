import asyncio

from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum


class BaseSlaveAgent:
    """Mock agent implementation; replace with a real agent/API later."""

    def __init__(self, role: RoleEnum):
        self.role = role

    async def process(self, message: AgentMessage) -> AgentResult:
        await asyncio.sleep(1)

        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            is_success=True,
            output_data={
                "detected_metrics": {
                    "timeout_ms": 5000,
                    "connections_dropped": True,
                },
                "target_service": "PostgreSQL_Prod",
                "severity": "CRITICAL",
            },
        )
