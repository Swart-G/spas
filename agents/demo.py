"""Functional reference agents for a local, controlled resource; no LLM or production access."""

from dataclasses import dataclass, field

from shared.contracts import AgentMessage, AgentResult, RoleEnum, StageEnum


@dataclass
class DemoResource:
    healthy: bool = False
    repair_succeeds: bool = True
    repair_calls: int = 0
    calls: list[str] = field(default_factory=list)


class DemoDiagnosticAgent:
    role = RoleEnum.DIAGNOSTIC

    def __init__(self, resource: DemoResource):
        self.resource = resource

    async def process(self, message: AgentMessage) -> AgentResult:
        if message.receiver != self.role:
            raise ValueError("Diagnostic received a message for another role")
        if message.stage == StageEnum.DIAGNOSIS:
            data = {
                "root_cause": "Simulated connection pool fault"
                if not self.resource.healthy
                else "Resource is already healthy; no repair is needed",
                "metrics": {"healthy": self.resource.healthy},
            }
        elif message.stage == StageEnum.VERIFICATION:
            data = {
                "resolved": self.resource.healthy,
                "details": "Resource is healthy"
                if self.resource.healthy
                else "Resource is still unhealthy",
                "metrics": {"healthy": self.resource.healthy},
            }
        else:
            raise ValueError("Diagnostic requires diagnosis or verification stage")
        self.resource.calls.append(message.stage.value)
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            stage=message.stage,
            is_success=True,
            output_data=data,
        )


class DemoWorkerAgent:
    role = RoleEnum.REMEDIATION

    def __init__(self, resource: DemoResource):
        self.resource = resource

    async def process(self, message: AgentMessage) -> AgentResult:
        if message.receiver != self.role or message.stage != StageEnum.REMEDIATION:
            raise ValueError("Worker requires a remediation message")
        cause = message.payload.get("root_cause")
        if not isinstance(cause, str) or not cause.strip():
            raise ValueError("Worker requires a diagnosis")
        self.resource.calls.append(message.stage.value)
        action = "Resource was already healthy; no change performed"
        if not self.resource.healthy:
            self.resource.repair_calls += 1
            self.resource.healthy = self.resource.repair_succeeds
            action = "Attempted connection pool reset"
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            stage=message.stage,
            is_success=True,
            output_data={"action_taken": action},
        )
