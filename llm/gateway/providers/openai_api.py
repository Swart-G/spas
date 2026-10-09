import httpx

from ..auth.manager import AuthManager
from ..config import BackendConfig
from ..core.errors import GatewayError
from ..core.models import Capabilities, ChatRequest, StreamEvent
from .codex_subscription import CodexSubscriptionAdapter, response_usage


class OpenAIAPIAdapter(CodexSubscriptionAdapter):
    """A separately configured and billed backend using a user's API key."""

    capabilities = Capabilities(json_output=True, temperature=True, max_output_tokens=True)

    def __init__(self, client: httpx.AsyncClient, auth: AuthManager, config: BackendConfig) -> None:
        super().__init__(client, auth, config)

    async def _credential(self) -> str:
        record = self.auth.require_account(self.config.account, "openai_api")
        if not record.get("api_key"):
            raise GatewayError("API-ключ не настроен.", code="reauth_required")
        return record["api_key"]

    def _payload(self, request: ChatRequest) -> dict:
        payload = super()._payload(request)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        limit = request.max_completion_tokens or request.max_tokens
        if limit is not None:
            payload["max_output_tokens"] = limit
        if request.response_format:
            payload["text"] = {"format": request.response_format.model_dump()}
        return payload

    def _incomplete(self, result: dict) -> StreamEvent:
        if result.get("incomplete_details", {}).get("reason") == "max_output_tokens":
            return StreamEvent("done", usage=response_usage(result), finish_reason="length")
        return super()._incomplete(result)

    async def list_models(self) -> list[dict[str, str]]:
        credential = await self._credential()
        try:
            response = await self.client.get(
                self.base_url + "/models",
                headers={"Authorization": "Bearer " + credential},
                timeout=self.config.timeout_seconds,
            )
            await self._check_response(response)
            return [{"id": model["id"], "name": model["id"]} for model in response.json()["data"]]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise GatewayError("Не удалось получить каталог моделей API.") from error
