
"""Контракт подключения Safety Checker."""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .Models import ExecutionContext


class SafetyDecision(BaseModel):
    """Ответ Safety Checker на запрос проверки команды."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    decision: Literal[
        "ALLOW",
        "DENY",
        "REQUIRE_APPROVAL",
    ]

    reason: str = Field(min_length=1, max_length=2000, strict=True)


class SafetyCheckerPort(Protocol):
    """Интерфейс подключения внешнего Safety Checker."""

    async def check(
        self,
        command: list[str],
        context: ExecutionContext,
    ) -> SafetyDecision | dict[str, object]:
        ...
