import argparse
import asyncio
import json
import math
from collections import deque
from pathlib import Path

import httpx
from prompt_toolkit.validation import Validator
from rich import box
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .auth.credentials import CredentialStore
from .auth.manager import AuthManager
from .cli import console, perform, save_config
from .config import BackendConfig, GatewayConfig
from .core.errors import GatewayError
from .main import GatewayKeys
from .presentation import AUTH_STATES as STATES
from .presentation import RESULTS, RETURN_PROMPT
from .terminal import Back, input_text, select

MODES = {
    "1": ("Подписки ChatGPT и Claude", "Доступ к моделям по подписке"),
    "2": ("API моделей", "Облачные и локальные API"),
    "3": ("Google Colab", "Запуск моделей на CPU и GPU"),
}
MODE_PROVIDERS = {
    "1": ("codex_subscription", "claude_subscription"),
    "2": ("openai_compatible", "openai_api"),
    "3": ("colab_managed",),
}


MODE_COLORS = {"1": "bright_cyan", "2": "bright_yellow", "3": "bright_magenta"}
STATE_COLORS = {
    "CONNECTED": "bright_green",
    "DISCONNECTED": "bright_white",
    "EXPIRED": "bright_yellow",
    "RATE_LIMITED": "bright_yellow",
    "REAUTH_REQUIRED": "bright_red",
}
UI_WIDTH = 112


def ui_print(content) -> None:
    console.print(Padding(content, (0, 2)), width=min(console.width, UI_WIDTH + 4))


def page_header(page: str) -> None:
    brand = Text.assemble(("СПАС", "bold bright_cyan"), ("  /  Центр моделей", "bright_white"))
    location = Text("Главная", style="bold bright_white")
    if page != "home":
        location = Text.assemble(
            ("Главная  /  ", "bright_white"),
            (
                "Тестирование"
                if page == "t"
                else "Настройки шлюза"
                if page == "g"
                else MODES[page][0],
                "bold bright_white",
            ),
        )
    if console.width < 112 and page == "home":
        brand.append("  ·  Главная", style="bright_white")
        ui_print(Group(brand, Rule(style="bright_black"), Text("")))
    else:
        ui_print(Group(Text(""), brand, Rule(style="bright_black"), location, Text("")))


def show_home(config: GatewayConfig, selected: str = "1") -> None:
    ui_print(Text("Подключения к моделям", style="bold bright_white"))
    ui_print(Text("Настройте доступ или откройте «Тестирование».", style="bright_white"))
    ui_print(Text(""))
    width = min(console.width - 4, UI_WIDTH)
    horizontal = width >= 108
    cards = []
    for key, (label, description) in MODES.items():
        count = len(mode_config(config, key).backends)
        color = MODE_COLORS[key]
        status = Text.assemble(
            ("●  " if count else "○  ", color if count else "bright_white"),
            (f"Подключений: {count}", "bright_white" if count else "bright_white"),
        )
        body = [Text(description, style="bright_white")]
        if horizontal:
            body.append(Text(""))
        body.append(status)
        cards.append(
            Panel(
                Group(*body),
                title=Text.assemble(
                    (" ▶ " if selected == key else "   ", f"bold {color}"),
                    (f" {label} ", "bold bright_white"),
                ),
                style="on #1d3441" if selected == key else "",
                title_align="left",
                border_style=color,
                padding=(1, 1) if horizontal else (0, 1),
            )
        )
    if horizontal:
        grid = Table.grid(expand=True, padding=(0, 1))
        for _ in cards:
            grid.add_column(ratio=1)
        grid.add_row(*cards)
        ui_print(grid)
    else:
        ui_print(Group(*cards))
    ui_print(Text(""))
    ui_print(
        Panel(
            Text.assemble(
                (" ▶ " if selected == "t" else "   ", "bold bright_cyan"),
                (" Тестирование", "bold bright_white"),
                ("   Отправить запрос выбранной модели", "bright_white"),
            ),
            border_style="bright_cyan" if selected == "t" else "bright_black",
            style="on #1d3441" if selected == "t" else "",
            padding=(0, 1),
        )
    )
    ui_print(
        Panel(
            Text.assemble(
                (" ▶ " if selected == "g" else "   ", "bold bright_cyan"),
                (" Настройки шлюза", "bold bright_white"),
                ("   Маршруты · Ключи доступа · Журнал", "bright_white"),
            ),
            box=box.ROUNDED,
            border_style="bright_cyan" if selected == "g" else "bright_black",
            style="on #1d3441" if selected == "g" else "",
            padding=(0, 1),
        )
    )
    ui_print(Text("↑↓ / ←→  Выбор   Enter  Выбрать   Esc / Ctrl+D  Закрыть", style="bright_white"))
    if horizontal:
        ui_print(Text(""))


