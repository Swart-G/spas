import json

import httpx
import pytest
from pydantic import ValidationError

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.manager import AuthManager
from llm.gateway.cli import parser, save_config
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.errors import GatewayError
from llm.gateway.core.models import ChatRequest
from llm.gateway.main import GatewayKeys, create_app, make_registry
from llm.gateway.providers.openai_compatible import OpenAICompatibleAdapter


def backend(**options):
    return BackendConfig.model_validate(
        {
            "provider": "openai_compatible",
            "account": "custom",
            "model": "org/model",
            "base_url": "https://custom.test/proxy/api/v3/",
            **options,
        }
    )


def request(stream=False, **options):
    return ChatRequest(
        model="custom-default",
        messages=[
            {"role": "system", "content": "Be concise"},
            {"role": "user", "content": "Hi"},
        ],
        stream=stream,
        **options,
    )


def chat_result():
    return {
        "choices": [{"index": 0, "message": {"content": "Hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    }


def response_result(**options):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "Hello"},
                ],
            }
        ],
        "usage": {"input_tokens": 2, "output_tokens": 1},
        **options,
    }


def frame(value):
    return "data: " + (value if isinstance(value, str) else json.dumps(value)) + "\n\n"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://localhost:1234", "http://localhost:1234/v1"),
        ("https://custom.test/v1/", "https://custom.test/v1"),
        ("https://custom.test/api/v3/", "https://custom.test/api/v3"),
        ("https://custom.test/proxy/openai", "https://custom.test/proxy/openai"),
        (" https://custom.test/v1/ ", "https://custom.test/v1"),
    ],
)
def test_custom_endpoint_and_config_roundtrip(tmp_path, url, expected):
    item = backend(base_url=url)
    assert item.base_url == expected
    config = GatewayConfig(backends={"custom": item}, routes={"alias": ["custom"]})
    path = tmp_path / "gateway.toml"
    save_config(config, path)
    assert GatewayConfig.load(path) == config


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://host",
        "https://user:secret@host",
        "https://host?key=secret",
        "https://host/#secret",
        "https://host:bad",
        "https://bad host",
        "https://custom.test/\x1b[2J",
    ],
)
def test_invalid_endpoint(url):
    with pytest.raises(ValidationError):
        backend(base_url=url)


def test_legacy_config_keeps_responses_defaults():
    item = BackendConfig(provider="openai_api", account="legacy", model="old-model")
    assert item.base_url == "https://api.openai.com/v1"
    assert item.api_format == "responses"
    assert item.api_json_output is True
    assert backend().api_format == "chat_completions"
    assert backend().api_json_output is False


@pytest.mark.parametrize("provider", ["openai_api", "openai_compatible"])
@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("trailing_slash", [False, True])
@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "/v1",
        "/proxy/responses",
        "/proxy/decisions",
        "/proxy/systemone",
        "/proxy/chat/completions",
    ],
)
async def test_full_endpoint_survives_config_roundtrip(
    tmp_path, provider, api_format, trailing_slash, prefix
):
    suffix = "/responses" if api_format == "responses" else "/chat/completions"
    endpoint = "https://custom.test" + prefix + suffix
    item = backend(provider=provider, base_url=endpoint + ("/" if trailing_slash else ""))
    config = GatewayConfig(backends={"custom": item}, routes={"alias": ["custom"]})
    path = tmp_path / "gateway.toml"
    save_config(config, path)
    restored = GatewayConfig.load(path).backends["custom"]
    assert restored.api_format == api_format
    calls = []

    def handler(upstream):
        calls.append((upstream.method, str(upstream.url)))
        if upstream.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "org/model"}]})
        result = response_result() if api_format == "responses" else chat_result()
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "test-key", provider=provider)
        adapter = OpenAICompatibleAdapter(client, auth, restored)
        assert (await adapter.generate(request())).content == "Hello"
        assert (await adapter.list_models())[0]["id"] == "org/model"
    assert calls == [("POST", endpoint), ("GET", "https://custom.test" + prefix + "/models")]


