import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        "remote_inference",
        "colab_managed",
    ]
    account: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    model: str = Field(min_length=1)
    transport: Literal["cli", "sdk"] = "cli"
    timeout_seconds: float = Field(default=180, gt=0, le=3600)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    base_url: str | None = None
    remote_streaming: bool = True
    remote_include_usage: bool = False
    remote_json_output: bool = False

    @model_validator(mode="after")
    def validate_endpoint(self) -> "BackendConfig":
        if self.provider != "remote_inference":
            if self.base_url is not None:
                raise ValueError("base_url is only supported for remote inference")
            return self
        if not self.base_url:
            raise ValueError("Remote inference requires a base URL")
        url = urlsplit(self.base_url.strip())
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(character.isspace() for character in self.base_url)
        ):
            raise ValueError("Use an HTTP(S) endpoint without credentials, query or fragment")
        _ = url.port  # Validate the port before sending any credentials.
        path = url.path.rstrip("/")
        if not path.endswith("/v1"):
            path += "/v1"
        self.base_url = urlunsplit((url.scheme, url.netloc, path, "", ""))
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
        for alias, backends in self.routes.items():
            if not alias or not backends or len(backends) != len(set(backends)):
                raise ValueError("Each route needs an alias and a nonempty, unique backend list")
            if any(backend not in self.backends for backend in backends):
                raise ValueError(f"Unknown backend in route {alias}")
        return self

    @classmethod
    def load(cls, path: Path) -> "GatewayConfig":
        with path.open("rb") as source:
            return cls.model_validate(tomllib.load(source))
