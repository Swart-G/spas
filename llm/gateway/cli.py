import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys
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

console = Console(highlight=False)


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


def save_config(config: GatewayConfig, path: Path) -> None:
    lines = [
        "# СПАС LLM Gateway; credentials live in the encrypted account store.",
        "fallback_on = " + json.dumps(config.fallback_on),
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
    CredentialStore._atomic_write(path, ("\n".join(lines) + "\n").encode())


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
    console.print("Настройка аккаунтов и проверки: SPAS TUI (docker compose run --rm tui).")


def print_accounts(auth: AuthManager) -> None:
    table = Table("Аккаунт", "Провайдер", "Состояние", "Подписка")
    for record in auth.accounts():
        table.add_row(
            record["account"],
            record["provider"],
            record["state"],
            record.get("subscription_type") or "—",
        )
    console.print(table)


async def perform(args, config_path: Path, state_dir: Path) -> None:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        auth = AuthManager(CredentialStore(state_dir), client)
        profiles = ClaudeProfiles(auth)
        if args.command == "accounts":
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
            console.print(f"{result['account']}: {result['state']}", markup=False)
        elif args.command == "import-claude":
            result = await profiles.import_profile(args.account, args.source)
            console.print(
                f"{result['account']}: {result['state']}. "
                "Выполните запрос в разделе Проверка SPAS TUI.",
                markup=False,
            )
        elif args.command == "logout":
            revoked = await auth.logout(args.account)
            console.print("Локальные credentials удалены.")
            if not revoked:
                console.print(
                    "Удалённый отзыв не подтверждён. Отключите приложение в настройках ChatGPT."
                )
        elif args.command == "api-key":
            await auth.save_api_key(args.account, getpass.getpass("OpenAI API key: "))
            console.print("API-ключ сохранён в зашифрованном виде.")
        elif args.command in {"models", "probe", "status"}:
            config = GatewayConfig.load(config_path)
            registry = make_registry(config, auth, client)
            if args.command in {"models", "status"}:
                if args.backend not in registry.backends:
                    raise GatewayError("Backend не найден.", code="model_not_found", status=404)
                provider = registry.backends[args.backend].provider
                data = (
                    await provider.list_models()
                    if args.command == "models"
                    else await provider.health()
                )
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
                    "messages": [{"role": "user", "content": args.prompt}],
                    "stream": args.stream,
                }
            )
            audit = RequestLog(state_dir)
            try:
                router = Router(registry, auth, audit)
                if args.stream:
                    async with aclosing(router.stream(request, "cli-probe")) as events:
                        async for event in events:
                            if event.kind == "delta":
                                console.print(event.text, end="", markup=False)
                    console.print()
                else:
                    result = await router.generate(request, "cli-probe")
                    console.print(result.content, markup=False)
                    if result.usage:
                        console.print(json.dumps(result.usage.as_dict()), markup=False)
            finally:
                audit.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="СПАС LLM Gateway")
    root.add_argument("--state-dir", type=Path, default=default_state_dir())
    root.add_argument("--config", type=Path, default=os.environ.get("SPAS_GATEWAY_CONFIG"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create local configuration and gateway keys")
    commands.add_parser("accounts", help="List accounts without revealing credentials")
    login = commands.add_parser("login", help="Authorize a subscription account")
    login.add_argument("provider", choices=["chatgpt", "claude"])
    login.add_argument("account")
    login.add_argument(
        "--consent", action="store_true", help="Explicitly request ChatGPT plan consent again"
    )
    imported = commands.add_parser(
        "import-claude", help="Encrypt a copy of a native Claude profile"
    )
    imported.add_argument("account")
    imported.add_argument("--source", type=Path, default=Path.home() / ".claude")
    for command in ("logout", "api-key"):
        item = commands.add_parser(command)
        item.add_argument("account")
    keys = commands.add_parser("key", help="Print a gateway key for a local client")
    keys.add_argument("--admin", action="store_true")
    models = commands.add_parser("models", help="Fetch the provider model catalog")
    models.add_argument("backend")
    status = commands.add_parser("status", help="Check backend availability without inference")
    status.add_argument("backend")
    probe = commands.add_parser("probe", help="Run one real inference request")
    probe.add_argument("backend", help="Backend name or logical model alias")
    probe.add_argument("--prompt", default="Ответь ровно одним словом: СПАС")
    probe.add_argument("--stream", action="store_true")
    route = commands.add_parser("route", help="Set a model route and explicit fallbacks")
    route.add_argument("alias")
    route.add_argument("backends", nargs="+")
    model = commands.add_parser("set-model", help="Choose the provider model and Claude transport")
    model.add_argument("backend")
    model.add_argument("model")
    model.add_argument("--transport", choices=["cli", "sdk"])
    commands.add_parser("tui", help="Interactive account, model, routing and log management")
    serve = commands.add_parser("serve", help="Start the local OpenAI-compatible gateway")
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
            save_config(GatewayConfig.model_validate(updated), config_path)
            console.print("Маршрут сохранён. Перезапустите Gateway.")
        elif args.command == "set-model":
            config = GatewayConfig.load(config_path)
            if args.backend not in config.backends:
                raise GatewayError("Backend не найден.")
            updated = config.model_dump()
            updated["backends"][args.backend]["model"] = args.model
            if args.transport:
                updated["backends"][args.backend]["transport"] = args.transport
            save_config(GatewayConfig.model_validate(updated), config_path)
            console.print("Модель сохранена. Перезапустите Gateway.")
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
    except (OSError, ValidationError) as error:
        console.print(f"Ошибка конфигурации: {type(error).__name__}. Проверьте путь и TOML.")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
