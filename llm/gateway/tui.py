import argparse
import asyncio
import json
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
from .terminal import Back, input_text, select

MODES = {
    "1": ("Подписки ChatGPT / Claude", "Вход в аккаунт и использование квоты подписки"),
    "2": ("Платный API", "OpenAI API с отдельным ключом и оплатой"),
    "3": ("Google Colab", "Google-аккаунты, GPU и серверы моделей"),
}
MODE_PROVIDERS = {
    "1": ("codex_subscription", "claude_subscription"),
    "2": ("openai_api",),
    "3": ("colab_managed",),
}
STATES = {
    "CONNECTED": "Авторизация сохранена",
    "DISCONNECTED": "Не подключён",
    "EXPIRED": "Сессия истекла",
    "RATE_LIMITED": "Достигнут лимит",
    "REAUTH_REQUIRED": "Нужен повторный вход",
}

MODE_COLORS = {"1": "cyan", "2": "yellow", "3": "magenta"}
STATE_COLORS = {
    "CONNECTED": "green",
    "DISCONNECTED": "grey62",
    "EXPIRED": "yellow",
    "RATE_LIMITED": "yellow",
    "REAUTH_REQUIRED": "red",
}
UI_WIDTH = 112


def ui_print(content) -> None:
    console.print(Padding(content, (0, 2)), width=min(console.width, UI_WIDTH + 4))


def page_header(page: str) -> None:
    brand = Text.assemble(("СПАС", "bold cyan"), ("  /  LLM Gateway", "grey70"))
    location = Text("Главная", style="bold white")
    if page != "home":
        location = Text.assemble(
            ("Главная  /  ", "grey62"),
            ("Gateway" if page == "g" else MODES[page][0], "bold white"),
        )
    if console.width < 112 and page == "home":
        brand.append("  ·  Главная", style="grey70")
        ui_print(Group(brand, Rule(style="grey35"), Text("")))
    else:
        ui_print(Group(Text(""), brand, Rule(style="grey35"), location, Text("")))


def show_home(config: GatewayConfig, selected: str = "1") -> None:
    ui_print(Text("Выберите способ работы с моделями", style="bold white"))
    ui_print(Text("Настройки — внутри выбранного раздела.", style="grey70"))
    ui_print(Text(""))
    width = min(console.width - 4, UI_WIDTH)
    horizontal = width >= 108
    cards = []
    for key, (label, description) in MODES.items():
        count = len(mode_config(config, key).backends)
        color = MODE_COLORS[key]
        status = Text.assemble(
            ("●  " if count else "○  ", color if count else "grey62"),
            (f"Подключений: {count}", "white" if count else "grey62"),
        )
        body = [Text(description, style="grey70")]
        if horizontal:
            body.append(Text(""))
        body.append(status)
        cards.append(
            Panel(
                Group(*body),
                title=Text.assemble(
                    (" ▶ " if selected == key else "   ", f"bold {color}"),
                    (f" {label} ", "bold white"),
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
                (" ▶ " if selected == "g" else "   ", "bold cyan"),
                (" Gateway", "bold white"),
                ("   Маршруты · Ключи доступа · Журнал", "grey70"),
            ),
            box=box.ROUNDED,
            border_style="cyan" if selected == "g" else "grey35",
            style="on #1d3441" if selected == "g" else "",
            padding=(0, 1),
        )
    )
    ui_print(
        Text(
            "▶ Выход" if selected == "q" else "  Выход",
            style="bold cyan on #1d3441" if selected == "q" else "grey62",
        )
    )
    ui_print(Text("↑↓ / ←→  Выбор   Enter  Открыть   Esc  Назад", style="grey70"))
    if horizontal:
        ui_print(Text(""))


def menu(
    options: list[tuple[str, str]],
    *,
    default: str | None = None,
    title: str = "Действия",
    home: GatewayConfig | None = None,
) -> str:
    def render(selected: str) -> str:
        with console.capture() as captured:
            if home is not None:
                show_home(home, selected)
            else:
                table = Table.grid(expand=True, padding=(0, 1))
                table.add_column(width=2, no_wrap=True)
                table.add_column()
                for key, label in options:
                    active = key == selected
                    style = "bold white on #1d3441" if active else "grey70"
                    table.add_row(
                        Text("▶" if active else " ", style="bold cyan"),
                        Text(label, style=style),
                        style="on #1d3441" if active else "",
                    )
                ui_print(Panel(table, title=Text(title), title_align="left", border_style="grey35"))
                ui_print(Text("↑↓  Выбор   Enter  Открыть   Esc  Назад", style="grey70"))
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
            "chatgpt": "ChatGPT",
            "claude": "Claude",
            "cli": "Claude CLI",
            "sdk": "Claude Agent SDK",
            "client": "Ключ агента",
            "admin": "Ключ администратора",
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


