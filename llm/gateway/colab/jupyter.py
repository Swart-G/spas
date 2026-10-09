import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from ..core.errors import GatewayError

MIME = "application/vnd.spas.colab+json"


class NoRedirectConnect(connect):
    def process_redirect(self, exc):
        # Proxy tokens are specific to this runtime; never forward them to another URL.
        return exc


class JupyterBridge:
    def __init__(self, client: httpx.AsyncClient, connection: dict):
        self.client = client
        self.url = connection["url"]
        self.headers = {
            "X-Colab-Runtime-Proxy-Token": connection["token"],
            "X-Colab-Client-Agent": "colab-cli",
        }

    async def request(self, method, path, **options):
        try:
            response = await self.client.request(
                method,
                self.url + path,
                headers=self.headers,
                follow_redirects=False,
                timeout=30,
                **options,
            )
            if response.status_code in (401, 403):
                raise GatewayError(
                    "Соединение Colab истекло. Повторите действие для обновления токена.",
                    code="colab_connection_expired",
                )
            if not 200 <= response.status_code < 300:
                raise GatewayError("Jupyter runtime недоступен.", code="provider_unavailable")
            data = response.json() if response.content else {}
            if not isinstance(data, dict):
                raise ValueError("Invalid Jupyter response")
            return data
        except (httpx.HTTPError, ValueError) as error:
            raise GatewayError("Не удалось подключиться к Jupyter runtime.") from error

    async def execute(self, code: str, *, wait_seconds=1800) -> AsyncIterator[dict]:
        kernel = await self.request("POST", "/api/kernels", json={"name": "python3"})
        kernel_id = kernel.get("id")
        if (
            not isinstance(kernel_id, str)
            or not kernel_id
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for c in kernel_id
            )
        ):
            raise GatewayError("Jupyter не создал kernel.", code="colab_invalid_response")
        request_id, session_id = str(uuid.uuid4()), str(uuid.uuid4())
        parsed = urlsplit(self.url + "/api/kernels/" + kernel_id + "/channels")
        ws_url = urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                parsed.path,
                urlencode(
                    {
                        "session_id": session_id,
                        "colab-runtime-proxy-token": self.headers["X-Colab-Runtime-Proxy-Token"],
                    }
                ),
                "",
            )
        )
        completed = False
        try:
            async with asyncio.timeout(wait_seconds):
                async with NoRedirectConnect(
                    ws_url,
                    additional_headers=self.headers,
                    origin=urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
                    max_size=8 * 1024 * 1024,
                    open_timeout=30,
                    proxy=None,
                ) as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "header": {
                                    "msg_id": request_id,
                                    "username": "spas",
                                    "session": session_id,
                                    "date": datetime.now(UTC).isoformat(),
                                    "msg_type": "execute_request",
                                    "version": "5.3",
                                },
                                "parent_header": {},
                                "metadata": {},
                                "channel": "shell",
                                "content": {
                                    "code": code,
                                    "silent": False,
                                    "store_history": False,
                                    "user_expressions": {},
                                    "allow_stdin": False,
                                    "stop_on_error": True,
                                },
                                "buffers": [],
                            }
                        )
                    )
                    reply, idle = False, False
                    async for raw in websocket:
                        if not isinstance(raw, str):
                            raise GatewayError(
                                "Jupyter вернул неподдерживаемый бинарный кадр.",
                                code="colab_invalid_response",
                            )
                        data = json.loads(raw)
                        if data.get("parent_header", {}).get("msg_id") != request_id:
                            continue
                        kind = data.get("msg_type") or data.get("header", {}).get("msg_type")
                        content = data.get("content", {})
                        if kind == "error" or (
                            kind == "execute_reply" and content.get("status") != "ok"
                        ):
                            # Python tracebacks may contain request data or credentials.
                            raise GatewayError(
                                "Выполнение на runtime завершилось ошибкой.",
                                code="colab_execution_failed",
                            )
                        if kind in {"display_data", "execute_result"}:
                            event = content.get("data", {}).get(MIME)
                            if event is not None:
                                if not isinstance(event, dict):
                                    raise GatewayError(
                                        "Неверный ответ runtime.", code="colab_invalid_response"
                                    )
                                yield event
                        elif kind == "execute_reply":
                            reply = True
                        elif kind == "status" and content.get("execution_state") == "idle":
                            idle = True
                        if reply and idle:
                            completed = True
                            return
                    raise GatewayError(
                        "Jupyter завершил соединение без подтверждения.",
                        code="provider_incomplete",
                        status=502,
                    )
        except TimeoutError as error:
            raise GatewayError(
                "Истекло время выполнения на Colab.", code="provider_timeout", status=504
            ) from error
        except (WebSocketException, OSError, ValueError, TypeError, AttributeError) as error:
            raise GatewayError("Соединение с Colab прервано.") from error
        finally:
            # Never keep cancelled inference executing on an idle kernel.
            if not completed:
                with suppress(GatewayError, asyncio.CancelledError):
                    await self.request("POST", "/api/kernels/" + kernel_id + "/interrupt")
            with suppress(GatewayError, asyncio.CancelledError):
                await self.request("DELETE", "/api/kernels/" + kernel_id)
