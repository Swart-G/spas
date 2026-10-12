import asyncio
import json
import logging
from time import monotonic
from uuid import uuid4

from safety_analyzer.contracts import AnalyzeRequest, AnalyzeResponse
from safety_analyzer.gateway import GatewayError, LLMGateway

logger = logging.getLogger("safety_analyzer")


class Analyzer:
    def __init__(self, gateway: LLMGateway):
        self.gateway = gateway

    async def analyze(self, request: AnalyzeRequest) -> AnalyzeResponse:
        started = monotonic()
        request_id = request.request_id or uuid4()
        try:
            async with asyncio.timeout(self.gateway.settings.timeout_seconds):
                result = await self.gateway.classify(request)
            response = AnalyzeResponse(
                request_id=request_id,
                decision="allow" if result.verdict == "safe" else "block",
                verdict=result.verdict,
                categories=result.categories,
                status="ok",
                model=self.gateway.settings.model,
                latency_ms=int((monotonic() - started) * 1000),
            )
        except (GatewayError, TimeoutError) as exc:
            response = AnalyzeResponse(
                request_id=request_id,
                decision="block",
                verdict="unknown",
                categories=[],
                status="error",
                error_code=exc.code if isinstance(exc, GatewayError) else "gateway_timeout",
                model=self.gateway.settings.model,
                latency_ms=int((monotonic() - started) * 1000),
            )
        logger.info(
            json.dumps(
                {
                    "event": "safety_analysis",
                    "request_id": str(request_id),
                    "target": request.target,
                    "model": response.model,
                    "policy_version": response.policy_version,
                    "decision": response.decision,
                    "verdict": response.verdict,
                    "status": response.status,
                    "error_code": response.error_code,
                    "latency_ms": response.latency_ms,
                }
            )
        )
        return response
