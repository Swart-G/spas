"""Контракт подключения Safety Checker"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .Models import ExecutionContext


class SafetyDecision(BaseModel):
    """Ожидаемый ответ внешнего компонента Safety Checker на запрос проверки команды.
    Формат ответа Safety Checker: разрешить команду, запретить или запросить подтверждение, плюс причина решения."""
    model_config = ConfigDict(extra='forbid', frozen=True, revalidate_instances='always')
    decision: Literal['ALLOW', 'DENY', 'REQUIRE_APPROVAL']
    reason: str


class SafetyCheckerPort(Protocol):
    """Интерфейс для подключения Safety Checker. Он требует асинхронный метод check(command, context), но сам команды не проверяет."""
    async def check(
        self, command: str, context: ExecutionContext,
    ) -> SafetyDecision | dict[str, object]: ...
