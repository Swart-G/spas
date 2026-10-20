import ast
import asyncio
import base64
import hashlib
import json
import threading
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import requests
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.google import COLAB_SCOPE, GoogleAuth, GoogleOAuth
from llm.gateway.auth.manager import AuthManager, account_key
from llm.gateway.colab.cli_api import ColabCLIAPI
from llm.gateway.colab.manager import ColabManager, DeploymentSettings
from llm.gateway.colab.tui import google_action
from llm.gateway.core.errors import GatewayError
from llm.gateway.terminal import Back


@pytest.mark.parametrize("cancel", [False, True])
async def test_google_tui_uses_real_async_prompt(tmp_path, monkeypatch, capsys, cancel):
    exchange = AsyncMock(
        return_value={
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "expires_in": 3600,
            "scope": "openid email " + COLAB_SCOPE,
            "id_token": "signed-token",
        }
    )
    monkeypatch.setattr(GoogleOAuth, "token_request", exchange)
    monkeypatch.setattr(GoogleOAuth, "validate_identity", AsyncMock(return_value={"sub": "123"}))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b" if cancel else "private-code\r")
        if cancel:
            with pytest.raises(Back):
                await google_action(tmp_path, "login_cli", "google-main")
            exchange.assert_not_awaited()
        else:
            await google_action(tmp_path, "login_cli", "google-main")
            assert exchange.await_args.args[0]["code"] == "private-code"
    record = CredentialStore(tmp_path).get(account_key("google-main"))
    assert record["state"] == ("REAUTH_REQUIRED" if cancel else "CONNECTED")
    assert "private-code" not in capsys.readouterr().out


