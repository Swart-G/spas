import asyncio
import json
from pathlib import Path

import httpx
from rich.table import Table
from rich.text import Text

from ..auth.credentials import CredentialStore
from ..auth.google import GoogleAuth
from ..auth.manager import AuthManager, account_key
from ..cli import apply_gateway, console, save_config
from ..config import BackendConfig, GatewayConfig
from ..core.errors import GatewayError
from ..presentation import AUTH_STATES, RETURN_PROMPT, STAGES, show_status
from ..terminal import Back, input_text_async
from ..tui import ask, confirm, execute, menu, number, page_header
from .lifetime import time_label
from .manager import ColabManager, DeploymentSettings
from .progress import StartupDisplay


def integer(label, *, default, **bounds):
    return number(label, default=default, integer=True, **bounds)


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
            console.print(
                "Не удалось выполнить действие. Проверьте файл и настройки Colab.", style="red"
            )
        try:
            ask(RETURN_PROMPT, default="")
        except (Back, KeyboardInterrupt):
            pass


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
            console.print("Аккаунт Google подключён.")
        elif operation == "logout":
            revoked = await google.logout(account)
            console.print("Выход из аккаунта Google выполнен на этом устройстве.")
            if not revoked:
                console.print(
                    "Чтобы завершить доступ в Google, отзовите его в настройках аккаунта."
                )
        elif operation == "access":
            manager = ColabManager(google.store, client)
            specs = await manager.api.specs(account)
            console.print("Подключение к Colab подтверждено.")
            table = Table("Тип среды", "Ускоритель", "Память", "Доступность")
            for spec in specs:
                key = spec.get("key", {})
                accelerator = key.get("accelerator", "NONE")
                table.add_row(
                    "GPU" if key.get("variant") == "VARIANT_GPU" else "CPU",
                    accelerator if accelerator != "NONE" else "—",
                    "Увеличенная" if key.get("shape") == "SHAPE_HIGHMEM" else "Стандартная",
                    "Доступна" if spec.get("eligible") else "Недоступна",
                )
            console.print(table)


def show_google_accounts(state_dir):
    table = Table("Аккаунт Google", "Почта", "Состояние")
    for record in CredentialStore(state_dir).all_accounts():
        if record["provider"] == "google_colab":
            public = AuthManager.public_account(record)
            table.add_row(
                public["account"],
                public.get("email") or "—",
                AUTH_STATES.get(public.get("state"), "Нет подключения"),
            )
    console.print(table if table.row_count else "Аккаунтов пока нет. Подключите аккаунт Google.")


def manage_google(state_dir):
    def show_accounts():
        console.print(
            "Откройте ссылку в браузере, войдите в Google и вставьте полученный код в это окно."
        )
        show_google_accounts(state_dir)

    menu_loop(
        [
            ("login_cli", "Подключить аккаунт Google"),
            ("access", "Проверить подключение к Colab"),
            ("logout", "Выйти из Google"),
            ("configure", "Свой OAuth-клиент (расширенная настройка)"),
            ("login", "Войти через свой OAuth-клиент"),
        ],
        "Аккаунты Google",
        lambda operation: google_form(state_dir, operation),
        before=show_accounts,
    )


def google_form(state_dir, operation):
    account = ask("Имя аккаунта Google в СПАС", default="google-main")
    options = {}
    if operation == "configure":
        path = ask("Файл OAuth-клиента в контейнере (Enter — ввести вручную)", default="")
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
            options["client_id"] = ask("ID OAuth-клиента (client_id)")
            options["client_secret"] = ask("Секрет OAuth-клиента (client_secret)", password=True)
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


