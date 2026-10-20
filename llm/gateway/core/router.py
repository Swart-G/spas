import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import aclosing

from ..auth.manager import AuthManager
from .audit import RequestLog
from .decisions import validate_result
from .errors import GatewayError
from .models import ChatRequest, Generation, StreamEvent
from .registry import Registry
from .system_one import SystemOneRequest, from_native_result, to_native, validate_system_result


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

    async def decide(self, request, request_id):
        capabilities = self.registry.capabilities(request.model)
        if not capabilities.decisions:
            raise GatewayError(
                "Маршрут не поддерживает типизированные решения.",
                code="unsupported_parameter",
                status=400,
            )
        backends = self.registry.resolve(request.model)
        system_one = isinstance(request, SystemOneRequest)
        if (
            system_one
            and any(q.type == "box" for q in request.questions.values())
            and not capabilities.decision_box
        ):
            raise GatewayError(
                "Маршрут не поддерживает Box. Выберите модель с поддержкой Box.",
                code="unsupported_parameter",
                status=400,
            )
        native = (
            to_native(request)
            if system_one
            and any(
                backend.config.provider in {"remote_inference", "colab_managed"}
                for backend in backends
            )
            else None
        )
        for index, backend in enumerate(backends):
            started = time.monotonic()
            recorded = False
            try:
                self.auth.check_cooldown(backend.config.account)
                try:
                    async with asyncio.timeout(backend.config.timeout_seconds):
                        async with backend.semaphore:
                            self.auth.check_cooldown(backend.config.account)
                            try:
                                native_backend = native is not None and backend.config.provider in {
                                    "remote_inference",
                                    "colab_managed",
                                }
                                data = await backend.provider.decide(
                                    native if native_backend else request
                                )
                                result = (
                                    from_native_result(data, request)
                                    if native_backend
                                    else validate_system_result(data, request)
                                    if system_one
                                    else validate_result(data, request)
                                )
                            except GatewayError as error:
                                await self.auth.note_result(backend.config.account, error)
                                recorded = True
                                raise
                            await self.auth.note_result(backend.config.account)
                            recorded = True
                except TimeoutError as error:
                    raise GatewayError(
                        "Модель не ответила за отведённое время. "
                        "Повторите запрос или увеличьте время ожидания.",
                        code="provider_timeout",
                        status=504,
                    ) from error
                self.audit.record(
                    request_id=request_id,
                    model=request.model,
                    backend=backend.name,
                    status="completed",
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
                return result
            except GatewayError as error:
                if not recorded:
                    await self.auth.note_result(backend.config.account, error)
                self.audit.record(
                    request_id=request_id,
                    model=request.model,
                    backend=backend.name,
                    status=error.code,
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
                if error.code not in self.registry.config.fallback_on or index + 1 == len(backends):
                    raise
            except asyncio.CancelledError:
                self.audit.record(
                    request_id=request_id,
                    model=request.model,
                    backend=backend.name,
                    status="cancelled",
                )
                raise

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
                                                "Сервис нарушил порядок ответа. Повторите запрос."
                                            )
                                        if event.kind == "done":
                                            terminal = event
                                        elif event.text:
                                            visible = True
                                            yield event
                                if terminal is None:
                                    raise GatewayError(
                                        "Сервис не подтвердил завершение ответа. Повторите запрос.",
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
                        "Модель не ответила за отведённое время. "
                        "Повторите запрос или увеличьте время ожидания.",
                        code="provider_timeout",
                        status=504,
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
