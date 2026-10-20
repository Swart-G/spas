import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.google import COLAB_SCOPE, GoogleAttempt, GoogleAuth, GoogleOAuth
from llm.gateway.auth.manager import AuthManager, account_key
from llm.gateway.core.errors import GatewayError

CLIENT_ID = "123456-spas.apps.googleusercontent.com"


@pytest.fixture
def signing_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="google-key", use="sig", alg="RS256")

    def sign(**overrides):
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "google-subject",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "email": "owner@example.test",
            **overrides,
        }
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "google-key"})

    return jwk, sign


def connected_google(store, *, expires_at=0):
    store.put(
        account_key("google-main"),
        {
            "kind": "account",
            "provider": "google_colab",
            "account": "google-main",
            "client_id": CLIENT_ID,
            "client_secret": "desktop-secret",
            "subject": "google-subject",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "scopes": [COLAB_SCOPE, "openid"],
            "expires_at": expires_at,
            "state": "CONNECTED",
        },
    )


def test_google_authorization_uses_pkce_and_only_google_scopes():
    attempt = GoogleAttempt(CLIENT_ID, "")
    attempt.redirect_uri = "http://127.0.0.1:3456/auth/callback"
    url = urlsplit(attempt.authorization_url("owner@example.test", consent=True))
    params = parse_qs(url.query)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest())
    assert (url.scheme, url.hostname, url.path) == (
        "https",
        "accounts.google.com",
        "/o/oauth2/v2/auth",
    )
    assert params["code_challenge"] == [challenge.decode().rstrip("=")]
    assert params["code_challenge_method"] == ["S256"]
    assert params["nonce"] == [attempt.nonce]
    assert params["state"] == [attempt.state]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert COLAB_SCOPE in params["scope"][0].split()
    assert "resource" not in params and "ext_agent_host_id" not in params
    with pytest.raises(GatewayError):
        attempt.validate_callback({"code": "code", "state": "wrong"})


async def test_google_login_verifies_identity_and_encrypts_secrets(
    tmp_path, monkeypatch, signing_key
):
    jwk, sign = signing_key
    attempt_seen = []
    requests = []

    def response(request):
        requests.append(request)
        if request.url.path == "/oauth2/v3/certs":
            return httpx.Response(200, json={"keys": [jwk]})
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        parameters = parse_qs(request.content.decode())
        attempt = attempt_seen[0]
        assert parameters["grant_type"] == ["authorization_code"]
        assert parameters["client_secret"] == ["desktop-secret"]
        assert parameters["code_verifier"] == [attempt.verifier]
        assert "resource" not in parameters
        return httpx.Response(
            200,
            json={
                "access_token": "google-access",
                "refresh_token": "google-refresh",
                "expires_in": 3600,
                "scope": "openid " + COLAB_SCOPE,
                "id_token": sign(nonce=attempt.nonce),
                "token_type": "Bearer",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        store = CredentialStore(tmp_path)
        auth = GoogleAuth(store, client)
        await auth.configure("google-main", CLIENT_ID, "desktop-secret")

        async def authorize(attempt, announce, **options):
            attempt.redirect_uri = "http://127.0.0.1:3456/auth/callback"
            attempt_seen.append(attempt)
            assert options["consent"] is True
            return "code", attempt.client_id

        monkeypatch.setattr(auth.oauth, "authorize", authorize)
        await auth.login("google-main", lambda url: None)
        assert await auth.access_token("google-main") == "google-access"
        saved = auth.record("google-main")
        assert saved["subject"] == "google-subject"
        assert saved["email"] == "owner@example.test"
        public = AuthManager(store, client).accounts()
        assert all(
            secret not in json.dumps(public)
            for secret in ("google-access", "google-refresh", "desktop-secret", CLIENT_ID)
        )
        assert all(
            b"google-refresh" not in path.read_bytes() for path in store.records.glob("*.enc")
        )
        assert len(requests) == 2


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://attacker.test"},
        {"aud": "another-client"},
        {"nonce": "wrong"},
        {"exp": int(time.time()) - 60},
    ],
)
async def test_google_rejects_untrusted_identity(signing_key, claims):
    jwk, sign = signing_key
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": [jwk]}))
    ) as client:
        auth = GoogleOAuth(client)
        with pytest.raises(GatewayError) as error:
            await auth.validate_identity(
                sign(nonce="expected", **claims) if "nonce" not in claims else sign(**claims),
                CLIENT_ID,
                "expected",
            )
        assert error.value.code == "invalid_identity"


async def test_concurrent_google_refresh_rotates_once(tmp_path):
    store = CredentialStore(tmp_path)
    connected_google(store)
    calls = []

    async def response(request):
        calls.append(parse_qs(request.content.decode()))
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        auth = GoogleAuth(store, client)
        assert await asyncio.gather(*(auth.access_token("google-main") for _ in range(3))) == [
            "new-access",
            "new-access",
            "new-access",
        ]
        assert len(calls) == 1
        assert calls[0]["refresh_token"] == ["old-refresh"]
        saved = auth.record("google-main")
        assert saved["refresh_token"] == "rotated-refresh"
        assert COLAB_SCOPE in saved["scopes"]


@pytest.mark.parametrize(
    "status, code, cleared",
    [
        (400, "invalid_grant", True),
        (503, "temporarily_unavailable", False),
    ],
)
async def test_google_refresh_distinguishes_revocation_from_network(
    tmp_path, status, code, cleared
):
    store = CredentialStore(tmp_path)
    connected_google(store)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"error": code, "secret": "do-not-print"})
        )
    ) as client:
        auth = GoogleAuth(store, client)
        with pytest.raises(GatewayError) as error:
            await auth.access_token("google-main")
        assert "do-not-print" not in str(error.value)
        saved = auth.record("google-main")
        assert ("refresh_token" not in saved) is cleared
        assert saved["client_id"] == CLIENT_ID
        if cleared:
            assert saved["state"] == "REAUTH_REQUIRED"


async def test_google_logout_revokes_and_clears_only_tokens(tmp_path):
    store = CredentialStore(tmp_path)
    connected_google(store)
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert await AuthManager(store, client).logout("google-main") is True
    saved = store.get(account_key("google-main"))
    assert saved["state"] == "REAUTH_REQUIRED"
    assert "access_token" not in saved and "refresh_token" not in saved
    assert saved["client_id"] == CLIENT_ID
    assert str(requests[0].url) == "https://oauth2.googleapis.com/revoke"
    assert parse_qs(requests[0].content.decode())["token"] == ["old-refresh"]
