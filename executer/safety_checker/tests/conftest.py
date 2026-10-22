from contextlib import asynccontextmanager
from inspect import isawaitable

import httpx
import pytest

from safety_analyzer.app import create_app
from safety_analyzer.commands import CommandAnalyzer, configured_rules
from safety_analyzer.config import Settings
from safety_analyzer.gateway import LLMGateway
from safety_analyzer.service import Analyzer


def unread_mock_transport(handler):
    """Match network streaming: Response(json=...) otherwise eagerly consumes its body."""

    async def dispatch(request):
        response = handler(request)
        if isawaitable(response):
            response = await response
        if response.is_stream_consumed:
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=httpx.ByteStream(response.content),
            )
        return response

    return httpx.MockTransport(dispatch)


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
        async with httpx.AsyncClient(transport=unread_mock_transport(handler)) as client:
            yield Analyzer(LLMGateway(configured, client))

    return factory


@pytest.fixture
def command_analyzer_for(settings):
    @asynccontextmanager
    async def factory(handler, rules=None, **overrides):
        configured = Settings(**(settings.model_dump() | overrides))
        async with httpx.AsyncClient(
            transport=unread_mock_transport(handler), trust_env=False
        ) as client:
            yield CommandAnalyzer(
                LLMGateway(configured, client),
                configured_rules(configured) if rules is None else rules,
            )

    return factory


@pytest.fixture
def api_for(settings):
    @asynccontextmanager
    async def factory(handler, **overrides):
        configured = Settings(**(settings.model_dump() | overrides))
        async with httpx.AsyncClient(transport=unread_mock_transport(handler)) as gateway_client:
            app = create_app(configured, gateway_client)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://analyzer.test"
                ) as client:
                    yield client

    return factory


@pytest.fixture(scope="module")
def actual_executor(tmp_path_factory):
    import importlib
    from pathlib import Path
    from types import SimpleNamespace

    REFERENCE = (
        Path(__file__).resolve().parents[1]
        / "reports/reference/00b54e3ebfb768ebfdf0ee69304879b3cb1c7c7b"
    )
    pytest.importorskip("mcp", reason="install integration dependencies")
    assert (REFERENCE / ".complete").is_file(), "run scripts/prepare_executor_reference.py"
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(REFERENCE))
        patch.setenv("EXECUTOR_LOG_DIR", str(tmp_path_factory.mktemp("executor-logs")))
        server = importlib.import_module("executer.server_mcp.Server")
        models = importlib.import_module("executer.server_mcp.Models")
        real_rules = importlib.import_module("executer.safety_checker")
        yield SimpleNamespace(server=server, models=models, rules=real_rules)
        importlib.import_module("executer.server_mcp.Logging").shutdown_logging()
