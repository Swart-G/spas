import argparse
import asyncio
import fcntl
import getpass
import json
import os
import secrets
import sys
from collections.abc import Callable
from contextlib import aclosing
from pathlib import Path

import httpx
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from .auth.claude import ClaudeProfiles
from .auth.credentials import CredentialStore
from .auth.manager import AuthManager
from .config import BackendConfig, GatewayConfig, default_state_dir
from .core.audit import RequestLog
from .core.errors import GatewayError
from .core.models import ChatRequest
from .core.router import Router
from .main import GatewayKeys, make_registry
from .presentation import (
    AUTH_STATES,
    PROVIDERS,
    safe_terminal_text,
    show_models,
    show_status,
    show_usage,
)

console = Console(highlight=False, style="bright_white")


def initial_config() -> GatewayConfig:
    return GatewayConfig(
        backends={
            "chatgpt": BackendConfig(
                provider="codex_subscription", account="chatgpt-main", model="gpt-6.1-sol"
            ),
            "claude": BackendConfig(
                provider="claude_subscription", account="claude-main", model="sonnet"
            ),
        },
        routes={"codex-default": ["chatgpt"], "sonnet-default": ["claude"]},
    )


def save_config(
    config: GatewayConfig,
    path: Path,
    *,
    expected: GatewayConfig | None = None,
    before_write: Callable[[], None] | None = None,
) -> None:
    lines = [
        "# СПАС — шлюз моделей. Данные доступа хранятся отдельно в зашифрованном хранилище.",
        "fallback_on = " + json.dumps(config.fallback_on),
        "",
        "[backends]",
        "",
    ]
    for name, backend in config.backends.items():
        lines.append("[backends." + json.dumps(name) + "]")
        for key, value in backend.model_dump().items():
            if value is None:
                continue
            lines.append(key + " = " + json.dumps(value, ensure_ascii=False))
        lines.append("")
    lines.append("[routes]")
    for alias, backends in config.routes.items():
        lines.append(json.dumps(alias) + " = " + json.dumps(backends))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name("." + path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if expected is not None:
            try:
                current = GatewayConfig.load(path)
            except (FileNotFoundError, ValueError):
                current = None
            if current != expected:
                raise GatewayError(
                    "Настройки изменены в другом окне. Повторите действие.",
                    code="config_changed",
                    status=409,
                )
        if before_write is not None:
            before_write()
        CredentialStore._atomic_write(path, ("\n".join(lines) + "\n").encode())
    finally:
        os.close(fd)


async def apply_gateway(state_dir: Path):
    keys = GatewayKeys.load(CredentialStore(state_dir))
    if not keys.admin:
        raise GatewayError("Не настроен ключ администратора шлюза.")
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
                "Шлюз не применил настройки. Проверьте его доступность и ключ администратора."
            )
    except httpx.HTTPError as error:
        raise GatewayError(
            "Шлюз недоступен. Настройки сохранены; повторите применение после его запуска."
        ) from error


def initialize(state_dir: Path, config_path: Path) -> None:
    store = CredentialStore(state_dir)
    with store._sync_lock("gateway-keys"):
        if store.get("gateway-keys") is None:
            store.put(
                "gateway-keys",
                {"client": secrets.token_urlsafe(32), "admin": secrets.token_urlsafe(32)},
            )
    if not config_path.exists():
        save_config(initial_config(), config_path)
    console.print(f"Конфигурация: {config_path}", markup=False)
    console.print("Откройте интерфейс СПАС: docker compose run --rm tui.")


def print_accounts(auth: AuthManager) -> None:
    table = Table("Аккаунт", "Сервис", "Состояние", "Подписка")
    for record in auth.accounts():
        table.add_row(
            record["account"],
            PROVIDERS.get(record["provider"], "Другой сервис"),
            AUTH_STATES.get(record["state"], "Нет данных"),
            record.get("subscription_type") or "—",
        )
    console.print(table)


