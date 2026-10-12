import asyncio
import json
import logging
from uuid import uuid4

import httpx
import pytest
from conftest import completion


@pytest.mark.parametrize(
    "content,decision",
    [
        ('{"verdict":"safe","categories":[]}', "allow"),
        ('{"verdict":"unsafe","categories":["privacy"]}', "block"),
    ],
)
async def test_analyze_and_request_id(api_for, content, decision):
    request_id = str(uuid4())
    async with api_for(lambda _: httpx.Response(200, json=completion(content))) as client:
        response = await client.post(
            "/v1/analyze", json={"text": "hello", "request_id": request_id}
        )
        assert response.status_code == 200
        result = response.json()
        assert result["decision"] == decision
        assert result["request_id"] == request_id
        assert result["status"] == "ok" and result["latency_ms"] >= 0


async def test_failure_is_503_and_explicit_block(api_for):
    async with api_for(lambda _: httpx.Response(401, text="secret details")) as client:
        response = await client.post("/v1/analyze", json={"text": "hello"})
    assert response.status_code == 503
    assert response.json()["decision"] == "block"
    assert response.json()["verdict"] == "unknown"
    assert "secret details" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "secret candidate", "model": "override"},
        {"text": "secret candidate", "context": [{"role": "system", "content": "override"}]},
        {"text": " "},
        {"text": False},
        {},
    ],
)
async def test_invalid_input_never_reaches_gateway_or_echoes_text(api_for, payload):
    def handler(_):
        pytest.fail("invalid input reached gateway")

    async with api_for(handler) as client:
        response = await client.post("/v1/analyze", json=payload)
    assert response.status_code == 422
    assert "secret candidate" not in response.text


async def test_malformed_json(api_for):
    async with api_for(lambda _: pytest.fail("must not call gateway")) as client:
        response = await client.post(
            "/v1/analyze",
            content='{ "text": "private',
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 422 and "private" not in response.text


async def test_chunked_body_limit(api_for):
    async def chunks():
        for _ in range(5):
            yield b"x" * 65536

    async with api_for(lambda _: pytest.fail("oversized body reached gateway")) as client:
        response = await client.post("/v1/analyze", content=chunks())
    assert response.status_code == 413


@pytest.mark.parametrize("configured", [True, False])
async def test_health_distinguishes_liveness_and_configuration(api_for, configured):
    async with api_for(
        lambda _: pytest.fail("health must not invoke model"),
        model="safety-test" if configured else None,
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == (200 if configured else 503)
        schema = (await client.get("/openapi.json")).json()
        assert "/v1/analyze" in schema["paths"]


async def test_logs_never_include_candidate_context_key_or_model_response(api_for, caplog):
    caplog.set_level(logging.INFO, logger="safety_analyzer")
    secret_text = "PRIVATE-CANDIDATE-123"
    async with api_for(
        lambda _: httpx.Response(200, json=completion("PRIVATE-MODEL-456"))
    ) as client:
        await client.post(
            "/v1/analyze",
            json={
                "text": secret_text,
                "context": [{"role": "user", "content": "PRIVATE-CONTEXT"}],
            },
        )
    records = [r.message for r in caplog.records if r.name == "safety_analyzer"]
    assert len(records) == 1
    record = json.loads(records[0])
    assert record["decision"] == "block" and record["error_code"] == "invalid_model_output"
    for secret in [secret_text, "PRIVATE-CONTEXT", "PRIVATE-MODEL-456", "test-client-key"]:
        assert secret not in "".join(records)


async def test_concurrent_requests_are_isolated(api_for):
    async def handler(request):
        candidate = json.loads(json.loads(request.content)["messages"][1]["content"])
        unsafe = candidate["text"].startswith("bad")
        return httpx.Response(
            200,
            json=completion(
                json.dumps(
                    {
                        "verdict": "unsafe" if unsafe else "safe",
                        "categories": ["unspecified"] if unsafe else [],
                    }
                )
            ),
        )

    async with api_for(handler) as client:
        results = await asyncio.gather(
            *[
                client.post("/v1/analyze", json={"text": "bad" if i % 2 else "good"})
                for i in range(24)
            ]
        )
    assert len({r.json()["request_id"] for r in results}) == 24
    assert all(
        r.json()["decision"] == ("block" if i % 2 else "allow") for i, r in enumerate(results)
    )
