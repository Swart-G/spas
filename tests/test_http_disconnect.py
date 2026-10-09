import asyncio
import json

import httpx
import pytest

from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.models import Capabilities, StreamEvent
from llm.gateway.main import GatewayKeys, create_app

KEY = "client-key-for-disconnect-tests"


@pytest.mark.parametrize("phase", ["nonstream", "priming", "streaming"])
async def test_tcp_disconnect_closes_provider_and_releases_slot(
    tmp_path, live_server, tcp_completion, phase
):
    class BlockingProvider:
        capabilities = Capabilities()

        def __init__(self):
            self.started = asyncio.Event()
            self.closed = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            try:
                if self.calls == 1:
                    self.started.set()
                    if phase == "streaming":
                        yield StreamEvent("delta", text="first")
                    await self.release.wait()
                yield StreamEvent("delta", text="completed")
                yield StreamEvent("done")
            finally:
                self.closed.set()

    provider = BlockingProvider()
    config = GatewayConfig(
        backends={
            "test": BackendConfig(
                provider="remote_inference",
                account="test",
                model="tiny",
                base_url="http://unused.test",
                timeout_seconds=30,
            )
        },
        routes={"local": ["test"]},
    )
    app = create_app(
        config, state_dir=tmp_path, keys=GatewayKeys(KEY), providers={"test": provider}
    )
    body = {"model": "local", "messages": [{"role": "user", "content": "hello"}]}
    async with live_server(app) as (host, port):
        reader, writer = await tcp_completion(
            host, port, json.dumps({**body, "stream": phase != "nonstream"}).encode(), KEY
        )
        try:
            await asyncio.wait_for(provider.started.wait(), 2)
            if phase == "streaming":
                await asyncio.wait_for(reader.readuntil(b"first"), 2)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(provider.closed.wait(), 2)
            assert not app.state.registry.backends["test"].semaphore.locked()
            assert app.state.audit.recent()[-1]["status"] == "cancelled"
            async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
                response = await client.post(
                    "/v1/chat/completions", headers={"Authorization": "Bearer " + KEY}, json=body
                )
            assert response.status_code == 200
            assert response.json()["choices"][0]["message"]["content"] == "completed"
        finally:
            writer.close()
            await writer.wait_closed()
            provider.release.set()
