"""Check the current real port and the agreed list[str] input change with mypy."""

from typing import Protocol

from executer.server_mcp.Models import ExecutionContext
from executer.server_mcp.Safety_contract import SafetyCheckerPort, SafetyDecision
from executer.server_mcp.Server import create_server

from executer.safety_checker import RuleBasedSafetyChecker
from shared.contracts.safety import ExecutorSafetyCheckerPort


class ArgvExecutorPort(Protocol):
    # Proposed input change only; context and response use real Executor types.
    async def check(
        self, command: list[str], context: ExecutionContext,
    ) -> SafetyDecision | dict[str, object]: ...


def current_executor_port() -> SafetyCheckerPort:
    checker = RuleBasedSafetyChecker()
    create_server(checker=checker)
    return checker


def argv_executor_port() -> ArgvExecutorPort:
    return RuleBasedSafetyChecker()


def shared_port() -> ExecutorSafetyCheckerPort:
    return RuleBasedSafetyChecker()
