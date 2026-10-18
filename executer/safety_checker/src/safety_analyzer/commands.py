"""Rules first, then contextual model assessment. This module never executes argv."""

import asyncio
import json
import logging
from time import monotonic
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from safety_analyzer.command_contracts import (
    CommandAnalysisRequest,
    CommandAnalysisResponse,
    CommandContext,
    RuleDecision,
)
from safety_analyzer.config import Settings
from safety_analyzer.gateway import GatewayError, LLMGateway
from safety_analyzer.rule_based import RuleBasedSafetyChecker, SafetyPolicy

logger = logging.getLogger("safety_analyzer.commands")


class RuleChecker(Protocol):
    async def check(self, command: list[str], context: CommandContext) -> dict[str, object]: ...


def configured_rules(settings: Settings) -> RuleBasedSafetyChecker:
    # Deployment settings only; no request can expand these scopes or remove hard denies.
    return RuleBasedSafetyChecker(
        SafetyPolicy(
            readable_paths=settings.readable_paths,
            containers=settings.containers,
            denied_executables=settings.denied_executables,
        )
    )


class RuleError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CommandAnalyzer:
    def __init__(self, gateway: LLMGateway, rules: RuleChecker):
        self.gateway, self.rules = gateway, rules

    async def _rules(self, request: CommandAnalysisRequest) -> RuleDecision:
        argv = list(request.command)
        try:
            async with asyncio.timeout(self.gateway.settings.rule_timeout_seconds):
                raw = await self.rules.check(argv, request.context.model_copy(deep=True))
        except TimeoutError as exc:
            raise RuleError("rule_checker_timeout") from exc
        except Exception as exc:
            raise RuleError("rule_checker_unavailable") from exc
        try:
            # No extras and no truthiness/coercion of ALLOW; this is the Executor's wire shape.
            decision = RuleDecision.model_validate(raw)
            if argv != request.command or not decision.reason.strip():
                raise ValueError("checker mutated argv or returned a blank reason")
            return decision
        except (ValidationError, ValueError, TypeError) as exc:
            raise RuleError("invalid_rule_output") from exc

    async def analyze(self, request: CommandAnalysisRequest) -> CommandAnalysisResponse:
        # Revalidate and detach every nested list/dict before the first await.
        request = CommandAnalysisRequest.model_validate_json(request.model_dump_json())
        started = monotonic()
        request_id = request.request_id or uuid4()
        rule_result = None
        model_result = None
        stage = "rules"
        error = None
        try:
            async with asyncio.timeout(self.gateway.settings.command_timeout_seconds):
                rule_result = await self._rules(request)
                if rule_result.decision != "ALLOW":
                    decision = rule_result.decision
                    risk = "MEDIUM" if decision == "REQUIRE_APPROVAL" else "HIGH"
                    reason, source = rule_result.reason, "rules"
                else:
                    stage = "model"
                    model_result = await self.gateway.classify_command(request, rule_result)
                    decision, risk = model_result.decision, model_result.risk
                    reason, source = model_result.reason, "model"
        except (RuleError, GatewayError) as exc:
            error = exc.code
        except TimeoutError:
            error = "rule_checker_timeout" if stage == "rules" else "gateway_timeout"
        except Exception:
            # A broken provider must never become an implicit permission or leak its exception.
            error = "rule_checker_unavailable" if stage == "rules" else "gateway_unavailable"
        if error:
            decision, risk, source = "DENY", "HIGH", "error"
            reason = "Safety assessment unavailable; command must not execute"
            model_result = None
        response = CommandAnalysisResponse(
            request_id=request_id,
            input_sha256=request.fingerprint(),
            decision=decision,
            risk=risk,
            reason=reason,
            source=source,
            rules=rule_result,
            model_result=model_result,
            status="error" if error else "ok",
            error_code=error,
            model=self.gateway.settings.model if stage == "model" else None,
            latency_ms=int((monotonic() - started) * 1000),
        )
        logger.info(
            json.dumps(
                {
                    "event": "command_analysis",
                    "request_id": str(request_id),
                    "input_sha256": response.input_sha256,
                    "decision": response.decision,
                    "risk": response.risk,
                    "source": response.source,
                    "status": response.status,
                    "error_code": response.error_code,
                    "latency_ms": response.latency_ms,
                    "rule_decision": rule_result.decision if rule_result else None,
                }
            )
        )
        return response
