import asyncio
import json

import httpx
import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from llm.gateway.colab.jupyter import JupyterBridge
from llm.gateway.colab.terminal import relay, terminal_socket


async def test_terminal_proxy_io_resize_and_detach_delete_only_created_terminal():
    calls, received, output = [], [], []
    frames = asyncio.Queue()

    async def handler(socket):
        assert socket.request.headers["X-Colab-Runtime-Proxy-Token"] == "private"
        assert "colab-runtime-proxy-token=private" in socket.request.path
        await socket.send(json.dumps(["stdout", "remote prompt"]))
        for _ in range(2):
            received.append(json.loads(await socket.recv()))
        await frames.put("\x1d")
        await socket.wait_closed()

    def rest(request):
        calls.append((request.method, request.url.path))
        assert request.headers["X-Colab-Runtime-Proxy-Token"] == "private"
        return (
            httpx.Response(201, json={"name": "7"})
            if request.method == "POST"
            else httpx.Response(204)
        )

    await frames.put("echo hello\r\x03")
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(transport=httpx.MockTransport(rest)) as client:
            bridge = JupyterBridge(client, {"url": f"http://127.0.0.1:{port}", "token": "private"})
            async with terminal_socket(bridge) as socket:
                await asyncio.wait_for(
                    relay(socket, frames.get, output.append, lambda: (100, 40)), 2
                )
    assert received == [["set_size", 40, 100], ["stdin", "echo hello\r\x03"]]
    assert output == ["remote prompt"]
    assert calls == [("POST", "/api/terminals"), ("DELETE", "/api/terminals/7")]


@pytest.mark.parametrize("failure", ["cancel", "redirect"])
async def test_terminal_connection_failure_or_cancel_cleans_up(failure):
    calls = []

    def rest(request):
        calls.append(request.method)
        return (
            httpx.Response(201, json={"name": "1"})
            if request.method == "POST"
            else httpx.Response(204)
        )

    def redirect(connection, request):
        response = connection.respond(302, "redirect")
        response.headers["Location"] = "ws://127.0.0.1:1/leak"
        return response

    async def handler(socket):
        await socket.wait_closed()

    async with serve(
        handler, "127.0.0.1", 0, process_request=redirect if failure == "redirect" else None
    ) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(transport=httpx.MockTransport(rest)) as client:
            bridge = JupyterBridge(client, {"url": f"http://127.0.0.1:{port}", "token": "private"})
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else InvalidStatus):
                async with terminal_socket(bridge):
                    raise asyncio.CancelledError()
    assert calls == ["POST", "DELETE"]
