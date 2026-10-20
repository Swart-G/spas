import copy
import json

import httpx
import pytest
from pydantic import ValidationError

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.manager import AuthManager
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.errors import GatewayError
from llm.gateway.core.system_one import (
    SystemOneRequest,
    from_native_result,
    parse_decision,
    to_native,
    validate_system_result,
)
from llm.gateway.main import GatewayKeys, create_app

BODY = {
    "model": "decision",
    "state": {"ticket": "Checkout is down"},
    "questions": {
        "team": {
            "type": "choice",
            "instructions": "Who owns this?",
            "criteria": {"payments": "Payment failures", "support": None},
        },
        "urgent": {"type": "noul", "instructions": "Is the service unavailable?"},
        "severity": {
            "type": "score",
            "instructions": "Severity?",
            "criteria": ["No impact", "Degraded", "Service down"],
        },
    },
}
RESULT = {
    "model": "upstream",
    "answers": {
        "team": {
            "type": "choice",
            "choice": "payments",
            "confidence": 0.8,
            "probabilities": {"payments": 0.9, "support": 0.1},
        },
        "urgent": {"type": "noul", "noul": 0.99},
        "severity": {
            "type": "score",
            "score": 1.8,
            "confidence": 0.7,
            "probabilities": {"0": 0, "1": 0.2, "2": 0.8},
            "legend": {"0": "No impact", "1": "Degraded", "2": "Service down"},
        },
    },
    "usage": {"input_tokens": 100, "output_tokens": 0, "cost": 0},
    "state": "private-provider-content",
    "internal": "private",
}


def config(**options):
    return BackendConfig.model_validate(
        {
            "provider": "openai_compatible",
            "account": "custom",
            "model": "inception/mercury-decide:free",
            "api_format": "responses",
            "base_url": "https://openrouter.ai/api/alpha/decisions",
            **options,
        }
    )


@pytest.mark.parametrize(
    "base_url, api_format, expected",
    [
        (
            "https://openrouter.ai/api/alpha/decisions",
            "responses",
            "https://openrouter.ai/api/alpha/decisions",
        ),
        ("https://openrouter.ai/api/v1", "responses", "https://openrouter.ai/api/alpha/decisions"),
        (
            "https://openrouter.ai/api/v1/systemone",
            "responses",
            "https://openrouter.ai/api/v1/systemone",
        ),
        ("https://custom.test/v1", "systemone", "https://custom.test/v1/systemone"),
        ("https://custom.test/api/decisions", "decisions", "https://custom.test/api/decisions"),
    ],
)
def test_decision_endpoints_are_not_chat_paths(base_url, api_format, expected):
    item = config(base_url=base_url, api_format=api_format)
    assert item.decision_model
    assert item.decision_url == expected
    assert item.api_format in {"decisions", "systemone"}
    if "openrouter.ai" in base_url:
        assert item.models_url == "https://openrouter.ai/api/v1/models"


def test_box_capabilities_follow_model_and_explicit_override():
    assert not config().supports_box
    assert config(model="typesafe/jev-1.14").supports_box
    assert not config(model="typesafe/jev-1.13").supports_box
    assert config(model="custom-model", api_format="decisions", decision_box=True).supports_box


@pytest.mark.parametrize(
    "question",
    [
        {"type": "choice", "criteria": {}},
        {"type": "choice", "criteria": {"": None}},
        {"type": "score", "criteria": []},
        {"type": "score", "criteria": list(range(11))},
        {"type": "noul"},
        {"type": "noul", "instructions": "test", "criteria": {"other": "x"}},
        {"type": "box", "instructions": "object", "criteria": ["x"]},
    ],
)
def test_invalid_question_shapes_fail_before_inference(question):
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**BODY, "questions": {"question": question}})


def test_criteria_preserves_explicit_null_descriptions():
    request = SystemOneRequest.model_validate(BODY)
    assert request.payload("actual")["questions"]["team"]["criteria"]["support"] is None
    assert request.payload("actual")["model"] == "actual"


def test_typed_answers_and_fractional_score_keep_only_public_contract():
    result = validate_system_result(RESULT, SystemOneRequest.model_validate(BODY))
    assert result["model"] == "decision"
    assert result["answers"]["severity"]["score"] == 1.8
    assert result["usage"]["cost"] == 0
    assert "state" not in result and "internal" not in result


@pytest.mark.parametrize(
    "name, patch",
    [
        ("team", {"choice": "unknown"}),
        ("team", {"probabilities": {"payments": 0.5}}),
        ("team", {"probabilities": {"payments": 0.8, "support": 0.5}}),
        ("team", {"confidence": float("nan")}),
        ("urgent", {"noul": True}),
        ("urgent", {"noul": 1.1}),
        ("severity", {"score": 3.0}),
        ("severity", {"type": "choice"}),
    ],
)
def test_invalid_answers_are_not_successful(name, patch):
    data = copy.deepcopy(RESULT)
    data["answers"][name].update(patch)
    with pytest.raises(GatewayError) as caught:
        validate_system_result(data, SystemOneRequest.model_validate(BODY))
    assert caught.value.code == "provider_incomplete"


