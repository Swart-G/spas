"""Context-aware argv analysis. Executor receives only decision and reason."""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from safety_analyzer.contracts import StrictModel

Decision = Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
Risk = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
Factor = Literal[
    "destruction",
    "privilege_escalation",
    "secret_exposure",
    "exfiltration",
    "untrusted_code",
    "service_disruption",
    "scope_violation",
    "context_conflict",
    "insufficient_context",
    "unspecified",
]
Text = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
Argument = Annotated[str, StringConstraints(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")]


class HistoryEntry(StrictModel):
    role: Literal["user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=8000)


class CommandContext(StrictModel):
    incident_id: Text
    reason: Text
    task: str = Field(default="", max_length=8000)
    history: list[HistoryEntry] = Field(default_factory=list, max_length=40)
    observations: list[Text] = Field(default_factory=list, max_length=20)
    constraints: list[Text] = Field(default_factory=list, max_length=20)
    environment: dict[str, Text] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def validate_context(self) -> "CommandContext":
        if not self.incident_id.strip() or not self.reason.strip():
            raise ValueError("incident_id and reason must be nonblank")
        if any(not key or len(key) > 100 for key in self.environment):
            raise ValueError("environment keys must have 1..100 characters")
        # Reject oversized snapshots: never silently truncate evidence or prior constraints.
        if len(self.model_dump_json()) > 64000:
            raise ValueError("context exceeds 64000 serialized characters")
        return self


class CommandAnalysisRequest(StrictModel):
    command: list[Argument] = Field(min_length=1, max_length=4096)
    context: CommandContext
    request_id: UUID | None = Field(default=None, strict=False)

    @model_validator(mode="after")
    def validate_command(self) -> "CommandAnalysisRequest":
        if sum(map(len, self.command)) + len(self.command) - 1 > 4096:
            raise ValueError("argv exceeds 4096 characters including separators")
        return self

    def snapshot(self) -> "CommandAnalysisRequest":
        return self.model_copy(deep=True)

    def fingerprint(self) -> str:
        content = json.dumps(
            self.model_dump(mode="json", exclude={"request_id"}),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(content.encode()).hexdigest()


class RuleDecision(StrictModel):
    decision: Decision
    reason: Text


class CommandModelVerdict(StrictModel):
    decision: Decision
    risk: Risk
    factors: list[Factor] = Field(max_length=10)
    reason: Text

    @model_validator(mode="after")
    def validate_risk(self) -> "CommandModelVerdict":
        if len(set(self.factors)) != len(self.factors):
            raise ValueError("duplicate risk factors")
        if not self.reason.strip():
            raise ValueError("reason must be nonblank")
        if self.decision == "ALLOW" and (self.risk != "LOW" or self.factors):
            raise ValueError("ALLOW requires LOW risk and no factors")
        if self.decision != "ALLOW" and not self.factors:
            raise ValueError("restricted decisions require at least one risk factor")
        if self.decision == "REQUIRE_APPROVAL" and self.risk in {"HIGH", "CRITICAL"}:
            raise ValueError("HIGH/CRITICAL risk must be DENY")
        return self


class CommandAnalysisResponse(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    policy_version: Literal["command-1"] = "command-1"
    request_id: UUID
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Decision
    risk: Risk
    reason: Text
    source: Literal["rules", "model", "error"]
    rules: RuleDecision | None
    model_result: CommandModelVerdict | None
    status: Literal["ok", "error"]
    error_code: (
        Literal[
            "not_configured",
            "gateway_timeout",
            "gateway_unavailable",
            "invalid_model_output",
            "rule_checker_timeout",
            "rule_checker_unavailable",
            "invalid_rule_output",
        ]
        | None
    ) = None
    model: str | None
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_precedence(self) -> "CommandAnalysisResponse":
        if self.status == "error":
            if self.decision != "DENY" or not self.error_code or self.source != "error":
                raise ValueError("errors must deny execution")
        else:
            if self.error_code or self.rules is None:
                raise ValueError("successful decisions require a rule result")
            if self.rules.decision != "ALLOW":
                if self.decision != self.rules.decision or self.model_result is not None:
                    raise ValueError("model must not override or run past a rule restriction")
                if self.source != "rules":
                    raise ValueError("rule restrictions must be attributed to rules")
            elif self.model_result is None or self.source != "model":
                raise ValueError("rule ALLOW requires model evaluation")
            elif (self.decision, self.risk) != (self.model_result.decision, self.model_result.risk):
                raise ValueError("decision must match the validated model result")
        if self.decision == "ALLOW" and (
            self.status != "ok"
            or self.risk != "LOW"
            or self.rules is None
            or self.rules.decision != "ALLOW"
            or self.model_result is None
            or self.model_result.decision != "ALLOW"
        ):
            raise ValueError("ALLOW requires both rule and model permission")
        return self

    def executor_decision(self) -> dict[str, object]:
        # The real Executor uses extra=forbid. Never pass risk/hash/model metadata here.
        return {"decision": self.decision, "reason": self.reason}
