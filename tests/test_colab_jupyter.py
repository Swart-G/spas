import asyncio
import json
from contextlib import aclosing

import httpx
import pytest
from websockets.asyncio.server import serve

from llm.gateway.colab.jupyter import MIME, JupyterBridge
from llm.gateway.core.errors import GatewayError


def message(kind, request_id, content):
    return json.dumps(
        {"header": {"msg_type": kind}, "parent_header": {"msg_id": request_id}, "content": content}
    )


def kernel_rest(calls):
    def respond(request):
        assert request.headers["X-Colab-Runtime-Proxy-Token"] == "runtime-token"
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/kernels":
            assert json.loads(request.content) == {"name": "python3"}
            return httpx.Response(201, json={"id": "kernel-1"})
        return httpx.Response(204)

    return httpx.MockTransport(respond)


@pytest.mark.parametrize("idle_first", [False, True])
async def test_jupyter_requires_correlated_reply_and_idle(idle_first):
    calls, requests = [], []

    async def handler(socket):
        assert socket.request.headers["X-Colab-Runtime-Proxy-Token"] == "runtime-token"
        request = json.loads(await socket.recv())
        requests.append(request)
        request_id = request["header"]["msg_id"]
        await socket.send(message("display_data", "other", {"data": {MIME: {"bad": True}}}))
        await socket.send(message("stream", request_id, {"text": "secret-log"}))
        await socket.send(message("display_data", request_id, {"data": {MIME: {"type": "chunk"}}}))
        replies = [
            message("execute_reply", request_id, {"status": "ok"}),
            message("status", request_id, {"execution_state": "idle"}),
        ]
        for reply in reversed(replies) if idle_first else replies:
            await socket.send(reply)
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(transport=kernel_rest(calls)) as client:
            bridge = JupyterBridge(
                client, {"url": f"http://127.0.0.1:{port}", "token": "runtime-token"}
            )
            assert [event async for event in bridge.execute("print('hello')")] == [
                {"type": "chunk"}
            ]
    assert calls == [("POST", "/api/kernels"), ("DELETE", "/api/kernels/kernel-1")]
    assert requests[0]["content"]["store_history"] is False
    assert requests[0]["content"]["allow_stdin"] is False


@pytest.mark.parametrize("kind", ["error", "disconnect", "timeout", "close_consumer"])
async def test_jupyter_interrupts_and_deletes_failed_or_cancelled_kernel(kind):
    calls = []

    async def handler(socket):
        request = json.loads(await socket.recv())
        request_id = request["header"]["msg_id"]
        if kind == "error":
            await socket.send(
                message("error", request_id, {"traceback": ["private-token in traceback"]})
            )
        elif kind == "close_consumer":
            await socket.send(message("display_data", request_id, {"data": {MIME: {"part": 1}}}))
        elif kind == "disconnect":
            return
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(transport=kernel_rest(calls)) as client:
            bridge = JupyterBridge(
                client, {"url": f"http://127.0.0.1:{port}", "token": "runtime-token"}
            )
            if kind == "close_consumer":
                async with aclosing(bridge.execute("code")) as events:
                    assert await anext(events) == {"part": 1}
            else:
                with pytest.raises(GatewayError) as error:
                    async for _ in bridge.execute(
                        "code", wait_seconds=0.05 if kind == "timeout" else 2
                    ):
                        pass
                assert "private-token" not in str(error.value)
                assert (
                    error.value.code
                    == {
                        "error": "colab_execution_failed",
                        "disconnect": "provider_incomplete",
                        "timeout": "provider_timeout",
                    }[kind]
                )
    assert calls[-2:] == [
        ("POST", "/api/kernels/kernel-1/interrupt"),
        ("DELETE", "/api/kernels/kernel-1"),
    ]


async def test_jupyter_cancellation_closes_websocket_and_kernel():
    calls = []
    entered = asyncio.Event()

    async def handler(socket):
        await socket.recv()
        entered.set()
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(transport=kernel_rest(calls)) as client:
            bridge = JupyterBridge(
                client, {"url": f"http://127.0.0.1:{port}", "token": "runtime-token"}
            )

            async def consume():
                async with aclosing(bridge.execute("code")) as events:
                    async for _ in events:
                        pass

            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert calls[-2:][0][1].endswith("/interrupt")
    assert calls[-1][0] == "DELETE"


async def test_jupyter_websocket_never_follows_redirect_with_proxy_token():
    calls, leaks = [], []

    async def target(socket):
        leaks.append(socket.request.headers.get("X-Colab-Runtime-Proxy-Token"))
        await socket.close()

    async with serve(target, "127.0.0.1", 0) as destination:
        target_port = destination.sockets[0].getsockname()[1]

        def redirect(connection, request):
            response = connection.respond(302, "redirect")
            response.headers["Location"] = f"ws://127.0.0.1:{target_port}/target"
            return response

        async def unused(socket):
            raise AssertionError("Redirect endpoint should not establish a websocket")

        async with serve(unused, "127.0.0.1", 0, process_request=redirect) as source:
            port = source.sockets[0].getsockname()[1]
            async with httpx.AsyncClient(transport=kernel_rest(calls)) as client:
                bridge = JupyterBridge(
                    client, {"url": f"http://127.0.0.1:{port}", "token": "runtime-token"}
                )
                with pytest.raises(GatewayError):
                    async for _ in bridge.execute("code"):
                        pass
    assert leaks == []
    assert calls[-1][0] == "DELETE"