def number(label: str, *, default: float, integer: bool = False) -> float | int:
    def valid(value: str) -> bool:
        try:
            int(value) if integer else float(value)
            return True
        except ValueError:
            return False

    value = input_text(
        label,
        default=str(default),
        validator=Validator.from_callable(
            valid,
            error_message="Введите целое число" if integer else "Введите число",
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
                Text("Подключений пока нет. Начните с настройки подключения.", style="grey70"),
                title="Подключения",
                title_align="left",
                border_style="grey35",
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
        header_style="bold grey70",
        border_style="grey35",
        padding=(0, 1),
        expand=True,
    )
    for name, backend in config.backends.items():
        account = accounts.get(backend.account)
        state = account["state"] if account else "DISCONNECTED"
        table.add_row(
            Text(name, style="bold white"),
            Text(backend.account, style="grey70"),
            Text(backend.model, style="cyan"),
            Text("● " + STATES.get(state, state), style=STATE_COLORS.get(state, "grey70")),
        )
    ui_print(Panel(table, title="Подключения", title_align="left", border_style="grey35"))
    ui_print(Text("Доступ к модели подтверждается проверочным запросом.", style="grey62"))
    ui_print(Text(""))


def execute(config_path: Path, state_dir: Path, command: str, **options) -> None:
    asyncio.run(perform(argparse.Namespace(command=command, **options), config_path, state_dir))


def manage_accounts(config_path: Path, state_dir: Path, mode: str | None = None) -> None:
    options = []
    if mode in {None, "1"}:
        options.extend(
            [
                ("login", "Войти в ChatGPT / Claude"),
                ("import-claude", "Импортировать профиль Claude"),
            ]
        )
    if mode in {None, "2"}:
        options.append(("api-key", "Сохранить API-ключ OpenAI"))
    options.extend([("logout", "Отключить аккаунт"), ("accounts", "Список аккаунтов")])
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
                        Text(STATES.get(account["state"], account["state"])),
                    )
                console.print(table if records else "Аккаунтов пока нет. Добавьте авторизацию.")
                return
            arguments = {}
            if operation == "login":
                provider = ask("Провайдер", choices=["chatgpt", "claude"], default="claude")
                arguments["provider"] = provider
                arguments["account"] = ask("Имя аккаунта", default=f"{provider}-main")
                arguments["consent"] = provider == "chatgpt" and confirm(
                    "Повторно запросить согласие на использование подписки?", default=False
                )
            elif operation == "logout":
                if not records:
                    console.print("Аккаунтов пока нет.")
                    return
                arguments["account"] = ask("Аккаунт", choices=[r["account"] for r in records])
            elif operation != "accounts":
                arguments["account"] = ask(
                    "Имя аккаунта",
                    default="openai-paid" if operation == "api-key" else "claude-main",
                )
            if operation == "import-claude":
                arguments["source"] = Path(ask("Каталог профиля внутри контейнера"))
            if operation == "api-key":
                key = ask("API-ключ OpenAI", password=True)
                asyncio.run(save_paid_key(state_dir, arguments["account"], key))
                console.print("API-ключ сохранён в зашифрованном виде.")
            else:
                execute(config_path, state_dir, operation, **arguments)
        except Back:
            continue
        return


def configure_backend(config: GatewayConfig, config_path: Path, mode: str | None = None) -> None:
    visible = mode_config(config, mode) if mode else config
    console.print(
        "Существующие подключения: " + (", ".join(visible.backends) or "пока нет"), markup=False
    )
    name = ask("Имя подключения (существующее или новое)")
    current = config.backends.get(name)
    if current and mode and current.provider not in MODE_PROVIDERS[mode]:
        raise GatewayError("Это имя занято подключением из другого режима. Выберите другое имя.")
    if current and current.provider in {"remote_inference", "colab_managed"}:
        console.print("Настройки Colab находятся в разделе Google Colab.")
        return
    providers = (
        list(MODE_PROVIDERS[mode])
        if mode
        else ["codex_subscription", "claude_subscription", "openai_api"]
    )
    provider = (
        providers[0]
        if len(providers) == 1
        else ask(
            "Тип провайдера",
            choices=providers,
            default=current.provider if current else "claude_subscription",
        )
    )
    defaults = {
        "codex_subscription": ("chatgpt-main", "gpt-6.1-sol"),
        "claude_subscription": ("claude-main", "sonnet"),
        "openai_api": ("openai-paid", "gpt-6.1-sol"),
    }
    account_default, model_default = defaults[provider]
    same_provider = current is not None and current.provider == provider
    account = ask("Аккаунт", default=current.account if same_provider else account_default)
    model = ask("Модель", default=current.model if same_provider else model_default)
    transport = "cli"
    if provider == "claude_subscription":
        transport = ask(
            "Транспорт Claude",
            choices=["cli", "sdk"],
            default=current.transport if current else "cli",
        )
    timeout = number("Таймаут, секунд", default=current.timeout_seconds if current else 180)
    concurrency = number(
        "Одновременных запросов", default=current.max_concurrency if current else 1, integer=True
    )
    backend = BackendConfig(
        provider=provider,
        account=account,
        model=model,
        transport=transport,
        timeout_seconds=timeout,
        max_concurrency=concurrency,
    )
    updated = config.model_dump()
    updated["backends"][name] = backend.model_dump()
    save_config(GatewayConfig.model_validate(updated), config_path)
    console.print(
        "Подключение сохранено. Для новой модели задайте маршрут в разделе Gateway → Маршруты."
    )
    console.print("Изменения сервера применятся после перезапуска Gateway.")


