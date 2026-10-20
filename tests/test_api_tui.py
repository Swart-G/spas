import asyncio

import httpx
import pytest

from llm.gateway import api_tui, tui
from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.manager import AuthManager
from llm.gateway.cli import initial_config, save_config
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.errors import GatewayError
from llm.gateway.core.models import ChatRequest
from llm.gateway.providers.openai_compatible import OpenAICompatibleAdapter


def setup(tmp_path):
    path = tmp_path / "gateway.toml"
    save_config(initial_config(), path)
    return path


def form(
    monkeypatch,
    *,
    name="custom",
    base_url="http://localhost:1234",
    auth_mode="key",
    api_key="secret-test-key",
    alias="custom-default",
    cancel_at=None,
):
    values = {
        "Название подключения": name,
        "Профиль доступа к API": "custom",
        "Адрес API": base_url,
        "Формат API": "chat_completions",
        "ID модели в сервисе": "org/model",
        "Параметр ограничения длины ответа": "max_tokens",
        "API-ключ": api_key,
        "Имя модели в шлюзе": alias,
    }

    def ask(label, **kwargs):
        if label == cancel_at:
            raise tui.Back()
        if label == "API-ключ":
            assert kwargs["password"] is True
        return values[label]

    monkeypatch.setattr(tui, "ask", ask)
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: kwargs["default"])
    monkeypatch.setattr(tui, "number", lambda *args, **kwargs: kwargs["default"])
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: auth_mode)


@pytest.mark.parametrize("auth_mode", ["key", "none"])
def test_create_connection_and_route_in_one_form(monkeypatch, tmp_path, capsys, auth_mode):
    path = setup(tmp_path)
    previous = GatewayConfig.load(path)
    form(monkeypatch, auth_mode=auth_mode)
    api_tui.configure_api(path, tmp_path)
    saved = GatewayConfig.load(path)
    assert saved.backends["custom"].provider == "openai_compatible"
    assert saved.backends["custom"].base_url == "http://localhost:1234/v1"
    assert saved.backends["custom"].model == "org/model"
    assert saved.routes["custom-default"] == ["custom"]
    assert saved.backends["claude"] == previous.backends["claude"]
    assert saved.routes["codex-default"] == previous.routes["codex-default"]
    record = CredentialStore(tmp_path).get("account:custom")
    assert record["no_auth"] == (auth_mode == "none")
    if auth_mode == "key":
        assert record["api_key"] == "secret-test-key"
    assert "secret-test-key" not in path.read_text()
    assert "secret-test-key" not in capsys.readouterr().out


def test_cancel_after_entering_key_does_not_save(monkeypatch, tmp_path):
    path = setup(tmp_path)
    before = path.read_bytes()
    form(monkeypatch, cancel_at="Имя модели в шлюзе")
    with pytest.raises(tui.Back):
        api_tui.configure_api(path, tmp_path)
    assert path.read_bytes() == before
    assert CredentialStore(tmp_path).all_accounts() == []


@pytest.mark.parametrize(
    "url,alias", [("bad-url", "custom-default"), ("https://api.test/v1", "codex-default")]
)
def test_invalid_form_does_not_mutate_config_or_secrets(monkeypatch, tmp_path, url, alias):
    path = setup(tmp_path)
    before = path.read_bytes()
    form(monkeypatch, base_url=url, alias=alias)
    with pytest.raises((ValueError, GatewayError)):
        api_tui.configure_api(path, tmp_path)
    assert path.read_bytes() == before
    assert CredentialStore(tmp_path).all_accounts() == []


def test_new_destination_requires_explicit_auth_choice(monkeypatch, tmp_path):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    form(monkeypatch, base_url="https://different.test/api/v2")

    def menu(options, **kwargs):
        assert "keep" not in [key for key, _ in options]
        raise tui.Back()

    monkeypatch.setattr(tui, "menu", menu)
    with pytest.raises(tui.Back):
        api_tui.configure_api(path, tmp_path, "custom")
    assert GatewayConfig.load(path).backends["custom"].base_url == "http://localhost:1234/v1"
    assert CredentialStore(tmp_path).get("account:custom")["api_key"] == "secret-test-key"


