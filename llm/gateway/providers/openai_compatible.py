"""Configurable text inference over OpenAI-compatible HTTP APIs."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import aclosing

import httpx

from ..auth.manager import AuthManager
from ..config import BackendConfig, has_control_characters
from ..core.decisions import DecisionRequest
from ..core.errors import GatewayError, upstream_payload_error
from ..core.models import Capabilities, ChatRequest, StreamEvent, Usage
from ..core.system_one import from_native, to_native_result
from .remote_inference import RemoteInferenceAdapter, chat_frames, invalid_response, validated_text


class OpenAICompatibleAdapter(RemoteInferenceAdapter):
    """Share the Chat Completions transport with user-managed inference servers."""

    def __init__(self, client: httpx.AsyncClient, auth: AuthManager, config: BackendConfig) -> None:
        self.client, self.auth, self.config = client, auth, config
        self.capabilities = Capabilities(
            chat=not config.decision_model,
            decisions=config.decision_model,
            decision_box=config.supports_box,
            streaming=config.api_streaming and not config.decision_model,
            json_output=bool(config.api_json_output) and not config.decision_model,
            temperature=not config.decision_model,
            max_output_tokens=not config.decision_model,
        )

    def _headers(self) -> dict[str, str]:
        record = self.auth.require_account(self.config.account, self.config.provider)
        if record.get("state") == "REAUTH_REQUIRED":
            raise GatewayError(
                "Доступ к API недействителен. Обновите ключ в настройках подключения.",
                code="reauth_required",
            )
        key = record.get("api_key")
        if not key and not record.get("no_auth"):
            raise GatewayError("API-ключ не настроен.", code="reauth_required")
        return {"Authorization": "Bearer " + key} if key else {}

    async def decide(self, request):
        if not self.capabilities.decisions:
            raise GatewayError(
                "Выберите подключение к модели решений.", status=400, code="unsupported_parameter"
            )
        normalized = from_native(request) if isinstance(request, DecisionRequest) else request
        if (
            any(q.type == "box" for q in normalized.questions.values())
            and not self.config.supports_box
        ):
            raise GatewayError(
                "Эта модель не поддерживает Box.", status=400, code="unsupported_parameter"
            )
        try:
            response = await self.client.post(
                self.config.decision_url,
                json=normalized.payload(self.config.model),
                headers=self._headers(),
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
            )
            await self._check_response(response)
            data = response.json()
            if not isinstance(data, dict):
                raise invalid_response()
            if data.get("error") is not None:
                raise upstream_payload_error(
                    data["error"], retry_after=response.headers.get("retry-after")
                )
            return to_native_result(data, request) if isinstance(request, DecisionRequest) else data
        except httpx.TimeoutException as error:
            raise GatewayError(
                "Decision API не ответил вовремя.", code="provider_timeout", status=504
            ) from error
        except httpx.HTTPError as error:
            raise GatewayError("Decision API недоступен. Проверьте адрес и сеть.") from error
        except (ValueError, TypeError, KeyError) as error:
            raise invalid_response() from error

    async def list_models(self):
        try:
            response = await self.client.get(
                self.config.models_url,
                headers=self._headers(),
                params={"output_modalities": "decisions"}
                if self.config.decision_model
                and "openrouter.ai" == httpx.URL(self.config.base_url).host
                else None,
                timeout=min(self.config.timeout_seconds, 10),
                follow_redirects=False,
            )
            await self._check_response(response)
            body = response.json()
            if not isinstance(body, dict):
                raise invalid_response()
            if body.get("error") is not None:
                raise upstream_payload_error(
                    body["error"], retry_after=response.headers.get("retry-after")
                )
            data = body["data"]
            if not isinstance(data, list):
                raise invalid_response()
            models = []
            for item in data:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not item["id"].strip()
                    or item["id"] != item["id"].strip()
                    or has_control_characters(item["id"])
                ):
                    raise invalid_response()
                name = item.get("name")
                if name is not None and (not isinstance(name, str) or has_control_characters(name)):
                    raise invalid_response()
                architecture = item.get("architecture")
                if architecture is not None and not isinstance(architecture, dict):
                    raise invalid_response()
                modalities = (architecture or {}).get("output_modalities")
                if modalities is not None and (
                    not isinstance(modalities, list)
                    or any(not isinstance(value, str) for value in modalities)
                ):
                    raise invalid_response()
                modalities = modalities or []
                kind = None
                if "decisions" in modalities:
                    kind = "decision"
                elif "text" in modalities:
                    kind = "chat"
                models.append(
                    {
                        "id": item["id"],
                        "name": name or item["id"],
                        "kind": kind,
                    }
                )
        except GatewayError as error:
            await self.auth.note_result(self.config.account, error)
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
            public = GatewayError(
                "Не удалось загрузить каталог моделей. Повторите попытку или укажите ID вручную."
            )
            await self.auth.note_result(self.config.account, public)
            raise public from error
        await self.auth.note_result(self.config.account)
        return models

    def _payload(self, request: ChatRequest) -> dict:
        self.capabilities.validate(request)
        limit = request.max_completion_tokens or request.max_tokens
        if self.config.api_format == "responses":
            payload = {
                "model": self.config.model,
                "input": [message.model_dump() for message in request.messages],
                "stream": request.stream,
                "store": False,
            }
            if limit is not None:
                payload["max_output_tokens"] = limit
            if request.temperature is not None:
                payload["temperature"] = request.temperature
            if request.response_format:
                payload["text"] = {"format": request.response_format.model_dump()}
            return payload
        payload = request.model_dump(
            exclude_none=True,
            exclude={"model", "stream_options", "n", "max_tokens", "max_completion_tokens"},
        )
        payload["model"] = self.config.model
        if limit is not None:
            payload[self.config.api_token_limit] = limit
        if request.stream and self.config.api_include_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    @staticmethod
    def _response_parts(result: dict) -> tuple[str, bool]:
        if result.get("error") is not None:
            raise upstream_payload_error(result["error"])
        output = result.get("output", [])
        if not isinstance(output, list):
            raise invalid_response()
        parts, refused = [], False
        for item in output:
            if not isinstance(item, dict):
                raise invalid_response()
            kind = item.get("type")
            if kind == "reasoning":
                continue
            if kind != "message":
                raise GatewayError(
                    "API вернул неподдерживаемый вызов инструмента.",
                    code="unexpected_tool_call",
                    status=502,
                )
            content = item.get("content")
            if not isinstance(content, list):
                raise invalid_response()
            for block in content:
                if not isinstance(block, dict):
                    raise invalid_response()
                if block.get("type") == "output_text":
                    text = block.get("text")
                elif block.get("type") == "refusal":
                    refused = True
                    text = block.get("refusal")
                else:
                    raise invalid_response()
                parts.append(validated_text(text))
        return "".join(parts), refused

    @staticmethod
    def _response_done(result: dict, refused: bool = False) -> StreamEvent:
        if result.get("error") is not None:
            raise upstream_payload_error(result["error"])
        status = result.get("status")
        reason = "content_filter" if refused else "stop"
        if status == "incomplete":
            details = result.get("incomplete_details") or {}
            if details.get("reason") == "max_output_tokens":
                reason = "length"
            elif details.get("reason") == "content_filter":
                reason = "content_filter"
            else:
                raise GatewayError("API не завершил ответ.", code="provider_incomplete", status=502)
        elif status != "completed":
            raise GatewayError(
                "API не подтвердил завершение.", code="provider_incomplete", status=502
            )
        usage = result.get("usage")
        normalized = None
        if usage is not None:
            if not isinstance(usage, dict):
                raise invalid_response()
            counts = [usage.get("input_tokens"), usage.get("output_tokens")]
            if any(type(value) is not int or value < 0 for value in counts):
                raise invalid_response()
            normalized = Usage(*counts)
        return StreamEvent("done", usage=normalized, finish_reason=reason)

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        if self.config.api_format != "responses":
            async with aclosing(super().stream(request)) as events:
                async for event in events:
                    yield event
            return
        payload, headers = self._payload(request), self._headers()
        try:
            async with self.client.stream(
                "POST",
                self.config.api_base_url + "/responses",
                json=payload,
                headers=headers,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
            ) as response:
                await self._check_response(response)
                if not request.stream:
                    await response.aread()
                    result = response.json()
                    if not isinstance(result, dict):
                        raise invalid_response()
                    text, refused = self._response_parts(result)
                    done = self._response_done(result, refused)
                    if text:
                        yield StreamEvent("delta", text=text)
                    yield done
                    return
                had_text, refused = False, False
                text_digest = hashlib.sha256()
                async for data in chat_frames(response):
                    if data is None:
                        break
                    kind = data.get("type")
                    if kind in {"response.output_text.delta", "response.refusal.delta"}:
                        text = validated_text(data.get("delta"))
                        refused |= kind == "response.refusal.delta"
                        if text:
                            had_text = True
                            text_digest.update(text.encode())
                            yield StreamEvent("delta", text=text)
                    elif kind in {"response.completed", "response.incomplete"}:
                        result = data.get("response")
                        if not isinstance(result, dict):
                            raise invalid_response()
                        if result.get("error") is not None:
                            raise upstream_payload_error(
                                result["error"], retry_after=response.headers.get("retry-after")
                            )
                        text, output_refused = self._response_parts(result)
                        done = self._response_done(result, refused or output_refused)
                        if (
                            had_text
                            and "output" in result
                            and text_digest.digest() != hashlib.sha256(text.encode()).digest()
                        ):
                            raise GatewayError(
                                "Поток API не соответствует итоговому ответу.",
                                code="provider_incomplete",
                                status=502,
                            )
                        if not had_text and text:
                            yield StreamEvent("delta", text=text)
                        yield done
                        return
                    elif kind in {"response.failed", "error"}:
                        result = data.get("response")
                        error = result.get("error") if isinstance(result, dict) else None
                        raise upstream_payload_error(
                            error or data.get("error") or data,
                            retry_after=response.headers.get("retry-after"),
                        )
        except httpx.TimeoutException as error:
            raise GatewayError(
                "API не ответил вовремя.", code="provider_timeout", status=504
            ) from error
        except httpx.HTTPError as error:
            raise GatewayError("Сервер API недоступен. Проверьте адрес и сеть.") from error
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise invalid_response() from error
        raise GatewayError("Поток API оборвался.", code="provider_incomplete", status=502)
