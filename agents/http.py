"""AgentMessage/AgentResult transport for independently deployed agent services."""

import asyncio
from math import isfinite

import httpx

from shared.contracts import (
    AgentCompletionUnknownError,
    AgentMessage,
    AgentResult,
    RoleEnum,
    validate_agent_result,
)


class HttpAgent:
    def __init__(self, role: RoleEnum, endpoint: str, *, request_timeout: float = 10.0):
        if not isinstance(role, RoleEnum) or role == RoleEnum.ORCHESTRATOR:
            raise ValueError("HTTP agent requires an agent role")
        url = httpx.URL(endpoint)
        if url.scheme not in ("http", "https") or not url.host:
            raise ValueError("Agent endpoint must be an absolute HTTP(S) URL")
        if not isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request_timeout must be positive and finite")
        self.role = role
        self.endpoint = str(url)
        self.request_timeout = request_timeout

    async def process(self, message: AgentMessage) -> AgentResult:
        if message.receiver != self.role:
            raise ValueError("HTTP agent received a message for another role")
        try:
            # A per-call client bounds resource ownership; no application teardown
            # hook is needed to release this request's sockets and connections.
            async with httpx.AsyncClient(timeout=self.request_timeout, trust_env=False) as client:
                response = await client.post(self.endpoint, json=message.model_dump(mode="json"))
                response.raise_for_status()
                return validate_agent_result(
                    message, AgentResult.model_validate_json(response.content)
                )
        except (asyncio.CancelledError, httpx.TransportError) as exc:
            # Closing a socket is not an acknowledgement of remote cancellation.
            # An ordinary exception makes cleanup unconfirmed and blocks retries.
            raise AgentCompletionUnknownError("Remote agent completion is unconfirmed") from exc
