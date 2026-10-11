import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from executer.safety_checker import RuleBasedSafetyChecker, SafetyPolicy
from shared.contracts.safety import CommandRequest, Decision, RiskLevel


class SafetyCheckerTests(unittest.TestCase):
    def setUp(self):
        self.checker = RuleBasedSafetyChecker(SafetyPolicy(
            readable_paths=frozenset({"/srv/spas/logs", "/srv/spas/app.log", "/srv/spas/a b.log"}),
            containers=frozenset({"backend"}),
        ))

    def check_cases(self, commands, expected):
        for command in commands:
            with self.subTest(command=command):
                result = self.checker.check(CommandRequest(command))
                self.assertEqual(result.decision, expected)
                self.assertEqual(result.allowed, expected is Decision.ALLOW)

    def test_allow_only_supported_read_operations(self):
        self.check_cases([
            "pwd", "  pwd  ", "uname", "uname -a", "uname -s -r",
            "ls -lah /srv/spas/logs", "ls /srv/spas/logs", "cat /srv/spas/app.log",
            'cat "/srv/spas/a b.log"', "cat /srv/spas/app.log /srv/spas/a' 'b.log",
            "docker ps", "docker ps -a", "docker ps --all",
            "docker logs --tail 100 -- backend", "docker logs --tail 1000 -- backend",
        ], Decision.ALLOW)

    def test_deny_destructive_commands_and_wrappers(self):
        self.check_cases([
            "rm -rf /", "/bin/rm -rf /", "r'm' -rf /", "sudo ls", "env ls",
            "bash -c pwd", "python3 -c pass", "python3.13 -c pass", "find / -delete", "xargs rm",
            "dd if=/dev/zero of=/dev/sda", "chmod 777 /", "shutdown now",
            "docker rm backend", "docker system prune", "docker volume rm data",
            "docker exec backend sh", "docker run alpine", "docker container rm backend",
            "docker compose down", "docker cp backend:/etc/passwd /tmp/passwd",
        ], Decision.DENY)

    def test_deny_shell_syntax_even_inside_quotes(self):
        self.check_cases([
            "pwd; rm -rf /", "pwd && uname", "pwd || uname", "pwd | cat",
            "pwd &", "pwd > /tmp/out", "cat < /srv/spas/app.log", "pwd\nuname",
            "pwd\runame", "pwd\t", "pwd\x00", "pwd\x1b", "pwd\u2028uname",
            "cat $(pwd)", "cat `pwd`", "cat $HOME", "cat ~/secret", "cat /srv/*",
            "cat /srv/?", "cat /srv/[ab]", "(pwd)", "{ pwd; }", "pwd # comment",
            'cat "$(rm -rf /)"', "p\\wd", "cat <(pwd)", "cat /srv/{a,b}",
        ], Decision.DENY)

    def test_unknown_commands_and_argument_bypasses_never_allow(self):
        self.check_cases([
            "curl https://example.com", "unknown",
            "pwd --help", "uname /etc/shadow", "docker inspect backend",
            "docker -H tcp://remote ps", "docker --config /tmp/evil ps",
            "docker ps --format evil", "git status",
        ], Decision.REQUIRE_APPROVAL)

    def test_file_scope_and_unsupported_flags(self):
        self.check_cases([
            "cat /etc/shadow", "cat /srv/spas/app.log /etc/shadow", "cat", "ls",
            "ls .", "cat /srv/spas/../secret", "cat /srv/spas/app.log.bak",
            "cat /srv//spas/app.log", "cat -", "ls -R /srv/spas/logs",
            "ls -- /srv/spas/logs", "cat --help", "/bin/ls /srv/spas/logs",
            "PATH=/tmp pwd", "./pwd",
        ], Decision.DENY)

    def test_container_scope_and_bounds(self):
        self.check_cases([
            "docker logs backend", "docker logs --tail 0 -- backend",
            "docker logs --tail 1001 -- backend",
            "docker logs --tail " + "9" * 1000 + " -- backend", "docker logs --tail -1 -- backend",
            "docker logs --tail x -- backend", "docker logs --tail 10 -- db",
            "docker logs --tail 10 -- backend --follow", "docker restart -- db",
            "docker restart -- backend db", "docker restart backend",
        ], Decision.DENY)

    def test_extra_arguments_cannot_inherit_allow_decision(self):
        for base in ("pwd", "uname -a", "ls /srv/spas/logs", "cat /srv/spas/app.log",
                     "docker ps", "docker logs --tail 100 -- backend"):
            for suffix in (" --help", " --config /tmp/evil", " /etc/shadow", " --follow",
                           " ; rm /tmp/file", " && pwd", " $(pwd)", " > /tmp/out"):
                with self.subTest(base=base, suffix=suffix):
                    self.assertFalse(self.checker.check(CommandRequest(base + suffix)).allowed)

    def test_command_length_boundary(self):
        result = self.checker.check(CommandRequest("pwd" + " " * 4093))
        self.assertTrue(result.allowed)
        result = self.checker.check(CommandRequest("pwd" + " " * 4094))
        self.assertEqual(result.decision, Decision.DENY)

    def test_restart_requires_approval(self):
        result = self.checker.check(CommandRequest("docker restart -- backend"))
        self.assertEqual(result.decision, Decision.REQUIRE_APPROVAL)
        self.assertEqual(result.risk, RiskLevel.MEDIUM)
        self.assertFalse(result.allowed)

    def test_default_policy_grants_no_file_or_container_access(self):
        checker = RuleBasedSafetyChecker()
        for command in ("cat /srv/spas/app.log", "docker logs --tail 10 -- backend",
                        "docker restart -- backend"):
            with self.subTest(command=command):
                self.assertEqual(checker.check(CommandRequest(command)).decision, Decision.DENY)

    def test_denylist_takes_precedence_over_allowlist(self):
        checker = RuleBasedSafetyChecker(SafetyPolicy(denied_executables={"pwd", "docker"}))
        for command in ("pwd", "docker ps"):
            with self.subTest(command=command):
                self.assertEqual(checker.check(CommandRequest(command)).rule_id, "denylist")

    def test_invalid_requests_fail_closed(self):
        self.check_cases(["", " ", "'", '"pwd', "''", "pwd ''", "p" * 4097], Decision.DENY)
        for request in (None, {}, "pwd", CommandRequest(None), CommandRequest(123)):
            with self.subTest(request=request):
                self.assertFalse(self.checker.check(request).allowed)

    def test_risk_and_explanation(self):
        for command, risk in (("pwd", RiskLevel.LOW), ("rm file", RiskLevel.CRITICAL),
                              ("unknown", RiskLevel.HIGH)):
            result = self.checker.check(CommandRequest(command))
            self.assertEqual(result.risk, risk)
            self.assertTrue(result.rule_id)
            self.assertTrue(result.reason)

    def test_exact_argv_and_immutable_result(self):
        result = self.checker.check(CommandRequest('cat "/srv/spas/a b.log"'))
        self.assertEqual(result.argv, ("cat", "/srv/spas/a b.log"))
        with self.assertRaises(FrozenInstanceError):
            result.decision = Decision.ALLOW

    def test_policy_copies_collections_and_validates_configuration(self):
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

    def test_checker_does_not_execute_commands(self):
        with patch("subprocess.run", side_effect=AssertionError("Must not execute")), \
             patch("os.system", side_effect=AssertionError("Must not execute")):
            self.check_cases(["pwd", "docker ps"], Decision.ALLOW)


if __name__ == "__main__":
    unittest.main()