def test_edit_preserves_existing_key_and_routes(monkeypatch, tmp_path):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    before = GatewayConfig.load(path)
    record = CredentialStore(tmp_path).get("account:custom")
    form(monkeypatch, auth_mode="keep")
    api_tui.configure_api(path, tmp_path, "custom")
    assert CredentialStore(tmp_path).get("account:custom") == record
    assert GatewayConfig.load(path).routes == before.routes


@pytest.mark.parametrize("auth_mode", ["key", "none"])
@pytest.mark.parametrize("same_destination", [False, True])
def test_replacing_access_preserves_running_adapter_credentials(
    monkeypatch, tmp_path, auth_mode, same_destination
):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    before = GatewayConfig.load(path)
    old_backend = before.backends["custom"]
    store = CredentialStore(tmp_path)
    old_record = store.get("account:" + old_backend.account)
    destination = old_backend.base_url if same_destination else "https://different.test/api/v2"
    form(
        monkeypatch,
        base_url=destination,
        auth_mode=auth_mode,
        api_key="new-destination-key",
    )
    api_tui.configure_api(path, tmp_path, "custom")
    after = GatewayConfig.load(path)
    new_backend = after.backends["custom"]
    assert new_backend.account != old_backend.account
    assert store.get("account:" + old_backend.account) == old_record
    assert after.routes == before.routes
    calls = []

    def handler(request):
        calls.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}]},
        )

    async def generate():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = AuthManager(store, client)
            request = ChatRequest(model="custom", messages=[{"role": "user", "content": "Hi"}])
            for backend in (old_backend, new_backend):
                adapter = OpenAICompatibleAdapter(client, auth, backend)
                assert (await adapter.generate(request)).content == "Hello"

    asyncio.run(generate())
    assert calls == [
        (old_backend.api_base_url + "/chat/completions", "Bearer secret-test-key"),
        (
            new_backend.api_base_url + "/chat/completions",
            "Bearer new-destination-key" if auth_mode == "key" else None,
        ),
    ]


def test_repeated_destination_edits_keep_each_credential_account(monkeypatch, tmp_path):
    path = setup(tmp_path)
    accounts = []
    for index, destination in enumerate(
        ["https://first.test/v1", "https://second.test/v1", "https://first.test/v1"]
    ):
        form(monkeypatch, base_url=destination, api_key=f"key-{index}")
        api_tui.configure_api(path, tmp_path, "custom" if index else None)
        accounts.append(GatewayConfig.load(path).backends["custom"].account)
    assert len(set(accounts)) == 3
    store = CredentialStore(tmp_path)
    assert [store.get("account:" + account)["api_key"] for account in accounts] == [
        "key-0",
        "key-1",
        "key-2",
    ]


def test_deleted_connections_never_reuse_retained_accounts(monkeypatch, tmp_path):
    path = setup(tmp_path)
    store = CredentialStore(tmp_path)
    accounts = []
    for index in range(6):
        form(monkeypatch, base_url=f"https://service-{index}.test/v1", api_key=f"key-{index}")
        api_tui.configure_api(path, tmp_path)
        accounts.append(GatewayConfig.load(path).backends["custom"].account)
        monkeypatch.setattr(tui, "confirm", lambda *a, **kw: True)
        api_tui.delete_connection(path, "custom")
    assert len(set(accounts)) == 6
    assert [store.get("account:" + account)["api_key"] for account in accounts] == [
        f"key-{index}" for index in range(6)
    ]


def test_new_account_avoids_configured_and_retained_collisions(monkeypatch, tmp_path):
    path = setup(tmp_path)
    config = GatewayConfig.load(path)
    config.backends["reserved"] = BackendConfig(
        provider="openai_compatible", account="custom", model="old", base_url="https://old.test"
    )
    save_config(config, path)
    store = CredentialStore(tmp_path)
    occupied = "custom-" + "a" * 32
    store.put(
        "account:" + occupied,
        {"kind": "account", "account": occupied, "provider": "openai_compatible", "api_key": "old"},
    )
    tokens = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(api_tui.secrets, "token_hex", lambda _: next(tokens))
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    saved = GatewayConfig.load(path)
    assert saved.backends["custom"].account == "custom-" + "b" * 32
    assert store.get("account:custom") is None
    assert store.get("account:" + occupied)["api_key"] == "old"


