import asyncio
import json
import os
from pathlib import Path

import httpx
from rich.table import Table

from ..auth.credentials import CredentialStore
from ..auth.google import GoogleAuth
from ..auth.manager import AuthManager, account_key
from ..cli import console, save_config
from ..config import BackendConfig, GatewayConfig
from ..core.errors import GatewayError
from ..main import GatewayKeys
from ..terminal import Back, input_text_async
from ..tui import ask, confirm, execute, menu, number, page_header
from .manager import ColabManager, DeploymentSettings


def integer(label, *, default):
    return number(label, default=default, integer=True)


def menu_loop(options, title, action, *, before=lambda: None):
    while True:
        console.clear()
        page_header("3")
        before()
        operation = menu(options() if callable(options) else options, title=title)
        if operation == "b":
            raise Back()
        try:
            action(operation)
        except Back:
            continue
        except KeyboardInterrupt:
            console.print("Действие отменено.")
        except GatewayError as error:
            console.print(error.message, markup=False, style="red")
        except (OSError, ValueError):
            console.print("Проверьте файл, профиль и параметры Colab.", style="red")
        try:
            ask("Enter или Esc — вернуться", default="")
        except (Back, KeyboardInterrupt):
            pass


STAGES = {
    "ALLOCATING": "Создаётся runtime",
    "INSTALLING": "Устанавливается inference-сервер",
    "DOWNLOADING": "Скачивается модель",
    "STARTING": "Модель загружается в память",
    "READY": "Сервер модели готов",
    "VERIFYING": "Проверяется реальная генерация",
    "VERIFIED": "Модель проверена и готова для агентов",
}


def announce(stage):
    console.print(STAGES.get(stage, stage), markup=False)


async def apply_gateway(state_dir: Path):
    keys = GatewayKeys.load(CredentialStore(state_dir))
    if not keys.admin:
        raise GatewayError("Нужен внутренний ключ администратора Gateway.")
    url = os.environ.get("SPAS_GATEWAY_URL", "http://127.0.0.1:8008").rstrip("/")
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            response = await client.post(
                url + "/admin/reload", headers={"Authorization": "Bearer " + keys.admin}
            )
        if response.status_code == 409:
            raise GatewayError("Подключение занято запросом. Повторите применение настроек.")
        if response.status_code != 200:
            raise GatewayError(
                "Gateway не применил настройки. Проверьте его состояние и ключ администратора."
            )
    except httpx.HTTPError as error:
        raise GatewayError(
            "Gateway недоступен. Сохранённые настройки можно применить позже."
        ) from error


async def google_action(state_dir, operation, account, **options):
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        google = GoogleAuth(CredentialStore(state_dir), client)
        if operation == "configure":
            await google.configure(account, options["client_id"], options["client_secret"])
        elif operation in {"login", "login_cli"}:
            if operation == "login_cli":
                await google.configure_cli(account)
            await google.login(
                account,
                lambda url: console.print(url, markup=False, soft_wrap=True),
                consent=options.get("consent", False),
                read_code=lambda: input_text_async("Код Google из браузера", password=True),
            )
            console.print("Google-аккаунт подключён.")
        elif operation == "logout":
            if not await google.logout(account):
                console.print("Локальная сессия удалена. Отзыв Google не подтверждён.")
        elif operation == "access":
            manager = ColabManager(google.store, client)
            specs = await manager.api.specs(account)
            console.print("Подключение к Colab подтверждено.")
            console.print(json.dumps(specs, ensure_ascii=False, indent=2), markup=False)


def show_google_accounts(state_dir):
    table = Table("Google-профиль", "Email", "Состояние")
    for record in CredentialStore(state_dir).all_accounts():
        if record["provider"] == "google_colab":
            public = AuthManager.public_account(record)
            table.add_row(public["account"], public.get("email") or "—", public["state"])
    console.print(table)


