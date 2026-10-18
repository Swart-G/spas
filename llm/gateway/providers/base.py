from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import aclosing

from ..core.errors import GatewayError
from ..core.models import Capabilities, ChatRequest, Generation, StreamEvent


class LLMProvider(ABC):
    capabilities = Capabilities()

    async def generate(self, request: ChatRequest) -> Generation:
        parts: list[str] = []
        terminal: StreamEvent | None = None
        async with aclosing(self.stream(request)) as events:
            async for event in events:
                if event.kind == "delta":
                    parts.append(event.text)
                else:
                    terminal = event
        if terminal is None:
            raise GatewayError("Поток провайдера завершился без подтверждения результата.")
        return Generation("".join(parts), terminal.usage, terminal.finish_reason)

    @abstractmethod
    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]: ...

    @abstractmethod
    async def health(self) -> dict: ...

    @abstractmethod
    async def list_models(self) -> list[dict[str, str]]: ...
