"""Check the real Executor package/validator and its current string-input gate.

Executor at 79f4fd0 still accepts strings. They must fail closed until its argv
migration lands. No command or shell process is started in these tests.
"""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from executer.safety_checker import RuleBasedSafetyChecker, SafetyPolicy

try:
    EXECUTOR_AVAILABLE = importlib.util.find_spec("executer.server_mcp.Server") is not None
except ModuleNotFoundError:
    EXECUTOR_AVAILABLE = False
EXECUTOR_AVAILABLE = EXECUTOR_AVAILABLE and all(
    importlib.util.find_spec(name) is not None for name in ("mcp", "mcp_types", "pydantic")
)
if EXECUTOR_AVAILABLE:
    from executer.server_mcp.Models import ExecutionContext
    from executer.server_mcp.Safety_contract import SafetyDecision

    import executer
    from executer.server_mcp import Server


@unittest.skipUnless(EXECUTOR_AVAILABLE, "Requires Executor source and Requirements-executor.txt")
class ExecutorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.checker = RuleBasedSafetyChecker(SafetyPolicy(containers={"backend"}))
        self.context = ExecutionContext(incident_id="incident-1", reason="Backend unavailable")

    def test_real_executor_package_initializer_is_present(self):
        self.assertIsNotNone(executer.__file__)
        self.assertEqual(Path(executer.__file__).name, "__init__.py")

    async def test_response_is_accepted_by_real_executor_validator(self):
        for argv, expected in ((["pwd"], "ALLOW"),
                               (["docker", "--debug", "rm", "backend"], "DENY"),
                               (["docker", "restart", "--", "backend"], "REQUIRE_APPROVAL")):
            with self.subTest(argv=argv):
                raw = await self.checker.check(argv, self.context)
                validated = SafetyDecision.model_validate(raw)
                self.assertEqual(validated.decision, expected)
                self.assertEqual(validated.reason, raw["reason"])

    async def call(self, server, command):
        return await server.call_tool("execute_command", {
            "command": command,
            "context": self.context.model_dump(),
        })

    async def test_old_string_executor_cannot_start_any_command(self):
        server = Server.create_server(checker=self.checker)
        for command in ("pwd", "docker ps", "docker --debug rm backend",
                        "docker -D exec backend id", "docker restart -- backend", "pwd && uname"):
            with self.subTest(command=command):
                with patch.object(Server, "run_shell", AsyncMock()) as runner:
                    result = await self.call(server, command)
                runner.assert_not_awaited()
                self.assertEqual(result.structured_content["status"], "denied")
                self.assertTrue(result.structured_content["safety_checked"])

    async def test_checker_failure_does_not_reach_runner(self):
        class BrokenChecker(RuleBasedSafetyChecker):
            async def check(self, command, context):
                raise RuntimeError("Checker unavailable")

        server = Server.create_server(checker=BrokenChecker())
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
