"""Colab and other user-managed servers exposing OpenAI Chat Completions."""

import json
from collections.abc import AsyncIterator

import httpx

from ..auth.manager import AuthManager
from ..config import BackendConfig
from ..core.errors import GatewayError, upstream_error
from ..core.models import Capabilities, ChatRequest, StreamEvent, Usage
from .base import LLMProvider


def invalid_response() -> GatewayError:
    return GatewayError("Удалённый сервер вернул неверный формат ответа.", status=502)


def chat_usage(data: dict) -> Usage | None:
    usage = data.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise invalid_response()
    counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
    if any(type(value) is not int or value < 0 for value in counts):
        raise invalid_response()
    return Usage(*counts)


def finish_reason(value: str) -> str:
    if value not in {"stop", "length", "content_filter"}:
        raise GatewayError(
            "Удалённая модель не завершила текстовый ответ.",
            code="unexpected_tool_call"
            if value in {"tool_calls", "function_call"}
            else "provider_incomplete",
            status=502,
        )
    return value


async def chat_frames(response: httpx.Response) -> AsyncIterator[dict | None]:
    lines = []
    size = 0
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
            size += len(line)
            if size > 4 * 1024 * 1024:
                raise invalid_response()
        elif not line:
            if lines:
                raw = "\n".join(lines)
                if raw == "[DONE]":
                    yield None
                    return
                try:
                    data = json.loads(raw)
                except ValueError as error:
                    raise invalid_response() from error
                if not isinstance(data, dict):
                    raise invalid_response()
                yield data
            lines, size = [], 0
    raise GatewayError(
        "Поток удалённого сервера оборвался.", code="provider_incomplete", status=502
    )


class RemoteInferenceAdapter(LLMProvider):
    def __init__(self, client: httpx.AsyncClient, auth: AuthManager, config: BackendConfig) -> None:
        self.client, self.auth, self.config = client, auth, config
        self.capabilities = Capabilities(
            streaming=config.remote_streaming,
            json_output=config.remote_json_output,
            temperature=True,
            max_output_tokens=True,
        )

    def _headers(self) -> dict[str, str]:
        record = self.auth.require_account(self.config.account, "remote_inference")
        if record.get("state") == "REAUTH_REQUIRED":
            raise GatewayError("Обновите ключ сервера в SPAS TUI → Colab.", code="reauth_required")
        key = record.get("api_key")
        return {"Authorization": "Bearer " + key} if key else {}

    def _payload(self, request: ChatRequest) -> dict:
        self.capabilities.validate(request)
        payload = request.model_dump(exclude_none=True, exclude={"model", "stream_options"})
        payload["model"] = self.config.model
        limit = payload.pop("max_completion_tokens", None)
        if limit is not None:
            payload["max_tokens"] = limit
        if request.stream and self.config.remote_include_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def _check_response(self, response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            await response.aread()
            raise upstream_error(
                response.status_code, retry_after=response.headers.get("retry-after")
            )

    @staticmethod
    def _choices(data: dict) -> list[dict]:
        if data.get("error"):
            error = data["error"]
            code = error.get("code", "") if isinstance(error, dict) else ""
            raise upstream_error(429 if code in {"rate_limit", "rate_limit_exceeded"} else 503)
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise invalid_response()
        if choices and (not isinstance(choices[0], dict) or choices[0].get("index", 0) != 0):
            raise invalid_response()
        return choices

    @staticmethod
    def _text(message: dict) -> str:
        if not isinstance(message, dict):
            raise invalid_response()
        if message.get("tool_calls") or message.get("function_call"):
            raise GatewayError(
                "Сервер запросил инструмент.", code="unexpected_tool_call", status=502
            )
        text = message.get("content")
        if text is None:
            return ""
        if not isinstance(text, str):
            raise invalid_response()
        return text

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        payload, headers = self._payload(request), self._headers()
        try:
            async with self.client.stream(
                "POST",
                self.config.base_url + "/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
            ) as response:
                await self._check_response(response)
                if not request.stream:
                    await response.aread()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise invalid_response()
                    choices = self._choices(data)
                    if not choices:
                        raise invalid_response()
                    choice = choices[0]
                    reason = finish_reason(choice.get("finish_reason"))
                    text = self._text(choice.get("message"))
                    usage = chat_usage(data)
                    if text:
                        yield StreamEvent("delta", text=text)
                    yield StreamEvent("done", usage=usage, finish_reason=reason)
                    return
                terminal = None
                usage = None
                async for data in chat_frames(response):
                    if data is None:
                        if terminal is None:
                            raise GatewayError(
                                "Сервер не подтвердил завершение.",
                                code="provider_incomplete",
                                status=502,
                            )
                        yield StreamEvent("done", usage=usage, finish_reason=terminal)
                        return
                    choices = self._choices(data)
                    current_usage = chat_usage(data)
                    if current_usage is not None:
                        usage = current_usage
                    if choices:
                        choice = choices[0]
                        text = self._text(choice.get("delta", {}))
                        if terminal is not None:
                            raise invalid_response()
                        reason = choice.get("finish_reason")
                        if reason is not None:
                            terminal = finish_reason(reason)
                        if text:
                            yield StreamEvent("delta", text=text)
        except httpx.TimeoutException as error:
            raise GatewayError(
                "Удалённый сервер не ответил вовремя.", code="provider_timeout", status=504
            ) from error
        except httpx.HTTPError as error:
            raise GatewayError(
                "Удалённый сервер недоступен. Проверьте runtime и URL Colab."
            ) from error
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise invalid_response() from error

    async def list_models(self) -> list[dict[str, str]]:
        try:
            response = await self.client.get(
                self.config.base_url + "/models",
                headers=self._headers(),
                timeout=min(self.config.timeout_seconds, 10),
                follow_redirects=False,
            )
            await self._check_response(response)
            data = response.json()["data"]
            if not isinstance(data, list) or any(
                not isinstance(model, dict)
                or not isinstance(model.get("id"), str)
                or not model["id"]
                for model in data
            ):
                raise invalid_response()
            models = [{"id": model["id"], "name": model["id"]} for model in data]
        except GatewayError as error:
            await self.auth.note_result(self.config.account, error)
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            public = GatewayError("Каталог сервера недоступен. Проверьте runtime и URL Colab.")
            await self.auth.note_result(self.config.account, public)
            raise public from error
        await self.auth.note_result(self.config.account)
        return models

    async def health(self) -> dict:
        try:
            models = await self.list_models()
            record = self.auth.require_account(self.config.account, "remote_inference")
            return {
                **self.auth.public_account(record),
                "available": True,
                "model_available": any(model["id"] == self.config.model for model in models),
            }
        except GatewayError as error:
            record = self.auth.store.get("account:" + self.config.account)
            return {
                "account": self.config.account,
                "available": False,
                "state": self.auth.public_account(record)["state"] if record else "DISCONNECTED",
                "code": error.code,
            }
