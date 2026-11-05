"""Deterministic diagnostic stub; no infrastructure calls are performed."""

from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum, StageEnum


class DiagnosticAgent:
    role = RoleEnum.DIAGNOSTIC

    async def process(self, message: AgentMessage) -> AgentResult:
        if message.receiver != self.role:
            raise ValueError("Diagnostic received a message for another role")
        if message.stage == StageEnum.DIAGNOSIS:
            data = {"root_cause": "Mock diagnosis: exhausted database connection pool"}
        elif message.stage == StageEnum.VERIFICATION:
            data = {
                "resolved": True,
                "details": "Mock verification: service health checks passed",
            }
        else:
            raise ValueError("Diagnostic agent requires diagnosis or verification")
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            stage=message.stage,
            is_success=True,
            output_data=data,
        )
