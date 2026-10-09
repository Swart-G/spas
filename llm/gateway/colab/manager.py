import asyncio
import re
import secrets
import time
import uuid
from collections.abc import Callable
from contextlib import aclosing
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..auth.credentials import CredentialStore
from ..auth.google import GoogleAuth
from ..auth.manager import AuthManager, account_key
from ..core.errors import GatewayError
from .api import ColabOperationError, resource_name
from .cli_api import ColabControl
from .jupyter import JupyterBridge
from .scripts import bootstrap_script, status_script, stop_script


def setup_diagnostic(event):
    """Expose fixed classifications, never subprocess output or remote tracebacks."""
    data = event.get("diagnostic")
    if not isinstance(data, dict):
        return {}, ""
    detail, parts = {}, []
    steps = {
        "prepare": "подготовка",
        "venv": "создание окружения",
        "dependencies": "установка пакетов",
        "torch": "установка PyTorch",
        "model_download": "скачивание модели",
    }
    reasons = {
        "ensurepip_missing": "отсутствует ensurepip",
        "pip_missing": "отсутствует pip",
        "disk_full": "недостаточно места",
        "package_unavailable": "версия пакета недоступна",
        "dependency_conflict": "конфликт зависимостей",
        "network_error": "ошибка сети",
        "subprocess_failed": "процесс установки завершился с ошибкой",
    }
    if isinstance(data.get("step"), str) and data["step"] in steps:
        detail["step"] = data["step"]
        parts.append("Шаг: " + steps[data["step"]])
    if isinstance(data.get("reason"), str) and data["reason"] in reasons:
        detail["reason"] = data["reason"]
        parts.append(reasons[data["reason"]])
    version = data.get("python")
    if isinstance(version, str) and re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{1,3}", version):
        detail["python"] = version
        parts.append("Python " + version)
    package = data.get("package")
    if isinstance(package, str) and re.fullmatch(
        r"(?:fastapi|uvicorn|transformers|huggingface-hub|accelerate|safetensors|torch)"
        r"(?:[<=>!]+\d[\d.*,+<=>!-]*)?",
        package,
    ):
        detail["package"] = package
        parts.append(package)
    code = data.get("exit_code")
    if type(code) is int and -1000 < code < 1000:
        detail["exit_code"] = code
        parts.append("код выхода " + str(code))
    return detail, "; ".join(parts)


class DeploymentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    google_account: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    variant: Literal["VARIANT_CPU", "VARIANT_GPU"] = "VARIANT_GPU"
    accelerator: str = Field(min_length=1, max_length=30)
    shape: Literal["SHAPE_STANDARD", "SHAPE_HIGHMEM"] = "SHAPE_STANDARD"
    runtime_version: str = ""
    model_repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    revision: str = Field(default="main", min_length=1, max_length=200)
    precision: Literal["auto", "float16", "bfloat16", "float32"] = "auto"
    port: int = Field(default=8000, ge=1024, le=65535)
    max_context: int = Field(default=4096, ge=256, le=131072)
    max_new_tokens: int = Field(default=256, gt=0, le=8192)
    setup_timeout: int = Field(default=3600, ge=60, le=14400)
    startup_timeout: int = Field(default=600, ge=30, le=3600)

    @model_validator(mode="after")
    def check_context(self):
        if self.max_new_tokens >= self.max_context:
            raise ValueError("The output limit must be smaller than the context limit")
        if self.variant == "VARIANT_CPU" and self.accelerator != "NONE":
            raise ValueError("CPU runtimes require accelerator NONE")
        return self

    def spec(self):
        return {"variant": self.variant, "accelerator": self.accelerator, "shape": self.shape}


