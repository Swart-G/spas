import asyncio
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn


@pytest.fixture
def live_server():
    @asynccontextmanager
    async def serve(app):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("Server stopped during startup")
                    await asyncio.sleep(0.01)
            yield host, port
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 5)
            finally:
                sock.close()

    return serve


@pytest.fixture
def tcp_completion():
    async def send(host, port, body, key):
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Authorization: Bearer {key}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        return reader, writer

    return send
