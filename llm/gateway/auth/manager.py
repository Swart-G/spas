import re
import time
from collections.abc import Callable

import httpx

from ..core.errors import GatewayError
from ..core.models import AccountState
from .credentials import CredentialStore
from .oauth import (
    DIRECT_SCOPE,
    ISSUER,
    TERMINAL_REFRESH_ERRORS,
    ChatGPTOAuth,
    OAuthAttempt,
    OAuthError,
)


def account_key(account: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", account):
        raise GatewayError("Неверное имя аккаунта.", code="invalid_account", status=400)
    return "account:" + account


class AuthManager:
    def __init__(self, store: CredentialStore, client: httpx.AsyncClient) -> None:
        self.store = store
        self.oauth = ChatGPTOAuth(client)

    def require_account(self, account: str, provider: str) -> dict:
        record = self.store.get(account_key(account))
        if record is None or record.get("provider") != provider:
            raise GatewayError(
                "Аккаунт этого провайдера не подключён. Выполните вход через spas-llm.",
                code="reauth_required",
            )
        return record

    @staticmethod
    def public_account(record: dict) -> dict:
        state = record.get("state", AccountState.DISCONNECTED)
        if state == AccountState.RATE_LIMITED and record.get("retry_at", 0) <= time.time():
            state = AccountState.CONNECTED
        if (
            state == AccountState.CONNECTED
            and record.get("expires_at", float("inf")) <= time.time()
        ):
            state = AccountState.EXPIRED
        # Explicit whitelist: neither token fields nor Claude's native profile are serialized.
        return {
            "account": record["account"],
            "provider": record["provider"],
            "state": state,
            "email": record.get("email"),
            "expires_at": record.get("expires_at"),
            "retry_at": record.get("retry_at"),
            "subscription_type": record.get("subscription_type"),
            "last_success_at": record.get("last_success_at"),
            "stage": record.get("stage"),
            "runtime_name": record.get("runtime_name"),
        }

    def accounts(self) -> list[dict]:
        return [self.public_account(record) for record in self.store.all_accounts()]

    def check_cooldown(self, account: str) -> None:
        record = self.store.get(account_key(account)) or {}
        remaining = record.get("retry_at", 0) - time.time()
        if remaining > 0:
            raise GatewayError(
                "Backend ожидает восстановления лимита.",
                code="rate_limited",
                status=429,
                retry_after=remaining,
            )

    async def note_result(self, account: str, error: GatewayError | None = None) -> None:
        key = account_key(account)
        async with self.store.lock(key):
            record = self.store.get(key)
            if record is None:
                return
            now = time.time()
            if error is None:
                record["last_success_at"] = now
                if record.get("retry_at", 0) > now:
                    record["state"] = AccountState.RATE_LIMITED
                else:
                    record["state"] = AccountState.CONNECTED
                    record.pop("retry_at", None)
            elif error.code == "rate_limited":
                record["state"] = AccountState.RATE_LIMITED
                # A short circuit breaker is not an inferred subscription reset time.
                record["retry_at"] = max(
                    record.get("retry_at", 0),
                    now + (error.retry_after if error.retry_after is not None else 60),
                )
            elif error.code == "reauth_required":
                record["state"] = AccountState.REAUTH_REQUIRED
            elif record.get("provider") in {"remote_inference", "colab_managed"}:
                record["state"] = AccountState.DISCONNECTED
            else:
                return
            self.store.put(key, record)

    async def login_chatgpt(
        self, account: str, announce: Callable[[str], None], *, consent: bool = False
    ) -> dict:
        key = account_key(account)
        async with self.store.lock(key):
            saved = self.store.get(key) or {
                "kind": "account",
                "account": account,
                "provider": "codex_subscription",
                "state": AccountState.REAUTH_REQUIRED,
            }
            if saved["provider"] != "codex_subscription":
                raise GatewayError("Это имя уже используется другим провайдером.")
            attempt = OAuthAttempt(
                saved.get("client_id", "dynamic_agent_client"), self.store.host_id()
            )
            code, client_id = await self.oauth.authorize(
                attempt,
                announce,
                login_hint=saved.get("email"),
                consent=consent,
            )
            # Retain registration even when the one-use code expires or exchange fails.
            saved.update(client_id=client_id, ext_agent_host_id=attempt.host_id)
            self.store.put(key, saved)
            tokens = await self.oauth.token_request(
                {
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "code_verifier": attempt.verifier,
                    "redirect_uri": attempt.redirect_uri,
                }
            )
            identity = await self.oauth.validate_identity(
                tokens.get("id_token", ""), client_id, attempt.nonce
            )
            if saved.get("subject") and saved["subject"] != identity["sub"]:
                raise GatewayError("Выбран другой ChatGPT-аккаунт.", code="oauth_account_mismatch")
            record = self._with_tokens(saved, tokens, clear_cooldown=True)
            record.update(subject=identity["sub"], issuer=ISSUER, email=identity.get("email"))
            self.store.put(key, record)
            return self.public_account(record)

    @staticmethod
    def _with_tokens(saved: dict, tokens: dict, *, clear_cooldown: bool = False) -> dict:
        try:
            access_token = tokens["access_token"]
            expires_in = float(tokens["expires_in"])
            if not isinstance(access_token, str) or not access_token or not 0 < expires_in < 1e9:
                raise ValueError("Invalid token response")
            if tokens.get("token_type", "Bearer").lower() != "bearer":
                raise ValueError("Unsupported token type")
            scopes = tokens.get("scope")
            scopes = scopes.split() if scopes is not None else saved.get("scopes", [])
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise GatewayError("Некорректный набор OAuth-токенов.") from error
        record = {
            **saved,
            "access_token": access_token,
            "expires_at": time.time() + expires_in,
            "scopes": scopes,
            "state": AccountState.CONNECTED,
        }
        for name in ("refresh_token", "id_token"):
            if tokens.get(name):
                record[name] = tokens[name]
        if not clear_cooldown and record.get("retry_at", 0) > time.time():
            record["state"] = AccountState.RATE_LIMITED
        else:
            record.pop("retry_at", None)
        if DIRECT_SCOPE not in scopes:
            record["state"] = AccountState.REAUTH_REQUIRED
        return record

    async def access_token(self, account: str) -> str:
        key = account_key(account)
        async with self.store.lock(key):
            record = self.require_account(account, "codex_subscription")
            if DIRECT_SCOPE not in record.get("scopes", []):
                raise GatewayError(
                    "Вход выполнен без разрешения использовать подписку ChatGPT.",
                    code="reauth_required",
                )
            if record.get("state") == AccountState.REAUTH_REQUIRED or not record.get(
                "access_token"
            ):
                raise GatewayError("Нужен повторный вход в ChatGPT.", code="reauth_required")
            if record.get("expires_at", 0) > time.time() + 60:
                return record["access_token"]
            if not record.get("refresh_token"):
                raise GatewayError("OAuth-сессия истекла; выполните вход.", code="reauth_required")
            try:
                tokens = await self.oauth.token_request(
                    {
                        "grant_type": "refresh_token",
                        "client_id": record["client_id"],
                        "refresh_token": record["refresh_token"],
                    }
                )
            except OAuthError as error:
                if error.oauth_code in TERMINAL_REFRESH_ERRORS:
                    record = self._clear_tokens(record)
                    self.store.put(key, record)
                    raise GatewayError(
                        "OAuth-сессия отозвана; выполните вход.", code="reauth_required"
                    ) from error
                raise
            if tokens.get("id_token"):
                identity = await self.oauth.validate_identity(
                    tokens["id_token"], record["client_id"]
                )
                if identity["sub"] != record["subject"]:
                    raise GatewayError(
                        "OAuth обновил другую учётную запись.", code="oauth_account_mismatch"
                    )
            updated = self._with_tokens(record, tokens)
            self.store.put(key, updated)
            if DIRECT_SCOPE not in updated["scopes"]:
                raise GatewayError("Разрешение на подписку отозвано.", code="reauth_required")
            return updated["access_token"]

    @staticmethod
    def _clear_tokens(record: dict) -> dict:
        record = {**record, "state": AccountState.REAUTH_REQUIRED}
        for name in (
            "access_token",
            "refresh_token",
            "id_token",
            "expires_at",
            "retry_at",
            "profile",
            "api_key",
        ):
            record.pop(name, None)
        return record

    async def logout(self, account: str) -> bool:
        saved = self.store.get(account_key(account))
        if saved and saved.get("provider") == "google_colab":
            from .google import GoogleAuth

            return await GoogleAuth(self.store, self.oauth.client).logout(account)
        key = account_key(account)
        async with self.store.lock(key):
            record = self.store.get(key)
            if record is None:
                return True
            revoked = True
            if record.get("provider") == "codex_subscription" and record.get("refresh_token"):
                try:
                    await self.oauth.revoke(record["client_id"], record["refresh_token"])
                except GatewayError:
                    revoked = False
            self.store.put(key, self._clear_tokens(record))
            return revoked

    async def save_api_key(self, account: str, api_key: str) -> None:
        key = account_key(account)
        async with self.store.lock(key):
            existing = self.store.get(key)
            if existing and existing["provider"] != "openai_api":
                raise GatewayError("Это имя уже используется другим провайдером.")
            if not api_key.strip():
                raise GatewayError("API-ключ пуст.", code="invalid_account", status=400)
            self.store.put(
                key,
                {
                    "kind": "account",
                    "account": account,
                    "provider": "openai_api",
                    "state": AccountState.CONNECTED,
                    "api_key": api_key.strip(),
                },
            )

    async def save_remote_key(self, account: str, api_key: str) -> None:
        key = account_key(account)
        async with self.store.lock(key):
            existing = self.store.get(key)
            if existing and existing["provider"] != "remote_inference":
                raise GatewayError("Это имя уже используется другим провайдером.")
            if existing and existing.get("api_key", "") == api_key.strip():
                return
            self.store.put(
                key,
                {
                    "kind": "account",
                    "account": account,
                    "provider": "remote_inference",
                    "state": AccountState.DISCONNECTED,
                    "api_key": api_key.strip(),
                },
            )
