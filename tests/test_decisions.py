import json

import httpx
import pytest
from pydantic import ValidationError

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.manager import account_key
from llm.gateway.colab.manager import DeploymentSettings
from llm.gateway.colab.server import create_server
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.decisions import DecisionRequest, validate_result
from llm.gateway.core.errors import GatewayError
from llm.gateway.main import GatewayKeys, create_app

BODY = {
    "model": "decision",
    "context": "Service down",
    "schema": {
        "priority": {"type": "enum", "description": "Urgency", "choices": ["high", "low"]},
        "incident": {"type": "boolean", "description": "Is the service down?"},
    },
}
RESULT = {
    "output": {"priority": "high", "incident": True},
    "fields": {
        "priority": {"scores": {"high": 0.8, "low": 0.2}},
        "incident": {"scores": {"false": 0.1, "true": 0.9}},
    },
    "context": "private data",
}


@pytest.mark.parametrize(
    "patch",
    [
        {"type": "enum", "choices": ["a", "a"], "description": "test"},
        {"type": "enum", "choices": [], "description": "test"},
        {"type": "boolean", "choices": ["true"], "description": "test"},
        {"type": "object", "description": "test"},
    ],
)
def test_invalid_decision_schema_rejected(patch):
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate({**BODY, "schema": {"bad": patch}})


@pytest.mark.parametrize(
    "scores", [{"high": float("nan"), "low": 0.2}, {"high": 0.8, "low": 0.3}, {"unexpected": 1.0}]
)
def test_invalid_probabilities_rejected(scores):
    data = json.loads(json.dumps(RESULT))
    data["fields"]["priority"]["scores"] = scores
    with pytest.raises(GatewayError):
        validate_result(data, DecisionRequest.model_validate(BODY))


def test_nimble_autodetection_and_cpu_rejection():
    assert BackendConfig(
        provider="colab_managed", account="gpu", model="bespokelabs/Bespoke-Nimble-9B"
    ).decision_model
    with pytest.raises(ValidationError):
        DeploymentSettings(
            google_account="google",
            model_repo="bespokelabs/Bespoke-Nimble-9B",
            variant="VARIANT_CPU",
            accelerator="NONE",
        )


@pytest.mark.parametrize(
    "failure,fallback,status",
    [
        ("rate_limited", True, 200),
        ("provider_timeout", True, 200),
        ("provider_timeout", False, 504),
    ],
)
async def test_gateway_decisions_auth_routing_fallback_and_audit(
    tmp_path, failure, fallback, status
):
    store = CredentialStore(tmp_path)
    for account in ["first", "second"]:
        store.put(
            account_key(account),
            {
                "kind": "account",
                "account": account,
                "provider": "remote_inference",
                "state": "CONNECTED",
                "api_key": "server-secret",
            },
        )
    config = GatewayConfig(
        backends={
            name: BackendConfig(
                provider="remote_inference",
                account=name,
                model="nimble",
                model_kind="nimble",
                base_url=f"https://{name}.test",
            )
            for name in ["first", "second"]
        },
        routes={"decision": ["first", "second"]},
        fallback_on=[failure] if fallback else [],
    )
    calls = []

    def remote(request):
        calls.append(request.url.host)
        assert request.url.path == "/v1/decisions"
        assert request.headers["Authorization"] == "Bearer server-secret"
        assert json.loads(request.content)["model"] == "nimble"
        if request.url.host == "first.test":
            if failure == "provider_timeout":
                raise httpx.ReadTimeout("private upstream timeout", request=request)
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json=RESULT)

    keys = GatewayKeys("client-key-" + "x" * 30)
    app = create_app(config, state_dir=tmp_path, keys=keys, transport=httpx.MockTransport(remote))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://gateway"
        ) as client:
            assert (await client.post("/v1/decisions", json=BODY)).status_code == 401
            response = await client.post(
                "/v1/decisions", json=BODY, headers={"Authorization": "Bearer " + keys.client}
            )
            assert response.status_code == status
            if status == 200:
                assert response.json()["output"] == RESULT["output"]
            else:
                assert response.json()["error"]["code"] == "provider_timeout"
            assert "private upstream" not in response.text
            assert "context" not in response.json()
            chat = await client.post(
                "/v1/chat/completions",
                json={"model": "decision", "messages": [{"role": "user", "content": "hello"}]},
                headers={"Authorization": "Bearer " + keys.client},
            )
            assert chat.status_code == 400
    assert calls == (["first.test", "second.test"] if fallback else ["first.test"])


async def test_private_decision_server_uses_native_scorer_and_authorization():
    engine = type(
        "Engine", (), {"load": lambda self: None, "decide": lambda self, request: RESULT}
    )()
    app = create_server(
        {"model_repo": "decision", "server_key": "private", "decision_model": True}, engine=engine
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://runtime"
    ) as client:
        assert (await client.post("/v1/decisions", json=BODY)).status_code == 401
        response = await client.post(
            "/v1/decisions", json=BODY, headers={"Authorization": "Bearer private"}
        )
        assert response.status_code == 200
        assert response.json()["output"]["incident"] is True


async def test_cancelled_decision_keeps_model_locked_until_worker_finishes():
    import asyncio
    import threading

    entered, release = threading.Event(), threading.Event()

    class Engine:
        def decide(self, request):
            entered.set()
            assert release.wait(3)
            return RESULT

    app = create_server(
        {"model_repo": "decision", "server_key": "private", "decision_model": True},
        engine=Engine(),
    )
    headers = {"Authorization": "Bearer private"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://runtime"
    ) as client:
        task = asyncio.create_task(client.post("/v1/decisions", json=BODY, headers=headers))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            busy = await client.post("/v1/decisions", json=BODY, headers=headers)
            assert busy.status_code == 429
        finally:
            release.set()
        async with asyncio.timeout(2):
            while True:
                response = await client.post("/v1/decisions", json=BODY, headers=headers)
                if response.status_code != 429:
                    break
                await asyncio.sleep(0.01)
        assert response.status_code == 200
