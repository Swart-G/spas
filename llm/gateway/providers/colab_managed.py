from contextlib import aclosing

from ..colab.manager import ColabManager, DeploymentSettings
from ..colab.scripts import inference_script
from ..core.errors import GatewayError, upstream_error
from ..core.models import Capabilities, StreamEvent
from .remote_inference import RemoteInferenceAdapter, chat_usage, finish_reason, invalid_response


class ColabManagedAdapter(RemoteInferenceAdapter):
    """Private model traffic through Google's authenticated Jupyter proxy."""

    def __init__(self, client, auth, config, *, manager=None):
        super().__init__(client, auth, config)
        self.capabilities = Capabilities(
            chat=not config.decision_model,
            decisions=config.decision_model,
            streaming=config.remote_streaming and not config.decision_model,
            json_output=False,
            temperature=not config.decision_model,
            max_output_tokens=not config.decision_model,
        )
        self.manager = manager or ColabManager(auth.store, client)

    def inference_record(self):
        record = self.manager.record(self.config.account)
        if record.get("settings", {}).get("auto_create_runtime") and record.get("stage") not in {
            "READY",
            "VERIFYING",
        }:
            raise GatewayError(
                "Модель в Colab ещё не готова. Дождитесь запуска или проверьте состояние среды.",
                code="provider_unavailable",
                status=503,
                retry_after=15,
            )
        return record

    async def decide(self, request):
        record = self.inference_record()
        settings = DeploymentSettings.model_validate(record["settings"])
        bridge = await self.manager.connect(self.config.account)
        result = None
        async with aclosing(
            bridge.execute(
                inference_script(
                    settings.port,
                    record["server_key"],
                    request.payload(self.config.model),
                    decision=True,
                ),
                wait_seconds=self.config.timeout_seconds,
            )
        ) as events:
            async for event in events:
                if event.get("type") == "http_error":
                    raise upstream_error(
                        event.get("status", 503), retry_after=event.get("retry_after")
                    )
                if event.get("type") != "response" or result is not None:
                    raise invalid_response()
                result = event.get("data")
        if not isinstance(result, dict):
            raise invalid_response()
        return result

    async def stream(self, request):
        payload = self._payload(request)
        if request.stream:
            payload["stream_options"] = {"include_usage": True}
        record = self.inference_record()
        settings = DeploymentSettings.model_validate(record["settings"])
        bridge = await self.manager.connect(self.config.account)
        terminal, usage, confirmed = None, None, False
        refused = False
        code = inference_script(settings.port, record["server_key"], payload)
        async with aclosing(
            bridge.execute(code, wait_seconds=self.config.timeout_seconds)
        ) as events:
            async for event in events:
                kind = event.get("type")
                if kind == "http_error":
                    raise upstream_error(
                        event.get("status", 503), retry_after=event.get("retry_after")
                    )
                if confirmed:
                    raise invalid_response()
                if kind == "transport_done":
                    if terminal is None:
                        raise GatewayError(
                            "Модель не подтвердила завершение.",
                            code="provider_incomplete",
                            status=502,
                        )
                    confirmed = True
                elif kind in {"response", "chunk"}:
                    data = event.get("data")
                    if not isinstance(data, dict):
                        raise invalid_response()
                    choices = self._choices(data)
                    current_usage = chat_usage(data)
                    if current_usage is not None:
                        usage = current_usage
                    if kind == "response":
                        if request.stream or not choices or terminal is not None:
                            raise invalid_response()
                        choice = choices[0]
                        terminal = finish_reason(choice.get("finish_reason"))
                        text, refused = self._message_parts(choice.get("message"))
                        confirmed = True
                        if text:
                            yield StreamEvent("delta", text=text)
                    elif choices:
                        if not request.stream or terminal is not None:
                            raise invalid_response()
                        choice = choices[0]
                        text, part_refused = self._message_parts(choice.get("delta", {}))
                        refused |= part_refused
                        if choice.get("finish_reason") is not None:
                            terminal = finish_reason(choice["finish_reason"])
                        if text:
                            yield StreamEvent("delta", text=text)
                else:
                    raise invalid_response()
        if not confirmed or terminal is None:
            raise GatewayError(
                "Соединение Colab не подтвердило ответ.", code="provider_incomplete", status=502
            )
        yield StreamEvent(
            "done", usage=usage, finish_reason="content_filter" if refused else terminal
        )

    async def list_models(self):
        return await self.manager.models(self.config.account)

    async def health(self):
        try:
            return await self.manager.status(self.config.account)
        except GatewayError as error:
            return {"account": self.config.account, "available": False, "code": error.code}