def manage_google(state_dir):
    def show_accounts():
        console.print(
            "Вход через официальное приложение Colab CLI: откройте ссылку в браузере "
            "и вставьте код Google сюда. Собственный Google проект не требуется."
        )
        show_google_accounts(state_dir)

    menu_loop(
        [
            ("login_cli", "Войти в Google через Colab CLI"),
            ("access", "Проверить подключение к Colab"),
            ("logout", "Выйти из Google"),
            ("configure", "Дополнительно: свой Desktop OAuth-клиент для Colab API beta"),
            ("login", "Дополнительно: вход через свой OAuth-клиент"),
        ],
        "Google-аккаунт",
        lambda operation: google_form(state_dir, operation),
        before=show_accounts,
    )


def google_form(state_dir, operation):
    account = ask("Имя Google-аккаунта", default="google-main")
    options = {}
    if operation == "configure":
        path = ask(
            "Путь к JSON Desktop OAuth-клиента внутри контейнера (Enter — ввод вручную)", default=""
        )
        if path:
            with Path(path).open() as source:
                data = json.load(source)
            installed = data.get("installed") if isinstance(data, dict) else None
            if (
                not isinstance(installed, dict)
                or not isinstance(installed.get("client_id"), str)
                or not isinstance(installed.get("client_secret", ""), str)
            ):
                raise GatewayError("Нужен JSON OAuth-клиента типа Desktop, с разделом installed.")
            options = {
                "client_id": installed["client_id"],
                "client_secret": installed.get("client_secret", ""),
            }
        else:
            options["client_id"] = ask("Google Desktop client_id")
            options["client_secret"] = ask("Google client_secret", password=True)
    elif operation == "login":
        options["consent"] = confirm("Повторно запросить согласие Google?", default=False)
    asyncio.run(google_action(state_dir, operation, account, **options))


async def available_specs(state_dir, google_account):
    async with httpx.AsyncClient(follow_redirects=False) as client:
        manager = ColabManager(CredentialStore(state_dir), client)
        return await manager.api.specs(google_account)


async def save_deployment(state_dir, account, settings, hf_token):
    async with httpx.AsyncClient() as client:
        await ColabManager(CredentialStore(state_dir), client).configure(
            account, settings, hf_token
        )


