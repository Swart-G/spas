"""Run against real pinned SPAS code with mocked remote inference, without credentials."""

import json

import httpx
import pytest

pytest.importorskip("llm.gateway.main", reason="install the integration dependency group")
from conftest import completion  # noqa: E402
from llm.gateway.auth.credentials import CredentialStore  # noqa: E402
from llm.gateway.auth.manager import AuthManager  # noqa: E402
from llm.gateway.config import BackendConfig, GatewayConfig  # noqa: E402
from llm.gateway.main import GatewayKeys, create_app  # noqa: E402

from safety_analyzer.config import Settings  # noqa: E402
from safety_analyzer.contracts import AnalyzeRequest  # noqa: E402
from safety_analyzer.gateway import LLMGateway  # noqa: E402
from safety_analyzer.service import Analyzer  # noqa: E402

pytestmark = pytest.mark.gateway_contract


@pytest.mark.parametrize(
    "mode,content,json_capability,expected",
    [
        ("label", "safe", False, "allow"),
        ("label", "unsafe", False, "block"),
        ("json", '{"verdict":"safe","categories":[]}', True, "allow"),
        ("json", '{"verdict":"unsafe","categories":["privacy"]}', True, "block"),
        ("json", '{"verdict":"safe","categories":[]}', False, "block"),
        ("label", "safe with explanation", False, "block"),
    ],
)
async def test_analyzer_through_spas_remote_adapter(
    tmp_path, mode, content, json_capability, expected
):
    calls = []

    def upstream(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert str(request.url) == "https://runtime.test/v1/chat/completions"
        assert payload["model"] == "model-placeholder"
        assert payload["max_tokens"] == 128
        assert payload["response_format"]["type"] == ("text" if mode == "label" else "json_object")
        return httpx.Response(200, json=completion(content))

    config = GatewayConfig(
        backends={
            "safety": BackendConfig(
                provider="remote_inference",
                account="test-runtime",
                model="model-placeholder",
                base_url="https://runtime.test/v1",
                remote_json_output=json_capability,
            )
        },
        routes={"safety-default": ["safety"]},
        fallback_on=[],
    )
    async with httpx.AsyncClient() as auth_client:
        await AuthManager(CredentialStore(tmp_path), auth_client).save_remote_key(
            "test-runtime", ""
        )
    app = create_app(
        config,
        state_dir=tmp_path,
        keys=GatewayKeys("c" * 32),
        transport=httpx.MockTransport(upstream),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client:
            settings = Settings(
                gateway_base_url="http://gateway.test/v1",
                gateway_api_key="c" * 32,
                model="safety-default",
                output_mode=mode,
            )
            result = await Analyzer(LLMGateway(settings, client)).analyze(
                AnalyzeRequest(text="hello")
            )
    assert result.decision == expected
    if mode == "json" and not json_capability:
        assert result.error_code == "gateway_unavailable" and calls == []
    elif content == "safe with explanation":
        assert result.error_code == "invalid_model_output"
    else:
        assert result.status == "ok" and len(calls) == 1
