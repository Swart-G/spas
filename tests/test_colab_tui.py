from unittest.mock import AsyncMock

import pytest

from llm.gateway import tui as main_tui
from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.manager import account_key
from llm.gateway.cli import initial_config, initialize
from llm.gateway.colab import tui
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.errors import GatewayError


def replies(monkeypatch, values):
    values = iter(values)
    secrets = []

    def ask(label, **options):
        value = next(values)
        if options.get("choices"):
            assert value in options["choices"]
        if "token" in label or "secret" in label:
            assert options.get("password") is True
            secrets.append(value)
        return value

    monkeypatch.setattr(tui, "ask", ask)
    return secrets


def test_google_oauth_client_can_be_configured_from_unified_tui(tmp_path, monkeypatch, capsys):
    replies(
        monkeypatch,
        ["google-main", "", "123-spas.apps.googleusercontent.com", "private-client-secret"],
    )
    tui.google_form(tmp_path, "configure")
    record = CredentialStore(tmp_path).get(account_key("google-main"))
    assert record["client_id"] == "123-spas.apps.googleusercontent.com"
    assert record["provider"] == "google_colab" and record["state"] == "REAUTH_REQUIRED"
    assert "private-client-secret" not in capsys.readouterr().out


def test_managed_colab_wizard_saves_model_gpu_transport_and_encrypted_tokens(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    store = CredentialStore(tmp_path)
    store.put(
        account_key("google-main"),
        {
            "kind": "account",
            "provider": "google_colab",
            "account": "google-main",
            "state": "CONNECTED",
        },
    )
    secrets = replies(
        monkeypatch,
        [
            "colab-gpu",
            "google-main",
            "1",
            "",
            "team/tiny",
            "main",
            "auto",
            "private-hf-token",
            "colab-default",
        ],
    )
    monkeypatch.setattr(tui, "integer", lambda *args, **kwargs: kwargs["default"])
    monkeypatch.setattr(tui, "number", lambda *args, **kwargs: 240)

    async def available(*args):
        return [
            {
                "key": {"variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"},
                "eligible": True,
            }
        ]

    monkeypatch.setattr(tui, "available_specs", available)
    tui.configure_deployment(initial_config(), path, tmp_path)
    config = GatewayConfig.load(path)
    backend = config.backends["colab-gpu"]
    assert backend.provider == "colab_managed" and backend.base_url is None
    assert backend.timeout_seconds == 240 and backend.max_concurrency == 1
    assert config.routes["colab-default"] == ["colab-gpu"]
    assert config.backends["claude"] == initial_config().backends["claude"]
    record = store.get(account_key("colab-gpu"))
    assert (
        record["settings"]["accelerator"] == "T4"
        and record["settings"]["model_repo"] == "team/tiny"
    )
    assert record["settings"]["google_account"] == "google-main"
    assert record["hf_token"] == "private-hf-token" and record["server_key"]
    assert secrets == ["private-hf-token"]
    assert (
        "private-hf-token" not in path.read_text()
        and "private-hf-token" not in capsys.readouterr().out
    )
    assert all(b"private-hf-token" not in file.read_bytes() for file in store.records.glob("*.enc"))


def test_api_access_denial_leaves_gateway_configuration_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    store = CredentialStore(tmp_path)
    store.put(
        account_key("google-main"),
        {"kind": "account", "provider": "google_colab", "account": "google-main"},
    )
    original = path.read_bytes()
    replies(monkeypatch, ["colab-gpu", "google-main"])

    async def denied(*args):
        raise GatewayError("Project approval required", code="colab_api_access_denied")

    monkeypatch.setattr(tui, "available_specs", denied)
    with pytest.raises(GatewayError):
        tui.configure_deployment(initial_config(), path, tmp_path)
    assert path.read_bytes() == original
    assert store.get(account_key("colab-gpu")) is None


def test_full_colab_management_is_accessible_from_main_tui(tmp_path, monkeypatch):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    selections = iter(["3", "q"])

    def menu(*args, **kwargs):
        choice = next(selections)
        if choice == "b":
            raise main_tui.Back()
        return choice

    calls = []
    monkeypatch.setattr(main_tui, "menu", menu)
    monkeypatch.setattr(main_tui, "ask", lambda *args, **kwargs: "")
    monkeypatch.setattr(tui, "manage_full_colab", lambda *args: calls.append(args))
    main_tui.run_tui(path, tmp_path)
    assert len(calls) == 1 and calls[0][1:] == (path, tmp_path)


def test_escape_google_form_returns_to_google_menu(monkeypatch, tmp_path):
    choices = iter(["configure", "b"])
    calls = []

    def menu(*args, **kwargs):
        choice = next(choices)
        calls.append(choice)
        if choice == "b":
            raise main_tui.Back()
        return choice

    monkeypatch.setattr(tui, "menu", menu)
    monkeypatch.setattr(tui, "ask", lambda *args, **kwargs: (_ for _ in ()).throw(main_tui.Back()))
    with pytest.raises(main_tui.Back):
        tui.manage_google(tmp_path)
    assert calls == ["configure", "b"]


def test_escape_google_menu_returns_to_full_colab_menu(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    choices = iter(["google", "b", "b"])
    calls = []

    def menu(*args, **kwargs):
        calls.append(kwargs["title"])
        choice = next(choices)
        if choice == "b":
            raise main_tui.Back()
        return choice

    monkeypatch.setattr(tui, "menu", menu)
    with pytest.raises(main_tui.Back):
        tui.manage_full_colab(initial_config(), path, tmp_path)
    assert calls == ["Google Colab", "Google-аккаунт", "Google Colab"]


def test_colab_screen_opens_existing_connection_without_legacy_http_form(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    config = GatewayConfig.load(path)
    config.backends["gpu-one"] = BackendConfig(
        provider="colab_managed", account="profile-one", model="team/tiny"
    )
    config.backends["legacy-http"] = BackendConfig(
        provider="remote_inference", account="old-http", model="old", base_url="https://old.test"
    )
    main_tui.save_config(config, path)
    CredentialStore(tmp_path).put(
        account_key("profile-one"),
        {
            "kind": "account",
            "account": "profile-one",
            "provider": "colab_managed",
            "stage": "READY",
            "state": "CONNECTED",
            "runtime_name": "runtimes/one",
            "settings": {"accelerator": "T4"},
        },
    )
    selections = iter(["connection:gpu-one", "status", "b", "b"])
    menus = []

    def menu(options, **kwargs):
        menus.append(options)
        choice = next(selections)
        if choice == "b":
            raise main_tui.Back()
        assert choice in dict(options)
        return choice

    monkeypatch.setattr(tui, "menu", menu)
    monkeypatch.setattr(
        tui,
        "ask",
        lambda label, **options: (
            "" if label.startswith("Enter") else pytest.fail("Повторный выбор подключения")
        ),
    )
    action = AsyncMock()
    monkeypatch.setattr(tui, "deployment_action", action)
    with pytest.raises(main_tui.Back):
        tui.manage_full_colab(config, path, tmp_path)
    action.assert_awaited_once_with(tmp_path, "profile-one", "status")
    assert "connection:legacy-http" not in dict(menus[0])
    assert "create" in dict(menus[0]) and "configure" in dict(menus[1])
    assert "create" not in dict(menus[1])
    assert "Добавить или настроить сервер" not in str(menus)
    output = capsys.readouterr().out
    assert "T4" in output and "готов" in output.lower()


@pytest.mark.parametrize("failed", [False, True])
def test_remove_managed_connection_releases_runtime_before_cleaning_routes(
    tmp_path, monkeypatch, failed
):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    config = GatewayConfig.load(path)
    config.backends["gpu"] = BackendConfig(
        provider="colab_managed", account="gpu-main", model="team/tiny"
    )
    config.routes.update({"gpu-default": ["gpu"], "fallback": ["gpu", "claude"]})
    main_tui.save_config(config, path)
    deletion = AsyncMock(side_effect=GatewayError("Runtime недоступен") if failed else None)
    reload = AsyncMock()
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(tui, "deployment_action", deletion)
    monkeypatch.setattr(tui, "apply_gateway", reload)
    if failed:
        with pytest.raises(GatewayError):
            tui.full_colab_action(config, path, tmp_path, "remove", name="gpu")
        assert GatewayConfig.load(path) == config
        reload.assert_not_awaited()
    else:
        tui.full_colab_action(config, path, tmp_path, "remove", name="gpu")
        saved = GatewayConfig.load(path)
        assert "gpu" not in saved.backends and "gpu-default" not in saved.routes
        assert saved.routes["fallback"] == ["claude"]
        reload.assert_awaited_once_with(tmp_path)
    deletion.assert_awaited_once_with(tmp_path, "gpu-main", "delete")
