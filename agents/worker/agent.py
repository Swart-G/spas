"""Deterministic remediation stub; no infrastructure changes are performed."""

from agents.base import BaseSlaveAgent
from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum, StageEnum


class WorkerAgent(BaseSlaveAgent):
    def __init__(self):
        super().__init__(RoleEnum.REMEDIATION)

    async def process(self, message: AgentMessage) -> AgentResult:
        if message.stage != StageEnum.REMEDIATION or not message.payload.get(
            "root_cause"
        ):
            raise ValueError("Worker requires remediation stage and a diagnosis")
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            stage=message.stage,
            is_success=True,
            output_data={
                "action_taken": "Mock remediation: reset database connection pool"
            },
        )
