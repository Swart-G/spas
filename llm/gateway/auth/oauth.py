"""Official Sign in with ChatGPT: public client, PKCE and loopback callback."""

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

from ..core.errors import GatewayError

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {DIRECT_SCOPE}"
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


class OAuthError(GatewayError):
    def __init__(self, oauth_code: str) -> None:
        super().__init__("Не удалось завершить OAuth; повторите вход.", code="oauth_error")
        self.oauth_code = oauth_code


@dataclass
class OAuthAttempt:
    client_id: str
    host_id: str
    redirect_uri: str = ""
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(64), repr=False)

    def authorization_url(self, login_hint: str | None = None, consent: bool = False) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
        parameters = {
            "client_id": self.client_id,
            "ext_agent_host_id": self.host_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge.decode().rstrip("="),
        }
        if self.client_id == "dynamic_agent_client":
            parameters["agent_name_hint"] = "СПАС"
        if login_hint:
            parameters["login_hint"] = login_hint
        if consent:
            parameters["prompt"] = "consent"
        # id_token_hint is optional; omitting it keeps the displayed URL free of credentials.
        return AUTHORIZE_URL + "?" + urlencode(parameters)

    def validate_callback(self, parameters: dict[str, str]) -> tuple[str, str]:
        if not hmac.compare_digest(parameters.get("state", ""), self.state):
            raise GatewayError("OAuth state не совпадает.", code="invalid_oauth_state", status=400)
        if error := parameters.get("error"):
            raise OAuthError(error)
        client_id = parameters.get("client_id", self.client_id)
        if not client_id or client_id == "dynamic_agent_client":
            raise GatewayError("OAuth не выдал client_id.", code="oauth_registration_incomplete")
        if self.client_id != "dynamic_agent_client" and client_id != self.client_id:
            raise GatewayError("OAuth вернул другой client_id.", code="oauth_account_mismatch")
        if not parameters.get("code"):
            raise GatewayError("OAuth не вернул код авторизации.", code="oauth_error")
        return parameters["code"], client_id


class ChatGPTOAuth:
    issuer = ISSUER
    jwks_url = JWKS_URL
    identity_provider = "OpenAI"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._jwks: dict = {}
        self._jwks_until = 0.0

    async def authorize(
        self,
        attempt: OAuthAttempt,
        announce: Callable[[str], None],
        *,
        login_hint: str | None = None,
        consent: bool = False,
        wait_seconds: float = 180,
    ) -> tuple[str, str]:
        result: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

        async def callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            status = "400 Bad Request"
            message = "Invalid callback. Return to the terminal."
            try:
                request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
                method, target, _ = request.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
                parsed = urlsplit(target)
                if method != "GET" or parsed.path != "/auth/callback":
                    raise ValueError("Not the callback path")
                raw = parse_qs(parsed.query, keep_blank_values=True)
                if any(len(values) != 1 for values in raw.values()):
                    raise ValueError("Repeated OAuth parameters")
                parameters = {key: values[0] for key, values in raw.items()}
                if not hmac.compare_digest(parameters.get("state", ""), attempt.state):
                    raise ValueError("Wrong state")
                if result.done():
                    status = "409 Conflict"
                else:
                    result.set_result(parameters)
                    status = "200 OK"
                    message = "Callback received. Return to the terminal to verify sign-in."
            except (
                TimeoutError,
                ValueError,
                UnicodeError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                pass
            finally:
                body = message.encode()
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n"
                    "Cache-Control: no-store\r\n\r\n".encode()
                    + body
                )
                try:
                    await writer.drain()
                except ConnectionError:
                    pass
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(callback, "127.0.0.1", 0, limit=16384)
        assert server.sockets
        port = server.sockets[0].getsockname()[1]
        attempt.redirect_uri = f"http://127.0.0.1:{port}/auth/callback"
        url = attempt.authorization_url(login_hint, consent)
        async with server:
            announce(url)
            await asyncio.to_thread(webbrowser.open, url)
            try:
                parameters = await asyncio.wait_for(result, wait_seconds)
            except TimeoutError as error:
                raise GatewayError("Время ожидания входа истекло.", code="oauth_timeout") from error
        return attempt.validate_callback(parameters)

    async def token_request(self, parameters: dict[str, str]) -> dict:
        try:
            response = await self.client.post(TOKEN_URL, data={**parameters, "resource": RESOURCE})
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise GatewayError("Сервис авторизации OpenAI недоступен.") from error
        if response.is_error:
            value = data.get("error", "oauth_error") if isinstance(data, dict) else "oauth_error"
            if isinstance(value, dict):
                value = value.get("code", "oauth_error")
            raise OAuthError(str(value))
        if not isinstance(data, dict):
            raise GatewayError("Некорректный ответ сервиса авторизации.")
        return data

    async def validate_identity(self, token: str, client_id: str, nonce: str | None = None) -> dict:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in {"RS256", "ES256"} or not header.get("kid"):
                raise ValueError("Unsupported ID token signature")
            keys = self._jwks.get("keys", [])
            if self._jwks_until < time.time() or not any(
                key.get("kid") == header["kid"] for key in keys
            ):
                response = await self.client.get(self.jwks_url)
                response.raise_for_status()
                self._jwks = response.json()
                self._jwks_until = time.time() + 600
                keys = self._jwks["keys"]
            key = next(key for key in keys if key.get("kid") == header["kid"])
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256", "ES256"],
                issuer=self.issuer,
                audience=client_id,
                leeway=5,
                options={"require": ["sub", "exp", "iat", "iss", "aud"]},
            )
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError("Missing identity")
            if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
                raise ValueError("Wrong nonce")
            return claims
        except (
            jwt.PyJWTError,
            httpx.HTTPError,
            ValueError,
            KeyError,
            StopIteration,
            TypeError,
        ) as error:
            raise GatewayError(
                f"Не удалось проверить ID token {self.identity_provider}.", code="invalid_identity"
            ) from error

    async def revoke(self, client_id: str, refresh_token: str) -> None:
        try:
            discovery = await self.client.get(f"{ISSUER}/.well-known/openid-configuration")
            discovery.raise_for_status()
            endpoint = discovery.json()["revocation_endpoint"]
            parsed = urlsplit(endpoint)
            if parsed.scheme != "https" or parsed.netloc != "auth.openai.com":
                raise ValueError("Unexpected revocation endpoint")
            response = await self.client.post(
                endpoint,
                data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": client_id,
                },
            )
            response.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError) as error:
            raise GatewayError("Не удалось подтвердить отзыв сессии OpenAI.") from error