def configure_deployment(config, config_path, state_dir, *, name=None):
    store = CredentialStore(state_dir)
    accounts = [
        record["account"] for record in store.all_accounts() if record["provider"] == "google_colab"
    ]
    if not accounts:
        raise GatewayError("Сначала настройте Google-аккаунт и выполните вход.")
    if name is None:
        name = ask("Имя нового подключения Colab", default="colab-gpu")
        if name in config.backends:
            raise GatewayError("Подключение уже существует. Выберите его в списке для настройки.")
    current = config.backends.get(name)
    if current and current.provider != "colab_managed":
        raise GatewayError("Это имя занято другим типом подключения.")
    account = current.account if current else name
    record = store.get(account_key(account))
    if record and record.get("provider") != "colab_managed":
        raise GatewayError("Имя профиля уже занято другим провайдером. Выберите другое имя.")
    old = DeploymentSettings.model_validate(record["settings"]) if record else None
    google_account = ask(
        "Google-аккаунт", choices=accounts, default=old.google_account if old else accounts[0]
    )
    specs = asyncio.run(available_specs(state_dir, google_account))
    eligible = [
        item["key"]
        for item in specs
        if item.get("eligible") and item["key"].get("variant") in {"VARIANT_GPU", "VARIANT_CPU"}
    ]
    if not eligible:
        raise GatewayError("Нет доступных CPU/GPU runtime. Проверьте квоту аккаунта.")
    table = Table("№", "Тип", "GPU", "Память")
    for index, spec in enumerate(eligible):
        table.add_row(
            str(index + 1),
            "GPU" if spec["variant"] == "VARIANT_GPU" else "CPU",
            spec["accelerator"],
            "Увеличенная RAM" if spec["shape"] == "SHAPE_HIGHMEM" else "Стандартная",
        )
    console.print(table)
    cli_transport = store.get(account_key(google_account)).get("auth_transport") == "cli"
    if cli_transport:
        console.print("Доступность выбранного GPU и RAM проверяется Colab при запуске.")
    default_spec = (
        old.spec()
        if old
        else {"variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"}
    )
    default_index = next((i for i, spec in enumerate(eligible) if spec == default_spec), 0)
    choice = ask(
        "Runtime",
        choices=[str(index + 1) for index in range(len(eligible))],
        default=str(default_index + 1),
    )
    spec = eligible[int(choice) - 1]
    settings = DeploymentSettings(
        google_account=google_account,
        **{"variant": spec["variant"], "accelerator": spec["accelerator"], "shape": spec["shape"]},
        runtime_version=""
        if cli_transport
        else ask("Образ Colab (Enter — актуальный)", default=old.runtime_version if old else ""),
        model_repo=ask(
            "Hugging Face модель (safetensors и chat template)",
            default=old.model_repo if old else "Qwen/Qwen2.5-1.5B-Instruct",
        ),
        revision=ask("Ревизия модели", default=old.revision if old else "main"),
        precision=ask(
            "Точность весов",
            choices=["auto", "float16", "bfloat16", "float32"],
            default=old.precision if old else "auto",
        ),
        port=integer("Порт модели внутри runtime", default=old.port if old else 8000),
        max_context=integer(
            "Максимальный контекст, токенов", default=old.max_context if old else 4096
        ),
        max_new_tokens=integer(
            "Максимальный ответ по умолчанию, токенов", default=old.max_new_tokens if old else 256
        ),
        setup_timeout=integer(
            "Лимит установки и скачивания, секунд", default=old.setup_timeout if old else 3600
        ),
        startup_timeout=integer(
            "Лимит загрузки модели, секунд", default=old.startup_timeout if old else 600
        ),
    )
    hf_token = ask("Hugging Face token (Enter — сохранить, - удалить)", password=True)
    hf_token = None if not hf_token else ("" if hf_token == "-" else hf_token)
    timeout = number(
        "Таймаут ответа через транспорт Colab, секунд",
        default=current.timeout_seconds if current else 180,
    )
    alias = ask(
        "Имя модели для агентов СПАС",
        default=next(
            (alias for alias, backends in config.routes.items() if backends == [name]),
            f"{name}-default",
        ),
    )
    if alias in config.routes and config.routes[alias] != [name]:
        if not confirm("Заменить существующий маршрут?", default=False):
            return
    updated = config.model_dump()
    updated["backends"][name] = BackendConfig(
        provider="colab_managed",
        account=account,
        model=settings.model_repo,
        timeout_seconds=timeout,
        max_concurrency=current.max_concurrency if current else 1,
    ).model_dump()
    updated["routes"][alias] = [name]
    validated = GatewayConfig.model_validate(updated)
    asyncio.run(save_deployment(state_dir, account, settings, hf_token))
    save_config(validated, config_path)
    console.print(
        "Настройки сохранены. Изменения модели применяются через «Полный запуск / восстановление»."
    )


async def deployment_action(state_dir, account, operation):
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        manager = ColabManager(CredentialStore(state_dir), client)
        if operation == "start":
            await manager.start(account, announce)
        elif operation == "stop":
            await manager.stop(account)
        elif operation == "delete":
            await manager.delete(account)
        elif operation == "status":
            console.print(
                json.dumps(await manager.status(account), ensure_ascii=False, indent=2),
                markup=False,
            )


def managed_backends(config):
    return {
        name: backend
        for name, backend in config.backends.items()
        if backend.provider == "colab_managed"
    }


STAGE_LABELS = {
    "CONFIGURED": "Настроен",
    "READY": "Готов",
    "STOPPED": "Модель остановлена",
    "DELETED": "Runtime удалён",
    "FAILED": "Ошибка запуска",
    "EXPIRED": "Runtime истёк",
    "INTERRUPTED": "Запуск прерван",
    **STAGES,
}


def show_deployments(config_path, state_dir, *, name=None):
    store = CredentialStore(state_dir)
    config = GatewayConfig.load(config_path)
    table = Table("Подключение", "Модель", "GPU", "Состояние", "Маршруты")
    for key, backend in managed_backends(config).items():
        if name is not None and key != name:
            continue
        record = store.get(account_key(backend.account)) or {}
        settings = record.get("settings", {})
        stage = record.get("stage")
        table.add_row(
            key,
            backend.model,
            settings.get("accelerator", "—"),
            STAGE_LABELS.get(stage, stage or "Не настроен"),
            ", ".join(alias for alias, backends in config.routes.items() if key in backends) or "—",
        )
    console.print(table)


