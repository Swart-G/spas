"""Provisional safety contracts shared by Safety Checker and Executor MCP."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol, TypedDict


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class CommandRequest:
    command: str


@dataclass(frozen=True)
class SafetyResult:
    decision: Decision
    risk: RiskLevel
    rule_id: str
    reason: str
    argv: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        """Approval requests are NOT permission to execute."""
        return self.decision is Decision.ALLOW


# The Executor validates responses with extra='forbid', so do not return
# SafetyResult directly: risk/rule_id/argv are internal checker metadata.
class ExecutionContextPort(Protocol):
    """Structural interface matching Executor's frozen ExecutionContext model."""

    @property
    def incident_id(self) -> str: ...

    @property
    def reason(self) -> str: ...


class ExecutorSafetyDecision(TypedDict):
    """Wire response accepted by Executor's SafetyDecision.model_validate()."""

    decision: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    reason: str


class ExecutorSafetyCheckerPort(Protocol):
    async def check(
        self, command: str, context: ExecutionContextPort,
    ) -> ExecutorSafetyDecision: ...