@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("key", ["secret-custom-key", ""])
async def test_full_gateway_custom_api(tmp_path, api_format, key):
    item = backend(
        api_format=api_format, api_json_output=True, api_token_limit="max_completion_tokens"
    )
    seen = []

    def handler(upstream):
        seen.append(upstream)
        suffix = "/responses" if api_format == "responses" else "/chat/completions"
        assert str(upstream.url) == "https://custom.test/proxy/api/v3" + suffix
        assert upstream.headers.get("authorization") == ("Bearer " + key if key else None)
        payload = json.loads(upstream.content)
        assert payload["model"] == "org/model"
        assert payload["stream"] is False
        assert payload["temperature"] == 0.5
        if api_format == "responses":
            assert payload["input"] == [message.model_dump() for message in request().messages]
            assert payload["store"] is False
            assert payload["max_output_tokens"] == 50
            assert payload["text"]["format"] == {"type": "json_object"}
            result = response_result()
        else:
            assert payload["messages"] == [message.model_dump() for message in request().messages]
            assert payload["max_completion_tokens"] == 50
            assert "max_tokens" not in payload and "stream_options" not in payload
            result = chat_result()
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient() as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", key, provider="openai_compatible", no_auth=not key)
    config = GatewayConfig(backends={"custom": item}, routes={"custom-default": ["custom"]})
    keys = GatewayKeys(client="gateway-key-for-tests-123456")
    app = create_app(config, state_dir=tmp_path, keys=keys, transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + keys.client},
                json=request(
                    max_tokens=50, temperature=0.5, response_format={"type": "json_object"}
                ).model_dump(exclude_none=True),
            )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Hello"
    assert response.json()["usage"]["total_tokens"] == 3
    assert len(seen) == 1
    assert "secret-custom-key" not in (tmp_path / "requests.jsonl").read_text()


@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("include_usage", [False, True])
async def test_streams_and_usage(tmp_path, api_format, include_usage):
    def handler(upstream):
        payload = json.loads(upstream.content)
        assert payload["stream"] is True
        if api_format == "chat_completions":
            assert ("stream_options" in payload) == include_usage
            chunks = [
                {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            if include_usage:
                chunks.append({"choices": [], "usage": chat_result()["usage"]})
            chunks.append("[DONE]")
        else:
            chunks = [
                {"type": "response.output_text.delta", "delta": "Hello"},
                {"type": "response.completed", "response": response_result()},
            ]
        return httpx.Response(200, text="".join(frame(chunk) for chunk in chunks))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "", provider="openai_compatible", no_auth=True)
        adapter = OpenAICompatibleAdapter(
            client, auth, backend(api_format=api_format, api_include_usage=include_usage)
        )
        events = [event async for event in adapter.stream(request(stream=True))]
    assert "".join(event.text for event in events) == "Hello"
    assert events[-1].kind == "done"
    assert sum(event.kind == "done" for event in events) == 1
    assert (events[-1].usage is not None) == (include_usage or api_format == "responses")


@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
async def test_truncated_stream_cannot_succeed(tmp_path, api_format):
    data = (
        {"choices": [{"delta": {"content": "partial"}}]}
        if api_format == "chat_completions"
        else {"type": "response.output_text.delta", "delta": "partial"}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=frame(data)))
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "", provider="openai_compatible", no_auth=True)
        adapter = OpenAICompatibleAdapter(client, auth, backend(api_format=api_format))
        with pytest.raises(GatewayError) as error:
            await adapter.generate(request(stream=True))
        assert error.value.code == "provider_incomplete"


@pytest.mark.parametrize(
    "result, reason",
    [
        (
            response_result(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            ),
            "length",
        ),
        (
            response_result(
                output=[
                    {"type": "message", "content": [{"type": "refusal", "refusal": "Cannot help"}]}
                ]
            ),
            "content_filter",
        ),
    ],
)
async def test_responses_completion_reasons(tmp_path, result, reason):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result))
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "key", provider="openai_compatible")
        adapter = OpenAICompatibleAdapter(client, auth, backend(api_format="responses"))
        assert (await adapter.generate(request())).finish_reason == reason


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "reauth_required"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (302, "provider_unavailable"),
    ],
)
async def test_catalog_errors_do_not_leak_credentials_and_redirects(tmp_path, status, code):
    calls = []

    def handler(upstream):
        calls.append(upstream)
        return httpx.Response(
            status,
            json={"error": {"message": "secret-key"}},
            headers={"Retry-After": "20", "Location": "https://other.test/v1"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "secret-key", provider="openai_compatible")
        adapter = OpenAICompatibleAdapter(client, auth, backend())
        with pytest.raises(GatewayError) as error:
            await adapter.list_models()
        assert error.value.code == code
        assert "secret-key" not in error.value.message
        assert len(calls) == 1
        if status == 429:
            assert error.value.retry_after == 20
            assert auth.public_account(auth.store.get("account:custom"))["state"] == "RATE_LIMITED"


async def test_catalog_and_health_use_custom_endpoint(tmp_path):
    def handler(upstream):
        assert str(upstream.url) == "https://custom.test/proxy/api/v3/models"
        return httpx.Response(200, json={"data": [{"id": "org/model"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "key", provider="openai_compatible")
        registry = make_registry(
            GatewayConfig(backends={"api": backend()}, routes={}), auth, client
        )
        assert isinstance(registry.backends["api"].provider, OpenAICompatibleAdapter)
        result = await registry.backends["api"].provider.health()
        assert result["available"] is True and result["model_available"] is True


@pytest.mark.parametrize(
    "metadata,kind",
    [
        ({}, None),
        ({"architecture": None}, None),
        ({"architecture": {}}, None),
        ({"architecture": {"input_modalities": ["text"]}}, None),
        ({"architecture": {"output_modalities": None}}, None),
        ({"architecture": {"output_modalities": []}}, None),
        ({"architecture": {"output_modalities": ["image"]}}, None),
        ({"architecture": {"output_modalities": ["text"]}}, "chat"),
        ({"architecture": {"output_modalities": ["decisions"]}}, "decision"),
    ],
)
async def test_catalog_kind_requires_explicit_output_modalities(tmp_path, metadata, kind):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"id": "org/model", **metadata}]})
        )
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "key", provider="openai_compatible")
        models = await OpenAICompatibleAdapter(client, auth, backend()).list_models()
    assert models[0].get("kind") == kind