def configure_deployment(config, config_path, state_dir, *, name=None, advanced=False):
    store = CredentialStore(state_dir)
    accounts = [
        record["account"] for record in store.all_accounts() if record["provider"] == "google_colab"
    ]
    if not accounts:
        raise GatewayError("Сначала подключите аккаунт в разделе «Аккаунты Google».")
    if name is None:
        name = ask("Название подключения", default="colab-gpu")
        if name in config.backends:
            raise GatewayError("Подключение уже существует. Выберите его в списке для настройки.")
    current = config.backends.get(name)
    if current and current.provider != "colab_managed":
        raise GatewayError("Это имя занято другим типом подключения.")
    account = current.account if current else name
    record = store.get(account_key(account))
    if record and record.get("provider") != "colab_managed":
        raise GatewayError("Это имя профиля уже используется другим сервисом. Выберите другое.")
    old = DeploymentSettings.model_validate(record["settings"]) if record else None
    google_account = ask(
        "Аккаунт Google", choices=accounts, default=old.google_account if old else accounts[0]
    )
    specs = asyncio.run(available_specs(state_dir, google_account))
    eligible = [
        item["key"]
        for item in specs
        if item.get("eligible") and item["key"].get("variant") in {"VARIANT_GPU", "VARIANT_CPU"}
    ]
    if not eligible:
        raise GatewayError("Нет доступных сред Colab. Проверьте квоту аккаунта Google.")
    table = Table("№", "Тип", "Ускоритель", "Оперативная память")
    for index, spec in enumerate(eligible):
        table.add_row(
            str(index + 1),
            "GPU" if spec["variant"] == "VARIANT_GPU" else "CPU",
            spec["accelerator"] if spec["accelerator"] != "NONE" else "—",
            "Увеличенная" if spec["shape"] == "SHAPE_HIGHMEM" else "Стандартная",
        )
    console.print(table)
    cli_transport = store.get(account_key(google_account)).get("auth_transport") == "cli"
    if cli_transport:
        console.print(
            "Colab проверит доступность выбранного ускорителя и объёма памяти при запуске."
        )
    default_spec = (
        old.spec()
        if old
        else {"variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"}
    )
    default_index = next((i for i, spec in enumerate(eligible) if spec == default_spec), 0)
    choice = ask(
        "Среда Colab",
        choices=[str(index + 1) for index in range(len(eligible))],
        default=str(default_index + 1),
    )
    spec = eligible[int(choice) - 1]
    settings_data = old.model_dump() if old else {}
    settings_data.update(
        google_account=google_account,
        **spec,
        model_repo=ask(
            "Репозиторий модели на Hugging Face",
            default=old.model_repo if old else "Qwen/Qwen2.5-1.5B-Instruct",
        ),
    )
    if advanced:
        settings_data["max_context"] = integer(
            "Размер контекста, токенов",
            default=old.max_context if old else 4096,
            minimum=256,
            maximum=131072,
        )
        settings_data.update(
            runtime_version=""
            if cli_transport
            else ask(
                "Образ Colab (Enter — актуальный)", default=old.runtime_version if old else ""
            ),
            revision=ask("Ревизия модели", default=old.revision if old else "main"),
            model_kind=ask(
                "Тип модели",
                choices=["auto", "chat", "nimble"],
                default=old.model_kind if old else "auto",
            ),
            precision=ask(
                "Точность весов",
                choices=["auto", "float16", "bfloat16", "float32"],
                default=old.precision if old else "auto",
            ),
            port=integer(
                "Порт сервера модели",
                default=old.port if old else 8000,
                minimum=1024,
                maximum=65535,
            ),
            max_new_tokens=integer(
                "Длина ответа по умолчанию, токенов",
                default=min(old.max_new_tokens if old else 256, settings_data["max_context"] - 1),
                minimum=1,
                maximum=min(8192, settings_data["max_context"] - 1),
            ),
            setup_timeout=integer(
                "Время ожидания установки, с",
                default=old.setup_timeout if old else 3600,
                minimum=60,
                maximum=14400,
            ),
            startup_timeout=integer(
                "Время ожидания загрузки модели, с",
                default=old.startup_timeout if old else 600,
                minimum=30,
                maximum=3600,
            ),
            download_stall_timeout=integer(
                "Время ожидания прогресса скачивания, с",
                default=old.download_stall_timeout if old else 180,
                minimum=30,
                maximum=1800,
            ),
            download_max_attempts=integer(
                "Максимум попыток скачивания",
                default=old.download_max_attempts if old else 3,
                minimum=1,
                maximum=10,
            ),
            download_retry_backoff=integer(
                "Пауза перед повторным скачиванием, с",
                default=old.download_retry_backoff if old else 5,
                minimum=0,
                maximum=60,
            ),
        )
    settings = DeploymentSettings.model_validate(settings_data)
    hf_token = ask("Токен Hugging Face (Enter — оставить, - — удалить)", password=True)
    hf_token = None if not hf_token else ("" if hf_token == "-" else hf_token)
    timeout = current.timeout_seconds if current else 180
    if advanced:
        timeout = number("Время ожидания ответа, с", default=timeout, minimum=1, maximum=3600)
    alias = ask(
        "Имя модели в шлюзе",
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
        model_kind=settings.model_kind,
        timeout_seconds=timeout,
        max_concurrency=current.max_concurrency if current else 1,
    ).model_dump()
    updated["routes"][alias] = [name]
    validated = GatewayConfig.model_validate(updated)
    save_config(
        validated,
        config_path,
        expected=config,
        before_write=lambda: asyncio.run(save_deployment(state_dir, account, settings, hf_token)),
    )
    console.print("Настройки сохранены. Выберите «Запустить модель», чтобы применить изменения.")


