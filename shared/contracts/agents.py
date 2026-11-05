"""Structural interface for agents, independent of orchestration and implementations."""

from typing import Protocol, runtime_checkable

from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum


class AgentCompletionUnknownError(RuntimeError):
    """An invocation stopped locally, but completion of its work cannot be confirmed."""


def validate_agent_result(message: AgentMessage, result: AgentResult) -> AgentResult:
    """Detach, revalidate and correlate a response without depending on the caller."""
    if not isinstance(result, AgentResult):
        raise TypeError("Agent result must be an AgentResult")

    # model_copy(update=...) and model_construct can bypass field validation.
    result = AgentResult.model_validate(result.model_dump())
    if result.message_id != message.message_id:
        raise ValueError("Agent result message_id mismatch")
    if result.incident_id != message.incident_id:
        raise ValueError("Agent result incident_id mismatch")
    if result.sender != message.receiver:
        raise ValueError("Unexpected agent role in result")
    if result.receiver != RoleEnum.ORCHESTRATOR:
        raise ValueError("Unexpected result receiver")
    if result.stage != message.stage:
        raise ValueError("Agent result stage mismatch")
    return result


@runtime_checkable
class Agent(Protocol):
    @property
    def role(self) -> RoleEnum:
        """Role used to route requests to this agent."""
        ...

    async def process(self, message: AgentMessage) -> AgentResult:
        """Return only after completion; raise AgentCompletionUnknownError if unconfirmed.

        Propagating CancelledError promises that resource cleanup has finished.
        """
        ...
