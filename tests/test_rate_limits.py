import asyncio
import time

import httpx
import pytest

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.manager import AuthManager, account_key
from llm.gateway.auth.oauth import DIRECT_SCOPE
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.audit import RequestLog
from llm.gateway.core.errors import GatewayError
from llm.gateway.core.models import Capabilities, ChatRequest, StreamEvent
from llm.gateway.core.registry import Registry
from llm.gateway.core.router import Router
from llm.gateway.providers.claude_subscription import ClaudeEventParser


@pytest.mark.parametrize("field", ["resetsAt", "resets_at"])
@pytest.mark.parametrize("reset_at, expected", [(4600, 3600), (900, 0), (0, 0)])
def test_claude_reset_timestamp(monkeypatch, field, reset_at, expected):
    monkeypatch.setattr("llm.gateway.providers.claude_subscription.time.time", lambda: 1000)
    with pytest.raises(GatewayError) as caught:
        ClaudeEventParser().parse(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", field: reset_at}}
        )
    assert caught.value.code == "rate_limited"
    assert caught.value.status == 429
    assert caught.value.retry_after == expected


class QueuedSemaphore(asyncio.Semaphore):
    def __init__(self):
        super().__init__(1)
        self.queued = asyncio.Event()
        self.waiters = 0

    async def acquire(self):
        if self.locked():
            self.waiters += 1
            if self.waiters == 2:
                self.queued.set()
        return await super().acquire()


class DelayedAuth(AuthManager):
    def __init__(self, store, client):
        super().__init__(store, client)
        self.recording = asyncio.Event()
        self.allow_record = asyncio.Event()
        self.results = 0

    async def note_result(self, account, error=None):
        if error is not None and error.code == "rate_limited":
            self.results += 1
            if self.results == 1:
                self.recording.set()
                await self.allow_record.wait()
        await super().note_result(account, error)


class FakeProvider:
    capabilities = Capabilities()

    def __init__(self, *, rate_limited=False):
        self.rate_limited = rate_limited
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request):
        self.calls += 1
        if self.rate_limited:
            self.started.set()
            await self.release.wait()
            raise GatewayError("Quota exhausted", code="rate_limited", status=429, retry_after=3600)
        yield StreamEvent("delta", text="fallback")
        yield StreamEvent("done")


@pytest.mark.parametrize("fallback", [False, True])
async def test_queued_requests_respect_new_cooldown(tmp_path, fallback):
    primary = FakeProvider(rate_limited=True)
    backup = FakeProvider()
    store = CredentialStore(tmp_path)
    store.put(
        account_key("primary"),
        {
            "kind": "account",
            "account": "primary",
            "provider": "claude_subscription",
            "state": "CONNECTED",
        },
    )
    config = GatewayConfig(
        backends={
            name: BackendConfig(provider="claude_subscription", account=name, model="sonnet")
            for name in ("primary", "backup")
        },
        routes={"model": ["primary", "backup"] if fallback else ["primary"]},
    )
    registry = Registry(config, {"primary": primary, "backup": backup})
    slot = QueuedSemaphore()
    registry.backends["primary"].semaphore = slot
    audit = RequestLog(tmp_path)
    tasks = []
    async with httpx.AsyncClient() as client:
        auth = DelayedAuth(store, client)
        router = Router(registry, auth, audit)
        request = ChatRequest(model="model", messages=[{"role": "user", "content": "hello"}])
        try:
            async with asyncio.timeout(5):
                tasks.append(asyncio.create_task(router.generate(request, "first")))
                await primary.started.wait()
                tasks.extend(
                    asyncio.create_task(router.generate(request, str(index))) for index in range(2)
                )
                await slot.queued.wait()
                primary.release.set()
                await auth.recording.wait()
                # Writing account state must finish while the first request owns the slot.
                assert slot.locked()
                assert primary.calls == 1
                auth.allow_record.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
            assert primary.calls == 1
            assert store.get(account_key("primary"))["retry_at"] > time.time() + 3500
            if fallback:
                assert backup.calls == 3
                assert all(result.content == "fallback" for result in results)
            else:
                assert backup.calls == 0
                assert all(isinstance(result, GatewayError) for result in results)
                assert all(result.code == "rate_limited" for result in results)
                assert all(result.retry_after > 3500 for result in results)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            audit.close()


@pytest.mark.parametrize("remaining", [3600, 0, -10])
async def test_success_preserves_only_active_cooldown(tmp_path, remaining):
    store = CredentialStore(tmp_path)
    now = time.time()
    retry_at = now + remaining
    store.put(
        account_key("shared"),
        {
            "kind": "account",
            "account": "shared",
            "provider": "claude_subscription",
            "state": "RATE_LIMITED",
            "retry_at": retry_at,
        },
    )
    async with httpx.AsyncClient() as client:
        auth = AuthManager(store, client)
        await auth.note_result("shared")
    saved = store.get(account_key("shared"))
    assert saved["last_success_at"] >= now
    if remaining > 0:
        assert saved["state"] == "RATE_LIMITED"
        assert saved["retry_at"] == retry_at
        with pytest.raises(GatewayError):
            auth.check_cooldown("shared")
    else:
        assert saved["state"] == "CONNECTED"
        assert "retry_at" not in saved