async def deployment_action(state_dir, account, operation, *, progress=None):
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        manager = ColabManager(CredentialStore(state_dir), client)
        if operation == "start":
            await manager.start(account, progress=progress or (lambda event: None))
        elif operation == "stop":
            await manager.stop(account)
        elif operation == "delete":
            await manager.delete(account)
        elif operation == "status":
            show_status(console, await manager.status(account))
        elif operation == "terminal":
            from .terminal import open_terminal

            await open_terminal(manager, account)


def managed_backends(config):
    return {
        name: backend
        for name, backend in config.backends.items()
        if backend.provider == "colab_managed"
    }


STAGE_LABELS = STAGES


def show_deployments(config_path, state_dir, *, name=None):
    store = CredentialStore(state_dir)
    config = GatewayConfig.load(config_path)
    compact = console.width < 112
    table = Table(
        *(
            ["Подключение / модель", "Ресурс", "Состояние", "Маршруты"]
            if compact
            else [
                "Подключение",
                "Модель",
                "Ускоритель",
                "Состояние",
                "Осталось времени",
                "Автозапуск",
                "Маршруты",
            ]
        )
    )
    for column in table.columns:
        column.overflow = "fold"
    for key, backend in managed_backends(config).items():
        if name is not None and key != name:
            continue
        record = store.get(account_key(backend.account)) or {}
        settings = record.get("settings", {})
        stage = record.get("stage")
        resource = (
            "CPU" if settings.get("accelerator") == "NONE" else settings.get("accelerator", "—")
        )
        status = STAGE_LABELS.get(stage, "Нет данных")
        remaining = time_label(record)
        auto = (
            ("Приостановлен" if record.get("auto_creation_paused") else "Включён")
            if settings.get("auto_create_runtime")
            else "Выключен"
        )
        routes = (
            ", ".join(alias for alias, backends in config.routes.items() if key in backends) or "—"
        )
        cells = (
            [
                key + "\n" + backend.model,
                resource,
                status + "\n" + remaining + "\nАвтозапуск: " + auto.lower(),
                routes,
            ]
            if compact
            else [key, backend.model, resource, status, remaining, auto, routes]
        )
        table.add_row(*(Text(value) for value in cells))
    console.print(
        table if table.row_count else "Подключений пока нет. Выберите «Добавить подключение»."
    )


