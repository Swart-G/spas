"""Google Desktop OAuth with PKCE, loopback, verified identity and rotating tokens."""

import base64
import hashlib
import inspect
import json
import time
from importlib import resources
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..core.errors import GatewayError
from ..core.models import AccountState
from .credentials import CredentialStore
from .manager import account_key
from .oauth import ChatGPTOAuth, OAuthAttempt, OAuthError

COLAB_SCOPE = "https://www.googleapis.com/auth/colaboratory"
SCOPES = "openid email profile " + COLAB_SCOPE
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleAttempt(OAuthAttempt):
    def authorization_url(self, login_hint=None, consent=False):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
        parameters = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge": challenge.decode().rstrip("="),
            "code_challenge_method": "S256",
            "access_type": "offline",
        }
        if login_hint:
            parameters["login_hint"] = login_hint
        if consent:
            parameters["prompt"] = "consent"
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(parameters)


class GoogleOAuth(ChatGPTOAuth):
    issuer = ["https://accounts.google.com", "accounts.google.com"]
    jwks_url = "https://www.googleapis.com/oauth2/v3/certs"
    identity_provider = "Google"

    async def token_request(self, parameters):
        try:
            response = await self.client.post(TOKEN_URL, data=parameters, follow_redirects=False)
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise GatewayError("Сервис авторизации Google недоступен.") from error
        if not isinstance(data, dict):
            raise GatewayError("Google вернул неверный ответ авторизации.")
        if response.is_error:
            raise OAuthError(str(data.get("error", "oauth_error")))
        return data


class ColabCLIAttempt(GoogleAttempt):
    def authorization_url(self, login_hint=None, consent=False):
        from colab_cli.auth import PUBLIC_SCOPES

        parsed = urlsplit(super().authorization_url(login_hint, True))
        parameters = dict(parse_qsl(parsed.query))
        parameters.update(scope=" ".join(PUBLIC_SCOPES), token_usage="remote")
        return urlunsplit(parsed._replace(query=urlencode(parameters)))


