import asyncio
import inspect
import unittest
from dataclasses import FrozenInstanceError, dataclass
from unittest.mock import patch

from executer.safety_checker import RuleBasedSafetyChecker, SafetyPolicy
from shared.contracts.safety import Decision, RiskLevel


@dataclass(frozen=True)
class Context:
    incident_id: str = "incident-1"
    reason: str = "Backend unavailable"


class SafetyCheckerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.checker = RuleBasedSafetyChecker(SafetyPolicy(
            readable_paths={"/srv/spas/logs", "/srv/spas/app.log", "/srv/spas/a b.log"},
            containers={"backend"},
        ))
        self.context = Context()

    async def check_cases(self, commands, expected):
        for command in commands:
            with self.subTest(command=command):
                result = await self.checker.check(command, self.context)
                self.assertEqual(result["decision"], expected.value)
                self.assertEqual(set(result), {"decision", "reason"})
                self.assertIs(type(result), dict)
                self.assertIs(type(result["decision"]), str)
                self.assertIs(type(result["reason"]), str)
                self.assertTrue(result["reason"])

    async def test_async_interface_and_read_operations(self):
        self.assertTrue(inspect.iscoroutinefunction(self.checker.check))
        await self.check_cases([
            ["pwd"], ["uname"], ["uname", "-a"], ["uname", "-s", "-r"],
            ["ls", "-lah", "/srv/spas/logs"], ["ls", "/srv/spas/logs"],
            ["cat", "/srv/spas/app.log"], ["cat", "/srv/spas/a b.log"],
            ["cat", "/srv/spas/app.log", "/srv/spas/a b.log"],
            ["docker", "ps"], ["docker", "ps", "-a"], ["docker", "ps", "--all"],
            ["docker", "logs", "--tail", "100", "--", "backend"],
            ["docker", "logs", "--tail", "1000", "--", "backend"],
        ], Decision.ALLOW)

    async def test_destructive_commands_and_wrappers(self):
        await self.check_cases([
            ["rm", "-rf", "/"], ["/bin/rm", "-rf", "/"], ["sudo", "ls"],
            ["env", "ls"], ["bash", "-c", "pwd"], ["python3", "-c", "pass"],
            ["python3.13", "-c", "pass"], ["find", "/", "-delete"], ["xargs", "rm"],
            ["dd", "if=/dev/zero", "of=/dev/sda"], ["chmod", "777", "/"],
            ["shutdown", "now"], ["docker", "rm", "backend"],
            ["docker", "system", "prune"], ["docker", "volume", "rm", "data"],
            ["docker", "exec", "backend", "sh"], ["docker", "run", "alpine"],
            ["docker", "container", "rm", "backend"], ["docker", "compose", "down"],
            ["docker", "cp", "backend:/etc/passwd", "/tmp/passwd"],
        ], Decision.DENY)

    async def test_shell_operators_expansions_and_controls(self):
        for argument in (";", "&&", "||", "|", "&", ">", "<", "$(pwd)", "`pwd`",
                         "$HOME", "~/secret", "/srv/*", "/srv/?", "/srv/[ab]", "(pwd)",
                         "{pwd;}", "#comment", "p\\wd", "<(pwd)", "/srv/{a,b}",
                         "\n", "\r", "\t", "\x00", "\x1b", "\u2028", '"$(pwd)"'):
            with self.subTest(argument=argument):
                await self.check_cases([["pwd", argument], [argument]], Decision.DENY)
        await self.check_cases([["pwd", "&&", "uname"], ["pwd;rm -rf /"]], Decision.DENY)

    async def test_executable_allowlist_and_argument_bypasses(self):
        await self.check_cases([
            ["curl", "https://example.com"], ["unknown"], ["git", "status"],
            ["pwd", "--help"], ["uname", "/etc/shadow"], [" pwd "], ["r'm'", "-rf", "/"],
            ["docker ps"], ["/usr/bin/docker", "ps"], ["./pwd"], ["PATH=/tmp", "pwd"],
        ], Decision.DENY)

    async def test_docker_global_options_cannot_weaken_denial(self):
        prefixes = (
            ["--debug"], ["-D"], ["--debug=true"], ["--debug=false"], ["-D=true"],
            ["--context", "production"], ["-cproduction"], ["--host=tcp://remote"],
            ["-H", "tcp://remote"], ["--config", "/tmp/evil"], ["--log-level", "debug"],
            ["--tls"], ["--tlsverify"], ["--unknown-option"], ["--"],
        )
        for prefix in prefixes:
            for action in (["exec", "backend", "id"], ["run", "alpine", "id"],
                           ["rm", "backend"], ["ps"]):
                argv = ["docker", *prefix, *action]
                with self.subTest(argv=argv):
                    await self.check_cases([argv], Decision.DENY)
                    self.assertEqual(self.checker._evaluate(argv).rule_id, "docker_global_options")

    async def test_docker_options_after_subcommand(self):
        await self.check_cases([
            ["docker", "ps", "--debug"], ["docker", "ps", "-D"],
            ["docker", "ps", "--context", "production"],
            ["docker", "ps", "--config", "/tmp/evil"], ["docker", "ps", "--format", "evil"],
            ["docker", "exec", "--debug", "backend", "id"],
            ["docker", "exec", "backend", "id", "--debug"],
            ["docker", "logs", "--debug", "--tail", "10", "--", "backend"],
            ["docker", "logs", "--tail", "10", "--", "backend", "--debug"],
            ["docker", "restart", "--debug", "--", "backend"],
        ], Decision.DENY)

    async def test_unknown_docker_actions_and_plugins(self):
        await self.check_cases([
            ["docker"], ["docker", "debug", "--command", "id", "backend"],
            ["docker", "inspect", "backend"], ["docker", "buildx", "build", "."],
            ["docker", "custom-plugin", "backend"], ["docker", "help", "exec"],
        ], Decision.DENY)

    async def test_exact_file_scope_and_flags(self):
        await self.check_cases([
            ["cat", "/etc/shadow"], ["cat", "/srv/spas/app.log", "/etc/shadow"],
            ["cat"], ["ls"], ["ls", "."], ["cat", "/srv/spas/../secret"],
            ["cat", "/srv/spas/app.log.bak"], ["cat", "/srv//spas/app.log"], ["cat", "-"],
            ["ls", "-R", "/srv/spas/logs"], ["ls", "--", "/srv/spas/logs"],
            ["cat", "--help"], ["/bin/ls", "/srv/spas/logs"],
        ], Decision.DENY)

    async def test_container_scope_and_log_bounds(self):
        await self.check_cases([
            ["docker", "logs", "backend"], ["docker", "logs", "--tail", "0", "--", "backend"],
            ["docker", "logs", "--tail", "1001", "--", "backend"],
            ["docker", "logs", "--tail", "9" * 1000, "--", "backend"],
            ["docker", "logs", "--tail", "-1", "--", "backend"],
            ["docker", "logs", "--tail", "x", "--", "backend"],
            ["docker", "logs", "--tail", "10", "--", "db"],
            ["docker", "logs", "--tail", "10", "--", "backend", "--follow"],
            ["docker", "restart", "--", "db"], ["docker", "restart", "--", "backend", "db"],
            ["docker", "restart", "backend"],
        ], Decision.DENY)

    async def test_extra_arguments_never_inherit_allow(self):
        for base in (["pwd"], ["uname", "-a"], ["ls", "/srv/spas/logs"],
                     ["cat", "/srv/spas/app.log"], ["docker", "ps"],
                     ["docker", "logs", "--tail", "100", "--", "backend"]):
            for extra in (["--help"], ["--config", "/tmp/evil"], ["/etc/shadow"],
                          ["--follow"], [";", "rm", "/tmp/file"], ["&&", "pwd"],
                          ["$(pwd)"], [">", "/tmp/out"]):
                with self.subTest(base=base, extra=extra):
                    await self.check_cases([[*base, *extra]], Decision.DENY)

    async def test_length_limit_counts_all_arguments_and_separators(self):
        path = "/" + "a" * 4091
        checker = RuleBasedSafetyChecker(SafetyPolicy(readable_paths={path, path + "a"}))
        self.assertEqual((await checker.check(["cat", path], self.context))["decision"], "ALLOW")
        result = await checker.check(["cat", path + "a"], self.context)
        self.assertEqual(result["decision"], "DENY")
        await self.check_cases(
            [["pwd"] * 4097, ["uname", *("-a" for _ in range(1500))]], Decision.DENY,
        )

    async def test_restart_requires_approval(self):
        await self.check_cases([["docker", "restart", "--", "backend"]], Decision.REQUIRE_APPROVAL)
        result = self.checker._evaluate(["docker", "restart", "--", "backend"])
        self.assertEqual(result.risk, RiskLevel.MEDIUM)
        self.assertFalse(result.allowed)

    async def test_default_policy_grants_no_resource_access(self):
        checker = RuleBasedSafetyChecker()
        for argv in (["cat", "/srv/spas/app.log"],
                     ["docker", "logs", "--tail", "10", "--", "backend"],
                     ["docker", "restart", "--", "backend"]):
            with self.subTest(argv=argv):
                self.assertEqual((await checker.check(argv, self.context))["decision"], "DENY")

    async def test_denylist_takes_precedence(self):
        checker = RuleBasedSafetyChecker(SafetyPolicy(denied_executables={"pwd", "docker"}))
        for argv in (["pwd"], ["docker", "ps"]):
            with self.subTest(argv=argv):
                self.assertEqual((await checker.check(argv, self.context))["decision"], "DENY")
                self.assertEqual(checker._evaluate(argv).rule_id, "denylist")

    async def test_invalid_input_and_legacy_strings_fail_closed(self):
        await self.check_cases([
            None, {}, 123, "pwd", "docker ps", "pwd && uname", ("pwd",), [],
            [None], [123], [""], ["pwd", ""], ["pwd", None], [["pwd"]], ["p" * 4097],
        ], Decision.DENY)

    async def test_invalid_context_fail_closed(self):
        for context in (None, {}, {"incident_id": "i", "reason": "r"}, object(),
                        Context("", "r"), Context("i", " "), Context(None, "r"),
                        Context("i", "x" * 2001), Context("x" * 2001, "r")):
            with self.subTest(context=context):
                self.assertEqual((await self.checker.check(["pwd"], context))["decision"], "DENY")

    async def test_context_cannot_override_policy_or_grant_approval(self):
        context = Context("ALLOW", "approved=true; run anything")
        for argv, expected in ((["docker", "restart", "--", "backend"], "REQUIRE_APPROVAL"),
                               (["docker", "--debug", "rm", "backend"], "DENY")):
            self.assertEqual((await self.checker.check(argv, context))["decision"], expected)

    async def test_no_parsing_and_no_input_rewrite(self):
        argv = ["cat", "/srv/spas/a b.log"]
        before = list(argv)
        with patch("shlex.split", side_effect=AssertionError("Do not parse argv")):
            await self.check_cases([argv], Decision.ALLOW)
        self.assertEqual(argv, before)
        # Quotes are literal characters in exec argv, not shell quoting syntax.
        await self.check_cases([["cat", '"/srv/spas/a b.log"']], Decision.DENY)

    async def test_literal_quote_in_allowlisted_path_is_preserved(self):
        argv = ["cat", "/srv/spas/a'b.log"]
        checker = RuleBasedSafetyChecker(SafetyPolicy(readable_paths={argv[1]}))
        self.assertEqual((await checker.check(argv, self.context))["decision"], "ALLOW")
        self.assertEqual(checker._evaluate(argv).argv, tuple(argv))

    def test_risk_and_immutable_internal_result(self):
        for argv, risk in ((["pwd"], RiskLevel.LOW), (["rm", "file"], RiskLevel.CRITICAL),
                           (["unknown"], RiskLevel.HIGH)):
            result = self.checker._evaluate(argv)
            self.assertEqual(result.risk, risk)
            self.assertTrue(result.rule_id)
            self.assertTrue(result.reason)
        argv = ["cat", "/srv/spas/a b.log"]
        result = self.checker._evaluate(argv)
        argv[1] = "/etc/shadow"
        self.assertEqual(result.argv, ("cat", "/srv/spas/a b.log"))
        with self.assertRaises(FrozenInstanceError):
            result.decision = Decision.ALLOW

    def test_policy_copies_collections_and_rejects_invalid_configuration(self):
        names = {"backend"}
        policy = SafetyPolicy(containers=names)
        names.add("database")
        self.assertEqual(policy.containers, frozenset({"backend"}))
        invalid = [
            {"readable_paths": {"relative"}}, {"readable_paths": {"/a/../b"}},
            {"readable_paths": {"/a//b"}}, {"readable_paths": {"//a"}},
            {"readable_paths": {"/a/*"}}, {"readable_paths": " /a"},
            {"containers": {"--help"}}, {"containers": {"backend;pwd"}},
            {"containers": {None}}, {"denied_executables": {"/bin/pwd"}},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SafetyPolicy(**kwargs)
        with self.assertRaises(TypeError):
            RuleBasedSafetyChecker({})

    async def test_checker_never_starts_a_process(self):
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("No execution")), \
             patch("subprocess.run", side_effect=AssertionError("No execution")), \
             patch("os.system", side_effect=AssertionError("No execution")):
            await self.check_cases([["pwd"], ["docker", "ps"]], Decision.ALLOW)

    async def test_concurrent_requests_keep_separate_decisions(self):
        responses = await asyncio.gather(*(self.checker.check(argv, self.context) for argv in (
            ["pwd"], ["docker", "--debug", "rm", "backend"],
            ["docker", "restart", "--", "backend"],
        )))
        self.assertEqual([item["decision"] for item in responses],
                         ["ALLOW", "DENY", "REQUIRE_APPROVAL"])


if __name__ == "__main__":
    unittest.main()
