import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..core.errors import GatewayError
from ..core.models import ChatRequest, StreamEvent
from .disconnect import until_disconnect


def build_routes(authorize) -> APIRouter:
    routes = APIRouter(prefix="/v1", dependencies=[Depends(authorize)])

    @routes.get("/models")
    async def models(request: Request) -> dict:
        return {"object": "list", "data": request.app.state.registry.models()}

    @routes.post("/chat/completions")
    async def completions(body: ChatRequest, request: Request):
        router = request.app.state.router
        response_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        common = {"id": response_id, "created": created, "model": body.model}
        if not body.stream:
            result = await until_disconnect(request, router.generate(body, response_id))
            return {
                **common,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.content},
                        "finish_reason": result.finish_reason,
                    }
                ],
                "usage": result.usage.as_dict() if result.usage else None,
            }

        # Keep SDK contexts in one producer task, including priming and cancellation.
        queue: asyncio.Queue[StreamEvent | GatewayError | None] = asyncio.Queue(maxsize=1)

        async def produce() -> None:
            try:
                async with aclosing(router.stream(body, response_id)) as events:
                    async for event in events:
                        await queue.put(event)
            except GatewayError as error:
                await queue.put(error)
            except Exception:
                await queue.put(GatewayError("Не удалось завершить запрос Gateway.", status=500))
            finally:
                # Don't block cleanup when a disconnected client stopped consuming the queue.
                if not asyncio.current_task().cancelling():
                    await queue.put(None)

        producer = asyncio.create_task(produce())

        async def cleanup() -> None:
            if not producer.done():
                producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer

        try:
            first = await until_disconnect(request, queue.get())
        except BaseException:
            await cleanup()
            raise
        if isinstance(first, GatewayError):
            await cleanup()
            raise first
        if first is None:
            await cleanup()
            raise GatewayError("Провайдер вернул пустой поток.")

        def chunk(delta: dict, finish_reason: str | None = None) -> str:
            data = {
                **common,
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish_reason},
                ],
            }
            if body.stream_options and body.stream_options.include_usage:
                data["usage"] = None
            return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

        async def consume() -> AsyncIterator[str]:
            try:
                yield chunk({"role": "assistant"})
                item = first
                while item is not None:
                    if isinstance(item, GatewayError):
                        yield "data: " + json.dumps(item.as_dict(), ensure_ascii=False) + "\n\n"
                        break
                    if item.kind == "delta":
                        yield chunk({"content": item.text})
                    else:
                        yield chunk({}, item.finish_reason)
                        if body.stream_options and body.stream_options.include_usage:
                            data = {
                                **common,
                                "object": "chat.completion.chunk",
                                "choices": [],
                                "usage": item.usage.as_dict() if item.usage else None,
                            }
                            yield "data: " + json.dumps(data) + "\n\n"
                    item = await queue.get()
                yield "data: [DONE]\n\n"
            finally:
                await cleanup()

        class GatewayStreamingResponse(StreamingResponse):
            async def __call__(self, scope, receive, send) -> None:
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    # Cleanup must also run when send() fails before Starlette's background task.
                    await cleanup()

        return GatewayStreamingResponse(
            consume(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return routes
