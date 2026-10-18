# Adapted from Swart-G/spas 00b54e3ebfb768ebfdf0ee69304879b3cb1c7c7b, shared/contracts/safety.py.
"""Safety policy metadata and the async argv port for Executor MCP."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, TypedDict


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


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
    """Internal field schema; the Executor port returns a plain dict[str, object]."""

    decision: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    reason: str


class ExecutorSafetyCheckerPort(Protocol):
    async def check(
        self,
        command: list[str],
        context: ExecutionContextPort,
    ) -> dict[str, object]: ...
