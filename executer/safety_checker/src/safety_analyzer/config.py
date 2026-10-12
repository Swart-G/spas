from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SAFETY_", extra="ignore")

    gateway_base_url: str | None = None
    gateway_api_key: SecretStr | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    output_mode: Literal["json", "label"] = "label"
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    max_tokens: int = Field(default=128, ge=16, le=1024)

    @model_validator(mode="after")
    def validate_gateway(self) -> "Settings":
        if self.gateway_base_url:
            parsed = urlsplit(self.gateway_base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or any(c.isspace() for c in self.gateway_base_url)
            ):
                raise ValueError("gateway_base_url must be an HTTP(S) URL without credentials")
            _ = parsed.port
            self.gateway_base_url = self.gateway_base_url.rstrip("/") + "/"
        return self

    @property
    def configured(self) -> bool:
        return bool(self.gateway_base_url and self.model and self.gateway_api_key)