async def test_bundled_application_login_pkce_and_encrypted_tokens(tmp_path):
    store = CredentialStore(tmp_path)
    urls, exchanges = [], []

    def exchange(request):
        fields = parse_qs(request.content.decode())
        exchanges.append(fields)
        assert request.url.host == "oauth2.googleapis.com"
        assert fields["redirect_uri"] == [
            "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
        ]
        assert fields["code"] == ["private-code"]
        return httpx.Response(
            200,
            json={
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_in": 3600,
                "scope": "openid email " + COLAB_SCOPE,
                "id_token": "signed-google-token",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as client:
        google = GoogleAuth(store, client)
        await google.configure_cli("google-main")
        google.oauth.validate_identity = AsyncMock(return_value={"sub": "123", "email": "a@b.test"})
        await google.login("google-main", urls.append, read_code=lambda: "private-code")
        query = parse_qs(urlsplit(urls[0]).query)
        assert query["token_usage"] == ["remote"]
        assert "https://www.googleapis.com/auth/userinfo.email" in query["scope"][0]
        verifier = exchanges[0]["code_verifier"][0]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert query["code_challenge"] == [challenge]
        google.oauth.validate_identity.assert_awaited_once_with(
            "signed-google-token", query["client_id"][0], query["nonce"][0]
        )
        assert await google.access_token("google-main") == "private-access"
        record = google.record("google-main")
        assert record["auth_transport"] == "cli" and record["state"] == "CONNECTED"
        assert "private-refresh" not in json.dumps(AuthManager.public_account(record))
        assert all(
            b"private-refresh" not in path.read_bytes() for path in store.records.glob("*.enc")
        )


class CLIService:
    """Drive the real CLI Client's assignment and XSRF parsers with fake HTTP."""

    def __init__(self):
        self.assignment = None
        self.requests = []
        self.fail_after_assign = False
        self.rate_limit = False

    def request(self, session, method, url, **kwargs):
        assert session.headers["Authorization"] == "Bearer google-access"
        assert kwargs["timeout"] == 30 and kwargs["allow_redirects"] is False
        parsed = urlsplit(url)
        assert parsed.hostname == "colab.research.google.com"
        query = parse_qs(parsed.query)
        self.requests.append((method, parsed.path, query))
        status, data = 200, {}
        if self.rate_limit:
            status = 429
        elif parsed.path.endswith("/assignments"):
            data = {"assignments": [self.assignment] if self.assignment else []}
        elif parsed.path.endswith("/assign"):
            assert query["variant"] == ["GPU"] and query["accelerator"] == ["T4"]
            if method == "GET":
                data = self.assignment or {
                    "acc": "T4",
                    "nbh": query["nbh"][0],
                    "token": "xsrf",
                    "variant": "GPU",
                }
            else:
                self.assignment = {
                    "endpoint": "endpoint-1",
                    "accelerator": "T4",
                    "variant": 1,
                    "machineShape": 0,
                    "runtimeProxyInfo": {
                        "url": "https://runtime.test/colab",
                        "token": "proxy-secret",
                        "tokenExpiresInSeconds": 600,
                    },
                }
                data = self.assignment
                if self.fail_after_assign:
                    status = 503
                    self.fail_after_assign = False
        elif "/unassign/" in parsed.path:
            if method == "GET":
                data = {"token": "xsrf"}
            else:
                self.assignment = None
        else:
            raise AssertionError(parsed.path)
        response = requests.Response()
        response.status_code = status
        response._content = (")]}'\n" + json.dumps(data)).encode()
        response.request = requests.Request(method, url).prepare()
        response.headers["Retry-After"] = "7200"
        return response


class Bridge:
    def __init__(self):
        self.codes = []

    def factory(self, client, connection):
        assert connection == {"url": "https://runtime.test/colab", "token": "proxy-secret"}
        return self

    async def execute(self, code, **kwargs):
        self.codes.append(code)
        encoded = ast.parse(code).body[1].value.args[0]
        payload = json.loads(base64.b64decode(ast.literal_eval(encoded.args[0])))
        if "server_source" in payload:
            yield {"stage": "READY", "snapshot_commit": "commit"}
        elif "payload" in payload:
            yield {
                "type": "response",
                "data": {
                    "choices": [
                        {"index": 0, "message": {"content": "СПАС"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
                },
            }
        elif "port" in payload:
            yield {"type": "models", "data": {"data": [{"id": "team/tiny"}]}}
        else:
            yield {"stage": "STOPPED"}


@pytest.fixture
async def cli_deployment(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path)
    store.put(
        account_key("google-main"),
        {
            "kind": "account",
            "provider": "google_colab",
            "account": "google-main",
            "auth_transport": "cli",
            "state": "CONNECTED",
            "scopes": [COLAB_SCOPE],
            "access_token": "google-access",
            "expires_at": time.time() + 3600,
        },
    )
    service, bridge = CLIService(), Bridge()
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda session, method, url, **kwargs: service.request(session, method, url, **kwargs),
    )
    async with httpx.AsyncClient() as client:
        manager = ColabManager(store, client, bridge_factory=bridge.factory)
        settings = DeploymentSettings(
            google_account="google-main", model_repo="team/tiny", accelerator="T4"
        )
        await manager.configure("colab-main", settings)
        yield manager, service, bridge


async def test_cli_lifecycle_reuses_assignment_and_deletes_it(cli_deployment):
    manager, service, bridge = cli_deployment
    specs = await manager.api.specs("google-main")
    assert specs and all(spec["availability"] == "on_start" for spec in specs)
    assert not service.assignment
    await manager.start("colab-main")
    assert manager.record("colab-main")["stage"] == "READY"
    await manager.start("colab-main")
    posts = [item for item in service.requests if item[:2] == ("POST", "/tun/m/assign")]
    assert len(posts) == 1
    await manager.stop("colab-main")
    assert service.assignment and manager.record("colab-main")["stage"] == "STOPPED"
    await manager.delete("colab-main")
    assert not service.assignment and manager.record("colab-main")["stage"] == "DELETED"


async def test_cli_lost_create_response_recovers_without_second_assignment(cli_deployment):
    manager, service, bridge = cli_deployment
    service.fail_after_assign = True
    with pytest.raises(GatewayError):
        await manager.start("colab-main")
    stable_id = manager.record("colab-main")["runtime_id"]
    await manager.start("colab-main")
    assert manager.record("colab-main")["runtime_id"] == stable_id
    assert len([item for item in service.requests if item[:2] == ("POST", "/tun/m/assign")]) == 1


async def test_cli_expired_runtime_status_never_allocates(cli_deployment):
    manager, service, bridge = cli_deployment
    await manager.start("colab-main")
    service.assignment = None
    count = len(service.requests)
    status = await manager.status("colab-main")
    assert not status["available"] and status["stage"] == "EXPIRED"
    assert all(method == "GET" for method, _, _ in service.requests[count:])


async def test_cli_rate_limit_blocks_following_control_requests(cli_deployment):
    manager, service, bridge = cli_deployment
    service.rate_limit = True
    with pytest.raises(GatewayError) as caught:
        await manager.api.specs("google-main")
    assert caught.value.code == "rate_limited" and caught.value.retry_after == 7200
    count = len(service.requests)
    with pytest.raises(GatewayError):
        await manager.api.specs("google-main")
    assert len(service.requests) == count


async def test_cli_cancel_waits_for_pending_control_operation(cli_deployment):
    manager, service, bridge = cli_deployment
    started, finish = threading.Event(), threading.Event()

    def allocate(client):
        started.set()
        assert finish.wait(3)

    api = ColabCLIAPI(manager.google, manager.client)
    task = asyncio.create_task(api.call("google-main", allocate))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
