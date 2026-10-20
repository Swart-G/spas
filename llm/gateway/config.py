import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

API_ENDPOINT_SUFFIXES = ("/chat/completions", "/responses", "/decisions", "/systemone")


def has_control_characters(value: str) -> bool:
    return any(
        ord(char) < 32 or 127 <= ord(char) < 160 or 0xD800 <= ord(char) <= 0xDFFF for char in value
    )


def default_state_dir() -> Path:
    if value := os.environ.get("SPAS_GATEWAY_STATE_DIR"):
        return Path(value).expanduser()
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "spas/llm"


class BackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal[
        "codex_subscription",
        "claude_subscription",
        "openai_api",
        "openai_compatible",
        "remote_inference",
        "colab_managed",
    ]
    account: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    model: str = Field(min_length=1)
    transport: Literal["cli", "sdk"] = "cli"
    timeout_seconds: float = Field(default=180, gt=0, le=3600)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    base_url: str | None = None
    api_format: Literal["chat_completions", "responses", "decisions", "systemone"] | None = None
    decision_box: bool | None = None
    api_streaming: bool = True
    api_include_usage: bool = False
    api_json_output: bool | None = None
    api_token_limit: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    remote_streaming: bool = True
    remote_include_usage: bool = False
    remote_json_output: bool = False
    model_kind: Literal["auto", "chat", "nimble", "decision"] = "auto"

    @field_validator("model")
    @classmethod
    def validate_model(cls, value):
        value = value.strip()
        if not value or has_control_characters(value):
            raise ValueError("Model ID must be nonempty and contain no control characters")
        return value

    @property
    def decision_model(self) -> bool:
        return (
            self.model_kind in {"nimble", "decision"}
            or self.api_format in {"decisions", "systemone"}
            or (
                self.model_kind == "auto"
                and (
                    self.model.lower().startswith("bespokelabs/bespoke-nimble-")
                    or "mercury-decide" in self.model.lower()
                    or self.model.lower().lstrip("~").startswith("typesafe/jev-")
                )
            )
        )

    @property
    def supports_box(self) -> bool:
        if not self.decision_model or self.provider not in {"openai_api", "openai_compatible"}:
            return False
        if self.decision_box is not None:
            return self.decision_box
        model = self.model.lower().lstrip("~").split("/")[-1].split(":")[0]
        return model in {"jev-latest", "jev-preview"} or model.startswith("jev-1.14")

    @property
    def api_base_url(self) -> str:
        base = self.base_url or ""
        for suffix in API_ENDPOINT_SUFFIXES:
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base

    @property
    def decision_url(self) -> str:
        base = self.base_url or ""
        if base.endswith(("/decisions", "/systemone")):
            return base
        base = self.api_base_url
        url = urlsplit(base)
        if url.hostname == "openrouter.ai" and self.api_format == "decisions":
            return urlunsplit((url.scheme, url.netloc, "/api/alpha/decisions", "", ""))
        return base + ("/systemone" if self.api_format == "systemone" else "/decisions")

    @property
    def models_url(self) -> str:
        base = self.api_base_url
        # OpenRouter's alpha inference and stable model catalog have different prefixes.
        url = urlsplit(base)
        if url.hostname == "openrouter.ai":
            return urlunsplit((url.scheme, url.netloc, "/api/v1/models", "", ""))
        return base + "/models"

    @model_validator(mode="after")
    def validate_endpoint(self) -> "BackendConfig":
        compatible = self.provider in {"openai_api", "openai_compatible"}
        if compatible:
            if self.api_json_output is None:
                self.api_json_output = self.provider == "openai_api"
            if self.api_format is None:
                self.api_format = (
                    "responses" if self.provider == "openai_api" else "chat_completions"
                )
            if self.provider == "openai_api" and self.base_url is None:
                self.base_url = "https://api.openai.com/v1"
        if self.provider != "remote_inference" and not compatible:
            if self.base_url is not None:
                raise ValueError("base_url is only supported for API backends")
            return self
        if not self.base_url:
            raise ValueError("API connection requires a base URL")
        self.base_url = self.base_url.strip()
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(character.isspace() for character in self.base_url)
            or has_control_characters(self.base_url)
        ):
            raise ValueError("Use an HTTP(S) endpoint without credentials, query or fragment")
        _ = url.port  # Validate the port before sending any credentials.
        path = url.path.rstrip("/")
        if compatible:
            for suffix, api_format in (
                ("/chat/completions", "chat_completions"),
                ("/responses", "responses"),
            ):
                if path.endswith(suffix):
                    prefix = path[: -len(suffix)]
                    # Preserve ambiguous prefixes so validation never changes their meaning.
                    if prefix and not prefix.endswith(API_ENDPOINT_SUFFIXES):
                        path = prefix
                    self.api_format = api_format
                    break
        # API providers may use /api/v3 or a proxy prefix. Preserve explicit paths.
        if not path or (not compatible and not path.endswith("/v1")):
            path += "/v1"
        self.base_url = urlunsplit((url.scheme, url.netloc, path, "", ""))
        if compatible:
            if path.endswith(("/decisions", "/systemone")):
                self.api_format = "systemone" if path.endswith("/systemone") else "decisions"
            elif self.decision_model and self.api_format in {"chat_completions", "responses"}:
                self.api_format = "decisions"
        return self


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backends: dict[str, BackendConfig]
    routes: dict[str, list[str]]
    fallback_on: list[Literal["rate_limited", "provider_unavailable", "provider_timeout"]] = Field(
        default_factory=lambda: ["rate_limited", "provider_unavailable"]
    )

    @model_validator(mode="after")
    def validate_routes(self) -> "GatewayConfig":
        if any(not name.strip() or has_control_characters(name) for name in self.backends):
            raise ValueError("Backend names must be nonempty and contain no control characters")
        for alias, backends in self.routes.items():
            if (
                not alias.strip()
                or len(alias) > 200
                or has_control_characters(alias)
                or not backends
                or len(backends) != len(set(backends))
            ):
                raise ValueError("Each route needs an alias and a nonempty, unique backend list")
            if any(backend not in self.backends for backend in backends):
                raise ValueError(f"Unknown backend in route {alias}")
        return self

    @classmethod
    def load(cls, path: Path) -> "GatewayConfig":
        with path.open("rb") as source:
            return cls.model_validate(tomllib.load(source))