def manage_models(
    config: GatewayConfig, config_path: Path, state_dir: Path, mode: str | None = None
) -> None:
    while True:
        operation = menu([("catalog", "Каталог моделей"), ("configure", "Настроить подключение")])
        try:
            if operation == "configure":
                configure_backend(config, config_path, mode)
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
    console.print(json.dumps(config.routes, ensure_ascii=False, indent=2), markup=False)
    alias = ask("Логическое имя модели (Enter — оставить)", default="")
    updated = config.model_dump()
    if alias:
        names = ask("Подключения по порядку через запятую").split(",")
        updated["routes"][alias] = [name.strip() for name in names]
    errors = ask(
        "Ошибки для резервного маршрута через запятую (пусто — отключить резерв)",
        default=",".join(config.fallback_on),
    )
    updated["fallback_on"] = [name.strip() for name in errors.split(",") if name.strip()]
    save_config(GatewayConfig.model_validate(updated), config_path)
    console.print("Маршруты сохранены. Изменения сервера применятся после перезапуска Gateway.")


def probe(config: GatewayConfig, config_path: Path, state_dir: Path) -> None:
    if not config.backends and not config.routes:
        console.print("Сначала настройте подключение.")
        return
    backend = ask(
        "Подключение или маршрут",
        choices=list(
            dict.fromkeys(
                [
                    *config.backends,
                    *config.routes,
                ]
            )
        ),
    )
    stream = confirm("Показывать ответ по мере получения (stream)?", default=False)
    prompt = ask("Проверочный запрос", default="Ответь ровно одним словом: СПАС")
    console.print("Проверка отправляет реальный запрос и расходует квоту выбранного провайдера.")
    execute(config_path, state_dir, "probe", backend=backend, prompt=prompt, stream=stream)


def show_key(state_dir: Path) -> None:
    keys = GatewayKeys.load(CredentialStore(state_dir))
    role = ask("Ключ доступа к Gateway", choices=["client", "admin"], default="client")
    value = keys.client if role == "client" else keys.admin
    if value is None:
        raise GatewayError("Ключ администратора не настроен.")
    console.print(value, markup=False)


async def save_paid_key(state_dir: Path, account: str, api_key: str) -> None:
    async with httpx.AsyncClient() as client:
        await AuthManager(CredentialStore(state_dir), client).save_api_key(account, api_key)


def show_logs(state_dir: Path) -> None:
    path = state_dir / "requests.jsonl"
    if path.exists():
        with path.open() as source:
            console.print("".join(deque(source, maxlen=20)), markup=False)
    else:
        console.print("Запросов пока нет.")


def run_tui(config_path: Path, state_dir: Path) -> None:
    page = "home"
    while True:
        config = GatewayConfig.load(config_path)
        console.clear()
        page_header(page)
        try:
            if page == "3":
                from .colab.tui import manage_full_colab

                try:
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
                            ("g", "Gateway"),
                            ("q", "Выход"),
                        ],
                        default="1",
                        home=config,
                    )
                except Back:
                    continue
                if action == "q":
                    return
                page = action
                continue
            try:
                if page == "g":
                    action = menu(
                        [
                            ("r", "Маршруты моделей и резервные подключения"),
                            ("p", "Проверить подключение или общий маршрут"),
                            ("k", "Ключи доступа к Gateway"),
                            ("l", "Журнал запросов"),
                            ("q", "Выход"),
                        ]
                    )
                else:
                    visible = mode_config(config, page)
                    ui_print(Text(MODES[page][1], style="grey70"))
                    ui_print(Text(""))
                    show_connections(visible, state_dir)
                    options = [
                        ("a", "Аккаунты и вход" if page == "1" else "API-ключи и аккаунты"),
                        ("m", "Модели и настройки подключений"),
                        ("p", "Отправить проверочный запрос"),
                    ]
                    if not visible.backends:
                        ui_print(
                            Text(
                                "Начните здесь: авторизация → подключение → проверка.",
                                style="grey70",
                            )
                        )
                        ui_print(Text(""))
                    action = menu([*options, ("q", "Выход")])
            except Back:
                page = "home"
                continue
            if action == "q":
                return
            if action == "a":
                manage_accounts(config_path, state_dir, page)
            elif action == "m":
                manage_models(config, config_path, state_dir, page)
            elif action == "r":
                manage_routes(config, config_path)
            elif action == "p":
                probe(config if page == "g" else visible, config_path, state_dir)
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
            console.print(error.message, markup=False, style="red")
        except (OSError, ValueError):
            console.print("Проверьте путь, аккаунт и настройки подключения.", style="red")
        try:
            ask("Enter или Esc — вернуться", default="")
        except (Back, KeyboardInterrupt):
            pass
        except EOFError:
            return
