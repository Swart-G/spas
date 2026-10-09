import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.validation import Validator

from llm.gateway.terminal import Back, input_text, select


@pytest.mark.parametrize(
    "keys, expected",
    [
        ("\r", "a"),
        ("\x1b[B\r", "b"),
        ("\x1b[A\r", "c"),
        ("\x1b[C\x1b[C\x1b[D\r", "b"),
        ("\t\r", "b"),
        ("\x1b[F\r", "c"),
        ("\x1b[B\x1b[H\r", "a"),
    ],
)
def test_arrow_selection(keys, expected):
    rendered = []

    def render(value):
        rendered.append(value)
        if value == expected:
            pipe.send_text("\r")
        return "▶ " + value

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys[:-1])
        assert select(["a", "b", "c"], "a", render) == expected
    assert rendered[-1] == expected


@pytest.mark.parametrize(
    "keys, exception",
    [
        ("\x1b", Back),
        ("\x03", KeyboardInterrupt),
        ("\x04", EOFError),
    ],
)
def test_cancel_menu_without_selection(keys, exception):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        with pytest.raises(exception):
            select(["dangerous"], None, lambda value: "▶ " + value)


def test_escape_cancels_text_input():
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("unsaved\x1b")
        with pytest.raises(Back):
            input_text("Имя подключения")


def test_text_default_and_password_input():
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\r")
        assert input_text("Модель", default="sonnet") == "sonnet"
        pipe.send_text("secret\r")
        assert input_text("Ключ", password=True) == "secret"


def test_numeric_input_validation_keeps_form_open():
    validator = Validator.from_callable(str.isdigit, error_message="Введите число")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        # Invalid Enter must keep the form open; replace the text and retry.
        pipe.send_text("oops\r\x01\x0b12\r")
        assert input_text("Количество", validator=validator) == "12"