def test_account_created_during_form_cannot_be_overwritten(monkeypatch, tmp_path):
    path = setup(tmp_path)
    before = path.read_bytes()
    form(monkeypatch)
    original_ask = tui.ask
    store = CredentialStore(tmp_path)

    def ask(label, **kwargs):
        if label == "Имя модели в шлюзе":
            store.put(
                "account:custom",
                {
                    "kind": "account",
                    "account": "custom",
                    "provider": "openai_compatible",
                    "api_key": "parallel-key",
                },
            )
        return original_ask(label, **kwargs)

    monkeypatch.setattr(tui, "ask", ask)
    with pytest.raises(GatewayError) as caught:
        api_tui.configure_api(path, tmp_path)
    assert caught.value.code == "account_exists"
    assert path.read_bytes() == before
    assert store.get("account:custom")["api_key"] == "parallel-key"


def test_concurrent_config_change_prevents_credentials_and_config_write(monkeypatch, tmp_path):
    path = setup(tmp_path)
    form(monkeypatch)
    original_ask = tui.ask

    def ask(label, **kwargs):
        if label == "Имя модели в шлюзе":
            parallel = GatewayConfig.load(path)
            parallel.routes["parallel"] = ["claude"]
            save_config(parallel, path)
        return original_ask(label, **kwargs)

    monkeypatch.setattr(tui, "ask", ask)
    with pytest.raises(GatewayError) as caught:
        api_tui.configure_api(path, tmp_path)
    assert caught.value.code == "config_changed"
    saved = GatewayConfig.load(path)
    assert saved.routes["parallel"] == ["claude"] and "custom" not in saved.backends
    assert CredentialStore(tmp_path).all_accounts() == []


@pytest.mark.parametrize("operation", ["parameters", "model", "delete"])
def test_existing_connection_actions_reject_stale_config(monkeypatch, tmp_path, operation):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    changed = False

    def concurrent_change():
        nonlocal changed
        if not changed:
            parallel = GatewayConfig.load(path)
            parallel.routes["parallel"] = ["claude"]
            save_config(parallel, path)
            changed = True

    def ask(label, **kwargs):
        concurrent_change()
        return "other-model"

    def confirm(*args, **kwargs):
        concurrent_change()
        return True

    monkeypatch.setattr(tui, "ask", ask)
    monkeypatch.setattr(tui, "confirm", confirm)
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: "manual")
    with pytest.raises(GatewayError) as caught:
        if operation == "parameters":
            # Keep the technical token-limit choice valid while changing the file.
            monkeypatch.setattr(tui, "ask", lambda *a, **kw: "max_tokens")
            api_tui.configure_api_parameters(path, "custom")
        elif operation == "model":
            api_tui.choose_model(path, tmp_path, "custom")
        else:
            api_tui.delete_connection(path, "custom")
    assert caught.value.code == "config_changed"
    saved = GatewayConfig.load(path)
    assert saved.routes["parallel"] == ["claude"]
    assert saved.backends["custom"].model == "org/model"


def test_basic_edit_preserves_explicit_chat_kind(monkeypatch, tmp_path):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    config = GatewayConfig.load(path)
    config.backends["custom"] = BackendConfig(
        provider="openai_compatible",
        account="custom",
        model="custom-mercury-decide-chat",
        model_kind="chat",
        base_url="http://localhost:1234/v1",
        api_format="chat_completions",
    )
    save_config(config, path)
    form(monkeypatch, auth_mode="keep")
    original_ask = tui.ask
    monkeypatch.setattr(
        tui,
        "ask",
        lambda label, **kw: (
            config.backends["custom"].model
            if label == "ID модели в сервисе"
            else original_ask(label, **kw)
        ),
    )
    api_tui.configure_api(path, tmp_path, "custom")
    saved = GatewayConfig.load(path).backends["custom"]
    assert saved.model_kind == "chat" and not saved.decision_model


def test_delete_connection_updates_fallback_routes(monkeypatch, tmp_path):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    config = GatewayConfig.load(path)
    config.routes["fallback"] = ["custom", "claude"]
    save_config(config, path)
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: True)
    api_tui.delete_connection(path, "custom")
    saved = GatewayConfig.load(path)
    assert "custom" not in saved.backends
    assert "custom-default" not in saved.routes
    assert saved.routes["fallback"] == ["claude"]
    assert saved.backends["claude"] == config.backends["claude"]


