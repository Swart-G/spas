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
        assert "q" not in values
        if value == "q":
            raise EOFError()
        if value == "b":
            raise tui.Back()
        assert value in values
        return value

    monkeypatch.setattr(tui, "ask", ask)
    monkeypatch.setattr(tui, "select", select)


def test_tui_probe(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["t", "claude", "hello", "", "q"])
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: pytest.fail("No stream question"))
    calls = []

    async def perform(args, config_path, state_dir):
        calls.append(args)

    monkeypatch.setattr(tui, "perform", perform)
    tui.run_tui(path, tmp_path)
    assert len(calls) == 1
    assert vars(calls[0]) == dict(
        command="probe", backend="claude", prompt="hello", stream=True, pretty=True
    )


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
    assert calls[0].consent is False


def test_tui_backend_and_routing(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["claude", "claude_subscription", "other-account", "opus", "sdk"])
    monkeypatch.setattr(tui, "number", lambda *args, **kw: 2 if kw.get("integer") else 120)
    tui.configure_backend(initial_config(), path, advanced=True)
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
    answers(monkeypatch, ["t", "claude", "hello", "", "q"])
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
        ("2", "api-model", ["claude-main", "colab-main"]),
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
    assert output.count("Подключения к моделям") == 2


def test_testing_lists_all_connections_without_duplicate_routes(
    monkeypatch, tmp_path, mixed_config
):
    def ask(*args, **kwargs):
        assert set(kwargs["choices"]) == set(mixed_config.backends)
        raise KeyboardInterrupt

    monkeypatch.setattr(tui, "ask", ask)
    with pytest.raises(KeyboardInterrupt):
        tui.probe(mixed_config, tmp_path / "config", tmp_path)


def test_api_configuration_preserves_other_modes(monkeypatch, tmp_path, mixed_config):
    path = tmp_path / "gateway.toml"
    save_config(mixed_config, path)
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
    with pytest.raises(GatewayError, match="другом разделе"):
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


def test_empty_testing_does_not_prompt_or_send(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    save_config(GatewayConfig(backends={}, routes={}), path)
    answers(monkeypatch, ["t", "", "q"])
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


def test_main_testing_can_probe_api_connection(monkeypatch, tmp_path, mixed_config):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    save_config(mixed_config, path)
    answers(monkeypatch, ["t", "paid", "hello", "", "q"])
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: False)
    calls = []
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: calls.append(kwargs))
    tui.run_tui(path, tmp_path)
    assert calls == [{"backend": "paid", "prompt": "hello", "stream": True}]


def test_escape_nested_menu_returns_without_pause(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["1", "m", "b", "a", "b", "b", "q"])
    tui.run_tui(path, tmp_path)


def test_escape_on_home_closes_interface(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["b"])
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


def test_subscription_ping_stays_in_section_without_inference(monkeypatch, tmp_path):
    path = tmp_path / "gateway.toml"
    initialize(tmp_path, path)
    answers(monkeypatch, ["1", "p", "claude", "", "b", "q"])
    calls = []
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: calls.append((args, kwargs)))
    tui.run_tui(path, tmp_path)
    assert calls == [((path, tmp_path, "status"), {"backend": "claude"})]


def test_empty_llm_text_sends_nothing(monkeypatch, tmp_path):
    answers(monkeypatch, ["claude", "   "])
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: pytest.fail("Must not send"))
    with pytest.raises(GatewayError, match="Введите текст"):
        tui.probe(initial_config(), tmp_path / "gateway.toml", tmp_path)


def test_disabled_streaming_needs_no_extra_question(monkeypatch, tmp_path, mixed_config):
    mixed_config.backends["paid"].api_streaming = False
    answers(monkeypatch, ["paid", "hello"])
    calls = []
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: pytest.fail("No question"))
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: calls.append(kwargs))
    tui.probe(mixed_config, tmp_path / "gateway.toml", tmp_path)
    assert calls == [{"backend": "paid", "prompt": "hello", "stream": False}]


async def test_stream_error_starts_after_a_newline(monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    from llm.gateway import cli
    from llm.gateway.core.models import StreamEvent

    path = tmp_path / "gateway.toml"
    save_config(initial_config(), path)

    class FailingRouter:
        def __init__(self, *args):
            pass

        async def stream(self, *args):
            yield StreamEvent("delta", text="Часть ответа")
            raise GatewayError("Соединение прервано")

    monkeypatch.setattr(cli, "Router", FailingRouter)
    with pytest.raises(GatewayError):
        await cli.perform(
            Namespace(command="probe", backend="claude", prompt="Test", stream=True, pretty=True),
            path,
            tmp_path,
        )
    assert capsys.readouterr().out.endswith("Часть ответа\n")


@pytest.mark.parametrize("stream", [False, True])
async def test_model_output_cannot_execute_terminal_controls(monkeypatch, tmp_path, capsys, stream):
    from argparse import Namespace

    from llm.gateway import cli
    from llm.gateway.core.models import Generation, StreamEvent

    text = "Hello\x1b[2J\rWorld\n\ud800"

    class ModelRouter:
        def __init__(self, *args):
            pass

        async def stream(self, *args):
            yield StreamEvent("delta", text=text)
            yield StreamEvent("done")

        async def generate(self, *args):
            return Generation(text, None, "stop")

    path = tmp_path / "gateway.toml"
    save_config(initial_config(), path)
    monkeypatch.setattr(cli, "Router", ModelRouter)
    await cli.perform(
        Namespace(command="probe", backend="claude", prompt="test", stream=stream, pretty=True),
        path,
        tmp_path,
    )
    output = capsys.readouterr().out
    assert "Hello\\x1b[2J\\x0dWorld" in output
    assert "\x1b" not in output and "\r" not in output
    assert "\\ud800" in output
