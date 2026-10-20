import base64

import pytest

from llm.gateway import api_tui, decision_tui, tui
from llm.gateway.cli import save_config
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.errors import GatewayError
from llm.gateway.terminal import Back


def configure(tmp_path, **options):
    path = tmp_path / "gateway.toml"
    backend = BackendConfig.model_validate(
        {
            "provider": "openai_compatible",
            "account": "custom",
            "model": "inception/mercury-decide:free",
            "base_url": "https://openrouter.ai/api/alpha/decisions",
            "api_format": "responses",
            **options,
        }
    )
    save_config(GatewayConfig(backends={"custom": backend}, routes={"decision": ["custom"]}), path)
    return path


@pytest.mark.parametrize(
    "mode,values,criteria",
    [
        (
            "choice",
            ["Checkout down", "Which team?", "billing;technical"],
            {"billing": None, "technical": None},
        ),
        (
            "noul",
            ["Checkout down", "Is it an outage?", "Unavailable", "Works normally"],
            {"true": "Unavailable", "false": "Works normally"},
        ),
        ("noul", ["Checkout down", "Is it an outage?", "", ""], None),
        (
            "score",
            ["Checkout down", "How severe?", "No impact;Degraded;Unavailable"],
            ["No impact", "Degraded", "Unavailable"],
        ),
    ],
)
def test_decision_test_sends_immediately_in_requested_order(
    monkeypatch, tmp_path, mode, values, criteria
):
    path = configure(tmp_path)
    labels, calls, menus = [], [], []
    replies = iter(values)

    def ask(label, **kwargs):
        labels.append(label)
        return next(replies)

    def menu(options, **kwargs):
        menus.append(options)
        assert "box" not in [key for key, _ in options]
        return mode

    monkeypatch.setattr(tui, "menu", menu)
    monkeypatch.setattr(tui, "ask", ask)
    monkeypatch.setattr(tui, "confirm", lambda *args, **kwargs: pytest.fail("No send confirmation"))
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: calls.append((args, kwargs)))
    decision_tui.run_test(path, tmp_path, "custom")
    assert len(menus) == len(calls) == 1
    assert labels[:2] == ["Состояние для оценки", "Вопрос"]
    args, options = calls[0]
    assert args == (path, tmp_path, "decide")
    assert options["backend"] == "custom" and options["pretty"] is True
    assert options["payload"]["state"] == "Checkout down"
    question = options["payload"]["questions"]["result"]
    assert question["type"] == mode and question["instructions"] == values[1]
    assert question.get("criteria") == criteria


def test_box_test_gets_image_before_question(monkeypatch, tmp_path):
    path = configure(tmp_path, model="typesafe/jev-1.14")
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image")
    replies = iter(["Photo", str(image), "Red car"])
    labels, calls = [], []

    def menu(options, **kwargs):
        assert [key for key, _ in options] == list(decision_tui.MODES)
        return "box"

    def ask(label, **kwargs):
        labels.append(label)
        return next(replies)

    monkeypatch.setattr(tui, "menu", menu)
    monkeypatch.setattr(tui, "ask", ask)
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: calls.append(kwargs))
    decision_tui.run_test(path, tmp_path, "custom")
    assert labels == ["Состояние для оценки", "Файл изображения", "Вопрос"]
    payload = calls[0]["payload"]
    assert payload["questions"] == {"result": {"type": "box", "instructions": "Red car"}}
    assert payload["state"][1]["type"] == "image_url"


@pytest.mark.parametrize(
    "mode,criteria",
    [("choice", "same;same"), ("choice", "a;"), ("score", "only one"), ("score", ";")],
)
def test_bad_criteria_do_not_send_request(monkeypatch, tmp_path, mode, criteria):
    path = configure(tmp_path)
    replies = iter(["State", "Question", criteria])
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: mode)
    monkeypatch.setattr(tui, "ask", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: pytest.fail("Must not send"))
    with pytest.raises(GatewayError):
        decision_tui.run_test(path, tmp_path, "custom")