class GoogleAuth:
    def __init__(self, store: CredentialStore, client: httpx.AsyncClient):
        self.store, self.client = store, client
        self.oauth = GoogleOAuth(client)

    async def configure(self, account: str, client_id: str, client_secret: str):
        key = account_key(account)
        if not client_id.endswith(".apps.googleusercontent.com"):
            raise GatewayError(
                "Нужен client_id приложения Google типа Desktop.",
                code="invalid_account",
                status=400,
            )
        async with self.store.lock(key):
            record = self.store.get(key)
            if record and record["provider"] != "google_colab":
                raise GatewayError("Имя аккаунта уже занято другим провайдером.")
            if record and record.get("auth_transport") == "cli":
                raise GatewayError(
                    "Для собственного OAuth-клиента используйте отдельное имя профиля."
                )
            if record and record.get("client_id") != client_id:
                record = None
            record = record or {
                "kind": "account",
                "account": account,
                "provider": "google_colab",
                "state": AccountState.REAUTH_REQUIRED,
            }
            record.update(client_id=client_id, client_secret=client_secret, auth_transport="api")
            self.store.put(key, record)

    async def configure_cli(self, account):
        """Use the official Colab application's bundled client, without a user project."""
        try:
            config = json.loads(
                resources.files("colab_cli").joinpath("oauth_config.json").read_text()
            )
            installed = config["installed"]
        except (ImportError, FileNotFoundError, KeyError, ValueError) as error:
            raise GatewayError("Colab CLI отсутствует. Пересоберите Compose-образ.") from error
        key = account_key(account)
        async with self.store.lock(key):
            record = self.store.get(key)
            if record and record.get("provider") != "google_colab":
                raise GatewayError("Имя аккаунта уже занято другим провайдером.")
            if record and record.get("auth_transport", "api") != "cli":
                raise GatewayError("Для входа через Colab CLI используйте отдельное имя профиля.")
            record = record or {
                "kind": "account",
                "account": account,
                "provider": "google_colab",
                "state": AccountState.REAUTH_REQUIRED,
            }
            record.update(
                client_id=installed["client_id"],
                client_secret=installed.get("client_secret", ""),
                auth_transport="cli",
            )
            self.store.put(key, record)

    def record(self, account):
        record = self.store.get(account_key(account))
        if not record or record.get("provider") != "google_colab":
            raise GatewayError(
                "Настройте Google-аккаунт в разделе полного управления Colab.",
                code="reauth_required",
            )
        return record

    @staticmethod
    def tokens(record, tokens):
        try:
            access = tokens["access_token"]
            expires = float(tokens["expires_in"])
            if not isinstance(access, str) or not access or not 0 < expires < 1e9:
                raise ValueError("Invalid token")
            if tokens.get("token_type", "Bearer").lower() != "bearer":
                raise ValueError("Unsupported token")
            scope = tokens.get("scope")
            scopes = scope.split() if scope is not None else record.get("scopes", [])
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise GatewayError("Google вернул некорректные токены.") from error
        updated = {
            **record,
            "access_token": access,
            "expires_at": time.time() + expires,
            "scopes": scopes,
            "state": AccountState.RATE_LIMITED
            if record.get("retry_at", 0) > time.time()
            else AccountState.CONNECTED,
        }
        if tokens.get("refresh_token"):
            updated["refresh_token"] = tokens["refresh_token"]
        if COLAB_SCOPE not in scopes:
            updated["state"] = AccountState.REAUTH_REQUIRED
        return updated

    async def login(self, account, announce, *, consent=False, read_code=None):
        key = account_key(account)
        async with self.store.lock(key):
            saved = self.record(account)
            if saved.get("auth_transport") == "cli":
                from colab_cli.auth import REMOTE_REDIRECT_URI

                if read_code is None:
                    raise GatewayError("Выполните вход Google через SPAS TUI.")
                attempt = ColabCLIAttempt(saved["client_id"], "", redirect_uri=REMOTE_REDIRECT_URI)
                announce(attempt.authorization_url(saved.get("email"), True))
                code = read_code()
                if inspect.isawaitable(code):
                    code = await code
                code = code.strip()
                if not code:
                    raise GatewayError("Код авторизации не введён.", code="oauth_error")
            else:
                attempt = GoogleAttempt(saved["client_id"], "")
                code, _ = await self.oauth.authorize(
                    attempt,
                    announce,
                    login_hint=saved.get("email"),
                    consent=consent or not saved.get("refresh_token"),
                )
            tokens = await self.oauth.token_request(
                {
                    "grant_type": "authorization_code",
                    "client_id": saved["client_id"],
                    "client_secret": saved["client_secret"],
                    "code": code,
                    "code_verifier": attempt.verifier,
                    "redirect_uri": attempt.redirect_uri,
                }
            )
            identity = await self.oauth.validate_identity(
                tokens.get("id_token", ""), saved["client_id"], attempt.nonce
            )
            if saved.get("subject") and saved["subject"] != identity["sub"]:
                raise GatewayError("Выбран другой Google-аккаунт.", code="oauth_account_mismatch")
            updated = self.tokens(saved, tokens)
            updated.update(subject=identity["sub"], email=identity.get("email"))
            self.store.put(key, updated)
            if updated["state"] == AccountState.REAUTH_REQUIRED:
                raise GatewayError("Не выдано разрешение управлять Colab.", code="reauth_required")

    async def access_token(self, account):
        key = account_key(account)
        async with self.store.lock(key):
            record = self.record(account)
            if record["state"] == AccountState.REAUTH_REQUIRED or COLAB_SCOPE not in record.get(
                "scopes", []
            ):
                raise GatewayError(
                    "Требуется вход Google с разрешением Colab.", code="reauth_required"
                )
            if record.get("expires_at", 0) > time.time() + 60 and record.get("access_token"):
                return record["access_token"]
            if not record.get("refresh_token"):
                raise GatewayError("Сессия Google истекла. Войдите снова.", code="reauth_required")
            try:
                tokens = await self.oauth.token_request(
                    {
                        "grant_type": "refresh_token",
                        "client_id": record["client_id"],
                        "client_secret": record["client_secret"],
                        "refresh_token": record["refresh_token"],
                    }
                )
            except OAuthError as error:
                if error.oauth_code in {"invalid_grant", "invalid_client", "unauthorized_client"}:
                    for field in ("access_token", "refresh_token", "expires_at", "scopes"):
                        record.pop(field, None)
                    record["state"] = AccountState.REAUTH_REQUIRED
                    self.store.put(key, record)
                    raise GatewayError(
                        "Сессия Google отозвана. Войдите снова.", code="reauth_required"
                    ) from error
                raise
            if tokens.get("id_token"):
                identity = await self.oauth.validate_identity(
                    tokens["id_token"], record["client_id"]
                )
                if identity["sub"] != record.get("subject"):
                    raise GatewayError(
                        "Изменилась учётная запись Google.", code="oauth_account_mismatch"
                    )
            updated = self.tokens(record, tokens)
            self.store.put(key, updated)
            if updated["state"] == AccountState.REAUTH_REQUIRED:
                raise GatewayError("Разрешение Colab отозвано.", code="reauth_required")
            return updated["access_token"]

    async def logout(self, account):
        key = account_key(account)
        async with self.store.lock(key):
            record = self.record(account)
            confirmed = True
            token = record.get("refresh_token") or record.get("access_token")
            if token:
                try:
                    response = await self.client.post(
                        "https://oauth2.googleapis.com/revoke",
                        data={"token": token},
                        follow_redirects=False,
                    )
                    confirmed = response.status_code == 200
                except httpx.HTTPError:
                    confirmed = False
            for field in ("access_token", "refresh_token", "expires_at", "scopes"):
                record.pop(field, None)
            record["state"] = AccountState.REAUTH_REQUIRED
            self.store.put(key, record)
            return confirmed