@pytest.mark.parametrize(
    "item",
    [
        {"id": " "},
        {"id": " org/model "},
        {"id": "org/model\x1b[2J"},
        {"id": "bad\ud800"},
        {"id": "org/model", "name": ["bad"]},
        {"id": "org/model", "name": "unsafe\x1b[2J"},
        {"id": "org/model", "name": "bad\ud800"},
        {"id": "org/model", "architecture": ["bad"]},
        {"id": "org/model", "architecture": {"output_modalities": "text"}},
        {"id": "org/model", "architecture": {"output_modalities": [1]}},
    ],
)
async def test_malformed_catalog_cannot_crash_or_control_the_tui(tmp_path, item):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=json.dumps({"data": [item]}).encode(),
                headers={"Content-Type": "application/json"},
            )
        )
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "key", provider="openai_compatible")
        with pytest.raises(GatewayError) as caught:
            await OpenAICompatibleAdapter(client, auth, backend()).list_models()
    assert caught.value.status == 502
    assert "unsafe" not in caught.value.message


@pytest.mark.parametrize(
    "transport_mode",
    ["chat-json", "chat-sse", "responses-json", "responses-sse", "decisions", "catalog"],
)
@pytest.mark.parametrize(
    "error_code,expected,state",
    [
        (429, "rate_limited", "RATE_LIMITED"),
        ("429", "rate_limited", "RATE_LIMITED"),
        ("insufficient_quota", "rate_limited", "RATE_LIMITED"),
        ("invalid_api_key", "reauth_required", "REAUTH_REQUIRED"),
    ],
)
async def test_embedded_errors_update_account_state_across_transports(
    tmp_path, transport_mode, error_code, expected, state
):
    api_format = (
        "decisions"
        if transport_mode == "decisions"
        else ("responses" if transport_mode.startswith("responses") else "chat_completions")
    )
    item = backend(api_format=api_format)
    calls = []

    def handler(upstream):
        calls.append(upstream)
        error = {"code": error_code, "message": "private-provider-body", "retry_after": 17}
        if transport_mode.endswith("sse"):
            data = (
                {"type": "response.failed", "response": {"error": error}}
                if api_format == "responses"
                else {"error": error, "choices": []}
            )
            return httpx.Response(200, text=frame(data), headers={"Retry-After": "17"})
        return httpx.Response(200, json={"error": error}, headers={"Retry-After": "17"})

    async with httpx.AsyncClient() as client:
        await AuthManager(CredentialStore(tmp_path), client).save_api_key(
            "custom", "key", provider="openai_compatible"
        )
    keys = GatewayKeys("client-key-for-error-tests-123456")
    app = create_app(
        GatewayConfig(backends={"custom": item}, routes={"custom-default": ["custom"]}),
        state_dir=tmp_path,
        keys=keys,
        transport=httpx.MockTransport(handler),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://gateway"
        ) as client:
            headers = {"Authorization": "Bearer " + keys.client}
            if transport_mode == "catalog":
                with pytest.raises(GatewayError) as caught:
                    await app.state.registry.backends["custom"].provider.list_models()
                assert caught.value.code == expected
            elif api_format == "decisions":
                response = await client.post(
                    "/v1/decisions",
                    headers=headers,
                    json={
                        "model": "custom-default",
                        "state": "test",
                        "questions": {"q": {"type": "noul", "instructions": "Is this a test?"}},
                    },
                )
                assert response.json()["error"]["code"] == expected
            else:
                response = await client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json=request(stream=transport_mode.endswith("sse")).model_dump(
                        exclude_none=True
                    ),
                )
                assert response.json()["error"]["code"] == expected
            saved = app.state.auth.store.get("account:custom")
            assert saved["state"] == state
            if state == "RATE_LIMITED":
                assert saved["retry_at"] > 0
                with pytest.raises(GatewayError) as caught:
                    app.state.auth.check_cooldown("custom")
                assert caught.value.code == "rate_limited"
                assert 0 < caught.value.retry_after <= 17
            else:
                with pytest.raises(GatewayError) as caught:
                    app.state.registry.backends["custom"].provider._headers()
                assert caught.value.code == "reauth_required"
    assert len(calls) == 1
    assert "private-provider-body" not in (tmp_path / "requests.jsonl").read_text()


