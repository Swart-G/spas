import json

import httpx
import pytest
from pydantic import ValidationError

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.manager import AuthManager, account_key
from llm.gateway.cli import save_config
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.audit import RequestLog
from llm.gateway.core.errors import GatewayError
from llm.gateway.core.models import ChatRequest
from llm.gateway.core.router import Router
from llm.gateway.main import GatewayKeys, create_app, make_registry
from llm.gateway.providers.remote_inference import RemoteInferenceAdapter


def remote(**options):
    return BackendConfig(
        provider="remote_inference",
        account="colab-main",
        model="tiny-model",
        base_url="https://runtime.test",
        **options,
    )


def request(stream=False, **options):
    return ChatRequest(
        model="colab-default",
        messages=[{"role": "user", "content": "hello"}],
        stream=stream,
        **options,
    )


def frame(data):
    return "data: " + (data if isinstance(data, str) else json.dumps(data)) + "\n\n"


def completion():
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://runtime.test", "https://runtime.test/v1"),
        ("https://runtime.test/v1/", "https://runtime.test/v1"),
        ("http://127.0.0.1:8000/proxy", "http://127.0.0.1:8000/proxy/v1"),
    ],
)
def test_endpoint_normalization(url, expected):
    assert remote().model_copy(update={}).provider == "remote_inference"
    backend = BackendConfig(
        provider="remote_inference", account="colab-main", model="tiny", base_url=url
    )
    assert backend.base_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://runtime.test",
        "https://key@runtime.test",
        "https://runtime.test?token=secret",
        "https://runtime.test/#secret",
        "https://runtime.test:bad",
        "https://bad host",
    ],
)
def test_invalid_endpoint(url):
    with pytest.raises(ValidationError):
        BackendConfig(provider="remote_inference", account="colab-main", model="tiny", base_url=url)


@pytest.mark.parametrize("key", ["secret-key", ""])
async def test_colab_full_gateway_request(tmp_path, key):
    seen = []

    def handler(upstream):
        seen.append(upstream)
        assert str(upstream.url) == "https://runtime.test/v1/chat/completions"
        assert upstream.headers.get("authorization", "") == ("Bearer " + key if key else "")
        payload = json.loads(upstream.content)
        assert payload["model"] == "tiny-model"
        assert payload["messages"][-1]["content"] == "hello"
        assert payload["max_tokens"] == 50
        assert "max_completion_tokens" not in payload
        return httpx.Response(200, json=completion())

    config = GatewayConfig(backends={"colab": remote()}, routes={"colab-default": ["colab"]})
    async with httpx.AsyncClient() as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_remote_key("colab-main", key)
    app = create_app(
        config,
        state_dir=tmp_path,
        keys=GatewayKeys("c" * 32, "a" * 32),
        transport=httpx.MockTransport(handler),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + "c" * 32},
                json=request(max_completion_tokens=50).model_dump(exclude_none=True),
            )
            assert response.status_code == 200
            assert response.json()["choices"][0]["message"]["content"] == "hello"
            assert response.json()["usage"]["total_tokens"] == 4
            public = await client.get(
                "/admin/accounts", headers={"Authorization": "Bearer " + "a" * 32}
            )
            assert "api_key" not in public.text
            assert public.json()["accounts"][0]["state"] == "CONNECTED"
    assert len(seen) == 1
    encrypted = list((tmp_path / "credentials").glob("*.enc"))
    assert encrypted
    assert key == "" or key.encode() not in b"".join(path.read_bytes() for path in encrypted)


async def adapter_for(tmp_path, handler, **options):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    auth = AuthManager(CredentialStore(tmp_path), client)
    await auth.save_remote_key("colab-main", "secret-key")
    return client, auth, RemoteInferenceAdapter(client, auth, remote(**options))