@pytest.mark.parametrize("source", ["catalog", "manual"])
def test_choose_model_and_preserve_connection(monkeypatch, tmp_path, source):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    before = GatewayConfig.load(path)
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: source)
    monkeypatch.setattr(tui, "ask", lambda *args, **kwargs: "other/model")

    async def catalog(state_dir, backend):
        assert backend == before.backends["custom"]
        return [{"id": "other/model"}]

    monkeypatch.setattr(api_tui, "fetch_models", catalog)
    api_tui.choose_model(path, tmp_path, "custom")
    after = GatewayConfig.load(path)
    assert after.backends["custom"].model == "other/model"
    assert after.backends["custom"].base_url == before.backends["custom"].base_url
    assert after.routes == before.routes


@pytest.mark.parametrize("api_format", ["decisions", "systemone"])
@pytest.mark.parametrize("same_model", [False, True])
def test_catalog_without_modalities_preserves_decision_connection(
    monkeypatch, tmp_path, api_format, same_model
):
    path = setup(tmp_path)
    config = GatewayConfig.load(path)
    config.backends["custom"] = BackendConfig(
        provider="openai_compatible",
        account="custom",
        model="custom-decision",
        base_url="https://custom.test/api/" + api_format,
        model_kind="decision",
    )
    save_config(config, path)
    asyncio.run(api_tui.save_auth(tmp_path, config.backends["custom"], "saved-key"))
    before = GatewayConfig.load(path)
    selected_model = "custom-decision" if same_model else "other-decision"
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: "catalog")

    def ask(label, **kwargs):
        if label == "Модель":
            return selected_model
        assert label == "Формат API" and not same_model
        assert api_format in kwargs["choices"]
        return api_format

    monkeypatch.setattr(tui, "ask", ask)

    async def catalog(state_dir, backend):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"data": [{"id": selected_model}]})
            )
        ) as client:
            auth = AuthManager(CredentialStore(state_dir), client)
            return await OpenAICompatibleAdapter(client, auth, backend).list_models()

    monkeypatch.setattr(api_tui, "fetch_models", catalog)
    api_tui.choose_model(path, tmp_path, "custom")
    saved = GatewayConfig.load(path).backends["custom"]
    assert saved.model == selected_model
    assert saved.decision_model is True
    assert saved.api_format == api_format
    assert saved.base_url == config.backends["custom"].base_url
    if same_model:
        assert GatewayConfig.load(path) == before


@pytest.mark.parametrize("source", ["catalog", "manual"])
@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
@pytest.mark.parametrize(
    "endpoint,expected_base",
    [
        ("https://custom.test/api/decisions", "https://custom.test/api"),
        ("https://custom.test/systemone", "https://custom.test"),
        ("https://openrouter.ai/api/alpha/decisions", "https://openrouter.ai/api/v1"),
    ],
)
def test_choose_chat_model_updates_protocol_and_endpoint(
    monkeypatch, tmp_path, source, api_format, endpoint, expected_base
):
    path = setup(tmp_path)
    config = GatewayConfig.load(path)
    config.backends["custom"] = BackendConfig(
        provider="openai_compatible",
        account="custom",
        model="custom-decision",
        base_url=endpoint,
        model_kind="decision",
        decision_box=True,
        timeout_seconds=240,
        max_concurrency=3,
    )
    config.routes["custom-default"] = ["custom"]
    save_config(config, path)
    asyncio.run(api_tui.save_auth(tmp_path, config.backends["custom"], "saved-key"))
    record = CredentialStore(tmp_path).get("account:custom")
    values = {
        "Модель": "org/chat",
        "ID модели": "org/chat",
        "Формат API": api_format,
    }
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: source)
    monkeypatch.setattr(tui, "ask", lambda label, **kwargs: values[label])

    async def catalog(state_dir, backend):
        return [{"id": "org/chat", "kind": "chat"}]

    monkeypatch.setattr(api_tui, "fetch_models", catalog)
    api_tui.choose_model(path, tmp_path, "custom")
    after = GatewayConfig.load(path)
    selected = after.backends["custom"]
    assert selected.model == "org/chat"
    assert selected.decision_model is False
    assert selected.api_format == api_format
    assert selected.api_base_url == expected_base
    assert selected.decision_box is None
    assert selected.timeout_seconds == 240 and selected.max_concurrency == 3
    assert after.routes == config.routes
    assert CredentialStore(tmp_path).get("account:custom") == record
    calls = []

    def handler(request):
        calls.append((str(request.url), request.headers.get("authorization")))
        result = (
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}
                ],
            }
            if api_format == "responses"
            else {"choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}]}
        )
        return httpx.Response(200, json=result)

    async def generate():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAICompatibleAdapter(
                client, AuthManager(CredentialStore(tmp_path), client), selected
            )
            request = ChatRequest(model="custom", messages=[{"role": "user", "content": "Hi"}])
            assert (await adapter.generate(request)).content == "Hello"

    asyncio.run(generate())
    suffix = "/responses" if api_format == "responses" else "/chat/completions"
    assert calls == [(expected_base + suffix, "Bearer saved-key")]


