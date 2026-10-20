import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from fastapi import HTTPException, Request

T = TypeVar("T")


async def until_disconnect(request: Request, operation: Awaitable[T]) -> T:
    """Run an operation until completion or disconnect, then join its cleanup."""

    async def disconnected() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    work = asyncio.ensure_future(operation)
    monitor = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((work, monitor), return_when=asyncio.FIRST_COMPLETED)
        if monitor in done:
            monitor.result()
            raise HTTPException(499, "Client disconnected")
        return work.result()
    finally:
        for task in (work, monitor):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, monitor, return_exceptions=True)
