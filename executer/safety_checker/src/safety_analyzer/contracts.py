"""Public API and the deliberately smaller model output contract."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Category(StrEnum):
    VIOLENCE = "violence"
    SELF_HARM = "self_harm"
    SEXUAL_CONTENT = "sexual_content"
    SEXUAL_MINORS = "sexual_minors"
    HATE = "hate"
    ILLICIT_BEHAVIOR = "illicit_behavior"
    PRIVACY = "privacy"
    CYBER_ABUSE = "cyber_abuse"
    DESTRUCTIVE_ACTION = "destructive_action"
    UNSPECIFIED = "unspecified"


class ContextMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class AnalyzeRequest(StrictModel):
    text: str = Field(min_length=1, max_length=32000)
    target: Literal["request", "response", "command"] = "request"
    context: list[ContextMessage] = Field(default_factory=list, max_length=20)
    request_id: UUID | None = Field(default=None, strict=False)

    @model_validator(mode="after")
    def validate_size(self) -> "AnalyzeRequest":
        if not self.text.strip() or any(not m.content.strip() for m in self.context):
            raise ValueError("text and context content must contain non-whitespace characters")
        if len(self.text) + sum(len(m.content) for m in self.context) > 64000:
            raise ValueError("combined text and context exceed 64000 characters")
        return self


class ModelVerdict(StrictModel):
    verdict: Literal["safe", "unsafe"]
    categories: list[Category] = Field(max_length=len(Category))

    @model_validator(mode="after")
    def validate_consistency(self) -> "ModelVerdict":
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("categories must be unique")
        if self.verdict == "safe" and self.categories:
            raise ValueError("safe verdict must have no categories")
        if self.verdict == "unsafe" and not self.categories:
            raise ValueError("unsafe verdict must have at least one category")
        if Category.UNSPECIFIED in self.categories and len(self.categories) != 1:
            raise ValueError("unspecified cannot be combined with other categories")
        return self


ErrorCode = Literal[
    "not_configured", "gateway_timeout", "gateway_unavailable", "invalid_model_output"
]


class AnalyzeResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["draft-1"] = "draft-1"
    request_id: UUID
    decision: Literal["allow", "block"]
    verdict: Literal["safe", "unsafe", "unknown"]
    categories: list[Category]
    status: Literal["ok", "error"]
    error_code: ErrorCode | None = None
    model: str | None
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_decision(self) -> "AnalyzeResponse":
        if self.status == "error":
            if (self.decision, self.verdict) != ("block", "unknown"):
                raise ValueError("errors must fail closed")
            if self.error_code is None or self.categories:
                raise ValueError("errors require a code and no invented categories")
        else:
            if self.error_code is not None or self.verdict == "unknown":
                raise ValueError("successful responses require a known verdict and no error")
            expected = "allow" if self.verdict == "safe" else "block"
            if self.decision != expected:
                raise ValueError("decision contradicts verdict")
            ModelVerdict(verdict=self.verdict, categories=self.categories)
        return self
