from contextlib import asynccontextmanager

import httpx
import pytest

from safety_analyzer.app import create_app
from safety_analyzer.config import Settings
from safety_analyzer.gateway import LLMGateway
from safety_analyzer.service import Analyzer


def completion(content='{"verdict":"safe","categories":[]}', finish_reason="stop"):
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ]
    }


@pytest.fixture
def settings():
    return Settings(
        gateway_base_url="https://gateway.test/v1/",
        gateway_api_key="test-client-key",
        model="safety-test",
        output_mode="json",
    )


@pytest.fixture
def analyzer_for(settings):
    @asynccontextmanager
    async def factory(handler, **overrides):
        configured = Settings(**(settings.model_dump() | overrides))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield Analyzer(LLMGateway(configured, client))

    return factory


@pytest.fixture
def api_for(settings):
    @asynccontextmanager
    async def factory(handler, **overrides):
        configured = Settings(**(settings.model_dump() | overrides))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as gateway_client:
            app = create_app(configured, gateway_client)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://analyzer.test"
                ) as client:
                    yield client

    return factory