@pytest.mark.parametrize("delta", ["Hel", "Different answer", "HelloHello"])
async def test_responses_stream_must_match_supplied_final_output(tmp_path, delta):
    data = frame({"type": "response.output_text.delta", "delta": delta})
    data += frame({"type": "response.completed", "response": response_result()})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=data))
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "key", provider="openai_compatible")
        adapter = OpenAICompatibleAdapter(client, auth, backend(api_format="responses"))
        with pytest.raises(GatewayError) as caught:
            await adapter.generate(request(stream=True))
    assert caught.value.code == "provider_incomplete"


@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
@pytest.mark.parametrize(
    "media_type",
    ["application/json", "application/problem+json", "Application/JSON; charset=utf-8"],
)
async def test_json_error_on_stream_request_is_normalized(tmp_path, api_format, media_type):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"error": {"code": 429}},
                headers={"Content-Type": media_type, "Retry-After": "17"},
            )
        )
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "key", provider="openai_compatible")
        adapter = OpenAICompatibleAdapter(client, auth, backend(api_format=api_format))
        with pytest.raises(GatewayError) as caught:
            await adapter.generate(request(stream=True))
    assert caught.value.code == "rate_limited" and caught.value.retry_after == 17


@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_invalid_unicode_is_rejected_before_output(tmp_path, api_format, stream):
    def handler(_):
        text = "invalid\ud800"
        if stream:
            chunk = (
                {"type": "response.output_text.delta", "delta": text}
                if api_format == "responses"
                else {"choices": [{"delta": {"content": text}}]}
            )
            return httpx.Response(200, text=frame(chunk))
        data = (
            response_result(
                output=[{"type": "message", "content": [{"type": "output_text", "text": text}]}]
            )
            if api_format == "responses"
            else {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
        )
        return httpx.Response(
            200, content=json.dumps(data).encode(), headers={"Content-Type": "application/json"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "key", provider="openai_compatible")
        adapter = OpenAICompatibleAdapter(client, auth, backend(api_format=api_format))
        events = []
        with pytest.raises(GatewayError) as caught:
            async for event in adapter.stream(request(stream=stream)):
                events.append(event)
    assert caught.value.status == 502 and events == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["stop", "content_filter"])
async def test_chat_refusals_reach_gateway_clients(tmp_path, stream, reason):
    refusal = "Cannot help with that request."

    def handler(upstream):
        assert json.loads(upstream.content)["stream"] is stream
        usage = {"prompt_tokens": 2, "completion_tokens": 1}
        if not stream:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": None, "refusal": refusal}, "finish_reason": reason}
                    ],
                    "usage": usage,
                },
            )
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "refusal": None}}]},
            {"choices": [{"delta": {"refusal": "Cannot help "}}]},
            {"choices": [{"delta": {"refusal": "with that request."}}]},
            {"choices": [{"delta": {}, "finish_reason": reason}]},
            {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage},
            "[DONE]",
        ]
        return httpx.Response(200, text="".join(frame(chunk) for chunk in chunks))

    async with httpx.AsyncClient() as client:
        await AuthManager(CredentialStore(tmp_path), client).save_api_key(
            "custom", "key", provider="openai_compatible"
        )
    keys = GatewayKeys(client="gateway-key-for-tests-123456")
    config = GatewayConfig(backends={"custom": backend()}, routes={"custom-default": ["custom"]})
    app = create_app(config, state_dir=tmp_path, keys=keys, transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://gateway"
        ) as client:
            body = request(
                stream=stream, stream_options={"include_usage": True} if stream else None
            )
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + keys.client},
                json=body.model_dump(exclude_none=True),
            )
    assert response.status_code == 200
    if stream:
        assert response.text.endswith("data: [DONE]\n\n")
        chunks = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        choices = [chunk["choices"][0] for chunk in chunks if chunk["choices"]]
        assert "".join(choice["delta"].get("content", "") for choice in choices) == refusal
        assert [choice["finish_reason"] for choice in choices if choice["finish_reason"]] == [
            "content_filter"
        ]
        assert chunks[-1]["usage"]["total_tokens"] == 3
    else:
        choice = response.json()["choices"][0]
        assert choice["message"]["content"] == refusal
        assert choice["finish_reason"] == "content_filter"
        assert response.json()["usage"]["total_tokens"] == 3


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("refusal", [True, 1, ["invalid"], {"text": "invalid"}])
async def test_chat_refusal_must_be_text(tmp_path, stream, refusal):
    def handler(_):
        message = {"content": None, "refusal": refusal}
        if stream:
            chunk = {"choices": [{"delta": message, "finish_reason": "stop"}]}
            return httpx.Response(200, text=frame(chunk) + frame("[DONE]"))
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": "stop"}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "key", provider="openai_compatible")
        adapter = OpenAICompatibleAdapter(client, auth, backend())
        with pytest.raises(GatewayError) as caught:
            await adapter.generate(request(stream=stream))
    assert caught.value.status == 502