async def perform(args, config_path: Path, state_dir: Path) -> None:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        auth = AuthManager(CredentialStore(state_dir), client)
        profiles = ClaudeProfiles(auth)
        if args.command == "colab-terminal":
            from .colab.manager import ColabManager
            from .colab.terminal import open_terminal

            config = GatewayConfig.load(config_path)
            backend = config.backends.get(args.backend)
            if backend is None or backend.provider != "colab_managed":
                raise GatewayError("Выберите управляемое подключение Colab.")
            await open_terminal(ColabManager(auth.store, client), backend.account)
        elif args.command == "accounts":
            print_accounts(auth)
        elif args.command == "login":
            if args.provider == "chatgpt":
                result = await auth.login_chatgpt(
                    args.account,
                    lambda url: console.print(url, markup=False),
                    consent=args.consent,
                )
            else:
                result = await profiles.login(args.account)
            console.print(
                f"{result['account']}: {AUTH_STATES.get(result['state'], 'Нет данных')}",
                markup=False,
            )
        elif args.command == "import-claude":
            result = await profiles.import_profile(args.account, args.source)
            console.print(
                f"{result['account']}: {AUTH_STATES.get(result['state'], 'Нет данных')}. "
                "Проверьте ответ модели в главном меню: «Тестирование».",
                markup=False,
            )
        elif args.command == "logout":
            revoked = await auth.logout(args.account)
            console.print("Сохранённый доступ удалён с этого устройства.")
            if not revoked:
                console.print(
                    "Чтобы завершить выход из ChatGPT, отзовите доступ приложения "
                    "в настройках аккаунта."
                )
        elif args.command == "api-key":
            await auth.save_api_key(
                args.account,
                "" if args.no_auth else getpass.getpass("API-ключ: "),
                provider=args.provider,
                no_auth=args.no_auth,
            )
            console.print("Доступ к API сохранён.")
        elif args.command in {"models", "probe", "status", "decide"}:
            config = GatewayConfig.load(config_path)
            registry = make_registry(config, auth, client)
            if args.command in {"models", "status"}:
                if args.backend not in registry.backends:
                    raise GatewayError(
                        "Подключение не найдено. Проверьте его название.",
                        code="model_not_found",
                        status=404,
                    )
                provider = registry.backends[args.backend].provider
                data = (
                    await provider.list_models()
                    if args.command == "models"
                    else await provider.health()
                )
                if getattr(args, "pretty", False):
                    if args.command == "models":
                        show_models(
                            console,
                            data,
                            decision=registry.backends[args.backend].config.decision_model,
                        )
                    else:
                        show_status(console, data)
                else:
                    console.print(json.dumps(data, ensure_ascii=False, indent=2), markup=False)
                return
            alias = args.backend
            if alias in config.backends:
                config = config.model_copy(update={"routes": {**config.routes, "probe": [alias]}})
                registry = make_registry(config, auth, client)
                alias = "probe"
            request = ChatRequest.model_validate(
                {
                    "model": alias,
                    "messages": [{"role": "user", "content": getattr(args, "prompt", "probe")}],
                    "stream": getattr(args, "stream", False),
                }
            )
            audit = RequestLog(state_dir)
            try:
                router = Router(registry, auth, audit)
                if args.command == "decide" or registry.capabilities(alias).decisions:
                    from .core.system_one import parse_decision

                    payload = (
                        args.payload
                        if getattr(args, "payload", None) is not None
                        else json.loads(args.input.read_text())
                        if args.command == "decide"
                        else {
                            "state": args.prompt,
                            "questions": {
                                "incident": {
                                    "type": "noul",
                                    "instructions": "Does the text describe a failure?",
                                }
                            },
                        }
                    )
                    if not isinstance(payload, dict):
                        raise GatewayError("JSON решения должен быть объектом.", status=400)
                    payload["model"] = alias
                    result = await router.decide(parse_decision(payload), "cli-decision")
                    if getattr(args, "pretty", False):
                        from .decision_tui import show_result

                        show_result(result)
                    else:
                        console.print(
                            json.dumps(result, ensure_ascii=False, indent=2), markup=False
                        )
                elif args.stream:
                    try:
                        async with aclosing(router.stream(request, "cli-probe")) as events:
                            async for event in events:
                                if event.kind == "delta":
                                    console.print(
                                        safe_terminal_text(event.text), end="", markup=False
                                    )
                    finally:
                        console.print()
                else:
                    result = await router.generate(request, "cli-probe")
                    console.print(safe_terminal_text(result.content), markup=False)
                    if result.usage:
                        if getattr(args, "pretty", False):
                            show_usage(console, result.usage.as_dict())
                        else:
                            console.print(json.dumps(result.usage.as_dict()), markup=False)
            finally:
                audit.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="СПАС — шлюз моделей")
    root.add_argument("--state-dir", type=Path, default=default_state_dir())
    root.add_argument("--config", type=Path, default=os.environ.get("SPAS_GATEWAY_CONFIG"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Создать настройки и ключи шлюза")
    commands.add_parser("accounts", help="Показать подключённые аккаунты")
    terminal = commands.add_parser("colab-terminal", help="Открыть терминал Colab")
    terminal.add_argument("backend")
    login = commands.add_parser("login", help="Подключить аккаунт с подпиской")
    login.add_argument("provider", choices=["chatgpt", "claude"])
    login.add_argument("account")
    login.add_argument(
        "--consent",
        action="store_true",
        help="Повторно запросить разрешение на доступ к подписке ChatGPT",
    )
    imported = commands.add_parser("import-claude", help="Импортировать профиль Claude")
    imported.add_argument("account")
    imported.add_argument("--source", type=Path, default=Path.home() / ".claude")
    for command in ("logout", "api-key"):
        item = commands.add_parser(command)
        item.add_argument("account")
        if command == "api-key":
            item.add_argument(
                "--provider", choices=["openai_compatible", "openai_api"], default="openai_api"
            )
            item.add_argument("--no-auth", action="store_true", help="Подключиться к API без ключа")
    keys = commands.add_parser("key", help="Показать ключ доступа к шлюзу")
    keys.add_argument("--admin", action="store_true")
    models = commands.add_parser("models", help="Показать каталог моделей сервиса")
    models.add_argument("backend")
    status = commands.add_parser("status", help="Проверить состояние подключения")
    status.add_argument("backend")
    probe = commands.add_parser("probe", help="Отправить проверочный запрос модели")
    probe.add_argument("backend", help="Название подключения или имя модели в шлюзе")
    probe.add_argument("--prompt", default="Ответь ровно одним словом: СПАС")
    probe.add_argument("--stream", action="store_true")
    decide = commands.add_parser("decide", help="Отправить запрос модели решений")
    decide.add_argument("backend", help="Название подключения или имя модели в шлюзе")
    decide.add_argument("--input", type=Path, required=True, help="Путь к JSON-файлу запроса")
    decide.add_argument("--pretty", action="store_true", help="Показать результат в удобном виде")
    route = commands.add_parser("route", help="Настроить маршрут и резервные подключения")
    route.add_argument("alias")
    route.add_argument("backends", nargs="+")
    model = commands.add_parser("set-model", help="Выбрать модель и способ подключения к Claude")
    model.add_argument("backend")
    model.add_argument("model")
    model.add_argument("--transport", choices=["cli", "sdk"])
    commands.add_parser("tui", help="Открыть интерфейс СПАС")
    serve = commands.add_parser("serve", help="Запустить шлюз моделей")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8008)
    return root


def main() -> None:
    args = parser().parse_args()
    state_dir = args.state_dir.expanduser()
    config_path = (args.config or state_dir / "gateway.toml").expanduser()
    try:
        if args.command == "init":
            initialize(state_dir, config_path)
        elif args.command == "key":
            keys = GatewayKeys.load(CredentialStore(state_dir))
            # Intentional local export of an internal gateway key, never provider tokens.
            if args.admin and keys.admin is None:
                raise GatewayError("Ключ администратора не настроен.")
            print(keys.admin if args.admin else keys.client)
        elif args.command == "route":
            config = GatewayConfig.load(config_path)
            updated = config.model_dump()
            updated["routes"][args.alias] = args.backends
            save_config(GatewayConfig.model_validate(updated), config_path, expected=config)
            console.print("Маршрут сохранён. Перезапустите шлюз, чтобы применить изменения.")
        elif args.command == "set-model":
            config = GatewayConfig.load(config_path)
            if args.backend not in config.backends:
                raise GatewayError("Подключение не найдено. Проверьте его название.")
            updated = config.model_dump()
            updated["backends"][args.backend]["model"] = args.model
            if args.transport:
                updated["backends"][args.backend]["transport"] = args.transport
            save_config(GatewayConfig.model_validate(updated), config_path, expected=config)
            console.print("Модель сохранена. Перезапустите шлюз, чтобы применить изменения.")
        elif args.command == "serve":
            import uvicorn

            from .main import create_app

            uvicorn.run(
                create_app(
                    GatewayConfig.load(config_path), state_dir=state_dir, config_path=config_path
                ),
                host=args.host,
                port=args.port,
                access_log=False,
            )
        elif args.command == "tui":
            from .tui import run_tui

            run_tui(config_path, state_dir)
        else:
            asyncio.run(perform(args, config_path, state_dir))
    except GatewayError as error:
        console.print(f"{error.code}: {error.message}", markup=False, style="red")
        sys.exit(1)
    except (OSError, ValidationError, json.JSONDecodeError) as error:
        console.print(f"Ошибка конфигурации: {type(error).__name__}. Проверьте путь и TOML.")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