class ColabManager:
    def __init__(
        self, store: CredentialStore, client: httpx.AsyncClient, *, bridge_factory=JupyterBridge
    ):
        self.store, self.client = store, client
        self.google = GoogleAuth(store, client)
        self.api = ColabControl(self.google, client)
        self.bridge_factory = bridge_factory

    def record(self, account):
        record = self.store.get(account_key(account))
        if not record or record.get("provider") != "colab_managed":
            raise GatewayError(
                "Сначала настройте управляемый сервер Colab.", code="reauth_required"
            )
        return record

    async def patch(self, account, **fields):
        key = account_key(account)
        async with self.store.lock(key):
            record = self.record(account)
            record.update(fields)
            self.store.put(key, record)
            return record

    async def configure(self, account, settings: DeploymentSettings, hf_token: str | None = None):
        self.google.record(settings.google_account)
        async with self.store.lock("colab-control:" + account):
            key = account_key(account)
            async with self.store.lock(key):
                existing = self.store.get(key)
                if existing and existing.get("provider") != "colab_managed":
                    raise GatewayError("Имя профиля уже занято другим провайдером.")
                if existing and (existing.get("runtime_name") or existing.get("request_id")):
                    previous = DeploymentSettings.model_validate(existing["settings"])
                    if (
                        previous.google_account != settings.google_account
                        or previous.spec() != settings.spec()
                        or previous.runtime_version != settings.runtime_version
                    ):
                        raise GatewayError(
                            "Сначала удалите текущий runtime, чтобы сменить аккаунт, GPU или образ."
                        )
                record = existing or {
                    "kind": "account",
                    "provider": "colab_managed",
                    "account": account,
                    "deployment_id": uuid.uuid4().hex,
                    "server_key": secrets.token_urlsafe(32),
                }
                if existing and existing.get("settings") == settings.model_dump():
                    if hf_token is not None:
                        record["hf_token"] = hf_token.strip()
                        self.store.put(key, record)
                    return
                record.update(
                    settings=settings.model_dump(),
                    model_repo=settings.model_repo,
                    state="DISCONNECTED",
                    stage="CONFIGURED",
                    last_error=None,
                    last_error_stage=None,
                    last_error_detail=None,
                )
                if hf_token is not None:
                    record["hf_token"] = hf_token.strip()
                self.store.put(key, record)

    async def connect(self, account):
        record = self.record(account)
        if not record.get("runtime_name"):
            raise GatewayError(
                "Runtime ещё не создан. Выберите Запуск в SPAS TUI.", code="provider_unavailable"
            )
        settings = DeploymentSettings.model_validate(record["settings"])
        try:
            runtime = await self.api.get(settings.google_account, record["runtime_name"])
        except GatewayError as error:
            if error.code == "colab_runtime_missing":
                await self.patch(
                    account, state="DISCONNECTED", stage="EXPIRED", last_error=error.code
                )
            raise
        return self.bridge_factory(self.client, self.api.connection(runtime))

    async def _allocate(self, account, record, settings):
        if record.get("runtime_name"):
            try:
                await self.api.get(settings.google_account, record["runtime_name"])
                return record
            except GatewayError as error:
                if error.code != "colab_runtime_missing":
                    raise
                # Explicit Start authorizes replacement of the expired assignment.
                record = await self.patch(
                    account, runtime_name=None, operation=None, request_id=None, runtime_id=None
                )
        if record.get("runtime_id") and not record.get("operation"):
            candidate = "runtimes/" + record["runtime_id"]
            try:
                await self.api.get(settings.google_account, candidate)
            except GatewayError as error:
                if error.code != "colab_runtime_missing":
                    raise
            else:
                # A create may have succeeded even when its response was lost. Recover
                # by stable resource ID, including after Google's request cache expires.
                return await self.patch(account, runtime_name=candidate, operation=None)
        if not record.get("request_id"):
            specs = await self.api.specs(settings.google_account)
            if not any(item.get("eligible") and item["key"] == settings.spec() for item in specs):
                raise GatewayError(
                    "Выбранный runtime сейчас недоступен этому аккаунту.",
                    code="colab_spec_unavailable",
                )
            record = await self.patch(
                account, request_id=str(uuid.uuid4()), runtime_id="spas-" + uuid.uuid4().hex
            )
        operation = record.get("operation")
        if not operation:
            operation = await self.api.create(
                settings.google_account,
                settings.spec(),
                record["runtime_id"],
                record["request_id"],
                settings.runtime_version,
            )
            record = await self.patch(account, operation=operation)
        try:
            runtime = await self.api.wait(settings.google_account, operation)
        except ColabOperationError:
            # A confirmed terminal failure can be retried by the next explicit Start.
            await self.patch(account, operation=None, request_id=None, runtime_id=None)
            raise
        name = resource_name(runtime.get("name"), "runtimes")
        return await self.patch(account, runtime_name=name, operation=None)

    async def start(self, account, announce: Callable[[str], None] = lambda stage: None):
        async with self.store.lock("colab-control:" + account):
            record = self.record(account)
            settings = DeploymentSettings.model_validate(record["settings"])
            auth = AuthManager(self.store, self.client)
            auth.check_cooldown(account)
            try:
                announce("ALLOCATING")
                await self.patch(
                    account,
                    state="DISCONNECTED",
                    stage="ALLOCATING",
                    last_error=None,
                    last_error_stage=None,
                    last_error_detail=None,
                )
                record = await self._allocate(account, record, settings)
                bridge = await self.connect(account)
                ready = False
                commit = None
                config = {
                    **settings.model_dump(),
                    "deployment_id": record["deployment_id"],
                    "server_key": record["server_key"],
                    "hf_token": record.get("hf_token", ""),
                }
                async with aclosing(
                    bridge.execute(bootstrap_script(config), wait_seconds=settings.setup_timeout)
                ) as events:
                    async for event in events:
                        stage = event.get("stage")
                        if stage == "FAILED":
                            failures = {
                                "INSTALLING": (
                                    "Не удалось установить зависимости сервера.",
                                    "colab_install_failed",
                                ),
                                "DOWNLOADING": (
                                    "Не удалось скачать модель. Проверьте репозиторий, "
                                    "ревизию и доступ Hugging Face.",
                                    "colab_download_failed",
                                ),
                                "STARTING": (
                                    "Модель не загрузилась. Проверьте формат весов, "
                                    "chat template и объём памяти выбранного runtime.",
                                    "colab_model_load_failed",
                                ),
                            }
                            phase = event.get("phase")
                            detail, description = setup_diagnostic(event)
                            message, code = failures.get(
                                phase,
                                (
                                    "Установка сервера завершилась ошибкой.",
                                    "colab_execution_failed",
                                ),
                            )
                            await self.patch(
                                account,
                                last_error_stage=phase if phase in failures else None,
                                last_error_detail=detail or None,
                            )
                            if description:
                                message += " " + description + "."
                            raise GatewayError(message, code=code)
                        if stage not in {"INSTALLING", "DOWNLOADING", "STARTING", "READY"}:
                            raise GatewayError(
                                "Runtime вернул неверный этап установки.",
                                code="colab_invalid_response",
                            )
                        if stage != "READY":
                            await self.patch(account, stage=stage)
                        commit = event.get("snapshot_commit") or commit
                        ready = stage == "READY"
                        announce(stage)
                if not ready:
                    raise GatewayError(
                        "Сервер не подтвердил готовность.", code="provider_incomplete"
                    )
                await self.patch(
                    account,
                    state="RATE_LIMITED"
                    if self.record(account).get("retry_at", 0) > time.time()
                    else "CONNECTED",
                    stage="VERIFYING",
                    snapshot_commit=commit,
                    last_error=None,
                    last_error_stage=None,
                    last_error_detail=None,
                )
                # Verify model generation through the same transport used by gateway clients.
                announce("VERIFYING")
                from ..config import BackendConfig
                from ..core.models import ChatRequest
                from ..providers.colab_managed import ColabManagedAdapter

                provider = ColabManagedAdapter(
                    self.client,
                    auth,
                    BackendConfig(
                        provider="colab_managed", account=account, model=settings.model_repo
                    ),
                    manager=self,
                )
                await provider.generate(
                    ChatRequest(
                        model="probe",
                        messages=[{"role": "user", "content": "Ответь одним словом: СПАС"}],
                        max_tokens=8,
                    )
                )
                await provider.auth.note_result(account)
                await self.patch(account, stage="READY", ready_at=time.time())
                announce("VERIFIED")
            except asyncio.CancelledError:
                await self.patch(account, state="DISCONNECTED", stage="INTERRUPTED")
                raise
            except (GatewayError, TimeoutError) as error:
                if not isinstance(error, GatewayError):
                    error = GatewayError(
                        "Истекло время ожидания runtime.", code="provider_timeout", status=504
                    )
                await self.patch(
                    account,
                    state="REAUTH_REQUIRED" if error.code == "reauth_required" else "DISCONNECTED",
                    stage="FAILED",
                    last_error=error.code,
                    last_error_stage=self.record(account).get("last_error_stage")
                    or self.record(account).get("stage"),
                )
                await auth.note_result(account, error)
                raise error

    async def stop(self, account):
        async with self.store.lock("colab-control:" + account):
            record = self.record(account)
            bridge = await self.connect(account)
            async with aclosing(
                bridge.execute(stop_script(record["deployment_id"]), wait_seconds=60)
            ) as events:
                async for _ in events:
                    pass
            await self.patch(
                account,
                stage="STOPPED",
                state="DISCONNECTED",
                last_error=None,
                last_error_stage=None,
                last_error_detail=None,
            )

    async def delete(self, account):
        async with self.store.lock("colab-control:" + account):
            record = self.record(account)
            settings = DeploymentSettings.model_validate(record["settings"])
            runtime = record.get("runtime_name")
            if not runtime and record.get("operation"):
                try:
                    result = await self.api.wait(settings.google_account, record["operation"])
                    runtime = resource_name(result.get("name"), "runtimes")
                except ColabOperationError:
                    pass
            elif not runtime and record.get("runtime_id"):
                # Recover an assignment whose create response was lost on the network.
                runtime = "runtimes/" + record["runtime_id"]
            if runtime:
                try:
                    operation = await self.api.delete(settings.google_account, runtime)
                    await self.api.wait(settings.google_account, operation)
                except GatewayError as error:
                    if error.code != "colab_runtime_missing":
                        raise
            await self.patch(
                account,
                runtime_name=None,
                operation=None,
                request_id=None,
                runtime_id=None,
                stage="DELETED",
                state="DISCONNECTED",
                snapshot_commit=None,
                ready_at=None,
                last_error=None,
                last_error_stage=None,
                last_error_detail=None,
            )

    async def models(self, account):
        record = self.record(account)
        settings = DeploymentSettings.model_validate(record["settings"])
        bridge = await self.connect(account)
        models = None
        async with aclosing(
            bridge.execute(status_script(settings.port, record["server_key"]), wait_seconds=30)
        ) as events:
            async for event in events:
                if event.get("type") == "models":
                    data = event.get("data")
                    data = data.get("data") if isinstance(data, dict) else None
                    if not isinstance(data, list) or any(
                        not isinstance(item, dict)
                        or not isinstance(item.get("id"), str)
                        or not item["id"]
                        for item in data
                    ):
                        raise GatewayError(
                            "Сервер вернул неверный каталог.", code="colab_invalid_response"
                        )
                    models = [{"id": item["id"], "name": item["id"]} for item in data]
        if models is None:
            raise GatewayError("Сервер модели остановлен. Выполните запуск в TUI.")
        return models

    async def status(self, account):
        availability = {}
        try:
            models = await self.models(account)
            availability.update(
                available=True,
                model_available=any(
                    item["id"] == self.record(account)["model_repo"] for item in models
                ),
            )
        except GatewayError as error:
            availability.update(available=False, code=error.code)
        record = self.record(account)
        public = {
            key: record.get(key)
            for key in (
                "account",
                "state",
                "stage",
                "runtime_name",
                "model_repo",
                "snapshot_commit",
                "last_error",
                "last_error_stage",
                "last_error_detail",
                "ready_at",
                "last_success_at",
            )
        }
        return {**public, **availability}
