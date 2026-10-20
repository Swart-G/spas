"""Connection-oriented setup for arbitrary OpenAI-compatible APIs."""

import asyncio
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from rich.table import Table
from rich.text import Text

from . import tui as ui
from .auth.credentials import CredentialStore
from .auth.manager import AuthManager
from .cli import apply_gateway, save_config
from .config import BackendConfig, GatewayConfig
from .core.errors import GatewayError
from .main import make_registry
from .presentation import FORMATS, RETURN_PROMPT
from .terminal import Back


def available_api_account(name, config, store):
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-_")[:64] or "api"
    reserved = {backend.account for backend in config.backends.values()}
    account = stem
    while account in reserved or store.get("account:" + account) is not None:
        account = stem[:31] + "-" + secrets.token_hex(16)
    return account


def configure_api(config_path: Path, state_dir: Path, name: str | None = None) -> None:
    config = GatewayConfig.load(config_path)
    name = name if name is not None else ui.ask("Название подключения").strip()
    if not name or len(name) > 64:
        raise GatewayError("Введите название подключения: от 1 до 64 символов.", status=400)
    current = config.backends.get(name)
    if current and current.provider not in ui.MODE_PROVIDERS["2"]:
        raise GatewayError(
            "Это название уже используется в другом разделе. Выберите другое.", status=400
        )
    provider = current.provider if current else "openai_compatible"
    store = CredentialStore(state_dir)
    account = current.account if current else available_api_account(name, config, store)
    ui.console.print(
        "Введите адрес API, например https://host/v1, или полный адрес /chat/completions "
        "либо /responses. "
        "Для моделей решений подойдёт полный адрес /decisions или /systemone."
    )
    base_url = ui.ask("Адрес API", default=current.base_url if current else "")
    path = urlsplit(base_url.strip()).path.rstrip("/")
    api_format = next(
        (
            kind
            for suffix, kind in [
                ("/chat/completions", "chat_completions"),
                ("/responses", "responses"),
                ("/decisions", "decisions"),
                ("/systemone", "systemone"),
            ]
            if path.endswith(suffix)
        ),
        None,
    )
    if api_format is None:
        api_format = ui.ask(
            "Формат API",
            choices=["chat_completions", "responses", "decisions", "systemone"],
            default=current.api_format if current else "chat_completions",
        )
    else:
        ui.console.print("Формат API: " + FORMATS[api_format])
    model = ui.ask(
        "ID модели в сервисе",
        default=current.model if current else "",
    )
    draft = BackendConfig(
        provider=provider,
        account=account,
        model=model.strip(),
        base_url=base_url,
        api_format=api_format,
        model_kind="chat"
        if current and current.model == model.strip() and current.model_kind == "chat"
        else "auto",
    )
    settings = current.model_dump() if current else {}
    settings.update(
        draft.model_dump(include={"provider", "account", "model", "base_url", "api_format"})
    )
    if current and (current.model != draft.model or current.api_format != draft.api_format):
        settings["model_kind"] = draft.model_kind
    if not current or current.model != draft.model:
        settings["decision_box"] = None
    if draft.decision_model:
        settings.update(api_streaming=False, api_json_output=False, api_include_usage=False)
    backend = BackendConfig.model_validate(settings)
    saved = store.get("account:" + backend.account)
    choices = ["key", "none"]
    # Changing the destination requires an explicit choice of credentials.
    if (
        current
        and current.base_url == backend.base_url
        and saved
        and saved.get("provider") == provider
        and (saved.get("api_key") or saved.get("no_auth"))
        and saved.get("state") != "REAUTH_REQUIRED"
    ):
        choices.insert(0, "keep")
    labels = {
        "key": "Ввести API-ключ",
        "none": "Без ключа",
        "keep": "Использовать сохранённый доступ",
    }
    auth_mode = ui.menu(
        [(value, labels[value]) for value in choices], default=choices[0], title="Доступ к API"
    )
    api_key = ui.ask("API-ключ", password=True) if auth_mode == "key" else ""
    if current and auth_mode != "keep":
        # Running adapters may still use any previously saved account until reload.
        account = available_api_account(current.account, config, store)
        backend = BackendConfig.model_validate({**backend.model_dump(), "account": account})
    updated = config.model_dump()
    updated["backends"][name] = backend.model_dump()
    if current is None:
        alias = ui.ask("Имя модели в шлюзе", default=f"{name}-default").strip()
        if alias in updated["routes"]:
            raise GatewayError("Это имя модели уже занято в шлюзе. Выберите другое.", status=400)
        updated["routes"][alias] = [name]
    validated = GatewayConfig.model_validate(updated)
    # All prompts and validation finish before credentials or config are changed.
    save_config(
        validated,
        config_path,
        expected=config,
        before_write=(
            lambda: asyncio.run(
                save_auth(
                    state_dir, backend, api_key, no_auth=auth_mode == "none", create_only=True
                )
            )
        )
        if auth_mode != "keep"
        else None,
    )
    ui.console.print("Подключение сохранено. Нажмите «Применить настройки», чтобы обновить шлюз.")


