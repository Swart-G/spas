
"""Ручной ввод argv: разбор кавычек без выполнения shell-операторов."""

import argparse
import asyncio
import logging
import sys
import shlex
import unicodedata

from mcp.server.mcpserver.exceptions import ToolError

from .Server import configured_server
from .Runner import OUTPUT_LIMIT_BYTES
from .Logging import setup_logging, shutdown_logging

BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def terminal_text(text: str) -> str:
    """Показать управляющие символы как текст, не команды терминалу.

    Сохраняем переводы строк, табуляцию и обычный Unicode (включая ZWJ-emoji).
    ESC, C1, CR и bidi-контроли показываем явно. Результат MCP не изменяется.
    """
    return "".join(
        char.encode("unicode_escape").decode("ascii")
        if char not in "\n\t" and (unicodedata.category(char) in {"Cc", "Cs"} or char in BIDI_CONTROLS)
        else char
        for char in text
    )


async def run_manual(*, allow_unchecked: bool = False) -> None:
    server = configured_server(allow_unchecked=allow_unchecked)

    print("Commands run inside the restricted Docker container.")
    print("Safety Checker: " + ("DISABLED for development" if allow_unchecked else "enabled"))
    print("Type a command, or type quit to leave.")

    while True:
        try:
            command = input("executor> ")

        except (EOFError, KeyboardInterrupt):
            print()
            break

        if command.strip() == "quit":
            break

        if not command.strip():
            continue

        try:
            argv = shlex.split(command)
        except ValueError:
            print("Invalid quotes. Example: uname -a", file=sys.stderr)
            continue

        try:
            result = await server.call_tool(
                "execute_command",
                {
                    "command": argv,
                    "context": {
                        "incident_id": "manual",
                        "reason": (
                            "Command entered by a user "
                            "in the manual Executor console"
                        ),
                    },
                },
            )

        except ToolError as exc:
            print(
                f"Executor error: {terminal_text(str(exc))}",
                file=sys.stderr,
            )
            continue

        data = result.structured_content

        if data["stdout"]:
            print(
                terminal_text(data["stdout"]),
                end=(
                    ""
                    if data["stdout"].endswith("\n")
                    else "\n"
                ),
            )

        if data["stderr"]:
            print(
                terminal_text(data["stderr"]),
                end=(
                    ""
                    if data["stderr"].endswith("\n")
                    else "\n"
                ),
                file=sys.stderr,
            )

        if data["error"]:
            print(
                f"Executor error: {terminal_text(data['error'])}",
                file=sys.stderr,
            )

        if data["stdout_truncated"]:
            print(
                f"[stdout truncated: beginning only, up to "
                f"{OUTPUT_LIMIT_BYTES} UTF-8 bytes]",
                file=sys.stderr,
            )

        if data["stderr_truncated"]:
            print(
                f"[stderr truncated: beginning only, up to "
                f"{OUTPUT_LIMIT_BYTES} UTF-8 bytes]",
                file=sys.stderr,
            )

        print(
            f"status={data['status']} "
            f"exit_code={data['exit_code']} "
            f"request_id={data['request_id']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enter Linux commands through Executor manually"
    )

    parser.add_argument(
        "--without-safety",
        action="store_true",
        help="Acknowledge that commands run without Safety Checker",
    )

    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("executor.manual")
    status = "stopped"
    logger.info("manual_started")
    try:
        asyncio.run(run_manual(allow_unchecked=args.without_safety))
    except KeyboardInterrupt:
        status = "interrupted"
    except Exception:
        status = "error"
        logger.error("manual_failed", exc_info=True)
        raise SystemExit(1) from None
    finally:
        logger.info("manual_stopped", extra={"status": status})
        shutdown_logging()


if __name__ == "__main__":
    main()
