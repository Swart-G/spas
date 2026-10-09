import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import aclosing

from ..auth.manager import AuthManager
from .audit import RequestLog
from .errors import GatewayError
from .models import ChatRequest, Generation, StreamEvent
from .registry import Registry


class Router:
    def __init__(self, registry: Registry, auth: AuthManager, audit: RequestLog) -> None:
        self.registry, self.auth, self.audit = registry, auth, audit

    async def generate(self, request: ChatRequest, request_id: str) -> Generation:
        pieces = []
        terminal = None
        async with aclosing(self.stream(request, request_id)) as events:
            async for event in events:
                if event.kind == "delta":
                    pieces.append(event.text)
                else:
                    terminal = event
        if terminal is None:
            raise GatewayError("Результат генерации не подтверждён.")
        return Generation("".join(pieces), terminal.usage, terminal.finish_reason)

    async def stream(self, request: ChatRequest, request_id: str) -> AsyncIterator[StreamEvent]:
        self.registry.capabilities(request.model).validate(request)
        backends = self.registry.resolve(request.model)
        for index, backend in enumerate(backends):
            started = time.monotonic()
            visible = False
            terminal = None
            result_recorded = False
            try:
                self.auth.check_cooldown(backend.config.account)
                try:
                    async with asyncio.timeout(backend.config.timeout_seconds):
                        async with backend.semaphore:
                            self.auth.check_cooldown(backend.config.account)
                            try:
                                async with aclosing(backend.provider.stream(request)) as events:
                                    async for event in events:
                                        if terminal is not None:
                                            raise GatewayError(
                                                "Провайдер продолжил завершённый ответ."
                                            )
                                        if event.kind == "done":
                                            terminal = event
                                        elif event.text:
                                            visible = True
                                            yield event
                                if terminal is None:
                                    raise GatewayError(
                                        "Провайдер не подтвердил результат.",
                                        code="provider_incomplete",
                                        status=502,
                                    )
                            except GatewayError as error:
                                # Publish the cooldown before a queued request takes this slot.
                                await self.auth.note_result(backend.config.account, error)
                                result_recorded = True
                                raise
                            await self.auth.note_result(backend.config.account)
                            result_recorded = True
                except TimeoutError as error:
                    raise GatewayError(
                        "Истекло время ожидания backend.", code="provider_timeout", status=504
                    ) from error
                self.audit.record(
                    request_id=request_id,
                    model=request.model,
                    backend=backend.name,
                    status="completed",
                    latency_ms=round((time.monotonic() - started) * 1000),
                    usage=terminal.usage.as_dict() if terminal.usage else None,
                )
                yield terminal
                return
            except GatewayError as error:
                if not result_recorded:
                    await self.auth.note_result(backend.config.account, error)
                self.audit.record(
                    request_id=request_id,
                    model=request.model,
                    backend=backend.name,
                    status=error.code,
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
                if (
                    visible
                    or error.code not in self.registry.config.fallback_on
                    or index + 1 == len(backends)
                ):
                    raise
                # Only the explicitly configured next backend can receive this request.
            except asyncio.CancelledError:
                self.audit.record(
                    request_id=request_id,
                    model=request.model,
                    backend=backend.name,
                    status="cancelled",
                )
                raise