def test_fourth_primitive_box_has_validated_normalized_edges():
    body = {
        "model": "box",
        "state": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}}],
        "questions": {"object": {"type": "box", "instructions": "The red object"}},
    }
    request = SystemOneRequest.model_validate(body)
    answer = {
        "answers": {
            "object": {
                "type": "box",
                "box": {"top": 0.2, "bottom": 0.8, "left": 0.1, "right": 0.9},
                "confidence": 0.8,
            }
        }
    }
    assert validate_system_result(answer, request)["answers"]["object"]["box"]["top"] == 0.2
    answer["answers"]["object"]["box"]["left"] = 0.99
    with pytest.raises(GatewayError):
        validate_system_result(answer, request)
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**body, "state": "no image"})


def test_system_one_can_use_native_nimble_without_changing_the_remote_server():
    request = SystemOneRequest.model_validate(BODY)
    native = to_native(request)
    assert native.fields["severity"].choices == ["0", "1", "2"]
    assert native.fields["urgent"].type == "boolean"
    result = from_native_result(
        {
            "output": {"team": "payments", "urgent": True, "severity": "2"},
            "fields": {
                "team": {"scores": {"payments": 0.9, "support": 0.1}},
                "urgent": {"scores": {"true": 0.99, "false": 0.01}},
                "severity": {"scores": {"0": 0, "1": 0.2, "2": 0.8}},
            },
        },
        request,
    )
    assert result["answers"]["severity"]["score"] == 1.8
    assert result["answers"]["urgent"]["noul"] == 0.99


@pytest.mark.parametrize("box", [False, True])
async def test_gateway_uses_decisions_contract_auth_and_audit(tmp_path, box):
    body = copy.deepcopy(BODY)
    item = config(model="typesafe/jev-1.14" if box else "inception/mercury-decide:free")
    expected = copy.deepcopy(RESULT)
    if box:
        body["state"] = [
            "Photo",
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}},
        ]
        body["questions"]["object"] = {"type": "box", "instructions": "Red object"}
        expected["answers"]["object"] = {
            "type": "box",
            "box": {"top": 0.1, "bottom": 0.9, "left": 0.2, "right": 0.8},
            "confidence": 0.8,
        }
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
        assert request.headers["Authorization"] == "Bearer private-key"
        payload = json.loads(request.content)
        assert payload["model"] == item.model
        assert payload["state"] == body["state"]
        assert payload["questions"] == body["questions"]
        assert "messages" not in payload and "stream" not in payload
        return httpx.Response(200, json=expected)

    async with httpx.AsyncClient() as client:
        await AuthManager(CredentialStore(tmp_path), client).save_api_key(
            "custom", "private-key", provider="openai_compatible"
        )
    keys = GatewayKeys("client-" + "x" * 30)
    app = create_app(
        GatewayConfig(backends={"api": item}, routes={"decision": ["api"]}),
        state_dir=tmp_path,
        keys=keys,
        transport=httpx.MockTransport(handler),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://gateway"
        ) as client:
            headers = {"Authorization": "Bearer " + keys.client}
            assert (await client.post("/v1/decisions", json=body)).status_code == 401
            response = await client.post("/v1/decisions", json=body, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["answers"]["severity"]["score"] == 1.8
            catalog = (await client.get("/v1/models", headers=headers)).json()["data"][0]
            assert catalog["capabilities"]["decisions"] is True
            assert catalog["capabilities"]["chat"] is False
            assert catalog["capabilities"]["decision_box"] is box
            chat = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json={"model": "decision", "messages": [{"role": "user", "content": "hello"}]},
            )
            assert chat.status_code == 400
    assert len(calls) == 1
    audit = (tmp_path / "requests.jsonl").read_text()
    assert (
        "private-key" not in audit and "Checkout" not in audit and "private-provider" not in audit
    )


@pytest.mark.parametrize(
    "status,code", [(401, "reauth_required"), (429, "rate_limited"), (503, "provider_unavailable")]
)
async def test_decision_http_failures_are_sanitized(tmp_path, status, code):
    from llm.gateway.providers.openai_compatible import OpenAICompatibleAdapter

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status, json={"error": {"message": "private-key"}}, headers={"Retry-After": "20"}
            )
        )
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "private-key", provider="openai_compatible")
        adapter = OpenAICompatibleAdapter(client, auth, config())
        with pytest.raises(GatewayError) as caught:
            await adapter.decide(parse_decision(BODY))
        assert caught.value.code == code and "private-key" not in caught.value.message
        if status == 429:
            assert caught.value.retry_after == 20
