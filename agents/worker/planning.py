"""Receive an incident description and return a plan via the agent contract."""

from agents.base import BaseSlaveAgent
from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum

from .planner import Planner, RemediationPlan, StubPlanner


class WorkerAgent(BaseSlaveAgent):
    """Plan only; execution remains a later stage of the incident pipeline."""

    def __init__(self, planner: Planner | None = None) -> None:
        super().__init__(RoleEnum.REMEDIATION)
        self.planner = planner if planner is not None else StubPlanner()

    async def process(self, message: AgentMessage) -> AgentResult:
        if not isinstance(message, AgentMessage):
            raise TypeError("Worker expects AgentMessage")
        # A model_copy(update=...) can bypass Pydantic validation. Restore it at
        # the agent boundary before trusting the role, identifiers or payload.
        message = AgentMessage.model_validate(message.model_dump())
        if message.receiver != self.role:
            raise ValueError("Message is not addressed to Worker Agent")
        if message.sender != RoleEnum.ORCHESTRATOR:
            raise ValueError("Worker accepts commands only from Orchestrator")

        description = message.payload.get("problem_description")
        if description is None:
            description = message.payload.get("text", message.task_description)

        try:
            if not isinstance(description, str) or not description.strip():
                raise ValueError("Problem description is required")
            if len(description) > 4000:
                raise ValueError("Problem description is too long")
            raw_plan = await self.planner.create_plan(description.strip())
            # Pydantic does not revalidate an existing model instance by
            # default; a detached dump also catches model_copy(update=...).
            plan = RemediationPlan.model_validate(
                raw_plan.model_dump() if isinstance(raw_plan, RemediationPlan) else raw_plan
            )
        except Exception:
            # Do not return provider errors or incident text to other agents.
            return AgentResult(
                message_id=message.message_id,
                incident_id=message.incident_id,
                sender=self.role,
                is_success=False,
                error_message="Worker could not create a valid plan",
            )

        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            is_success=True,
            output_data={"plan": plan.model_dump()},
        )
