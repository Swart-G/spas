import pytest

from llm.gateway import tui
from llm.gateway.cli import initial_config, initialize, save_config
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.errors import GatewayError


def answers(monkeypatch, values):
    responses = iter(values)

    def ask(*args, **kwargs):
        value = next(responses)
        if "choices" in kwargs:
            assert value in kwargs["choices"]
        return value

    def select(values, default, render):
        value = next(responses)
        tui.console.print(
            render(default if default in values else values[0]),
            markup=False,
            highlight=False,
            end="",
        )
        if value == "b":
            raise tui.Back()
        assert value in values
        return value

    monkeypatch.setattr(tui, "ask", ask)
    monkeypatch.setattr(tui, "select", select)


@pytest.mark.parametrize("stream", [False, True])
def test_tui_probe(monkeypatch, tmp_path, stream):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["1", "p", "claude", "hello", "", "q"])
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: stream)
    calls = []

    async def perform(args, config_path, state_dir):
        calls.append(args)

    monkeypatch.setattr(tui, "perform", perform)
    tui.run_tui(path, tmp_path)
    assert len(calls) == 1
    assert vars(calls[0]) == dict(command="probe", backend="claude", prompt="hello", stream=stream)


@pytest.mark.parametrize("provider", ["claude", "chatgpt"])
def test_tui_login(monkeypatch, tmp_path, provider):
    answers(monkeypatch, ["login", provider, f"{provider}-main"])
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: True)
    calls = []

    async def perform(args, config_path, state_dir):
        calls.append(args)

    monkeypatch.setattr(tui, "perform", perform)
    tui.manage_accounts(tmp_path / "config", tmp_path)
    assert calls[0].account == f"{provider}-main"
    assert calls[0].provider == provider
    assert calls[0].consent == (provider == "chatgpt")


def test_tui_backend_and_routing(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["claude", "claude_subscription", "other-account", "opus", "sdk"])
    monkeypatch.setattr(tui, "number", lambda *args, **kw: 2 if kw.get("integer") else 120)
    tui.configure_backend(initial_config(), path)
    config = GatewayConfig.load(path)
    backend = config.backends["claude"]
    assert (backend.account, backend.model, backend.transport) == ("other-account", "opus", "sdk")
    assert (backend.timeout_seconds, backend.max_concurrency) == (120, 2)
    answers(monkeypatch, ["diagnostic", "claude,chatgpt", "rate_limited,provider_timeout"])
    tui.manage_routes(config, path)
    config = GatewayConfig.load(path)
    assert config.routes["diagnostic"] == ["claude", "chatgpt"]
    assert config.fallback_on == ["rate_limited", "provider_timeout"]


def test_tui_probe_error_returns_to_menu(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["1", "p", "claude", "hello", "", "q"])
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: False)

    async def perform(args, config_path, state_dir):
        raise GatewayError("Needs login", code="reauth_required")

    monkeypatch.setattr(tui, "perform", perform)
    tui.run_tui(path, tmp_path)


@pytest.fixture
def mixed_config():
    config = initial_config()
    config.backends["paid"] = BackendConfig(
        provider="openai_api", account="openai-paid", model="api-model"
    )
    config.backends["colab"] = BackendConfig(
        provider="colab_managed",
        account="colab-main",
        model="remote-model",
    )
    config.routes.update(
        {
            "paid-default": ["paid"],
            "remote-default": ["colab"],
            "mixed": ["claude", "paid"],
        }
    )
    return config


def test_home_has_three_modes_without_connection_settings(monkeypatch, tmp_path, capsys):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    capsys.readouterr()
    answers(monkeypatch, ["q"])
    tui.run_tui(path, tmp_path)
    output = capsys.readouterr().out
    for label, _ in tui.MODES.values():
        assert label in output
    for detail in ["claude-main", "sonnet", "gpt-6.1-sol", "API-ключи и аккаунты"]:
        assert detail not in output


@pytest.mark.parametrize(
    "mode, expected, hidden",
    [
        ("1", "claude-main", ["openai-paid", "colab-main"]),
        ("2", "openai-paid", ["claude-main", "colab-main"]),
        ("3", "colab", ["claude-main", "openai-paid"]),
    ],
)
def test_mode_connections_and_back_navigation(
    monkeypatch, tmp_path, capsys, mixed_config, mode, expected, hidden
):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    save_config(mixed_config, path)
    capsys.readouterr()
    answers(monkeypatch, [mode, "b", "q"])
    tui.run_tui(path, tmp_path)
    output = capsys.readouterr().out
    assert expected in output
    assert all(account not in output for account in hidden)
    assert output.count("Выберите способ работы") == 2


def test_subscription_probe_excludes_paid_and_mixed_routes(monkeypatch, tmp_path, mixed_config):
    def ask(*args, **kwargs):
        assert set(kwargs["choices"]) == {"chatgpt", "claude", "codex-default", "sonnet-default"}
        raise KeyboardInterrupt

    monkeypatch.setattr(tui, "ask", ask)
    with pytest.raises(KeyboardInterrupt):
        tui.probe(tui.mode_config(mixed_config, "1"), tmp_path / "config", tmp_path)


