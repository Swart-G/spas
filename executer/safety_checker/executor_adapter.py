"""Async port adapter; no MCP/Pydantic dependency and no command execution."""

from shared.contracts.safety import CommandRequest, ExecutionContextPort, ExecutorSafetyDecision

from .checker import RuleBasedSafetyChecker


class ExecutorSafetyChecker:
    """Pass this adapter to Executor MCP's create_server(checker=...).

    Checking is bounded CPU-only work; an async method satisfies Executor's
    await protocol without a thread pool or additional I/O. Cancellation and
    checker exceptions are handled by the Executor's existing fail-closed path.
    Context supplies incident metadata, never permissions or policy overrides.
    """

    def __init__(self, checker: RuleBasedSafetyChecker | None = None) -> None:
        if checker is not None and not isinstance(checker, RuleBasedSafetyChecker):
            raise TypeError("checker must be a RuleBasedSafetyChecker")
        self._checker = checker if checker is not None else RuleBasedSafetyChecker()

    async def check(
        self, command: str, context: ExecutionContextPort,
    ) -> ExecutorSafetyDecision:
        try:
            values = (context.incident_id, context.reason)
        except AttributeError:
            return {"decision": "DENY", "reason": "Invalid Executor execution context"}
        if any(not isinstance(value, str) or not value.strip() or len(value) > 2000
               for value in values):
            return {"decision": "DENY", "reason": "Invalid Executor execution context"}
        result = self._checker.check(CommandRequest(command))
        return {"decision": result.decision.value, "reason": result.reason}