def manage_full_colab(config, config_path, state_dir):
    def options():
        backends = managed_backends(GatewayConfig.load(config_path))
        return [
            *(("connection:" + name, "Открыть " + name) for name in backends),
            ("google", "Аккаунты Google"),
            ("create", "Добавить подключение"),
            ("apply", "Применить настройки"),
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
            ("configure", "Настройки подключения"),
            ("parameters", "Расширенные параметры"),
            ("runtime_policy", "Время работы и автозапуск"),
            ("start", "Запустить модель"),
            ("status", "Пинг и состояние модели"),
            ("models", "Загруженные модели"),
            *(
                [
                    ("terminal", "Открыть терминал Colab"),
                    ("stop", "Остановить модель"),
                    ("delete", "Завершить среду Colab"),
                ]
                if active
                else []
            ),
            ("remove", "Удалить подключение"),
            ("apply", "Применить настройки"),
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
    if operation in {"create", "configure", "parameters"}:
        configure_deployment(
            config, config_path, state_dir, name=name, advanced=operation == "parameters"
        )
        return
    if operation == "runtime_policy":
        backend = config.backends[name]
        record = CredentialStore(state_dir).get(account_key(backend.account)) or {}
        settings = record.get("settings", {})
        console.print(
            "Если среда Colab завершит работу, автозапуск создаст новую и загрузит модель. "
            "После ручной остановки автозапуск приостанавливается. "
            "Оставшееся время приблизительное: Colab может завершить среду раньше."
        )
        enabled = (
            ask(
                "Автозапуск модели",
                choices=["off", "on"],
                default="on" if settings.get("auto_create_runtime") else "off",
            )
            == "on"
        )
        hours = integer(
            "Ожидаемое время работы среды, ч",
            default=settings.get("vm_lifetime_hours", 12),
            minimum=1,
            maximum=168,
        )

        async def save_policy():
            async with httpx.AsyncClient() as client:
                await ColabManager(CredentialStore(state_dir), client).set_runtime_policy(
                    backend.account, enabled=enabled, lifetime_hours=hours
                )

        asyncio.run(save_policy())
        console.print("Настройки автозапуска сохранены и применены.")
        return
    if operation == "apply":
        asyncio.run(apply_gateway(state_dir))
        console.print("Настройки применены.")
        return
    backends = managed_backends(config)
    if not backends:
        raise GatewayError("Сначала добавьте подключение в разделе «Google Colab».")
    if name not in backends:
        raise GatewayError("Выберите подключение Colab в списке.")
    if operation == "remove":
        if any(
            backend.account == backends[name].account
            for key, backend in backends.items()
            if key != name
        ):
            raise GatewayError(
                "Эта среда используется другим подключением. Сначала измените его настройки."
            )
        if not confirm(
            "Удалить подключение и завершить его среду Colab? Модель и файлы среды будут удалены.",
            default=False,
        ):
            return
        asyncio.run(deployment_action(state_dir, backends[name].account, "delete"))
        updated = config.model_dump()
        del updated["backends"][name]
        updated["routes"] = {
            alias: [item for item in items if item != name]
            for alias, items in config.routes.items()
            if any(item != name for item in items)
        }
        save_config(GatewayConfig.model_validate(updated), config_path, expected=config)
        asyncio.run(apply_gateway(state_dir))
        console.print("Подключение удалено. Среда Colab завершена.")
        return
    if operation == "delete" and not confirm(
        "Завершить среду Colab? Модель и файлы в ней будут удалены.", default=False
    ):
        return
    if operation == "start":
        backend = backends[name]
        with StartupDisplay(console, name, backend.model, nimble=backend.decision_model) as display:
            asyncio.run(
                deployment_action(state_dir, backend.account, operation, progress=display.update)
            )
            display.update({"step": "route", "status": "running"})
            try:
                asyncio.run(apply_gateway(state_dir))
            except GatewayError as error:
                raise GatewayError(
                    "Модель запущена, но шлюз пока не обновлён. " + error.message,
                    code="gateway_reload_pending",
                ) from error
            display.update({"step": "route", "status": "done"})
        console.print("Модель запущена. Маршрут в шлюзе настроен.")
    elif operation in {"stop", "delete", "status", "terminal"}:
        asyncio.run(deployment_action(state_dir, backends[name].account, operation))
    elif operation == "models":
        execute(config_path, state_dir, "models", backend=name)
