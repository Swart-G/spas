"""Регрессии границы checker/Runner и ограниченного вывода. Запуска команд на хосте нет."""

import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolRequestParams
from pydantic import TypeAdapter, ValidationError

from executer.safety_checker import RuleBasedSafetyChecker, SafetyPolicy
from executer.server_mcp import Server, Runner, Manual
from executer.server_mcp.Logging import JsonFormatter, PrivateRotatingFileHandler
from executer.server_mcp.Logging import setup_logging, shutdown_logging
from executer.server_mcp.Models import Command, ExecutionResult
from executer.server_mcp.Runner import read_limited
from executer.server_mcp.Configuration import load_checker

CONTEXT = {"incident_id": "test-incident", "reason": "Integration test"}


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_error_path_redacts_input_and_restores_logging(self):
        shutdown_logging()
        sdk = logging.getLogger("mcp")
        prior = (sdk.handlers[:], sdk.level, sdk.propagate)
        captured = io.StringIO()
        marker = "PRIVATE-INPUT-\x1b[2J-" + "x" * 5000
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict("os.environ", {"EXECUTOR_LOG_DIR": directory}), \
             redirect_stderr(captured):
            try:
                server = Server.create_server()
                root_handlers = logging.getLogger().handlers[:]
                # Реальный обработчик SDK повторно журналирует имя инструмента.
                result = await server._handle_call_tool(
                    None, CallToolRequestParams(name=marker, arguments={}),
                )
                self.assertTrue(result.is_error)
                self.assertNotIn("PRIVATE-INPUT", str(result))
                try:
                    raise ValueError("PRIVATE-INPUT-exception")
                except ValueError:
                    logging.getLogger("mcp.shared.session").exception("PRIVATE-INPUT-message")
                setup_logging()  # Повторная настройка не дублирует события.
            finally:
                shutdown_logging()
            outputs = [captured.getvalue(), *(p.read_text() for p in Path(directory).glob("*.log"))]
            self.assertGreater(len(outputs), 1)
            for output in outputs:
                self.assertNotIn("PRIVATE-INPUT", output)
                self.assertNotIn("\x1b", output)
                events = [json.loads(line) for line in output.splitlines()]
                sdk_events = [e for e in events if e["event"] == "mcp_sdk_event"]
                self.assertEqual(len(sdk_events), 2)
                self.assertEqual(sdk_events[1]["exceptions"][0]["type"], "ValueError")
        self.assertEqual((sdk.handlers, sdk.level, sdk.propagate), prior)
        self.assertEqual(logging.getLogger().handlers, root_handlers)

    async def test_busy_before_checker_and_recovery_after_cancel(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def check(command, context):
            started.set()
            await release.wait()
            return {"decision": "ALLOW", "reason": "test"}
        checker = SimpleNamespace(check=AsyncMock(side_effect=check))
        server = Server.create_server(checker=checker)
        payload = {"command": ["pwd"], "context": CONTEXT}
        with patch.object(Server, "run_command", AsyncMock(return_value=ExecutionResult(status="succeeded"))) as run:
            task = asyncio.create_task(server.call_tool("execute_command", payload))
            await asyncio.wait_for(started.wait(), 1)
            result = await asyncio.wait_for(server.call_tool("execute_command", payload), 1)
            self.assertTrue(result.is_error)
            self.assertIn("busy", result.structured_content["error"])
            self.assertFalse(result.structured_content["safety_checked"])
            self.assertEqual(checker.check.await_count, 1)
            run.assert_not_awaited()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            result = await server.call_tool("execute_command", payload)
            self.assertEqual(result.structured_content["status"], "succeeded")
            self.assertEqual(checker.check.await_count, 2)
            run.assert_awaited_once()

    async def test_manual_terminal_controls_are_visible_and_mcp_data_unchanged(self):
        payload = "Привет 😀 👩‍💻\n\ttext\x1b[2J\x1b]0;title\x07\x9b31m\r\b\u202eevil\u2066end"
        data = ExecutionResult(status="failed", stdout=payload, stderr=payload,
                               error=payload, exit_code=1).model_dump()
        server = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(structured_content=data)))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(Manual, "configured_server", return_value=server), \
             patch("builtins.input", side_effect=["cat /inputs/log.txt", "quit"]), \
             redirect_stdout(out), redirect_stderr(err):
            await Manual.run_manual()
        for output in (out.getvalue(), err.getvalue()):
            self.assertIn("Привет 😀 👩‍💻\n\ttext", output)
            for control in ("\x1b", "\x07", "\x9b", "\r", "\b", "\u202e", "\u2066"):
                self.assertNotIn(control, output)
                self.assertIn(control.encode("unicode_escape").decode("ascii"), output)
        self.assertEqual(data["stdout"], payload)
        self.assertEqual(data["stderr"], payload)

    async def test_manual_tool_errors_cannot_control_terminal(self):
        server = SimpleNamespace(call_tool=AsyncMock(side_effect=ToolError("bad\x1b[2J\x9b31m")))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(Manual, "configured_server", return_value=server), \
             patch("builtins.input", side_effect=["pwd", "quit"]), \
             redirect_stdout(out), redirect_stderr(err):
            await Manual.run_manual()
        self.assertNotIn("\x1b", err.getvalue())
        self.assertNotIn("\x9b", err.getvalue())
        self.assertIn(r"bad\x1b[2J\x9b31m", err.getvalue())

    async def call(self, checker, argv):
        server = Server.create_server(checker=checker)
        result = await server.call_tool("execute_command", {"command": argv, "context": CONTEXT})
        return result.structured_content

    async def test_allow_preserves_argv_and_context(self):
        checker = RuleBasedSafetyChecker()
        with patch.object(checker, "check", wraps=checker.check) as check, \
             patch.object(Server, "run_command", AsyncMock(return_value=ExecutionResult(status="succeeded"))) as run:
            result = await self.call(checker, ["uname", "-a"])
        self.assertTrue(result["safety_checked"])
        self.assertEqual(check.call_args.args[0], ["uname", "-a"])
        self.assertEqual(check.call_args.args[1].model_dump(), CONTEXT)
        self.assertEqual(run.call_args.args, (("uname", "-a"),))
        self.assertEqual(run.call_args.kwargs["request_id"], result["request_id"])

    async def test_checker_mutation_cannot_change_execution_snapshot(self):
        class MutatingChecker:
            async def check(self, command, context):
                command[:] = ["bash", "-c", "bad"]
                return {"decision": "ALLOW", "reason": "test"}
        with patch.object(Server, "run_command", AsyncMock(return_value=ExecutionResult(status="succeeded"))) as run:
            await self.call(MutatingChecker(), ["pwd"])
        self.assertEqual(run.call_args.args, (("pwd",),))

    async def test_deny_and_approval_do_not_start_process(self):
        checker = RuleBasedSafetyChecker(SafetyPolicy(containers={"backend"}))
        for argv, expected in ((["bash", "-c", "pwd"], "denied"),
                               (["docker", "restart", "--", "backend"], "approval_required")):
            with self.subTest(argv=argv), patch.object(Server, "run_command", AsyncMock()) as run:
                result = await self.call(checker, argv)
                run.assert_not_awaited()
                self.assertEqual(result["status"], expected)
                self.assertTrue(result["safety_checked"])

    async def test_missing_broken_and_invalid_checker_fail_closed(self):
        for payload in (None, {"decision": "ALLOW", "reason": "ok", "argv": ["pwd"]},
                        {"decision": "allow", "reason": "ok"}):
            checker = AsyncMock()
            checker.check.return_value = payload
            with self.subTest(payload=payload), patch.object(Server, "run_command", AsyncMock()) as run:
                result = await self.call(checker, ["pwd"])
                run.assert_not_awaited()
                self.assertEqual(result["status"], "error")
        checker = AsyncMock()
        checker.check.side_effect = RuntimeError("secret-must-not-escape")
        for gate in (None, checker):
            with patch.object(Server, "run_command", AsyncMock()) as run:
                result = await self.call(gate, ["pwd"])
                run.assert_not_awaited()
                self.assertEqual(result["status"], "error")
                self.assertNotIn("secret", json.dumps(result))

    async def test_checker_timeout_and_cancellation_do_not_start_process(self):
        class SlowChecker:
            async def check(self, command, context):
                await asyncio.sleep(60)
        with patch.object(Server, "run_command", AsyncMock()) as run:
            result = await self.call(SlowChecker(), ["pwd"])
            self.assertEqual(result["status"], "error")
            self.assertIn("timed out", result["error"])
            task = asyncio.create_task(self.call(SlowChecker(), ["pwd"]))
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            run.assert_not_awaited()

    async def test_legacy_string_rejected_before_checker(self):
        checker = AsyncMock()
        with self.assertRaises(ToolError):
            await self.call(checker, "pwd")
        checker.check.assert_not_awaited()

    async def test_validation_error_does_not_echo_large_or_secret_input(self):
        checker = AsyncMock()
        for value in ("SECRET-" + "x" * 10000, ["x"] * 5000):
            with self.assertRaises(ToolError) as caught:
                await self.call(checker, value)
            text = str(caught.exception)
            self.assertLess(len(text), 256)
            self.assertNotIn("SECRET", text)
        checker.check.assert_not_awaited()

    async def test_checker_reason_is_bounded(self):
        checker = AsyncMock()
        checker.check.return_value = {"decision": "DENY", "reason": "x" * 2001}
        with patch.object(Server, "run_command", AsyncMock()) as run:
            result = await self.call(checker, ["pwd"])
        self.assertEqual(result["status"], "error")
        self.assertLess(len(result["error"]), 256)
        run.assert_not_awaited()

    async def test_cancellation_during_spawn_keeps_process_for_cleanup(self):
        spawning, release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(pid=123, returncode=None,
                                  stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
        process.wait = AsyncMock(return_value=-9)

        async def spawn(*args, **kwargs):
            spawning.set()
            await release.wait()
            return process

        def kill_group(pid):
            process.returncode = -9
            process.stdout.feed_eof()
            process.stderr.feed_eof()

        with patch.object(Runner, "subprocess_options", return_value={}), \
             patch.object(Runner.asyncio, "create_subprocess_exec", side_effect=spawn), \
             patch.object(Runner, "kill_process_group", side_effect=kill_group) as kill, \
             patch.object(Runner, "kill_command_processes", return_value=0):
            task = asyncio.create_task(Runner.run_command(("fake",)))
            await spawning.wait()
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            kill.assert_called_once_with(123)
            self.assertFalse(Runner._EXECUTION_LOCK.locked())

    async def test_busy_rejects_second_spawn_and_cancel_releases_lock(self):
        spawned = asyncio.Event()
        process = SimpleNamespace(pid=123, returncode=None,
                                  stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
        process.wait = AsyncMock(return_value=-9)

        async def spawn(*args, **kwargs):
            spawned.set()
            return process

        def kill_group(pid):
            process.returncode = -9
            process.stdout.feed_eof()
            process.stderr.feed_eof()

        with patch.object(Runner, "subprocess_options", return_value={}), \
             patch.object(Runner.asyncio, "create_subprocess_exec", side_effect=spawn) as create, \
             patch.object(Runner, "kill_process_group", side_effect=kill_group), \
             patch.object(Runner, "kill_command_processes", return_value=0):
            task = asyncio.create_task(Runner.run_command(("fake",)))
            await spawned.wait()
            result = await Runner.run_command(("second",))
            self.assertEqual(result.status, "error")
            self.assertIn("busy", result.error)
            self.assertEqual(create.call_count, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertFalse(Runner._EXECUTION_LOCK.locked())

    async def test_output_boundaries_and_invalid_utf8(self):
        for raw, expected, truncated in (
            (b"a" * 8192, "a" * 8192, False),
            (b"a" * 8193, "a" * 8192, True),
            (b"a" * 8191 + "😀".encode(), "a" * 8191, True),
            (b"\xff" * 8192, "�" * 2730, True),
        ):
            stream = asyncio.StreamReader()
            stream.feed_data(raw)
            stream.feed_eof()
            self.assertEqual(await read_limited(stream), (expected, truncated))

    def test_total_argv_size_and_strict_types(self):
        adapter = TypeAdapter(Command)
        for command in ("pwd", [], [123], [""], ["pwd", "\x00"], ["pwd", "x" * 4093]):
            with self.subTest(command=command), self.assertRaises(ValidationError):
                adapter.validate_python(command)
        self.assertEqual(adapter.validate_python(["pwd", "x" * 4092])[0], "pwd")

    def test_invalid_deployment_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            with patch.dict("os.environ", {"EXECUTOR_POLICY_FILE": str(path)}):
                for policy in ({"readable_paths": ["/etc/passwd"]}, {"allow_everything": True}, []):
                    path.write_text(json.dumps(policy))
                    with self.assertRaises(ValueError):
                        load_checker()

    def test_nested_exception_group_keeps_root_cause_without_message(self):
        try:
            try:
                raise ValueError("secret-payload")
            except ValueError as exc:
                raise ExceptionGroup("secret-group", [ExceptionGroup("nested", [exc])])
        except ExceptionGroup:
            import sys
            record = logging.LogRecord("executor.test", logging.ERROR, __file__, 1,
                                       "server_failed", (), sys.exc_info())
        formatted = JsonFormatter().format(record)
        data = json.loads(formatted)
        self.assertEqual(data["exceptions"][0]["type"], "ValueError")
        self.assertTrue(data["exceptions"][0]["frames"])
        self.assertNotIn("secret", formatted)

    def test_logs_rotate_privately_without_payload_or_exception_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.log"
            handler = PrivateRotatingFileHandler(path, maxBytes=800, backupCount=2)
            handler.setFormatter(JsonFormatter())
            try:
                try:
                    raise ValueError("secret-marker")
                except ValueError:
                    import sys
                    record = logging.LogRecord("executor.test", logging.ERROR, __file__, 1,
                                               "internal_error", (), sys.exc_info())
                record.command = "secret-marker"
                record.stdout = "secret-marker"
                for _ in range(10):
                    handler.handle(record)
            finally:
                handler.close()
            files = list(Path(directory).iterdir())
            self.assertLessEqual(len(files), 3)
            self.assertGreater(len(files), 1)
            for file in files:
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("secret-marker", file.read_text())
                for line in file.read_text().splitlines():
                    self.assertEqual(json.loads(line)["exceptions"][0]["type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