async def test_colab_streaming_usage(tmp_path):
    def handler(upstream):
        assert json.loads(upstream.content)["stream_options"] == {"include_usage": True}
        content = frame(
            {"choices": [{"index": 0, "delta": {"content": "hel"}, "finish_reason": None}]}
        )
        content += frame(
            {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]}
        )
        content += frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
        content += frame({"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
        content += frame("[DONE]")
        return httpx.Response(200, text=content)

    client, auth, adapter = await adapter_for(tmp_path, handler, remote_include_usage=True)
    async with client:
        events = [event async for event in adapter.stream(request(True))]
    assert "".join(event.text for event in events) == "hello"
    assert events[-1].finish_reason == "length"
    assert events[-1].usage.as_dict()["total_tokens"] == 5


@pytest.mark.parametrize(
    "content",
    [
        frame({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}),
        frame({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        frame("[DONE]"),
        "data: invalid\n\n",
    ],
)
async def test_colab_rejects_incomplete_stream(tmp_path, content):
    client, auth, adapter = await adapter_for(tmp_path, lambda _: httpx.Response(200, text=content))
    async with client:
        with pytest.raises(GatewayError):
            _ = [event async for event in adapter.stream(request(True))]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "reauth_required"),
        (429, "rate_limited"),
        (503, "provider_unavailable"),
        (302, "provider_unavailable"),
    ],
)
async def test_colab_http_errors_and_no_redirect(tmp_path, status, code):
    calls = []

    def handler(upstream):
        calls.append(upstream)
        return httpx.Response(
            status,
            headers={"Retry-After": "7200", "Location": "https://other.test"},
            json={"error": {"message": "private upstream error"}},
        )

    client, auth, adapter = await adapter_for(tmp_path, handler)
    async with client:
        with pytest.raises(GatewayError) as caught:
            await adapter.generate(request())
    assert caught.value.code == code
    assert "private" not in caught.value.message
    assert len(calls) == 1
    if status == 429:
        assert caught.value.retry_after == 7200


async def test_colab_status_and_unavailable_runtime(tmp_path):
    available = True

    def handler(upstream):
        if not available:
            raise httpx.ConnectError("runtime stopped", request=upstream)
        return httpx.Response(200, json={"data": [{"id": "tiny-model"}]})

    client, auth, adapter = await adapter_for(tmp_path, handler)
    async with client:
        health = await adapter.health()
        assert health["available"] is True and health["model_available"] is True
        available = False
        health = await adapter.health()
        assert health["available"] is False and health["state"] == "DISCONNECTED"
        assert auth.accounts()[0]["state"] == "DISCONNECTED"


async def test_remote_credentials_preserve_cooldown_and_roundtrip(tmp_path):
    async with httpx.AsyncClient() as client:
        auth = AuthManager(CredentialStore(tmp_path), client)
        await auth.save_remote_key("colab-main", "secret-key")
        await auth.note_result(
            "colab-main", GatewayError("Limit", code="rate_limited", retry_after=3600)
        )
        before = auth.store.get(account_key("colab-main"))
        await auth.save_remote_key("colab-main", "secret-key")
        assert auth.store.get(account_key("colab-main")) == before
    config = GatewayConfig(backends={"colab": remote()}, routes={"colab-default": ["colab"]})
    path = tmp_path / "gateway.toml"
    save_config(config, path)
    assert GatewayConfig.load(path) == config
    assert "secret-key" not in path.read_text()


async def test_colab_explicit_fallback(tmp_path):
    calls = []

    def handler(upstream):
        calls.append(upstream.url.host)
        if upstream.url.host == "runtime.test":
            return httpx.Response(429, headers={"Retry-After": "3600"})
        return httpx.Response(200, json=completion())

    client, auth, adapter = await adapter_for(tmp_path, handler)
    config = GatewayConfig(
        backends={
            "colab": remote(),
            "backup": remote().model_copy(
                update={
                    "account": "backup-main",
                    "base_url": "https://backup.test/v1",
                }
            ),
        },
        routes={"colab-default": ["colab", "backup"]},
    )
    audit = RequestLog(tmp_path)
    async with client:
        try:
            await auth.save_remote_key("backup-main", "")
            router = Router(make_registry(config, auth, client), auth, audit)
            assert (await router.generate(request(), "one")).content == "hello"
            assert (await router.generate(request(), "two")).content == "hello"
        finally:
            audit.close()
    assert calls == ["runtime.test", "backup.test", "backup.test"]