def configure_api_parameters(config_path: Path, name: str) -> None:
    config = GatewayConfig.load(config_path)
    backend = config.backends[name]
    options = backend.model_dump()
    if backend.decision_model:
        options["decision_box"] = ui.confirm(
            "Модель поддерживает поиск объектов (Box)?", default=backend.supports_box
        )
    else:
        options["api_streaming"] = ui.confirm("Потоковый ответ", default=backend.api_streaming)
        if backend.api_format == "chat_completions":
            options["api_include_usage"] = options["api_streaming"] and ui.confirm(
                "Запрашивать статистику токенов в потоке", default=backend.api_include_usage
            )
            options["api_token_limit"] = ui.ask(
                "Параметр ограничения длины ответа",
                choices=["max_tokens", "max_completion_tokens"],
                default=backend.api_token_limit,
            )
        options["api_json_output"] = ui.confirm(
            "Модель поддерживает ответы в формате JSON?", default=bool(backend.api_json_output)
        )
    options["timeout_seconds"] = ui.number(
        "Время ожидания ответа, с", default=backend.timeout_seconds, minimum=1, maximum=3600
    )
    options["max_concurrency"] = ui.number(
        "Максимум одновременных запросов",
        default=backend.max_concurrency,
        integer=True,
        minimum=1,
        maximum=32,
    )
    updated = config.model_dump()
    updated["backends"][name] = BackendConfig.model_validate(options).model_dump()
    save_config(GatewayConfig.model_validate(updated), config_path, expected=config)
    ui.console.print("Параметры сохранены. Нажмите «Применить настройки», чтобы обновить шлюз.")


async def save_auth(state_dir, backend, api_key, *, no_auth=False, create_only=False):
    async with httpx.AsyncClient() as client:
        await AuthManager(CredentialStore(state_dir), client).save_api_key(
            backend.account,
            api_key,
            provider=backend.provider,
            no_auth=no_auth,
            create_only=create_only,
        )


async def fetch_models(state_dir, backend):
    async with httpx.AsyncClient(follow_redirects=False) as client:
        auth = AuthManager(CredentialStore(state_dir), client)
        config = GatewayConfig(backends={"selected": backend}, routes={})
        return await make_registry(config, auth, client).backends["selected"].provider.list_models()


def choose_model(config_path, state_dir, name):
    config = GatewayConfig.load(config_path)
    backend = config.backends[name]
    source = ui.menu(
        [("catalog", "Выбрать из каталога"), ("manual", "Указать ID вручную")], title="Выбор модели"
    )
    if source == "catalog":
        models = asyncio.run(fetch_models(state_dir, backend))
        if not models:
            raise GatewayError("В каталоге нет моделей. Выберите «Указать ID вручную».")
        model = ui.ask(
            "Модель",
            choices=list(dict.fromkeys(item["id"] for item in models)),
            default=backend.model,
        )
    else:
        model = ui.ask("ID модели", default=backend.model)
    model = model.strip()
    kind = (
        next(item for item in models if item["id"] == model).get("kind")
        if source == "catalog"
        else None
    )
    updated = config.model_dump()
    settings = updated["backends"][name]
    settings["model"] = model
    if model != backend.model:
        settings.update(model_kind="auto", decision_box=None)
    if kind == "decision":
        settings["model_kind"] = "decision"
        if backend.api_format not in {"decisions", "systemone"}:
            settings["api_format"] = "decisions"
    elif backend.decision_model and (model != backend.model or kind == "chat"):
        choices = ["chat_completions", "responses"]
        if kind != "chat":
            choices.append(backend.api_format)
        api_format = ui.ask(
            "Формат API",
            choices=choices,
            default=backend.api_format
            if backend.api_format in choices
            else "responses"
            if backend.provider == "openai_api"
            else "chat_completions",
        )
        settings["api_format"] = api_format
        settings["model_kind"] = (
            "chat" if api_format in {"chat_completions", "responses"} else "decision"
        )
        if settings["model_kind"] == "chat":
            suffix = "/responses" if api_format == "responses" else "/chat/completions"
            # The catalog uses the stable prefix even for OpenRouter's alpha decisions API.
            settings["base_url"] = backend.models_url.removesuffix("/models") + suffix
    elif kind == "chat":
        settings["model_kind"] = "chat"
    save_config(GatewayConfig.model_validate(updated), config_path, expected=config)
    ui.console.print("Модель сохранена. Нажмите «Применить настройки», чтобы обновить шлюз.")