@pytest.mark.parametrize("api_format", ["decisions", "systemone"])
def test_manual_decision_model_change_can_keep_existing_protocol(monkeypatch, tmp_path, api_format):
    path = setup(tmp_path)
    config = GatewayConfig.load(path)
    config.backends["custom"] = BackendConfig(
        provider="openai_compatible",
        account="custom",
        model="old-decision",
        base_url="https://custom.test/api/" + api_format,
    )
    save_config(config, path)
    values = {"ID модели": "new-decision", "Формат API": api_format}
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: "manual")
    monkeypatch.setattr(tui, "ask", lambda label, **kwargs: values[label])
    api_tui.choose_model(path, tmp_path, "custom")
    selected = GatewayConfig.load(path).backends["custom"]
    assert selected.model == "new-decision"
    assert selected.decision_model is True
    assert selected.api_format == api_format
    assert selected.base_url == config.backends["custom"].base_url


def test_cancel_protocol_selection_does_not_save_model(monkeypatch, tmp_path):
    path = setup(tmp_path)
    config = GatewayConfig.load(path)
    config.backends["custom"] = BackendConfig(
        provider="openai_compatible",
        account="custom",
        model="old-decision",
        base_url="https://custom.test/api/decisions",
    )
    save_config(config, path)
    before = path.read_bytes()
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: "manual")

    def ask(label, **kwargs):
        if label == "Формат API":
            raise tui.Back()
        return "org/chat"

    monkeypatch.setattr(tui, "ask", ask)
    with pytest.raises(tui.Back):
        api_tui.choose_model(path, tmp_path, "custom")
    assert path.read_bytes() == before
    assert CredentialStore(tmp_path).all_accounts() == []


def test_api_panel_excludes_subscription_and_colab(monkeypatch, tmp_path):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    config = GatewayConfig.load(path)
    config.backends["colab"] = BackendConfig(
        provider="colab_managed", account="colab", model="tiny"
    )
    save_config(config, path)

    def menu(options, **kwargs):
        keys = [key for key, _ in options]
        assert "connection:custom" in keys
        assert all(
            "claude" not in key and "chatgpt" not in key and "colab" not in key for key in keys
        )
        raise tui.Back()

    monkeypatch.setattr(tui, "menu", menu)
    with pytest.raises(tui.Back):
        api_tui.manage_api(path, tmp_path)


def test_cannot_overwrite_subscription_account(monkeypatch, tmp_path):
    path = setup(tmp_path)
    store = CredentialStore(tmp_path)
    store.put(
        "account:custom",
        {
            "account": "custom",
            "provider": "codex_subscription",
            "kind": "account",
            "state": "CONNECTED",
        },
    )
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    assert store.get("account:custom")["provider"] == "codex_subscription"
    backend = GatewayConfig.load(path).backends["custom"]
    assert backend.account != "custom"
    assert store.get("account:" + backend.account)["provider"] == "openai_compatible"