async def test_auth_is_explicit_and_logout_disables_keyless_api(tmp_path):
    async with httpx.AsyncClient() as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        with pytest.raises(GatewayError):
            await auth.save_api_key("custom", "", provider="openai_compatible")
        await auth.save_api_key("custom", "", provider="openai_compatible", no_auth=True)
        adapter = OpenAICompatibleAdapter(client, auth, backend())
        assert adapter._headers() == {}
        await auth.logout("custom")
        with pytest.raises(GatewayError):
            adapter._headers()


def test_capabilities_reject_disabled_features_before_network():
    adapter = OpenAICompatibleAdapter(None, None, backend(api_streaming=False))
    with pytest.raises(GatewayError, match="stream"):
        adapter._payload(request(stream=True))
    with pytest.raises(GatewayError, match="response_format"):
        adapter._payload(request(response_format={"type": "json_object"}))


def test_cli_keyless_option():
    args = parser().parse_args(
        ["api-key", "custom", "--provider", "openai_compatible", "--no-auth"]
    )
    assert args.provider == "openai_compatible" and args.no_auth


@pytest.mark.parametrize("include_usage", [False, True])
@pytest.mark.parametrize("reason", ["stop", "length"])
async def test_openrouter_usage_frame_can_repeat_the_terminal_choice(
    tmp_path, include_usage, reason
):
    chunks = [
        {
            "choices": [
                {"index": 0, "delta": {"content": "Переверните чашку."}, "finish_reason": None}
            ]
        },
        {"choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": reason}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "", "role": "assistant"},
                    "finish_reason": reason,
                    "native_finish_reason": reason,
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 6},
        },
        "[DONE]",
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="".join(frame(chunk) for chunk in chunks))
        )
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "", provider="openai_compatible", no_auth=True)
        adapter = OpenAICompatibleAdapter(client, auth, backend(api_include_usage=include_usage))
        events = [event async for event in adapter.stream(request(stream=True))]
    assert "".join(event.text for event in events) == "Переверните чашку."
    assert [event.kind for event in events] == ["delta", "done"]
    assert events[-1].finish_reason == reason
    assert events[-1].usage.as_dict()["total_tokens"] == 26


@pytest.mark.parametrize(
    "last_choice",
    [
        {"delta": {"content": "extra"}, "finish_reason": "stop"},
        {"delta": {}, "finish_reason": "length"},
        {"delta": {"tool_calls": [{"id": "unexpected"}]}, "finish_reason": "stop"},
    ],
)
async def test_usage_does_not_hide_new_content_or_conflicting_completion(tmp_path, last_choice):
    chunks = [
        {"choices": [{"delta": {"content": "Partial"}, "finish_reason": "stop"}]},
        {"choices": [last_choice], "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
        "[DONE]",
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="".join(frame(chunk) for chunk in chunks))
        )
    ) as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_api_key("custom", "", provider="openai_compatible", no_auth=True)
        adapter = OpenAICompatibleAdapter(client, auth, backend())
        with pytest.raises(GatewayError):
            await adapter.generate(request(stream=True))