def manage_full_colab(config, config_path, state_dir):
    def options():
        backends = managed_backends(GatewayConfig.load(config_path))
        return [
            *(("connection:" + name, "Открыть " + name) for name in backends),
            ("google", "Google-аккаунты"),
            ("create", "Создать подключение Colab"),
            ("apply", "Применить настройки в Gateway"),
        ]

    menu_loop(
        options,
        "Google Colab",
        lambda operation: full_colab_action(
            GatewayConfig.load(config_path), config_path, state_dir, operation
        ),
        before=lambda: show_deployments(config_path, state_dir),
    )


def manage_connection(name, config_path, state_dir):
    def options():
        config = GatewayConfig.load(config_path)
        backend = managed_backends(config).get(name)
        if backend is None:
            raise Back()
        record = CredentialStore(state_dir).get(account_key(backend.account)) or {}
        active = bool(
            record.get("runtime_name") or record.get("request_id") or record.get("operation")
        )
        return [
            ("configure", "Модель, GPU и параметры подключения"),
            ("start", "Полный запуск / восстановление — runtime, скачивание и сервер"),
            ("status", "Состояние runtime и модели"),
            ("models", "Каталог загруженных моделей"),
            ("probe", "Проверочный запрос, обычный или stream"),
            *(
                [
                    ("stop", "Остановить модель, сохранить runtime"),
                    ("delete", "Удалить runtime и освободить GPU"),
                ]
                if active
                else []
            ),
            ("remove", "Удалить подключение"),
            ("apply", "Применить настройки в Gateway"),
        ]

    menu_loop(
        options,
        "Colab / " + name,
        lambda operation: full_colab_action(
            GatewayConfig.load(config_path), config_path, state_dir, operation, name=name
        ),
        before=lambda: show_deployments(config_path, state_dir, name=name),
    )


def full_colab_action(config, config_path, state_dir, operation, *, name=None):
    if operation.startswith("connection:"):
        manage_connection(operation.split(":", 1)[1], config_path, state_dir)
        return
    if operation == "google":
        manage_google(state_dir)
        return
    if operation in {"create", "configure"}:
        configure_deployment(config, config_path, state_dir, name=name)
        return
    if operation == "apply":
        asyncio.run(apply_gateway(state_dir))
        console.print("Gateway применил настройки.")
        return
    backends = managed_backends(config)
    if not backends:
        raise GatewayError("Сначала настройте управляемое подключение Colab.")
    if name not in backends:
        raise GatewayError("Выберите подключение Colab в списке.")
    if operation == "remove":
        if any(
            backend.account == backends[name].account
            for key, backend in backends.items()
            if key != name
        ):
            raise GatewayError(
                "Профиль runtime используется ещё одним подключением. "
                "Сначала измените его настройки."
            )
        if not confirm("Удалить подключение, его runtime, модель и файлы?", default=False):
            return
        asyncio.run(deployment_action(state_dir, backends[name].account, "delete"))
        updated = config.model_dump()
        del updated["backends"][name]
        updated["routes"] = {
            alias: [item for item in items if item != name]
            for alias, items in config.routes.items()
            if any(item != name for item in items)
        }
        save_config(GatewayConfig.model_validate(updated), config_path)
        asyncio.run(apply_gateway(state_dir))
        console.print("Подключение и его runtime удалены.")
        return
    if operation == "delete" and not confirm(
        "Удалить runtime вместе с моделью и файлами?", default=False
    ):
        return
    if operation in {"start", "stop", "delete", "status"}:
        asyncio.run(deployment_action(state_dir, backends[name].account, operation))
        if operation == "start":
            try:
                asyncio.run(apply_gateway(state_dir))
            except GatewayError as error:
                raise GatewayError(
                    "Модель запущена. Настройки Gateway пока не применены: " + error.message,
                    code="gateway_reload_pending",
                ) from error
            console.print("Маршрут модели применён в Gateway.")
    elif operation == "models":
        execute(config_path, state_dir, "models", backend=name)
    elif operation == "probe":
        execute(
            config_path,
            state_dir,
            "probe",
            backend=name,
            stream=confirm("Потоковый ответ?", default=False),
            prompt=ask("Проверочный запрос", default="Ответь ровно одним словом: СПАС"),
        )
