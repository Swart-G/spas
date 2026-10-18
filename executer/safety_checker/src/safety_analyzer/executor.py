"""Async checker adapter for the real Executor MCP SafetyCheckerPort."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

import httpx

from safety_analyzer.command_contracts import CommandAnalysisRequest, CommandContext
from safety_analyzer.commands import CommandAnalyzer, RuleChecker, configured_rules
from safety_analyzer.config import Settings
from safety_analyzer.gateway import LLMGateway


class ExecutorContext(Protocol):
    @property
    def incident_id(self) -> str: ...
    @property
    def reason(self) -> str: ...


ContextProvider = Callable[[str, str], Awaitable[CommandContext]]


class AnalyzerSafetyChecker:
    """Inject into create_server(checker=...). No cached permits or execution endpoint."""

    def __init__(self, analyzer: CommandAnalyzer, context_provider: ContextProvider | None = None):
        self.analyzer, self.context_provider = analyzer, context_provider

    async def check(self, command: list[str], context: ExecutorContext) -> dict[str, object]:
        # Executor's own checker deadline is 5s; include context resolution in our budget.
        try:
            async with asyncio.timeout(4.5):
                return await self._check(command, context)
        except TimeoutError:
            return {"decision": "DENY", "reason": "Safety assessment deadline exceeded"}

    async def _check(self, command: list[str], context: ExecutorContext) -> dict[str, object]:
        try:
            # Snapshot input synchronously, before an async provider/LLM can observe mutations.
            base = CommandContext(incident_id=context.incident_id, reason=context.reason)
            request = CommandAnalysisRequest(command=command, context=base).snapshot()
            if self.context_provider is not None:
                async with asyncio.timeout(self.analyzer.gateway.settings.rule_timeout_seconds):
                    enriched = await self.context_provider(base.incident_id, base.reason)
                enriched = CommandContext.model_validate_json(enriched.model_dump_json())
                if enriched.incident_id != base.incident_id or enriched.reason != base.reason:
                    raise ValueError("provider returned context for a different request")
                request.context = enriched
            result = await self.analyzer.analyze(request)
            return result.executor_decision()
        except Exception:
            return {"decision": "DENY", "reason": "Invalid or unavailable command context"}


@asynccontextmanager
async def connected_checker(
    settings: Settings | None = None,
    *,
    rules: RuleChecker | None = None,
    context_provider: ContextProvider | None = None,
):
    """Own the direct HTTP client for the entire MCP server lifecycle."""
    settings = settings or Settings()
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        yield AnalyzerSafetyChecker(
            CommandAnalyzer(
                LLMGateway(settings, client), configured_rules(settings) if rules is None else rules
            ),
            context_provider,
        )