def test_api_configuration_preserves_other_modes(monkeypatch, tmp_path, mixed_config):
    path = tmp_path / "gateway.toml"
    answers(monkeypatch, ["paid", "openai-paid", "new-model"])
    monkeypatch.setattr(tui, "number", lambda *args, **kw: 2 if kw.get("integer") else 120)
    tui.configure_backend(mixed_config, path, "2")
    saved = GatewayConfig.load(path)
    assert saved.backends["paid"].model == "new-model"
    assert saved.backends["paid"].provider == "openai_api"
    assert saved.backends["claude"] == mixed_config.backends["claude"]
    assert saved.backends["colab"] == mixed_config.backends["colab"]
    assert saved.routes == mixed_config.routes
    answers(monkeypatch, ["claude"])
    with pytest.raises(GatewayError, match="другого режима"):
        tui.configure_backend(saved, path, "2")
    assert GatewayConfig.load(path) == saved


@pytest.mark.parametrize(
    "mode, required, forbidden",
    [
        ("1", "login", "api-key"),
        ("2", "api-key", "login"),
    ],
)
def test_auth_actions_stay_in_mode(monkeypatch, tmp_path, mode, required, forbidden):
    def select(values, default, render):
        assert required in values
        assert forbidden not in values
        assert "b" not in values
        raise tui.Back()

    monkeypatch.setattr(tui, "select", select)
    with pytest.raises(tui.Back):
        tui.manage_accounts(tmp_path / "config", tmp_path, mode)


def test_cancel_action_returns_to_same_section(monkeypatch, tmp_path, capsys):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["1", "m", "", "b", "q"])

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(tui, "manage_models", cancel)
    tui.run_tui(path, tmp_path)
    assert "Действие отменено" in capsys.readouterr().out


def test_empty_api_probe_does_not_prompt_or_send(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["2", "p", "", "b", "q"])
    tui.run_tui(path, tmp_path)


def test_colab_catalog_is_scoped_to_selected_connection(monkeypatch, tmp_path, mixed_config):
    from llm.gateway.colab import tui as colab_tui

    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    save_config(mixed_config, path)
    answers(monkeypatch, ["3", "q"])
    selections = iter(["connection:colab", "models", "b", "b"])

    def colab_menu(*args, **kwargs):
        choice = next(selections)
        if choice == "b":
            raise tui.Back()
        return choice

    monkeypatch.setattr(colab_tui, "menu", colab_menu)
    monkeypatch.setattr(colab_tui, "ask", lambda *args, **kwargs: "")
    calls = []
    monkeypatch.setattr(colab_tui, "execute", lambda *args, **kwargs: calls.append((args, kwargs)))
    tui.run_tui(path, tmp_path)
    assert calls == [((path, tmp_path, "models"), {"backend": "colab"})]


def test_gateway_can_probe_mixed_route(monkeypatch, tmp_path, mixed_config):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    save_config(mixed_config, path)
    answers(monkeypatch, ["g", "p", "mixed", "hello", "", "b", "q"])
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: False)
    calls = []
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: calls.append(kwargs))
    tui.run_tui(path, tmp_path)
    assert calls == [{"backend": "mixed", "prompt": "hello", "stream": False}]


def test_escape_nested_menu_returns_without_pause(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["1", "m", "b", "a", "b", "b", "q"])
    tui.run_tui(path, tmp_path)


def test_escape_on_home_keeps_home_open(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["b", "q"])
    tui.run_tui(path, tmp_path)


def test_escape_during_configuration_does_not_save(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    before = path.read_bytes()
    monkeypatch.setattr(tui, "input_text", lambda *args, **kwargs: "new-connection")

    def cancel(*args, **kwargs):
        raise tui.Back()

    monkeypatch.setattr(tui, "select", cancel)
    with pytest.raises(tui.Back):
        tui.configure_backend(initial_config(), path, "1")
    assert path.read_bytes() == before


def test_escape_provider_returns_to_accounts_menu(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    replies = iter(["login", "b", "b"])

    def select(values, default, render):
        value = next(replies)
        if value == "b":
            raise tui.Back()
        assert value in values
        return value

    monkeypatch.setattr(tui, "select", select)
    with pytest.raises(tui.Back):
        tui.manage_accounts(path, tmp_path, "1")
    assert tui.CredentialStore(tmp_path).all_accounts() == []


def test_tui_paid_key_is_saved_without_echo(monkeypatch, tmp_path, capsys):
    replies = iter(["openai-paid", "secret-api-key"])
    monkeypatch.setattr(tui, "select", lambda *args: "api-key")
    monkeypatch.setattr(tui, "input_text", lambda *args, **kwargs: next(replies))
    tui.manage_accounts(tmp_path / "config", tmp_path, "2")
    record = tui.CredentialStore(tmp_path).get("account:openai-paid")
    assert record["api_key"] == "secret-api-key"
    assert "secret-api-key" not in capsys.readouterr().out
