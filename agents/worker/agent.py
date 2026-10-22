"""Deterministic remediation stub; no infrastructure changes are performed."""

from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum, StageEnum


class WorkerAgent:
    role = RoleEnum.REMEDIATION

    async def process(self, message: AgentMessage) -> AgentResult:
        if message.receiver != self.role:
            raise ValueError("Worker received a message for another role")
        cause = message.payload.get("root_cause")
        if (
            message.stage != StageEnum.REMEDIATION
            or not isinstance(cause, str)
            or not cause.strip()
        ):
            raise ValueError("Worker requires remediation stage and a diagnosis")
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            stage=message.stage,
            is_success=True,
            output_data={"action_taken": "Mock remediation: reset database connection pool"},
        )