def menu(
    options: list[tuple[str, str]],
    *,
    default: str | None = None,
    title: str = "Выберите действие",
    home: GatewayConfig | None = None,
) -> str:
    def render(selected: str) -> str:
        with console.capture() as captured:
            if home is not None:
                show_home(home, selected)
            else:
                table = Table.grid(expand=True, padding=(0, 1))
                table.add_column(width=2, no_wrap=True)
                table.add_column(ratio=1)
                for key, label in options:
                    active = key == selected
                    style = "bold bright_white on #1d3441" if active else "bright_white"
                    table.add_row(
                        Text("▶" if active else " ", style="bold bright_cyan"),
                        Text(label, style=style),
                        style="on #1d3441" if active else "",
                    )
                ui_print(
                    Panel(
                        table,
                        title=Text(title, style="bold bright_white"),
                        title_align="left",
                        border_style="bright_black",
                    )
                )
                ui_print(
                    Text(
                        "↑↓  Выбор   Enter  Выбрать   Esc  Назад   Ctrl+D  Закрыть",
                        style="bright_white",
                    )
                )
        return captured.get()

    return select([key for key, _ in options], default, render)


def ask(
    label: str,
    *,
    choices: list[str] | None = None,
    default: str = "",
    password: bool = False,
) -> str:
    if choices is not None:
        labels = {
            "codex_subscription": "Подписка ChatGPT",
            "claude_subscription": "Подписка Claude",
            "openai_api": "OpenAI API",
            "openai_compatible": "OpenAI-совместимый API",
            "chat_completions": "Chat Completions — чат через /chat/completions",
            "responses": "Responses — запросы через /responses",
            "decisions": "Decisions — решения через /decisions",
            "systemone": "System One — решения через /systemone",
            "chatgpt": "ChatGPT",
            "claude": "Claude",
            "cli": "Claude CLI",
            "sdk": "Claude Agent SDK",
            "client": "Ключ агента",
            "admin": "Ключ администратора",
            "auto": "Автоматически",
            "chat": "Текстовая модель (LLM)",
            "nimble": "Модель решений Nimble",
            "float16": "FP16 — 16 бит",
            "bfloat16": "BF16 — 16 бит",
            "float32": "FP32 — 32 бита",
            "on": "Включён",
            "off": "Выключен",
        }
        return menu(
            [(value, labels.get(value, value)) for value in choices], default=default, title=label
        )
    return input_text(label, default=default, password=password)


def confirm(label: str, *, default: bool = False) -> bool:
    return (
        menu([("yes", "Да"), ("no", "Нет")], default="yes" if default else "no", title=label)
        == "yes"
    )