def delete_connection(config_path, name):
    config = GatewayConfig.load(config_path)
    affected = [alias for alias, names in config.routes.items() if name in names]
    ui.console.print("Связанные маршруты: " + (", ".join(affected) or "нет"), markup=False)
    if not ui.confirm(f"Удалить подключение {name}?", default=False):
        return
    updated = config.model_dump()
    del updated["backends"][name]
    updated["routes"] = {
        alias: remaining
        for alias, names in config.routes.items()
        if (remaining := [item for item in names if item != name])
    }
    save_config(GatewayConfig.model_validate(updated), config_path, expected=config)
    ui.console.print("Подключение удалено. Нажмите «Применить настройки», чтобы обновить шлюз.")


def connection_action(config_path, state_dir, name):
    config = GatewayConfig.load(config_path)
    action = ui.menu(
        [
            ("edit", "Настройки подключения"),
            ("parameters", "Расширенные параметры"),
            ("model", "Выбрать модель"),
            ("status", "Пинг подключения"),
            ("logout", "Удалить сохранённый доступ"),
            ("delete", "Удалить подключение"),
        ],
        title=name,
    )
    if action == "edit":
        configure_api(config_path, state_dir, name)
    elif action == "parameters":
        configure_api_parameters(config_path, name)
    elif action == "model":
        choose_model(config_path, state_dir, name)
    elif action == "delete":
        delete_connection(config_path, name)
    elif action == "logout":
        ui.execute(config_path, state_dir, "logout", account=config.backends[name].account)
    elif action == "status":
        ui.execute(config_path, state_dir, "status", backend=name)


def manage_api(config_path: Path, state_dir: Path):
    while True:
        config = ui.mode_config(GatewayConfig.load(config_path), "2")
        ui.console.clear()
        ui.page_header("2")
        records = {
            record["account"]: AuthManager.public_account(record)
            for record in CredentialStore(state_dir).all_accounts()
        }
        compact = ui.console.width < 112
        table = Table(
            "Подключение / модель" if compact else "Подключение",
            "Адрес API",
            "Формат",
            *([] if compact else ["Модель"]),
            "Состояние",
        )
        for column in table.columns:
            column.overflow = "fold"
        for name, backend in config.backends.items():
            table.add_row(
                Text(name + "\n" + backend.model if compact and name != backend.model else name),
                Text(backend.base_url),
                Text(
                    "Решения" if backend.decision_model else FORMATS.get(backend.api_format, "Чат")
                ),
                *([] if compact else [Text(backend.model)]),
                Text(
                    ui.STATES.get(records.get(backend.account, {}).get("state"), "Нет подключения")
                ),
            )
        ui.console.print(
            table if config.backends else "Подключений пока нет. Выберите «Добавить подключение»."
        )
        options = [
            *(("connection:" + name, name) for name in config.backends),
            ("new", "Добавить подключение"),
            ("apply", "Применить настройки"),
        ]
        action = ui.menu(options, title="API моделей")
        try:
            if action == "new":
                configure_api(config_path, state_dir)
            elif action == "apply":
                asyncio.run(apply_gateway(state_dir))
                ui.console.print("Настройки применены.")
            else:
                connection_action(config_path, state_dir, action.removeprefix("connection:"))
        except Back:
            continue
        except GatewayError as error:
            ui.console.print(error.message, markup=False, style="bright_red")
        except (OSError, ValueError):
            ui.console.print(
                "Не удалось выполнить действие. Проверьте адрес API, профиль доступа и модель.",
                style="bright_red",
            )
        except KeyboardInterrupt:
            ui.console.print("Действие отменено.")
        try:
            ui.ask(RETURN_PROMPT, default="")
        except (Back, KeyboardInterrupt):
            pass