def test_cancel_question_sends_nothing(monkeypatch, tmp_path):
    path = configure(tmp_path)
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: "noul")

    def ask(label, **kwargs):
        if label == "Вопрос":
            raise Back()
        return "State"

    monkeypatch.setattr(tui, "ask", ask)
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: pytest.fail("Must not send"))
    with pytest.raises(Back):
        decision_tui.run_test(path, tmp_path, "custom")


def test_main_testing_dispatches_to_decision_form(monkeypatch, tmp_path):
    path = configure(tmp_path)
    calls = []
    monkeypatch.setattr(decision_tui, "run_test", lambda *args: calls.append(args))
    monkeypatch.setattr(tui, "ask", lambda *args, **kwargs: "custom")
    tui.probe(GatewayConfig.load(path), path, tmp_path)
    assert calls == [(path, tmp_path, "custom")]


def test_api_connection_keeps_only_ping_as_test(monkeypatch, tmp_path):
    path = configure(tmp_path)
    calls = []

    def menu(options, **kwargs):
        keys = [key for key, _ in options]
        assert "status" in keys and "probe" not in keys and "decide" not in keys
        return "status"

    monkeypatch.setattr(tui, "menu", menu)
    monkeypatch.setattr(tui, "execute", lambda *args, **kwargs: calls.append((args, kwargs)))
    api_tui.connection_action(path, tmp_path, "custom")
    assert calls == [((path, tmp_path, "status"), {"backend": "custom"})]


def test_local_images_use_data_urls(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"local-image-data")
    part = decision_tui.image_part(path)
    assert base64.b64decode(part["image_url"]["url"].split(",")[1]) == path.read_bytes()
    path.write_text("invalid image")
    with pytest.raises(GatewayError):
        decision_tui.image_part(path)


def test_results_show_probabilities_fractional_scores_and_box(capsys):
    decision_tui.show_result(
        {
            "answers": {
                "urgent": {"type": "noul", "noul": 0.9},
                "severity": {
                    "type": "score",
                    "score": 1.8,
                    "confidence": 0.7,
                    "probabilities": {"0": 0.0, "1": 0.2, "2": 0.8},
                    "legend": {"0": "None", "1": "Some", "2": "High"},
                },
                "target": {
                    "type": "box",
                    "confidence": 0.9,
                    "box": {"top": 0.1, "bottom": 0.9, "left": 0.2, "right": 0.8},
                },
            }
        }
    )
    output = capsys.readouterr().out
    assert "1.80" in output
    assert "90.0%" in output
    assert "Верхняя" in output and "0.1000" in output
    assert "не означает вероятность правильного ответа" in " ".join(output.split())


def test_existing_decision_connection_form_hides_chat_parameters(monkeypatch, tmp_path):
    from llm.gateway.auth.credentials import CredentialStore

    path = configure(tmp_path)
    values = {
        "Адрес API": "https://openrouter.ai/api/alpha/decisions",
        "Формат API": "decisions",
        "ID модели в сервисе": "inception/mercury-decide:free",
    }
    monkeypatch.setattr(tui, "ask", lambda label, **kwargs: values[label])
    monkeypatch.setattr(tui, "menu", lambda *args, **kwargs: "none")
    monkeypatch.setattr(tui, "number", lambda *args, **kwargs: kwargs["default"])

    def confirm(label, **kwargs):
        assert "Box" in label
        return False

    monkeypatch.setattr(tui, "confirm", confirm)
    api_tui.configure_api(path, tmp_path, "custom")
    item = GatewayConfig.load(path).backends["custom"]
    assert item.api_format == "decisions"
    assert item.api_streaming is False
    assert item.supports_box is False
    assert item.decision_url == "https://openrouter.ai/api/alpha/decisions"
    assert CredentialStore(tmp_path).get("account:" + item.account)["no_auth"] is True
