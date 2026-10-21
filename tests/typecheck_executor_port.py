"""Static check against the real async argv port, without casts or ignored errors."""

from executer.safety_checker import RuleBasedSafetyChecker
from executer.safety_checker.permissions import AgentRole
from executer.server_mcp.Safety_contract import SafetyCheckerPort
from executer.server_mcp.Server import create_server
from shared.contracts.safety import ExecutorSafetyCheckerPort


def executor_port() -> SafetyCheckerPort:
    checker = RuleBasedSafetyChecker()
    create_server(checker=checker, agent_role=AgentRole.DIAGNOSTIC)
    create_server(checker=checker, agent_role=AgentRole.WORKER)
    return checker


def shared_port() -> ExecutorSafetyCheckerPort:
    return RuleBasedSafetyChecker()
