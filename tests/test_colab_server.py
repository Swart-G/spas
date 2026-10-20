import asyncio
import json
import threading

import httpx
import pytest
from starlette.requests import ClientDisconnect

from llm.gateway.colab.server import create_server

MODEL = "team/tiny-instruct"
CONFIG = {"model_repo": MODEL, "server_key": "private-server-key"}
BODY = {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]}
HEADERS = {"Authorization": "Bearer private-server-key"}


class Engine:
    def load(self):
        self.loaded = True

    def generate(self, request, cancelled):
        yield {"kind": "delta", "text": "СПАС"}
        yield {
            "kind": "done",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }


@pytest.mark.parametrize("stream", [False, True])
async def test_private_server_response_and_sse_contract(stream):
    engine = Engine()
    app = create_server(CONFIG, engine=engine)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://runtime"
        ) as client:
            assert engine.loaded
            assert (await client.get("/v1/models")).status_code == 401
            models = await client.get("/v1/models", headers=HEADERS)
            assert models.json()["data"][0]["id"] == MODEL
            response = await asyncio.wait_for(
                client.post(
                    "/v1/chat/completions",
                    headers=HEADERS,
                    json={**BODY, "stream": stream, "stream_options": {"include_usage": True}},
                ),
                2,
            )
            assert response.status_code == 200
            if stream:
                frames = [
                    line[6:] for line in response.text.splitlines() if line.startswith("data: ")
                ]
                assert frames[-1] == "[DONE]"
                chunks = [json.loads(frame) for frame in frames[:-1]]
                assert chunks[0]["choices"][0]["delta"]["content"] == "СПАС"
                assert chunks[1]["choices"][0]["finish_reason"] == "stop"
                assert chunks[-1]["usage"]["completion_tokens"] == 1
            else:
                assert response.json()["choices"][0]["message"]["content"] == "СПАС"
                assert response.json()["usage"]["total_tokens"] == 4
            rejected = await client.post(
                "/v1/chat/completions", headers=HEADERS, json={**BODY, "tools": []}
            )
            assert rejected.status_code == 422
            rejected = await client.post(
                "/v1/chat/completions", headers=HEADERS, json={**BODY, "n": 2}
            )
            assert rejected.status_code == 400


@pytest.mark.parametrize("stream", [False, True])
async def test_generation_error_never_reports_success_or_leaks_data(stream):
    class Broken(Engine):
        def generate(self, request, cancelled):
            yield {"kind": "delta", "text": "partial"}
            raise RuntimeError("private-server-key and user prompt in traceback")

    app = create_server(CONFIG, engine=Broken())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://runtime"
    ) as client:
        response = await asyncio.wait_for(
            client.post(
                "/v1/chat/completions",
                headers=HEADERS,
                json={**BODY, "stream": stream},
            ),
            2,
        )
        assert "private-server-key" not in response.text
        if stream:
            assert "generation_failed" in response.text and "[DONE]" not in response.text
        else:
            assert response.status_code == 502


async def test_cancelled_request_keeps_model_slot_until_worker_exits():
    class Blocking(Engine):
        def __init__(self):
            self.started = threading.Event()
            self.saw_cancellation = threading.Event()
            self.release_worker = threading.Event()
            self.exited = threading.Event()
            self.calls = 0

        def generate(self, request, cancelled):
            self.calls += 1
            if self.calls == 1:
                try:
                    self.started.set()
                    if not cancelled.wait(2):
                        raise RuntimeError("Cancellation did not reach model worker")
                    self.saw_cancellation.set()
                    self.release_worker.wait(2)
                    return
                finally:
                    self.exited.set()
            yield from super().generate(request, cancelled)

    engine = Blocking()
    app = create_server(CONFIG, engine=engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://runtime"
    ) as client:
        first = asyncio.create_task(client.post("/v1/chat/completions", headers=HEADERS, json=BODY))
        assert await asyncio.to_thread(engine.started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await asyncio.to_thread(engine.saw_cancellation.wait, 1)
        busy = await client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
        assert busy.status_code == 429 and busy.headers["Retry-After"] == "1"
        engine.release_worker.set()
        assert await asyncio.to_thread(engine.exited.wait, 1)
        for _ in range(20):
            response = await client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
            if response.status_code == 200:
                break
            await asyncio.sleep(0.01)
        assert response.status_code == 200


@pytest.mark.parametrize("stream", [False, True])
async def test_tcp_disconnect_signals_worker_and_preserves_lock_until_exit(
    live_server, tcp_completion, stream
):
    class Blocking(Engine):
        def __init__(self):
            self.started = threading.Event()
            self.saw_cancellation = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def generate(self, request, cancelled):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                if cancelled.wait(5):
                    self.saw_cancellation.set()
                self.release.wait(5)
                return
            yield from super().generate(request, cancelled)

    engine = Blocking()
    app = create_server(CONFIG, engine=engine)
    async with live_server(app) as (host, port):
        _, writer = await tcp_completion(
            host, port, json.dumps({**BODY, "stream": stream}).encode(), CONFIG["server_key"]
        )
        try:
            assert await asyncio.to_thread(engine.started.wait, 2)
            writer.close()
            await writer.wait_closed()
            assert await asyncio.to_thread(engine.saw_cancellation.wait, 2)
            async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
                busy = await client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
                assert busy.status_code == 429
                engine.release.set()
                async with asyncio.timeout(2):
                    while True:
                        response = await client.post(
                            "/v1/chat/completions", headers=HEADERS, json=BODY
                        )
                        if response.status_code != 429:
                            break
                        await asyncio.sleep(0.01)
                assert response.status_code == 200
        finally:
            engine.release.set()
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize("asgi_spec", ["2.3", "2.4"])
@pytest.mark.parametrize("failure_message", ["http.response.start", "http.response.body"])
async def test_sse_send_failure_cancels_worker_and_keeps_slot_until_exit(
    asgi_spec, failure_message
):
    class Blocking(Engine):
        def __init__(self):
            self.started = threading.Event()
            self.saw_cancellation = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def generate(self, request, cancelled):
            self.calls += 1
            if self.calls == 1:
                try:
                    self.started.set()
                    if failure_message == "http.response.body":
                        yield {"kind": "delta", "text": "first"}
                    if not cancelled.wait(5):
                        raise RuntimeError("Worker was not cancelled")
                finally:
                    if cancelled.is_set():
                        self.saw_cancellation.set()
                    self.release.wait(5)
                return
            yield from super().generate(request, cancelled)

    engine = Blocking()
    app = create_server(CONFIG, engine=engine)
    payload = json.dumps({**BODY, "stream": True}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": asgi_spec},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "headers": [
            (b"authorization", HEADERS["Authorization"].encode()),
            (b"content-type", b"application/json"),
        ],
    }
    body_sent = False
    sent_messages = []

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        sent_messages.append(message["type"])
        if message["type"] == failure_message:
            assert await asyncio.to_thread(engine.started.wait, 1)
            raise OSError("Connection closed while sending response")

    try:
        with pytest.raises((OSError, ClientDisconnect)):
            await asyncio.wait_for(app(scope, receive, send), 2)
        assert sent_messages[-1] == failure_message
        assert await asyncio.to_thread(engine.saw_cancellation.wait, 1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://runtime"
        ) as client:
            busy = await client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
            assert busy.status_code == 429
            engine.release.set()
            async with asyncio.timeout(2):
                while True:
                    response = await client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
                    if response.status_code != 429:
                        break
                    await asyncio.sleep(0.01)
            assert response.status_code == 200
    finally:
        engine.release.set()