def number(
    label: str,
    *,
    default: float,
    integer: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | int:
    def valid(value: str) -> bool:
        try:
            result = int(value) if integer else float(value)
            return (
                math.isfinite(result)
                and (minimum is None or result >= minimum)
                and (maximum is None or result <= maximum)
            )
        except (ValueError, OverflowError):
            return False

    message = "Введите целое число" if integer else "Введите число"
    if minimum is not None and maximum is not None:
        message += f" от {minimum:g} до {maximum:g}"
    value = input_text(
        label,
        default=str(default),
        validator=Validator.from_callable(
            valid,
            error_message=message,
        ),
    )
    return int(value) if integer else float(value)


def mode_config(config: GatewayConfig, mode: str) -> GatewayConfig:
    backends = {
        name: backend
        for name, backend in config.backends.items()
        if backend.provider in MODE_PROVIDERS[mode]
    }
    routes = {
        alias: names
        for alias, names in config.routes.items()
        if all(name in backends for name in names)
    }
    return config.model_copy(update={"backends": backends, "routes": routes})


def show_connections(config: GatewayConfig, state_dir: Path) -> None:
    if not config.backends:
        ui_print(
            Panel(
                Text(
                    "Подключений пока нет. Добавьте аккаунт и настройте модель.",
                    style="bright_white",
                ),
                title=Text("Подключения", style="bold bright_white"),
                title_align="left",
                border_style="bright_black",
            )
        )
        return
    accounts = {
        record["account"]: AuthManager.public_account(record)
        for record in CredentialStore(state_dir).all_accounts()
    }
    table = Table(
        "Подключение",
        "Аккаунт",
        "Модель",
        "Состояние",
        box=box.SIMPLE,
        header_style="bold bright_white",
        border_style="bright_black",
        padding=(0, 1),
        expand=True,
    )
    for name, backend in config.backends.items():
        account = accounts.get(backend.account)
        state = account["state"] if account else "DISCONNECTED"
        table.add_row(
            Text(name, style="bold bright_white"),
            Text(backend.account, style="bright_white"),
            Text(backend.model, style="bright_cyan"),
            Text(
                "● " + STATES.get(state, "Нет данных"),
                style=STATE_COLORS.get(state, "bright_white"),
            ),
        )
    ui_print(
        Panel(
            table,
            title=Text("Подключения", style="bold bright_white"),
            title_align="left",
            border_style="bright_black",
        )
    )
    ui_print(
        Text("Проверить ответ модели можно в главном меню: «Тестирование».", style="bright_white")
    )
    ui_print(Text(""))


def execute(config_path: Path, state_dir: Path, command: str, **options) -> None:
    if command in {"models", "status", "probe"}:
        options["pretty"] = True
    asyncio.run(perform(argparse.Namespace(command=command, **options), config_path, state_dir))


def manage_accounts(config_path: Path, state_dir: Path, mode: str | None = None) -> None:
    options = []
    if mode in {None, "1"}:
        options.extend(
            [
                ("login", "Подключить аккаунт ChatGPT или Claude"),
                ("import-claude", "Импортировать профиль Claude"),
            ]
        )
    if mode in {None, "2"}:
        options.append(("api-key", "Сохранить API-ключ"))
    options.extend([("logout", "Выйти из аккаунта"), ("accounts", "Список аккаунтов")])
    while True:
        operation = menu(options, default="accounts")
        try:
            records = CredentialStore(state_dir).all_accounts()
            if mode is not None:
                records = [
                    record for record in records if record["provider"] in MODE_PROVIDERS[mode]
                ]
            if operation == "accounts" and mode is not None:
                table = Table("Аккаунт", "Состояние")
                for record in records:
                    account = AuthManager.public_account(record)
                    table.add_row(
                        Text(account["account"]),
                        Text(STATES.get(account["state"], "Нет данных")),
                    )
                console.print(
                    table
                    if records
                    else "Аккаунтов пока нет. Подключите аккаунт ChatGPT или Claude."
                )
                return
            arguments = {}
            if operation == "login":
                provider = ask("Сервис", choices=["chatgpt", "claude"], default="claude")
                arguments["provider"] = provider
                arguments["account"] = ask("Имя аккаунта в СПАС", default=f"{provider}-main")
                arguments["consent"] = (
                    provider == "chatgpt"
                    and any(r["account"] == arguments["account"] for r in records)
                    and confirm(
                        "Повторно запросить согласие на использование подписки?", default=False
                    )
                )
            elif operation == "logout":
                if not records:
                    console.print("Аккаунтов пока нет.")
                    return
                arguments["account"] = ask("Аккаунт", choices=[r["account"] for r in records])
            elif operation != "accounts":
                arguments["account"] = ask(
                    "Имя аккаунта в СПАС",
                    default="openai-paid" if operation == "api-key" else "claude-main",
                )
            if operation == "import-claude":
                arguments["source"] = Path(ask("Путь к профилю Claude в контейнере"))
            if operation == "api-key":
                key = ask("API-ключ", password=True)
                asyncio.run(save_paid_key(state_dir, arguments["account"], key))
                console.print("API-ключ сохранён в зашифрованном виде.")
            else:
                execute(config_path, state_dir, operation, **arguments)
        except Back:
            continue
        return


def configure_backend(
    config: GatewayConfig, config_path: Path, mode: str | None = None, *, advanced: bool = False
) -> None:
    visible = mode_config(config, mode) if mode else config
    console.print("Подключения: " + (", ".join(visible.backends) or "пока нет"), markup=False)
    name = ask("Название подключения")
    current = config.backends.get(name)
    if current and mode and current.provider not in MODE_PROVIDERS[mode]:
        raise GatewayError("Это название уже используется в другом разделе. Выберите другое.")
    if current and current.provider in {"remote_inference", "colab_managed"}:
        console.print("Настройки Colab находятся в разделе Google Colab.")
        return
    providers = (
        list(MODE_PROVIDERS[mode])
        if mode
        else ["codex_subscription", "claude_subscription", "openai_api"]
    )
    provider = (
        (current.provider if current else "openai_api")
        if mode == "2"
        else providers[0]
        if len(providers) == 1
        else ask(
            "Сервис",
            choices=providers,
            default=current.provider if current else "claude_subscription",
        )
    )
    defaults = {
        "codex_subscription": ("chatgpt-main", "gpt-6.1-sol"),
        "claude_subscription": ("claude-main", "sonnet"),
        "openai_api": ("openai-paid", "gpt-6.1-sol"),
        "openai_compatible": ("api-main", ""),
    }
    account_default, model_default = defaults[provider]
    same_provider = current is not None and current.provider == provider
    account = ask("Аккаунт", default=current.account if same_provider else account_default)
    model = ask("Модель", default=current.model if same_provider else model_default)
    transport = current.transport if same_provider else "cli"
    if advanced and provider == "claude_subscription":
        transport = ask(
            "Способ подключения к Claude",
            choices=["cli", "sdk"],
            default=current.transport if current else "cli",
        )
    timeout = (
        number(
            "Время ожидания ответа, с",
            default=current.timeout_seconds if current else 180,
            minimum=1,
            maximum=3600,
        )
        if advanced
        else current.timeout_seconds
        if current
        else 180
    )
    concurrency = (
        number(
            "Максимум одновременных запросов",
            default=current.max_concurrency if current else 1,
            integer=True,
            minimum=1,
            maximum=32,
        )
        if advanced
        else current.max_concurrency
        if current
        else 1
    )
    settings = current.model_dump() if same_provider else {}
    settings.update(
        provider=provider,
        account=account,
        model=model,
        transport=transport,
        timeout_seconds=timeout,
        max_concurrency=concurrency,
    )
    backend = BackendConfig.model_validate(settings)
    updated = config.model_dump()
    updated["backends"][name] = backend.model_dump()
    save_config(GatewayConfig.model_validate(updated), config_path, expected=config)
    console.print("Подключение сохранено. Маршрут можно добавить в разделе «Настройки шлюза».")
    console.print("Перезапустите шлюз, чтобы применить изменения.")


def manage_models(
    config: GatewayConfig, config_path: Path, state_dir: Path, mode: str | None = None
) -> None:
    while True:
        operation = menu(
            [
                ("catalog", "Каталог моделей"),
                ("configure", "Настроить подключение"),
                ("parameters", "Расширенные параметры"),
            ]
        )
        try:
            if operation in {"configure", "parameters"}:
                configure_backend(config, config_path, mode, advanced=operation == "parameters")
            else:
                visible = mode_config(config, mode) if mode else config
                if not visible.backends:
                    console.print("Сначала настройте подключение.")
                    return
                backend = ask("Подключение", choices=list(visible.backends))
                execute(config_path, state_dir, "models", backend=backend)
        except Back:
            continue
        return


def manage_routes(config: GatewayConfig, config_path: Path) -> None:
    table = Table("Модель в шлюзе", "Подключения по порядку")
    for alias, backends in config.routes.items():
        table.add_row(Text(alias), Text(" → ".join(backends)))
    console.print(table if config.routes else "Маршрутов пока нет. Добавьте имя модели ниже.")
    console.print("Основное подключение укажите первым, резервные — после него.")
    alias = ask("Имя модели в шлюзе (Enter — оставить маршруты)", default="")
    updated = config.model_dump()
    if alias:
        names = ask("Подключения по порядку, через запятую").split(",")
        updated["routes"][alias] = [name.strip() for name in names]
    console.print(
        "Ошибки: rate_limited — лимит, provider_unavailable — недоступность, "
        "provider_timeout — время ожидания."
    )
    errors = ask(
        "Ошибки для перехода на резерв, через запятую (пусто — выключить)",
        default=",".join(config.fallback_on),
    )
    updated["fallback_on"] = [name.strip() for name in errors.split(",") if name.strip()]
    save_config(GatewayConfig.model_validate(updated), config_path, expected=config)
    console.print("Маршруты сохранены. Перезапустите шлюз, чтобы применить изменения.")


def probe(config: GatewayConfig, config_path: Path, state_dir: Path) -> None:
    if not config.backends:
        console.print("Сначала настройте подключение.")
        return
    backend = ask(
        "Подключение",
        choices=list(config.backends),
    )
    selected = config.backends[backend]
    if selected.decision_model:
        from .decision_tui import run_test

        run_test(config_path, state_dir, backend)
        return
    prompt = ask("Текст запроса")
    if not prompt.strip():
        raise GatewayError("Введите текст запроса.", status=400)
    stream = (
        selected.api_streaming
        if selected.provider in {"openai_api", "openai_compatible"}
        else selected.remote_streaming
        if selected.provider in {"remote_inference", "colab_managed"}
        else True
    )
    execute(config_path, state_dir, "probe", backend=backend, prompt=prompt, stream=stream)


def ping(config: GatewayConfig, config_path: Path, state_dir: Path) -> None:
    if not config.backends:
        console.print("Сначала настройте подключение.")
        return
    backend = ask("Подключение", choices=list(config.backends))
    execute(config_path, state_dir, "status", backend=backend)


def show_key(state_dir: Path) -> None:
    keys = GatewayKeys.load(CredentialStore(state_dir))
    role = ask("Ключ доступа к шлюзу", choices=["client", "admin"], default="client")
    value = keys.client if role == "client" else keys.admin
    if value is None:
        raise GatewayError("Ключ администратора не настроен.")
    console.print(value, markup=False)


async def save_paid_key(state_dir: Path, account: str, api_key: str) -> None:
    async with httpx.AsyncClient() as client:
        await AuthManager(CredentialStore(state_dir), client).save_api_key(account, api_key)


def show_logs(state_dir: Path) -> None:
    path = state_dir / "requests.jsonl"
    rows = []
    if path.exists():
        with path.open() as source:
            for line in deque(source, maxlen=20):
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        rows.append(data)
                except ValueError:
                    continue
    if not rows:
        console.print("В журнале пока нет запросов. Отправьте запрос из раздела «Тестирование».")
        return
    table = Table("Подключение", "Модель", "Результат", "Время", "Токены")
    for row in rows:
        usage = row.get("usage") or {}
        total = usage.get("total_tokens")
        if total is None and "prompt_tokens" in usage and "completion_tokens" in usage:
            total = usage["prompt_tokens"] + usage["completion_tokens"]
        latency = row.get("latency_ms")
        table.add_row(
            Text(str(row.get("backend", "—"))),
            Text(str(row.get("model", "—"))),
            Text(RESULTS.get(row.get("status"), "Ошибка запроса")),
            f"{latency} мс" if latency is not None else "—",
            str(total) if total is not None else "—",
        )
    console.print(table)


def run_tui(config_path: Path, state_dir: Path) -> None:
    page = "home"
    while True:
        config = GatewayConfig.load(config_path)
        console.clear()
        page_header(page)
        try:
            if page in {"2", "3"}:
                try:
                    if page == "2":
                        from .api_tui import manage_api

                        manage_api(config_path, state_dir)
                    else:
                        from .colab.tui import manage_full_colab

                        manage_full_colab(config, config_path, state_dir)
                except Back:
                    pass
                page = "home"
                continue
            if page == "home":
                try:
                    action = menu(
                        [
                            *((key, label) for key, (label, _) in MODES.items()),
                            ("t", "Тестирование"),
                            ("g", "Настройки шлюза"),
                        ],
                        default="1",
                        home=config,
                    )
                except Back:
                    return
                if action != "t":
                    page = action
                    continue
                console.clear()
                page_header("t")
                probe(config, config_path, state_dir)
            else:
                try:
                    if page == "g":
                        action = menu(
                            [
                                ("r", "Маршруты и резервные подключения"),
                                ("k", "Ключи доступа"),
                                ("l", "Журнал запросов"),
                            ]
                        )
                    else:
                        visible = mode_config(config, page)
                        ui_print(Text(MODES[page][1], style="bright_white"))
                        ui_print(Text(""))
                        show_connections(visible, state_dir)
                        action = menu(
                            [
                                ("a", "Аккаунты и вход"),
                                ("m", "Модели и подключения"),
                                ("p", "Пинг подключения"),
                            ]
                        )
                except Back:
                    page = "home"
                    continue
                if action == "a":
                    manage_accounts(config_path, state_dir, page)
                elif action == "m":
                    manage_models(config, config_path, state_dir, page)
                elif action == "r":
                    manage_routes(config, config_path)
                elif action == "p":
                    ping(visible, config_path, state_dir)
                elif action == "k":
                    show_key(state_dir)
                elif action == "l":
                    show_logs(state_dir)
        except Back:
            continue
        except EOFError:
            return
        except KeyboardInterrupt:
            console.print("\nДействие отменено.")
        except GatewayError as error:
            console.print(error.message, markup=False, style="bright_red")
        except (OSError, ValueError):
            console.print(
                "Не удалось выполнить действие. Проверьте путь к файлу и настройки подключения.",
                style="bright_red",
            )
        try:
            ask(RETURN_PROMPT, default="")
        except (Back, KeyboardInterrupt):
            pass
        except EOFError:
            return