@pytest.mark.parametrize("shared_backend", [True, False])
async def test_concurrent_success_does_not_erase_rate_limit(tmp_path, shared_backend):
    store = CredentialStore(tmp_path)
    store.put(
        account_key("shared"),
        {
            "kind": "account",
            "account": "shared",
            "provider": "claude_subscription",
            "state": "CONNECTED",
        },
    )
    limited = FakeProvider(rate_limited=True)

    class ConcurrentProvider(FakeProvider):
        async def stream(self, request):
            if request.messages[-1].content == "limit":
                async for event in limited.stream(request):
                    yield event
            else:
                self.calls += 1
                self.started.set()
                await self.release.wait()
                yield StreamEvent("delta", text="success")
                yield StreamEvent("done")

    success = ConcurrentProvider()
    config = GatewayConfig(
        backends={
            name: BackendConfig(
                provider="claude_subscription",
                account="shared",
                model="sonnet",
                max_concurrency=2,
            )
            for name in ("one", "two")
        },
        routes={"limit": ["one"], "success": ["one" if shared_backend else "two"]},
    )
    registry = Registry(config, {"one": success, "two": success})
    audit = RequestLog(tmp_path)
    tasks = []
    async with httpx.AsyncClient() as client:
        # Independent managers share the same account vault, as CLI probes do.
        rate_router = Router(registry, AuthManager(store, client), audit)
        success_router = Router(registry, AuthManager(CredentialStore(tmp_path), client), audit)
        try:
            async with asyncio.timeout(5):
                tasks.append(
                    asyncio.create_task(
                        rate_router.generate(
                            ChatRequest(
                                model="limit",
                                messages=[{"role": "user", "content": "limit"}],
                            ),
                            "limited",
                        )
                    )
                )
                tasks.append(
                    asyncio.create_task(
                        success_router.generate(
                            ChatRequest(
                                model="success",
                                messages=[{"role": "user", "content": "success"}],
                            ),
                            "success",
                        )
                    )
                )
                await limited.started.wait()
                await success.started.wait()
                limited.release.set()
                with pytest.raises(GatewayError):
                    await tasks[0]
                retry_at = store.get(account_key("shared"))["retry_at"]
                success.release.set()
                assert (await tasks[1]).content == "success"
                saved = store.get(account_key("shared"))
                assert saved["state"] == "RATE_LIMITED"
                assert saved["retry_at"] == retry_at
                assert saved["last_success_at"]
                with pytest.raises(GatewayError) as caught:
                    await success_router.generate(
                        ChatRequest(
                            model="success",
                            messages=[{"role": "user", "content": "success"}],
                        ),
                        "next",
                    )
                assert caught.value.code == "rate_limited"
                assert success.calls == 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            audit.close()


async def test_later_rate_limit_does_not_shorten_cooldown(tmp_path):
    store = CredentialStore(tmp_path)
    retry_at = time.time() + 3600
    store.put(
        account_key("shared"),
        {
            "kind": "account",
            "account": "shared",
            "provider": "claude_subscription",
            "state": "RATE_LIMITED",
            "retry_at": retry_at,
        },
    )
    async with httpx.AsyncClient() as client:
        auth = AuthManager(store, client)
        await auth.note_result("shared", GatewayError("Limit", code="rate_limited", retry_after=60))
    assert store.get(account_key("shared"))["retry_at"] == retry_at


@pytest.mark.parametrize("remaining", [3600, -1])
async def test_chatgpt_refresh_preserves_active_cooldown(tmp_path, remaining):
    store = CredentialStore(tmp_path)
    retry_at = time.time() + remaining
    store.put(
        account_key("chatgpt"),
        {
            "kind": "account",
            "account": "chatgpt",
            "provider": "codex_subscription",
            "client_id": "test-client",
            "scopes": [DIRECT_SCOPE],
            "access_token": "old",
            "refresh_token": "refresh",
            "expires_at": 0,
            "state": "RATE_LIMITED",
            "retry_at": retry_at,
        },
    )
    calls = []

    def refresh(request):
        calls.append(request)
        return httpx.Response(
            200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(refresh)) as client:
        auth = AuthManager(store, client)
        assert await auth.access_token("chatgpt") == "new"
        assert await auth.access_token("chatgpt") == "new"
        saved = store.get(account_key("chatgpt"))
        assert saved["refresh_token"] == "rotated"
        if remaining > 0:
            assert saved["state"] == "RATE_LIMITED" and saved["retry_at"] == retry_at
            with pytest.raises(GatewayError) as caught:
                auth.check_cooldown("chatgpt")
            assert caught.value.code == "rate_limited"
        else:
            assert saved["state"] == "CONNECTED" and "retry_at" not in saved
            auth.check_cooldown("chatgpt")
    assert len(calls) == 1


async def test_explicit_chatgpt_login_clears_cooldown(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path)
    store.put(
        account_key("chatgpt"),
        {
            "kind": "account",
            "account": "chatgpt",
            "provider": "codex_subscription",
            "state": "RATE_LIMITED",
            "retry_at": time.time() + 3600,
        },
    )
    async with httpx.AsyncClient() as client:
        auth = AuthManager(store, client)

        async def authorize(*args, **kwargs):
            return "code", "client"

        async def tokens(*args):
            return {"access_token": "new", "expires_in": 3600, "scope": DIRECT_SCOPE}

        async def identity(*args):
            return {"sub": "subject"}

        monkeypatch.setattr(auth.oauth, "authorize", authorize)
        monkeypatch.setattr(auth.oauth, "token_request", tokens)
        monkeypatch.setattr(auth.oauth, "validate_identity", identity)
        public = await auth.login_chatgpt("chatgpt", lambda _: None)
        assert public["state"] == "CONNECTED" and public["retry_at"] is None
        auth.check_cooldown("chatgpt")
