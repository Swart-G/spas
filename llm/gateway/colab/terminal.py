"""Local operator terminal over the authenticated Jupyter terminal protocol."""

import asyncio
import json
import os
import shutil
import sys
import termios
import tty
from contextlib import asynccontextmanager, suppress
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from websockets.exceptions import WebSocketException

from ..core.errors import GatewayError
from .jupyter import NoRedirectConnect


@asynccontextmanager
async def terminal_socket(bridge):
    terminal = await bridge.request("POST", "/api/terminals", json={})
    name = terminal.get("name")
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 200
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in name
        )
    ):
        raise GatewayError("Jupyter не создал терминал.", code="colab_invalid_response")
    path = "/" + quote(name, safe="")
    parsed = urlsplit(bridge.url + "/terminals/websocket" + path)
    url = urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            parsed.path,
            urlencode({"colab-runtime-proxy-token": bridge.headers["X-Colab-Runtime-Proxy-Token"]}),
            "",
        )
    )
    try:
        async with NoRedirectConnect(
            url,
            additional_headers=bridge.headers,
            origin=urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
            open_timeout=30,
            max_size=8 * 1024 * 1024,
            proxy=None,
        ) as socket:
            yield socket
    finally:
        with suppress(GatewayError):
            await bridge.request("DELETE", "/api/terminals" + path)


async def relay(socket, read, write, size):
    """Relay raw input; Ctrl+] detaches, Ctrl+C is sent to the remote shell."""

    async def input_loop():
        previous = None
        while True:
            dimensions = size()
            if dimensions != previous:
                columns, rows = dimensions
                await socket.send(json.dumps(["set_size", rows, columns]))
                previous = dimensions
            data = await read()
            if data is None or "\x1d" in data:
                return
            if data:
                await socket.send(json.dumps(["stdin", data]))

    async def output_loop():
        async for raw in socket:
            data = json.loads(raw)
            if not isinstance(data, list) or not data:
                raise ValueError("Invalid terminal frame")
            if data[0] == "stdout" and len(data) == 2 and isinstance(data[1], str):
                write(data[1])
            elif data[0] == "disconnect":
                return

    tasks = [asyncio.create_task(input_loop()), asyncio.create_task(output_loop())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def open_terminal(manager, account):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise GatewayError(
            "Терминал Colab требует интерактивный TTY.", code="terminal_requires_tty"
        )
    bridge = await manager.connect(account)
    print("Терминал Colab. Ctrl+] — вернуться, Ctrl+C — прервать удалённую команду.")
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def readable():
        queue.put_nowait(os.read(descriptor, 4096))

    # Decode incrementally because UTF-8 characters can span multiple reads.
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    async def read():
        try:
            data = await asyncio.wait_for(queue.get(), 0.2)
        except TimeoutError:
            return ""
        return decoder.decode(data) if data else None

    def write(data):
        sys.stdout.write(data)
        sys.stdout.flush()

    try:
        async with terminal_socket(bridge) as socket:
            tty.setraw(descriptor)
            loop.add_reader(descriptor, readable)
            await relay(socket, read, write, lambda: shutil.get_terminal_size((80, 24)))
    except (OSError, ValueError, WebSocketException) as error:
        raise GatewayError("Соединение с терминалом Colab прервано.") from error
    finally:
        loop.remove_reader(descriptor)
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
        print()
