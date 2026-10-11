"""Run with Executor source on PYTHONPATH and its Requirements-executor installed.

These tests use the real MCP tool and response validator, mocking only command
execution. No Docker command or shell process is started.
"""

import importlib.util
import unittest
from unittest.mock import AsyncMock, patch

from executer.safety_checker import ExecutorSafetyChecker, RuleBasedSafetyChecker, SafetyPolicy

try:
    EXECUTOR_AVAILABLE = importlib.util.find_spec("executer.server_mcp.Server") is not None
except ModuleNotFoundError:
    EXECUTOR_AVAILABLE = False
EXECUTOR_AVAILABLE = EXECUTOR_AVAILABLE and all(
    importlib.util.find_spec(name) is not None for name in ("mcp", "mcp_types", "pydantic")
)
if EXECUTOR_AVAILABLE:
    from executer.server_mcp.Models import ExecutionContext, ExecutionResult
    from executer.server_mcp.Safety_contract import SafetyDecision

    from executer.server_mcp import Server


@unittest.skipUnless(EXECUTOR_AVAILABLE, "Requires Executor source and Requirements-executor.txt")
class ExecutorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = ExecutorSafetyChecker(
            RuleBasedSafetyChecker(SafetyPolicy(containers={"backend"}))
        )
        self.context = ExecutionContext(incident_id="incident-1", reason="Backend unavailable")

    async def test_response_is_accepted_by_real_executor_validator(self):
        for command in ("pwd", "docker --debug rm backend", "docker restart -- backend"):
            with self.subTest(command=command):
                raw = await self.adapter.check(command, self.context)
                validated = SafetyDecision.model_validate(raw)
                self.assertEqual(validated.decision, raw["decision"])
                self.assertEqual(validated.reason, raw["reason"])

    async def call(self, server, command):
        return await server.call_tool("execute_command", {
            "command": command,
            "context": self.context.model_dump(),
        })

    async def test_allow_reaches_runner_once(self):
        server = Server.create_server(checker=self.adapter)
        runner = AsyncMock(return_value=ExecutionResult(status="succeeded", exit_code=0))
        with patch.object(Server, "run_shell", runner):
            result = await self.call(server, "pwd")
        runner.assert_awaited_once_with("pwd")
        self.assertEqual(result.structured_content["status"], "succeeded")
        self.assertTrue(result.structured_content["safety_checked"])

    async def test_global_docker_options_cannot_reach_runner(self):
        server = Server.create_server(checker=self.adapter)
        for command in ("docker --debug rm backend", "docker -D exec backend id",
                        "docker ps --debug", "docker debug --command id backend"):
            with self.subTest(command=command):
                with patch.object(Server, "run_shell", AsyncMock()) as runner:
                    result = await self.call(server, command)
                runner.assert_not_awaited()
                self.assertEqual(result.structured_content["status"], "denied")
                self.assertTrue(result.structured_content["safety_checked"])

    async def test_approval_does_not_reach_runner(self):
        server = Server.create_server(checker=self.adapter)
        with patch.object(Server, "run_shell", AsyncMock()) as runner:
            result = await self.call(server, "docker restart -- backend")
        runner.assert_not_awaited()
        self.assertEqual(result.structured_content["status"], "approval_required")
        self.assertTrue(result.structured_content["safety_checked"])

    async def test_checker_failure_does_not_reach_runner(self):
        class BrokenChecker(RuleBasedSafetyChecker):
            def check(self, request):
                raise RuntimeError("Checker unavailable")

        server = Server.create_server(checker=ExecutorSafetyChecker(BrokenChecker()))
        with patch.object(Server, "run_shell", AsyncMock()) as runner:
            result = await self.call(server, "pwd")
        runner.assert_not_awaited()
        self.assertEqual(result.structured_content["status"], "error")
        self.assertFalse(result.structured_content["safety_checked"])

    async def test_missing_checker_does_not_reach_runner(self):
        server = Server.create_server()
        with patch.object(Server, "run_shell", AsyncMock()) as runner:
            result = await self.call(server, "pwd")
        runner.assert_not_awaited()
        self.assertEqual(result.structured_content["status"], "error")


if __name__ == "__main__":
    unittest.main()