def test_legacy_key_still_works_after_edit(monkeypatch, tmp_path):
    path = setup(tmp_path)
    config = GatewayConfig.load(path)
    config.backends["legacy"] = BackendConfig(provider="openai_api", account="custom", model="old")
    save_config(config, path)

    async def save_key():
        async with httpx.AsyncClient() as client:
            await AuthManager(CredentialStore(tmp_path), client).save_api_key("custom", "old-key")

    asyncio.run(save_key())
    form(monkeypatch, base_url="https://api.openai.com/v1", auth_mode="keep")
    api_tui.configure_api(path, tmp_path, "legacy")
    saved = GatewayConfig.load(path)
    assert saved.backends["legacy"].provider == "openai_api"
    assert saved.backends["legacy"].api_format == "chat_completions"
    assert CredentialStore(tmp_path).get("account:custom")["api_key"] == "old-key"


def test_basic_form_accepts_model_names_and_full_urls_without_technical_questions(
    monkeypatch, tmp_path
):
    path = setup(tmp_path)
    name = "minimax/minimax-m3.1-flash:free"
    values = {
        "Название подключения": name,
        "Адрес API": "https://openrouter.ai/api/v1/chat/completions",
        "ID модели в сервисе": name,
        "API-ключ": "test-key",
        "Имя модели в шлюзе": "minimax-default",
    }
    monkeypatch.setattr(tui, "ask", lambda label, **kwargs: values[label])
    monkeypatch.setattr(tui, "confirm", lambda *a, **kw: pytest.fail("No capability questions"))
    monkeypatch.setattr(tui, "number", lambda *a, **kw: pytest.fail("No technical parameters"))
    monkeypatch.setattr(tui, "menu", lambda *a, **kw: "key")
    api_tui.configure_api(path, tmp_path)
    item = GatewayConfig.load(path).backends[name]
    assert item.base_url == "https://openrouter.ai/api/v1"
    assert item.model == name and item.api_format == "chat_completions"
    assert "/" not in item.account and ":" not in item.account
    assert item.api_streaming is True and item.api_include_usage is False


def test_basic_edit_preserves_advanced_api_parameters(monkeypatch, tmp_path):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)
    config = GatewayConfig.load(path)
    advanced = {
        "api_streaming": False,
        "api_include_usage": True,
        "api_json_output": True,
        "api_token_limit": "max_completion_tokens",
        "timeout_seconds": 240,
        "max_concurrency": 3,
    }
    for key, value in advanced.items():
        setattr(config.backends["custom"], key, value)
    save_config(config, path)
    form(monkeypatch, auth_mode="keep")
    monkeypatch.setattr(tui, "confirm", lambda *a, **kw: pytest.fail("No capability questions"))
    monkeypatch.setattr(tui, "number", lambda *a, **kw: pytest.fail("No technical parameters"))
    api_tui.configure_api(path, tmp_path, "custom")
    item = GatewayConfig.load(path).backends["custom"]
    assert {key: getattr(item, key) for key in advanced} == advanced


@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("change_model", [False, True])
def test_edit_can_leave_catalog_selected_decision_mode(
    monkeypatch, tmp_path, api_format, change_model
):
    path = setup(tmp_path)
    form(monkeypatch)
    api_tui.configure_api(path, tmp_path)

    async def catalog(state_dir, backend):
        return [{"id": "custom-decision", "kind": "decision"}]

    monkeypatch.setattr(api_tui, "fetch_models", catalog)
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: "catalog")
    monkeypatch.setattr(tui, "ask", lambda *args, **kwargs: "custom-decision")
    api_tui.choose_model(path, tmp_path, "custom")
    before = GatewayConfig.load(path)
    assert before.backends["custom"].model_kind == "decision"
    credentials = CredentialStore(tmp_path).get("account:custom")
    suffix = "/responses" if api_format == "responses" else "/chat/completions"
    values = {
        "Адрес API": "http://localhost:1234/v1" + suffix,
        "ID модели в сервисе": "org/chat" if change_model else "custom-decision",
    }
    monkeypatch.setattr(tui, "ask", lambda label, **kwargs: values[label])
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: "keep")
    api_tui.configure_api(path, tmp_path, "custom")
    after = GatewayConfig.load(path)
    selected = after.backends["custom"]
    assert selected.api_format == api_format
    assert selected.decision_model is False
    selected_adapter = OpenAICompatibleAdapter(None, None, selected)
    assert selected_adapter.capabilities.chat is True
    assert after.routes == before.routes
    assert CredentialStore(tmp_path).get("account:custom") == credentials
