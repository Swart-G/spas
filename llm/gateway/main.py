import hmac
import math
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api import admin, completions
from .auth.claude import ClaudeProfiles
from .auth.credentials import CredentialStore
from .auth.manager import AuthManager
from .config import GatewayConfig, default_state_dir
from .core.audit import RequestLog
from .core.errors import GatewayError
from .core.registry import Registry
from .core.router import Router
from .providers.base import LLMProvider
from .providers.claude_subscription import ClaudeSubscriptionAdapter
from .providers.codex_subscription import CodexSubscriptionAdapter
from .providers.colab_managed import ColabManagedAdapter
from .providers.openai_api import OpenAIAPIAdapter
from .providers.remote_inference import RemoteInferenceAdapter


@dataclass(frozen=True)
class GatewayKeys:
    client: str = field(repr=False)
    admin: str | None = field(default=None, repr=False)

    @classmethod
    def load(cls, store: CredentialStore) -> "GatewayKeys":
        record = store.get("gateway-keys") or {}
        client = os.environ.get("SPAS_GATEWAY_KEY") or record.get("client")
        admin_key = os.environ.get("SPAS_GATEWAY_ADMIN_KEY") or record.get("admin")
        if not client or len(client) < 24:
            raise GatewayError(
                "Выполните spas-llm init или задайте SPAS_GATEWAY_KEY (от 24 символов)."
            )
        if admin_key and (admin_key == client or len(admin_key) < 24):
            raise GatewayError("Для администратора требуется отдельный ключ от 24 символов.")
        return cls(client, admin_key)


def make_registry(config: GatewayConfig, auth: AuthManager, client: httpx.AsyncClient) -> Registry:
    profiles = ClaudeProfiles(auth)
    providers = {}
    for name, backend in config.backends.items():
        if backend.provider == "codex_subscription":
            provider = CodexSubscriptionAdapter(client, auth, backend)
        elif backend.provider == "claude_subscription":
            provider = ClaudeSubscriptionAdapter(profiles, backend)
        elif backend.provider == "remote_inference":
            provider = RemoteInferenceAdapter(client, auth, backend)
        elif backend.provider == "colab_managed":
            provider = ColabManagedAdapter(client, auth, backend)
        else:
            provider = OpenAIAPIAdapter(client, auth, backend)
        providers[name] = provider
    return Registry(config, providers)


def create_app(
    config: GatewayConfig | None = None,
    *,
    state_dir: Path | None = None,
    keys: GatewayKeys | None = None,
    providers: dict[str, LLMProvider] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    state_dir = state_dir or default_state_dir()
    config_path = config_path or Path(
        os.environ.get("SPAS_GATEWAY_CONFIG", str(state_dir / "gateway.toml"))
    )
    config = config or GatewayConfig.load(config_path)
    store = CredentialStore(state_dir)
    keys = keys or GatewayKeys.load(store)
    client = httpx.AsyncClient(transport=transport, timeout=30, follow_redirects=False)
    auth = AuthManager(store, client)
    registry = (
        Registry(config, providers)
        if providers is not None
        else make_registry(config, auth, client)
    )
    audit = RequestLog(state_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await client.aclose()
            audit.close()

    app = FastAPI(
        title="СПАС LLM Gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.auth = auth
    app.state.registry = registry
    app.state.audit = audit
    app.state.router = Router(registry, auth, audit)

    def reload_config():
        if providers is not None:
            raise GatewayError("Перезагрузка недоступна для встроенных тестовых провайдеров.")
        try:
            updated = GatewayConfig.load(config_path)
        except (OSError, ValueError) as error:
            raise GatewayError(
                "Не удалось прочитать сохранённые настройки Gateway.",
                code="invalid_config",
                status=400,
            ) from error
        current = app.state.registry
        for name, backend in current.backends.items():
            replacement = updated.backends.get(name)
            if replacement is not None and replacement != backend.config:
                if backend.semaphore._value < backend.config.max_concurrency:
                    raise GatewayError(
                        "Подключение выполняет запрос. Повторите применение настроек.",
                        code="backend_busy",
                        status=409,
                    )
        next_registry = make_registry(updated, auth, client)
        for name, backend in current.backends.items():
            if updated.backends.get(name) == backend.config:
                next_registry.backends[name] = backend
        app.state.registry = next_registry
        app.state.router = Router(next_registry, auth, audit)
        return {"status": "applied"}

    app.state.reload_config = reload_config

    def authorization(expected_key: str | None):
        async def authorize(request: Request) -> None:
            scheme, _, value = request.headers.get("authorization", "").partition(" ")
            if (
                expected_key is None
                or scheme.lower() != "bearer"
                or not hmac.compare_digest(value.encode(), expected_key.encode())
            ):
                raise GatewayError(
                    "Неверный ключ доступа к Gateway.", code="invalid_gateway_key", status=401
                )

        return authorize

    app.include_router(completions.build_routes(authorization(keys.client)))
    app.include_router(admin.build_routes(authorization(keys.admin)))

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, error: GatewayError) -> JSONResponse:
        headers = {}
        if error.retry_after is not None:
            headers["Retry-After"] = str(math.ceil(error.retry_after))
        if error.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(error.as_dict(), status_code=error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        problem = error.errors()[0]
        param = ".".join(str(part) for part in problem["loc"] if part != "body")
        public = GatewayError(
            "Неверный формат запроса или неподдерживаемый параметр.",
            code="invalid_parameter",
            status=400,
            param=param or None,
        )
        return JSONResponse(public.as_dict(), status_code=400)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        public = GatewayError("Внутренняя ошибка Gateway.", code="internal_error", status=500)
        return JSONResponse(public.as_dict(), status_code=500)

    return app
