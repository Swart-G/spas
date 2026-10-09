from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import GatewayError


class AccountState(StrEnum):
    CONNECTED = "CONNECTED"
    EXPIRED = "EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"
    DISCONNECTED = "DISCONNECTED"


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "developer", "user", "assistant"]
    content: str


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["text", "json_object"] = "text"


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    include_usage: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=200)
    messages: list[ChatMessage] = Field(min_length=1, max_length=1000)
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    response_format: ResponseFormat | None = None
    n: Literal[1] = 1

    @model_validator(mode="after")
    def validate_request(self) -> "ChatRequest":
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("Specify only one token limit")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("A user message is required")
        return self


@dataclass(frozen=True)
class Capabilities:
    streaming: bool = True
    json_output: bool = False
    tool_calling: bool = False
    temperature: bool = False
    max_output_tokens: bool = False

    def validate(self, request: ChatRequest) -> None:
        unsupported = []
        if request.stream and not self.streaming:
            unsupported.append("stream")
        if request.temperature is not None and not self.temperature:
            unsupported.append("temperature")
        if not self.max_output_tokens:
            unsupported.extend(
                name
                for name in ("max_tokens", "max_completion_tokens")
                if getattr(request, name) is not None
            )
        if (
            request.response_format
            and request.response_format.type != "text"
            and not self.json_output
        ):
            unsupported.append("response_format")
        if unsupported:
            raise GatewayError(
                f"Выбранный backend не поддерживает: {', '.join(unsupported)}.",
                code="unsupported_parameter",
                status=400,
                param=unsupported[0],
            )


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


@dataclass(frozen=True)
class StreamEvent:
    kind: Literal["delta", "done"]
    text: str = ""
    usage: Usage | None = None
    finish_reason: Literal["stop", "length", "content_filter"] = "stop"


@dataclass(frozen=True)
class Generation:
    content: str
    usage: Usage | None
    finish_reason: str
