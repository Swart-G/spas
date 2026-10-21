"""Real MCP handler, checker, Runner and JSON logs; only OS process boundaries are fake."""

import asyncio
import hashlib
import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp.server.mcpserver.exceptions import ToolError

from executer.safety_checker import RuleBasedSafetyChecker, SafetyPolicy
from executer.safety_checker.permissions import AgentRole
from executer.server_mcp import Runner, Server
from executer.server_mcp.Logging import PrivateRotatingFileHandler, shutdown_logging

CONTEXT = {"incident_id": "PRIVATE-INCIDENT", "reason": "PRIVATE-REASON"}


class FakeProcess:
    """In-memory pipes, return code and completion; never creates an OS process."""

    pid = 424242

    def __init__(self, *, running=False, stdout=b"", stderr=b"", exit_code=0):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.returncode = None
        self.finished = asyncio.Event()
        if not running:
            self.finish(exit_code)

    def finish(self, code):
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.finished.set()

    async def wait(self):
        await self.finished.wait()
        return self.returncode


class ExecutorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        shutdown_logging()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {"EXECUTOR_LOG_DIR": str(self.directory)}))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))
        self.addCleanup(shutdown_logging)
        self.checker = RuleBasedSafetyChecker(SafetyPolicy(containers={"backend"}))
        # There is no subprocess fallback, even if an assertion or policy fails.
        self.spawn = self.enterContext(
            patch.object(
                Runner.asyncio,
                "create_subprocess_exec",
                new_callable=AsyncMock,
            )
        )
        self.spawn.side_effect = AssertionError("Unexpected process creation")
        self.enterContext(patch.object(Runner, "subprocess_options", return_value={}))
        self.kill = self.enterContext(patch.object(Runner, "kill_process_group"))
        self.enterContext(patch.object(Runner, "kill_command_processes", return_value=0))
        self.enterContext(
            patch.object(Runner.os, "_exit", side_effect=AssertionError("Cleanup failed"))
        )

    def server(self, checker=None, *, role=AgentRole.WORKER):
        return Server.create_server(
            checker=self.checker if checker is None else checker, agent_role=role
        )

    async def call(self, server, argv, context=None):
        response = await server.call_tool(
            "execute_command",
            {
                "command": argv,
                "context": CONTEXT if context is None else context,
            },
        )
        self.assertEqual(json.loads(response.content[0].text), response.structured_content)
        self.assertEqual(response.is_error, response.structured_content["status"] != "succeeded")
        return response.structured_content

    def fake_process(self, **kwargs):
        process = FakeProcess(**kwargs)
        self.spawn.side_effect = None
        self.spawn.return_value = process

        def kill(pid):
            self.assertEqual(pid, process.pid)
            if process.returncode is None:
                process.finish(-9)

        self.kill.side_effect = kill
        return process

    def records(self, request_id):
        records = [
            json.loads(line)
            for path in self.directory.glob("*.log")
            for line in path.read_text().splitlines()
        ]
        records = [record for record in records if record.get("request_id") == request_id]
        self.assertTrue(records, "No persisted audit records for this request")
        return records

    def audit(self, data, *, role="worker", events=()):
        records = self.records(data["request_id"])
        self.assertEqual(records[0]["event"], "request_received")
        self.assertEqual(records[-1]["event"], "request_finished")
        self.assertEqual(sum(r["event"] == "request_finished" for r in records), 1)
        self.assertTrue(all(r["agent_role"] == role for r in records))
        self.assertEqual(records[-1]["status"], data["status"])
        self.assertEqual(records[-1]["safety_checked"], data["safety_checked"])
        self.assertGreaterEqual(records[-1]["duration_ms"], 0)
        names = [r["event"] for r in records]
        for event in events:
            self.assertIn(event, names)
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        return records

    async def test_allow_reaches_real_runner_with_exact_argv(self):
        self.fake_process(stdout=b"Linux\n")
        data = await self.call(self.server(), ["uname", "-s"])
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["stdout"], "Linux\n")
        self.assertEqual(data["exit_code"], 0)
        self.assertTrue(data["safety_checked"])
        self.spawn.assert_awaited_once()
        self.assertEqual(self.spawn.call_args.args, ("uname", "-s"))
        self.assertTrue(self.spawn.call_args.kwargs["start_new_session"])
        self.assertNotIn("shell", self.spawn.call_args.kwargs)
        records = self.audit(
            data, events=("safety_decision", "process_started", "process_finished")
        )
        expected = hashlib.sha256(CONTEXT["incident_id"].encode()).hexdigest()[:16]
        self.assertTrue(any(r.get("incident_ref") == expected for r in records))

    async def test_hard_denials_and_docker_bypasses_never_spawn(self):
        server = self.server()
        for argv in (
            ["rm", "-rf", "/"],
            ["bash", "-c", "pwd"],
            ["docker", "exec", "x", "sh"],
            ["docker", "--debug", "rm", "x"],
            ["docker", "-D", "exec", "x", "id"],
            ["docker", "ps", "--debug"],
            ["docker", "custom-plugin"],
            ["pwd", "&&", "uname"],
            ["pwd", ">", "/tmp/file"],
            ["$(pwd)"],
        ):
            with self.subTest(argv=argv):
                data = await self.call(server, argv)
                self.assertEqual(data["status"], "denied")
                self.assertTrue(data["safety_checked"])
                self.assertIsNone(data["exit_code"])
                records = self.audit(data, events=("safety_decision",))
                self.assertNotIn("process_started", [r["event"] for r in records])
        self.spawn.assert_not_awaited()

    async def test_approval_is_never_permission_to_execute(self):
        data = await self.call(self.server(), ["docker", "restart", "--", "backend"])
        self.assertEqual(data["status"], "approval_required")
        self.assertTrue(data["safety_checked"])
        self.spawn.assert_not_awaited()
        self.audit(data, events=("safety_decision",))

    async def test_diagnostic_denial_is_audited_before_checker(self):
        data = await self.call(
            self.server(role=AgentRole.DIAGNOSTIC), ["docker", "restart", "--", "backend"]
        )
        self.assertEqual(data["status"], "denied")
        self.assertFalse(data["safety_checked"])
        records = self.audit(data, role="diagnostic", events=("agent_access_denied",))
        self.assertNotIn("safety_check_started", [r["event"] for r in records])
        self.spawn.assert_not_awaited()

    async def test_missing_checker_fails_closed_and_is_audited(self):
        data = await self.call(Server.create_server(agent_role=AgentRole.WORKER), ["pwd"])
        self.assertEqual(data["status"], "error")
        self.assertFalse(data["safety_checked"])
        self.audit(data, events=("safety_checker_missing",))
        self.spawn.assert_not_awaited()

    async def test_checker_exception_is_redacted_and_audited(self):
        checker = AsyncMock()
        checker.check.side_effect = RuntimeError("PRIVATE-EXCEPTION")
        data = await self.call(self.server(checker), ["pwd"])
        self.assertEqual(data["status"], "error")
        self.assertFalse(data["safety_checked"])
        records = self.audit(data, events=("safety_check_failed",))
        failure = next(r for r in records if r["event"] == "safety_check_failed")
        self.assertEqual(failure["exceptions"][0]["type"], "RuntimeError")
        self.assertNotIn("PRIVATE-EXCEPTION", json.dumps(data))
        self.spawn.assert_not_awaited()

    async def test_invalid_checker_responses_cannot_spawn(self):
        checker = AsyncMock()
        server = self.server(checker)
        for raw in (
            None,
            True,
            {},
            {"decision": "ALLOW"},
            {"decision": "allow", "reason": "x"},
            {"decision": "ALLOW", "reason": "x", "role": "worker"},
            {"decision": "ALLOW", "reason": ""},
            {"decision": "ALLOW", "reason": "x" * 2001},
        ):
            with self.subTest(raw=raw):
                checker.check.return_value = raw
                data = await self.call(server, ["pwd"])
                self.assertEqual(data["status"], "error")
                self.assertFalse(data["safety_checked"])
                self.audit(data, events=("safety_check_failed",))
        self.spawn.assert_not_awaited()

    async def test_checker_timeout_releases_slot_and_has_terminal_audit_event(self):
        checker = AsyncMock()

        async def hang(command, context):
            await asyncio.Event().wait()

        checker.check.side_effect = hang
        server = self.server(checker)
        with patch.object(Server, "SAFETY_CHECK_TIMEOUT_SECONDS", 0.02):
            data = await self.call(server, ["pwd"])
        self.assertEqual(data["status"], "error")
        self.assertFalse(data["safety_checked"])
        self.audit(data, events=("safety_check_timeout",))
        self.spawn.assert_not_awaited()
        checker.check.side_effect = self.checker.check
        self.fake_process()
        self.assertEqual((await self.call(server, ["pwd"]))["status"], "succeeded")

    async def test_execution_timeout_runs_real_cleanup_and_is_audited(self):
        self.fake_process(running=True)
        real_run = Runner.run_command

        async def short_run(argv, *, request_id):
            return await real_run(argv, timeout_seconds=0.02, request_id=request_id)

        with patch.object(Server, "run_command", side_effect=short_run):
            data = await self.call(self.server(), ["pwd"])
        self.assertEqual(data["status"], "timed_out")
        self.assertEqual(data["exit_code"], -9)
        self.assertTrue(data["safety_checked"])
        self.kill.assert_called_once_with(424242)
        self.audit(data, events=("process_timeout", "process_finished"))
        self.assertFalse(Runner._EXECUTION_LOCK.locked())

    async def test_spawn_error_cannot_leak_exception_payload(self):
        self.spawn.side_effect = OSError("PRIVATE-SPAWN")
        data = await self.call(self.server(), ["pwd"])
        self.assertEqual(data["status"], "error")
        self.assertTrue(data["safety_checked"])
        self.assertIsNone(data["exit_code"])
        self.assertNotIn("PRIVATE-SPAWN", json.dumps(data))
        self.audit(data, events=("process_start_failed",))

    async def test_nonzero_exit_and_bounded_output_are_preserved(self):
        self.fake_process(stdout=b"A" * 9000, stderr=b"B" * 9000, exit_code=7)
        data = await self.call(self.server(), ["pwd"])
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["exit_code"], 7)
        self.assertEqual(data["stdout"], "A" * 8192)
        self.assertEqual(data["stderr"], "B" * 8192)
        self.assertTrue(data["stdout_truncated"])
        self.assertTrue(data["stderr_truncated"])
        self.audit(data, events=("process_finished",))

    async def test_read_scope_blocks_even_an_allow_decision(self):
        checker = RuleBasedSafetyChecker(SafetyPolicy(readable_paths={"/etc/passwd"}))
        data = await self.call(self.server(checker), ["cat", "/etc/passwd"])
        self.assertEqual(data["status"], "denied")
        self.assertTrue(data["safety_checked"])
        self.spawn.assert_not_awaited()
        self.audit(data, events=("read_scope_rejected",))

    async def test_protected_execution_policy_failure_does_not_spawn(self):
        with patch.object(Runner, "subprocess_options", side_effect=RuntimeError("PRIVATE-POLICY")):
            data = await self.call(self.server(), ["pwd"])
        self.assertEqual(data["status"], "error")
        self.assertTrue(data["safety_checked"])
        self.spawn.assert_not_awaited()
        self.audit(data, events=("execution_policy_failed",))

    async def test_invalid_input_is_rejected_audited_and_redacted(self):
        server = self.server()
        for command, context in (
            ("PRIVATE-STRING", CONTEXT),
            ([], CONTEXT),
            (["pwd", "\x00"], CONTEXT),
            (["pwd"], {**CONTEXT, "role": "worker"}),
        ):
            with self.subTest(command=command):
                with self.assertRaises(ToolError) as caught:
                    await self.call(server, command, context)
                self.assertNotIn("PRIVATE", str(caught.exception))
        events = [json.loads(line) for line in self.stderr.getvalue().splitlines()]
        finished = [r for r in events if r["event"] == "request_finished"]
        self.assertEqual(len(finished), 4)
        self.assertEqual(len({r["request_id"] for r in finished}), 4)
        self.assertTrue(all(r["status"] == "rejected" for r in finished))
        self.spawn.assert_not_awaited()

    async def test_busy_and_checker_cancellation_are_audited_and_release_slot(self):
        entered = asyncio.Event()

        async def hang(command, context):
            entered.set()
            await asyncio.Event().wait()

        checker = AsyncMock()
        checker.check.side_effect = hang
        server = self.server(checker)
        task = asyncio.create_task(self.call(server, ["pwd"]))
        await asyncio.wait_for(entered.wait(), 1)
        busy = await self.call(server, ["pwd"])
        self.assertEqual(busy["status"], "error")
        self.audit(busy, events=("request_busy",))
        self.assertEqual(checker.check.await_count, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        finished = [
            json.loads(line)
            for line in self.stderr.getvalue().splitlines()
            if json.loads(line)["event"] == "request_finished"
        ]
        cancelled = next(r for r in finished if r["status"] == "cancelled")
        self.assertFalse(cancelled["safety_checked"])
        self.assertNotEqual(cancelled["request_id"], busy["request_id"])
        self.spawn.assert_not_awaited()
        checker.check.side_effect = self.checker.check
        self.fake_process()
        data = await self.call(server, ["pwd"])
        self.assertEqual(data["status"], "succeeded")
        self.audit(data)

    async def test_execution_cancellation_cleans_up_and_records_safety_check(self):
        self.fake_process(running=True)
        entered = asyncio.Event()

        async def spawn(*args, **kwargs):
            entered.set()
            return self.spawn.return_value

        self.spawn.side_effect = spawn
        server = self.server()
        task = asyncio.create_task(self.call(server, ["pwd"]))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.kill.assert_called_once_with(424242)
        events = [json.loads(line) for line in self.stderr.getvalue().splitlines()]
        terminal = next(r for r in events if r["event"] == "request_finished")
        self.assertEqual(terminal["status"], "cancelled")
        self.assertTrue(terminal["safety_checked"])
        self.assertIn("process_cancelled", [r["event"] for r in events])
        self.assertFalse(Runner._EXECUTION_LOCK.locked())

    async def test_audit_omits_incident_reason_output_and_exception_messages(self):
        self.fake_process(stdout=b"PRIVATE-OUTPUT", stderr=b"PRIVATE-STDERR")
        data = await self.call(self.server(), ["pwd"])
        self.audit(data)
        self.spawn.side_effect = OSError("PRIVATE-EXCEPTION")
        data = await self.call(self.server(), ["pwd"])
        self.audit(data)
        for text in (
            self.stderr.getvalue(),
            *(p.read_text() for p in self.directory.glob("*.log")),
        ):
            self.assertNotIn("PRIVATE", text)
            for line in text.splitlines():
                record = json.loads(line)
                for key in ("command", "reason", "stdout", "stderr", "incident_id"):
                    self.assertNotIn(key, record)

    async def test_unavailable_audit_file_uses_stderr_without_changing_decision(self):
        shutdown_logging()
        bad_directory = self.directory / "not-a-directory"
        bad_directory.write_text("test")
        self.fake_process()
        with patch.dict("os.environ", {"EXECUTOR_LOG_DIR": str(bad_directory)}):
            data = await self.call(self.server(), ["pwd"])
        self.assertEqual(data["status"], "succeeded")
        events = [json.loads(line) for line in self.stderr.getvalue().splitlines()]
        self.assertIn("log_file_unavailable", [r["event"] for r in events])
        self.assertTrue(
            any(
                r.get("request_id") == data["request_id"] and r["event"] == "request_finished"
                for r in events
            )
        )

    async def test_concurrent_role_bindings_have_separate_audit_contexts(self):
        entered, release = asyncio.Event(), asyncio.Event()
        checker = AsyncMock()

        async def hold(command, context):
            entered.set()
            await release.wait()
            return await self.checker.check(command, context)

        checker.check.side_effect = hold
        worker = self.server(checker, role=AgentRole.WORKER)
        diagnostic = self.server(checker, role=AgentRole.DIAGNOSTIC)
        argv = ["docker", "restart", "--", "backend"]
        pending = asyncio.create_task(self.call(worker, argv))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            denied = await self.call(diagnostic, argv)
        finally:
            release.set()
            approval = await pending
        self.assertEqual(denied["status"], "denied")
        self.assertEqual(approval["status"], "approval_required")
        self.assertNotEqual(denied["request_id"], approval["request_id"])
        self.audit(denied, role="diagnostic", events=("agent_access_denied",))
        self.audit(approval, role="worker", events=("safety_decision",))
        self.spawn.assert_not_awaited()

    async def test_audit_write_failure_is_reported_once_and_cannot_weaken_denial(self):
        server = self.server()
        handler = next(
            h
            for h in logging.getLogger("executor").handlers
            if isinstance(h, PrivateRotatingFileHandler)
        )
        with patch.object(handler, "shouldRollover", side_effect=OSError("PRIVATE-DISK")):
            for _ in range(2):
                data = await self.call(server, ["rm", "file"])
                self.assertEqual(data["status"], "denied")
        records = [json.loads(line) for line in self.stderr.getvalue().splitlines()]
        self.assertEqual(sum(r["event"] == "log_file_write_failed" for r in records), 1)
        self.assertEqual(sum(r["event"] == "request_finished" for r in records), 2)
        self.assertNotIn("PRIVATE", self.stderr.getvalue())
        self.spawn.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
