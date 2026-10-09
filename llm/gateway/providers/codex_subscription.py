import json
from collections.abc import AsyncIterator

import httpx

from ..auth.manager import AuthManager
from ..config import BackendConfig
from ..core.errors import GatewayError, upstream_error
from ..core.models import ChatRequest, StreamEvent, Usage
from .base import LLMProvider


async def sse_data(response: httpx.Response) -> AsyncIterator[dict]:
    lines: list[str] = []
    size = 0
    async for line in response.aiter_lines():
        if not line:
            if lines:
                try:
                    data = json.loads("\n".join(lines))
                    if not isinstance(data, dict):
                        raise ValueError("Not an event object")
                except ValueError as error:
                    raise GatewayError("Провайдер вернул повреждённый SSE-поток.") from error
                yield data
            lines, size = [], 0
        elif line.startswith("data:"):
            data_line = line[5:].lstrip(" ")
            if data_line == "[DONE]":
                continue
            lines.append(data_line)
            size += len(data_line)
            if size > 4 * 1024 * 1024:
                raise GatewayError("Событие провайдера превысило допустимый размер.")
    if lines:
        # SSE requires a blank line terminating each frame; an unfinished frame is not success.
        raise GatewayError("SSE-поток оборвался внутри события.")


def response_error(error: dict) -> GatewayError:
    code = error.get("code", "")
    status = {
        "subscription_sharing_usage_limit_exceeded": 429,
        "subscription_sharing_usage_unavailable": 503,
        "subscription_sharing_user_unavailable": 503,
        "subscription_sharing_user_not_eligible": 403,
        "subscription_sharing_route_not_supported": 403,
        "subscription_sharing_unsupported_capability": 400,
        "subscription_sharing_invalid_user": 401,
        "chatpass_v2_scope_not_authorized": 403,
        "chatpass_v2_invalid_authorization_context": 403,
        "rate_limit_exceeded": 429,
        "insufficient_quota": 429,
        "invalid_api_key": 401,
    }.get(code, 503)
    return upstream_error(status, code)


def response_usage(result: dict) -> Usage | None:
    usage = result.get("usage")
    return Usage(usage["input_tokens"], usage["output_tokens"]) if usage else None


class CodexSubscriptionAdapter(LLMProvider):
    base_url = "https://api.openai.com/v1"

    def __init__(self, client: httpx.AsyncClient, auth: AuthManager, config: BackendConfig) -> None:
        self.client, self.auth, self.config = client, auth, config

    async def _credential(self) -> str:
        return await self.auth.access_token(self.config.account)

    def _incomplete(self, result: dict) -> StreamEvent:
        raise GatewayError(
            "Провайдер не завершил генерацию.", code="provider_incomplete", status=502
        )

    def _payload(self, request: ChatRequest) -> dict:
        self.capabilities.validate(request)
        payload = {
            "model": self.config.model,
            "input": [
                message.model_dump()
                for message in request.messages
                if message.role in {"user", "assistant"}
            ],
            "store": False,
            "stream": True,
        }
        instructions = "\n\n".join(
            message.content
            for message in request.messages
            if message.role in {"system", "developer"}
        )
        if instructions:
            payload["instructions"] = instructions
        return payload

    async def _check_response(self, response: httpx.Response) -> None:
        if response.is_error:
            await response.aread()
            code = ""
            try:
                error = response.json().get("error", {})
                if isinstance(error, dict):
                    code = error.get("code", "")
            except (ValueError, AttributeError):
                pass
            raise upstream_error(response.status_code, code, response.headers.get("retry-after"))

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        payload = self._payload(request)
        credential = await self._credential()
        had_text = False
        refused = False
        try:
            async with self.client.stream(
                "POST",
                self.base_url + "/responses",
                json=payload,
                headers={"Authorization": "Bearer " + credential},
                timeout=self.config.timeout_seconds,
            ) as response:
                await self._check_response(response)
                async for data in sse_data(response):
                    kind = data.get("type")
                    if kind in {"response.output_text.delta", "response.refusal.delta"}:
                        delta = data.get("delta", "")
                        if not isinstance(delta, str):
                            raise GatewayError("Неверный формат текстового события провайдера.")
                        refused |= kind == "response.refusal.delta"
                        if delta:
                            had_text = True
                            yield StreamEvent("delta", text=delta)
                    elif kind == "response.completed":
                        result = data.get("response", {})
                        if not had_text:
                            for item in result.get("output", []):
                                for block in item.get("content", []):
                                    text = block.get("text", block.get("refusal", ""))
                                    refused |= block.get("type") == "refusal"
                                    if text:
                                        yield StreamEvent("delta", text=text)
                        normalized = response_usage(result)
                        yield StreamEvent(
                            "done",
                            usage=normalized,
                            finish_reason="content_filter" if refused else "stop",
                        )
                        return
                    elif kind in {"response.failed", "error"}:
                        error = data.get("response", {}).get("error") or data.get("error") or data
                        raise response_error(error)
                    elif kind == "response.incomplete":
                        yield self._incomplete(data.get("response", {}))
                        return
        except httpx.TimeoutException as error:
            raise GatewayError(
                "Истекло время ожидания провайдера.", code="provider_timeout", status=504
            ) from error
        except httpx.HTTPError as error:
            raise GatewayError("Соединение с провайдером прервано.") from error
        raise GatewayError(
            "Поток завершился без response.completed.", code="provider_incomplete", status=502
        )

    async def health(self) -> dict:
        try:
            record = self.auth.require_account(self.config.account, self.config.provider)
            return self.auth.public_account(record)
        except GatewayError as error:
            return {"account": self.config.account, "state": "DISCONNECTED", "code": error.code}

    async def list_models(self) -> list[dict[str, str]]:
        credential = await self._credential()
        try:
            response = await self.client.get(
                self.base_url + "/models",
                headers={"Authorization": "Bearer " + credential},
                timeout=self.config.timeout_seconds,
            )
            await self._check_response(response)
            data = response.json()
            return [
                {"id": model["slug"], "name": model.get("display_name", model["slug"])}
                for model in data["models"]
                if model.get("visibility") == "list"
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise GatewayError("Не удалось получить каталог моделей провайдера.") from error
