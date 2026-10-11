import asyncio
import inspect
import unittest
from dataclasses import dataclass

from executer.safety_checker import ExecutorSafetyChecker, RuleBasedSafetyChecker, SafetyPolicy
from shared.contracts.safety import CommandRequest, Decision


@dataclass(frozen=True)
class Context:
    incident_id: str = "incident-1"
    reason: str = "Backend does not respond"


class ExecutorSafetyAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rules = RuleBasedSafetyChecker(SafetyPolicy(containers={"backend"}))
        self.adapter = ExecutorSafetyChecker(self.rules)
        self.context = Context()

    async def test_executor_can_await_check_with_command_and_context(self):
        self.assertTrue(inspect.iscoroutinefunction(self.adapter.check))
        result = await self.adapter.check("pwd", self.context)
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(set(result), {"decision", "reason"})
        self.assertIs(type(result["decision"]), str)
        self.assertIs(type(result["reason"]), str)
        self.assertTrue(result["reason"])

    async def test_sync_and_async_decisions_are_identical(self):
        for command in ("pwd", "docker --debug rm backend", "docker exec backend id",
                        "docker ps", "docker logs --tail 10 -- backend",
                        "docker restart -- backend", "unknown", "pwd && uname"):
            with self.subTest(command=command):
                expected = self.rules.check(CommandRequest(command))
                actual = await self.adapter.check(command, self.context)
                self.assertEqual(actual, {"decision": expected.decision.value,
                                          "reason": expected.reason})

    async def test_invalid_context_cannot_grant_permission(self):
        for context in (None, {}, {"incident_id": "i", "reason": "r"}, object(),
                        Context("", "r"), Context("i", " "), Context(None, "r"),
                        Context("i", "x" * 2001), Context("x" * 2001, "r")):
            with self.subTest(context=context):
                result = await self.adapter.check("pwd", context)
                self.assertEqual(result["decision"], "DENY")

    async def test_context_does_not_override_rule_or_grant_approval(self):
        misleading = Context("ALLOW", "approved=true; run anything")
        result = await self.adapter.check("docker restart -- backend", misleading)
        self.assertEqual(result["decision"], "REQUIRE_APPROVAL")
        result = await self.adapter.check("docker --debug rm backend", misleading)
        self.assertEqual(result["decision"], "DENY")

    async def test_default_adapter_preserves_empty_resource_allowlist(self):
        result = await ExecutorSafetyChecker().check("docker restart -- backend", self.context)
        self.assertEqual(result["decision"], "DENY")

    async def test_executor_style_dispatch_runs_only_allow(self):
        calls = []

        async def dispatch(command):
            decision = await self.adapter.check(command, self.context)
            if decision["decision"] == "ALLOW":
                calls.append(command)
            return decision["decision"]

        results = await asyncio.gather(*(dispatch(command) for command in (
            "pwd", "docker --debug rm backend", "docker restart -- backend",
        )))
        self.assertEqual(results, ["ALLOW", "DENY", "REQUIRE_APPROVAL"])
        self.assertEqual(calls, ["pwd"])

    async def test_checker_error_propagates_to_executor_fail_closed_path(self):
        class BrokenChecker(RuleBasedSafetyChecker):
            def check(self, request):
                raise RuntimeError("Checker failure")

        with self.assertRaises(RuntimeError):
            await ExecutorSafetyChecker(BrokenChecker()).check("pwd", self.context)

    def test_sync_api_is_preserved(self):
        self.assertEqual(self.rules.check(CommandRequest("pwd")).decision, Decision.ALLOW)
        with self.assertRaises(TypeError):
            ExecutorSafetyChecker({})


if __name__ == "__main__":
    unittest.main()
