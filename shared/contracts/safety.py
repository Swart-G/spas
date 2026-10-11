"""Provisional safety contracts shared by Safety Checker and Executor MCP."""

from dataclasses import dataclass
from enum import Enum


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
