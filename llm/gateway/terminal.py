"""Keyboard-driven terminal input shared by menus and configuration forms."""

from collections.abc import Callable

from prompt_toolkit import Application, PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.validation import Validator


class Back(Exception):
    """Return to the previous screen without running the selected action."""


def select(values: list[str], default: str | None, render: Callable[[str], str]) -> str:
    if not values:
        raise ValueError("A menu must contain at least one action")
    selected = values.index(default) if default in values else 0
    bindings = KeyBindings()
    cursor = Point(0, 0)

    def content():
        nonlocal cursor
        formatted = ANSI(render(values[selected]))
        plain = "".join(text for _, text, *_ in formatted.__pt_formatted_text__())
        row = next((i for i, line in enumerate(plain.splitlines()) if "▶" in line), 0)
        cursor = Point(0, row)
        return formatted

    @bindings.add("up")
    @bindings.add("left")
    @bindings.add("s-tab")
    def previous(event):
        nonlocal selected
        selected = (selected - 1) % len(values)

    @bindings.add("down")
    @bindings.add("right")
    @bindings.add("tab")
    def next_item(event):
        nonlocal selected
        selected = (selected + 1) % len(values)

    @bindings.add("home")
    def first(event):
        nonlocal selected
        selected = 0

    @bindings.add("end")
    def last(event):
        nonlocal selected
        selected = len(values) - 1

    @bindings.add("enter")
    def accept(event):
        event.app.exit(result=values[selected])

    @bindings.add("escape")
    def back(event):
        event.app.exit(exception=Back())

    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    @bindings.add("c-d")
    def eof(event):
        event.app.exit(exception=EOFError())

    control = FormattedTextControl(content, focusable=True, get_cursor_position=lambda: cursor)
    app: Application[str] = Application(
        layout=Layout(Window(control, dont_extend_height=True)),
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=True,
    )
    # Keep standalone Escape responsive while allowing complete arrow sequences.
    app.ttimeoutlen = 0.05
    app.timeoutlen = 0.2
    return app.run()


def input_text(
    label: str,
    *,
    default: str = "",
    password: bool = False,
    validator: Validator | None = None,
) -> str:
    session = text_session()
    return session.prompt(
        f"  {label}: ",
        default=default,
        is_password=password,
        validator=validator,
        validate_while_typing=False,
    )


async def input_text_async(label: str, *, password: bool = False) -> str:
    """Prompt inside an existing event loop, including OAuth login in the TUI."""
    session = text_session()
    return await session.prompt_async(f"  {label}: ", is_password=password)


def text_session() -> PromptSession[str]:
    bindings = KeyBindings()

    @bindings.add("escape")
    def back(event):
        event.app.exit(exception=Back())

    session: PromptSession[str] = PromptSession(key_bindings=bindings)
    session.app.ttimeoutlen = 0.05
    session.app.timeoutlen = 0.2
    return session
