"""Диагностические инструменты: границы данных и защищённый источник."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolRequestParams

from executer.server_mcp import Diagnostic_collector, Diagnostics, Server


CONTEXT = {"incident_id": "incident-1", "reason": "Investigate service"}


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "diagnostics"
        Path(self.directory.name).chmod(0o750)
        self.root.mkdir(mode=0o750)
        self.service = self.root / "gateway"
        self.service.mkdir(mode=0o750)
        self.root_patch = patch.object(Diagnostics, "DIAGNOSTIC_ROOT", self.root)
        self.policy_patch = patch.object(Diagnostics, "subprocess_options", return_value={})
        self.root_patch.start()
        self.policy_patch.start()
        self.addAsyncCleanup(self.root_patch.stop)
        self.addAsyncCleanup(self.policy_patch.stop)
        self.server = Server.create_server()

    def snapshot(self, **changes):
        data = {
            "service": "gateway", "observed_at": time.time(),
            "state": "running", "availability": "available",
            "logs": "first\nsecond\nthird\n", "logs_truncated": False,
            "state_collected": True, "logs_collected": True,
        }
        data.update(changes)
        target = self.service / "snapshot.json"
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        target.chmod(0o640)

    async def call(self, tool, **kwargs):
        return await self.server.call_tool(tool, {"service": "gateway", "context": CONTEXT, **kwargs})

    async def test_tools_share_format_and_bounded_logs(self):
        self.snapshot()
        for tool, kind, output in (
            ("get_service_logs", "logs", "second\nthird\n"),
            ("get_service_state", "state", "running"),
            ("check_service_availability", "availability", "available"),
        ):
            with self.subTest(tool=tool):
                result = await self.call(tool, **({"lines": 2} if kind == "logs" else {}))
                data = result.structured_content
                self.assertFalse(result.is_error)
                self.assertEqual(data["kind"], kind)
                self.assertEqual(data["output"], output)
                self.assertEqual(data["truncated"], kind == "logs")
                self.assertTrue(data["request_id"])
                self.assertEqual(set(data), {"status", "kind", "service", "output", "truncated", "observed_at", "error", "request_id"})

    async def test_mcp_sdk_dispatch_preserves_diagnostic_result(self):
        self.snapshot()
        reply = await self.server._handle_call_tool(
            None,
            CallToolRequestParams(name="get_service_state", arguments={"service": "gateway", "context": CONTEXT}),
        )
        self.assertFalse(reply.is_error)
        self.assertEqual(reply.structured_content["output"], "running")

    async def test_unknown_stale_and_symlink_fail_closed(self):
        result = await self.call("get_service_state")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["output"], "")
        self.snapshot(observed_at=time.time() - 300)
        result = await self.call("check_service_availability")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["output"], "")
        source = self.service / "snapshot.json"
        source.rename(self.service / "original.json")
        source.symlink_to(self.service / "original.json")
        result = await self.call("get_service_logs")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["output"], "")

    async def test_oversized_snapshot_and_output_are_bounded(self):
        self.snapshot(logs="Ж" * 7000)
        result = await self.call("get_service_logs")
        self.assertFalse(result.is_error)
        self.assertLessEqual(len(result.structured_content["output"].encode("utf-8")), Diagnostics.MAX_OUTPUT_BYTES)
        self.assertTrue(result.structured_content["truncated"])
        self.snapshot(logs="\x00" * 5000)
        result = await self.call("get_service_logs")
        self.assertFalse(result.is_error)
        self.assertLessEqual(len(json.dumps(result.structured_content["output"], ensure_ascii=False).encode()), Diagnostics.MAX_OUTPUT_BYTES)
        self.assertTrue(result.structured_content["truncated"])
        # Экранированный Unicode в JSON также допустим внутри лимита снимка.
        self.snapshot(logs="Ж" * 4100)
        escaped = json.dumps(json.loads((self.service / "snapshot.json").read_text()))
        (self.service / "snapshot.json").write_text(escaped)
        result = await self.call("get_service_logs")
        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content["truncated"])
        (self.service / "snapshot.json").write_bytes(b"x" * (Diagnostics.MAX_SNAPSHOT_BYTES + 1))
        result = await self.call("get_service_logs")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["output"], "")

    async def test_untrusted_service_names_and_inputs_rejected(self):
        self.snapshot()
        for value in ("../gateway", "/etc", "gateway/..", "UPPER", "gateway\x00"):
            with self.subTest(value=value), self.assertRaises(ToolError):
                await self.server.call_tool("get_service_state", {"service": value, "context": CONTEXT})
        for value in (0, 201, "100", True):
            with self.subTest(value=value), self.assertRaises(ToolError):
                await self.call("get_service_logs", lines=value)

    async def test_protected_container_required(self):
        self.snapshot()
        with patch.object(Diagnostics, "subprocess_options", side_effect=RuntimeError):
            result = await self.call("get_service_state")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["output"], "")

    async def test_extended_acl_is_rejected(self):
        self.snapshot()
        with patch.object(Diagnostics.os, "getxattr", return_value=b"extended-acl"):
            result = await self.call("get_service_logs")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["output"], "")

    async def test_command_identity_cannot_own_diagnostic_root(self):
        self.snapshot()
        real_fstat = os.fstat
        for field in ("st_uid", "st_gid"):
            def collision(fd):
                info = real_fstat(fd)
                return SimpleNamespace(st_mode=info.st_mode,
                    st_uid=10001 if field == "st_uid" else info.st_uid,
                    st_gid=10001 if field == "st_gid" else info.st_gid)
            with self.subTest(field=field), patch.object(Diagnostics.os, "fstat", side_effect=collision):
                for tool in ("get_service_logs", "get_service_state", "check_service_availability"):
                    result = await self.call(tool)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["output"], "")

    async def test_failed_collection_is_not_reported_as_empty_logs(self):
        self.snapshot(logs_collected=False, state_collected=False)
        for name in ("get_service_logs", "get_service_state", "check_service_availability"):
            with self.subTest(name=name):
                result = await self.call(name)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["output"], "")


class CollectorTests(unittest.TestCase):
    def test_collector_rejects_reserved_owner_and_group(self):
        for uid, gid in ((10001, 1000), (1000, 10001)):
            for boundary in ("private", "input_root"):
                with self.subTest(uid=uid, gid=gid, boundary=boundary), \
                     patch.object(Diagnostic_collector.os, "geteuid", return_value=uid), \
                     patch.object(Diagnostic_collector.os, "getegid", return_value=gid), \
                     patch.object(Diagnostic_collector.os, "fstat", return_value=SimpleNamespace(
                         st_mode=stat.S_IFDIR | 0o750, st_uid=uid, st_gid=gid)):
                    with self.assertRaises(ValueError):
                        Diagnostic_collector._validate_directory(123, **{boundary: True})

    def test_launcher_rejects_command_uid_before_starting_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            inputs = parent / "inputs"
            (inputs / "diagnostics").mkdir(parents=True, mode=0o750)
            inputs.chmod(0o750)
            # Model a host operator UID10001 without privileged chown in tests.
            scripts = {
                "id": "#!/bin/sh\necho 10001\n",
                "stat": "#!/bin/sh\nvalue=$(/usr/bin/stat \"$@\")\n"
                        f"if [ \"$2\" = %u ] && [ \"$value\" = {os.geteuid()} ]; then echo 10001; else echo \"$value\"; fi\n",
                "docker": "#!/bin/sh\necho UNEXPECTED_DOCKER_CALL\n",
            }
            for name, content in scripts.items():
                path = parent / name
                path.write_text(content)
                path.chmod(0o700)
            env = {**os.environ, "PATH": f"{parent}:{os.environ['PATH']}", "EXECUTOR_INPUTS_DIR": str(inputs)}
            launcher = Path(Diagnostic_collector.__file__).with_name("Run_docker.sh")
            result = subprocess.run(["/bin/bash", str(launcher)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Unsafe diagnostic ownership", result.stderr)
            self.assertNotIn("UNEXPECTED_DOCKER_CALL", result.stdout)

    def test_launcher_grants_snapshot_group_only_to_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            inputs = parent / "inputs"
            diagnostics = inputs / "diagnostics"
            diagnostics.mkdir(parents=True, mode=0o750)
            inputs.chmod(0o750)
            executable = parent / "docker"
            executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            executable.chmod(0o700)
            env = {**os.environ, "PATH": f"{parent}:{os.environ['PATH']}", "EXECUTOR_INPUTS_DIR": str(inputs)}
            launcher = Path(Diagnostic_collector.__file__).with_name("Run_docker.sh")
            result = subprocess.run(["/bin/bash", str(launcher)], env=env, capture_output=True, text=True, check=True)
            args = result.stdout.splitlines()
            self.assertIn(["--group-add", str(diagnostics.stat().st_gid)], [args[i:i + 2] for i in range(len(args) - 1)])
            parent.chmod(0o770)
            rejected = subprocess.run(["/bin/bash", str(launcher)], env=env, capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("Unsafe writable diagnostic ancestor", rejected.stderr)

    def test_private_snapshot_stays_readable_by_diagnostic_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o750)
            output = parent / "diagnostics"
            output.mkdir(mode=0o750)
            data = {
                "service": "gateway", "observed_at": time.time(), "state": "running",
                "availability": "available", "logs": "secret-log", "logs_truncated": False,
                "state_collected": True, "logs_collected": True,
            }
            Diagnostic_collector.write_snapshot(output, "gateway", data)
            snapshot = output / "gateway" / "snapshot.json"
            self.assertEqual(stat.S_IMODE((output / "gateway").stat().st_mode), 0o750)
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o640)
            with patch.object(Diagnostics, "DIAGNOSTIC_ROOT", output):
                self.assertEqual(Diagnostics.read_snapshot("gateway").logs, "secret-log")
                snapshot.chmod(0o644)
                with self.assertRaises(ValueError):
                    Diagnostics.read_snapshot("gateway")

    def test_new_directories_keep_required_modes_under_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o750)
            output = parent / "diagnostics"
            snapshot = {
                "service": "gateway", "observed_at": time.time(), "state": "running",
                "availability": "available", "logs": "", "logs_truncated": False,
                "state_collected": True, "logs_collected": True,
            }
            old_umask = os.umask(0o077)
            try:
                with Diagnostic_collector.open_output(output, create=True) as output_fd:
                    Diagnostic_collector._write_snapshot_fd(output_fd, "gateway", snapshot)
            finally:
                os.umask(old_umask)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o750)
            self.assertEqual(stat.S_IMODE((output / "gateway").stat().st_mode), 0o750)
            self.assertEqual(stat.S_IMODE((output / "gateway" / "snapshot.json").stat().st_mode), 0o640)

    def test_attacker_writable_output_and_service_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o750)
            output = parent / "diagnostics"
            output.mkdir(mode=0o750)
            output.chmod(0o777)
            with self.assertRaises(ValueError):
                Diagnostic_collector.write_snapshot(output, "gateway", {"service": "gateway"})
            output.chmod(0o750)
            service = output / "gateway"
            service.mkdir(mode=0o750)
            service.chmod(0o770)
            with self.assertRaises(ValueError):
                Diagnostic_collector.write_snapshot(output, "gateway", {"service": "gateway"})

    def test_removed_service_is_revoked_on_collector_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o750)
            output = parent / "diagnostics"
            config = parent / "services.json"
            sample = {
                "service": "", "observed_at": time.time(), "state": "running",
                "availability": "available", "logs": "log", "logs_truncated": False,
                "state_collected": True, "logs_collected": True,
            }
            def collect(service, _container):
                return {**sample, "service": service}
            argv = ["collector", "--config", str(config), "--output", str(output)]
            with patch.object(Diagnostic_collector, "collect_service", side_effect=collect), patch.object(sys, "argv", argv):
                config.write_text(json.dumps({"services": {"removed": "old", "gateway": "current"}}))
                config.chmod(0o600)
                Diagnostic_collector.main()
                self.assertTrue((output / "removed" / "snapshot.json").exists())
                config.write_text(json.dumps({"services": {"gateway": "current"}}))
                Diagnostic_collector.main()
            self.assertFalse((output / "removed" / "snapshot.json").exists())
            with patch.object(Diagnostics, "DIAGNOSTIC_ROOT", output):
                with self.assertRaises(FileNotFoundError):
                    Diagnostics.read_snapshot("removed")
                self.assertEqual(Diagnostics.read_snapshot("gateway").state, "running")

    def test_docker_output_caps_memory_and_keeps_recent_bytes(self):
        original = subprocess.Popen

        def spawn(_args, **kwargs):
            return original((sys.executable, "-c", "import sys; sys.stdout.write('A'*20000+'TAIL')"), **kwargs)

        with patch.object(Diagnostic_collector.subprocess, "Popen", side_effect=spawn):
            data, truncated, success = Diagnostic_collector.docker_output(("logs",), 1024, tail=True)
        self.assertTrue(success)
        self.assertTrue(truncated)
        self.assertEqual(len(data), 1024)
        self.assertTrue(data.endswith(b"TAIL"))

    def test_docker_log_tail_keeps_newer_stderr_after_stdout_backlog(self):
        old_logs = "old\n" * 1400  # More than one 4096-byte read.
        newest_log = "NEWEST_ERROR\n"
        original = subprocess.Popen

        def spawn(_args, **kwargs):
            child = original(
                (
                    sys.executable, "-c",
                    "import sys; sys.stdout.write(sys.argv[1]); sys.stdout.flush(); "
                    "sys.stderr.write(sys.argv[2]); sys.stderr.flush()",
                    old_logs, newest_log,
                ),
                **kwargs,
            )
            # Ensure both streams have data before docker_output begins its
            # reads; selector arrival order must not redefine log recency.
            child.wait(timeout=2)
            return child

        with patch.object(Diagnostic_collector.subprocess, "Popen", side_effect=spawn):
            data, truncated, success = Diagnostic_collector.docker_output(
                ("logs", "--tail", "200", "container"), 128, tail=True,
            )
        self.assertTrue(success)
        self.assertTrue(truncated)
        self.assertTrue(data.endswith(newest_log.encode()))

    def test_only_configured_containers_are_collected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"services": {"gateway": "spas-gateway-1"}}))
            config.chmod(0o600)
            self.assertEqual(Diagnostic_collector.load_services(config), {"gateway": "spas-gateway-1"})
            config.write_text(json.dumps({"services": {"../escape": "spas-gateway-1"}}))
            with self.assertRaises(ValueError):
                Diagnostic_collector.load_services(config)
            config.write_text(json.dumps({"services": {"gateway": "$(whoami)"}}))
            with self.assertRaises(ValueError):
                Diagnostic_collector.load_services(config)

    def test_insecure_preexisting_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "services.json"
            config.write_text(json.dumps({"services": {"gateway": "spas-gateway-1"}}))
            config.chmod(0o666)
            with self.assertRaises(ValueError):
                Diagnostic_collector.load_services(config)

    def test_health_unknown_without_healthcheck_and_snapshot_written(self):
        inspect = json.dumps({"Running": True, "Status": "running"}).encode()
        with patch.object(Diagnostic_collector, "docker_output", side_effect=[
            (inspect, False, True), (b"log\n", False, True),
        ]) as docker:
            data = Diagnostic_collector.collect_service("gateway", "spas-gateway-1")
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["availability"], "unknown")
        self.assertEqual(data["logs"], "log\n")
        self.assertEqual(docker.call_args_list[0].args[0][-1], "spas-gateway-1")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o750)
            output = Path(directory) / "diagnostics"
            output.mkdir(mode=0o750)
            Diagnostic_collector.write_snapshot(output, "gateway", data)
            self.assertEqual(json.loads((output / "gateway/snapshot.json").read_text())["logs"], "log\n")
            with self.assertRaises(ValueError):
                Diagnostic_collector.write_snapshot(output, "../escape", data)

    def test_large_docker_health_history_does_not_hide_state(self):
        # Docker's full .State includes Health.Log, which can dwarf the useful
        # Running and Health.Status fields. Simulate Docker applying --format.
        full_state = {
            "Running": True,
            "Health": {
                "Status": "healthy",
                "Log": [{"Output": "healthcheck output " * 30} for _ in range(20)],
            },
        }
        full_response = json.dumps(full_state)
        self.assertGreater(len(full_response.encode()), 4096)
        projected_response = json.dumps({"Running": True, "Health": {"Status": "healthy"}})
        original_popen = subprocess.Popen
        inspect_templates = []

        def fake_docker(argv, **kwargs):
            if argv[1] == "inspect":
                template = argv[argv.index("--format") + 1]
                inspect_templates.append(template)
                response = full_response if template == "{{json .State}}" else projected_response
            else:
                response = "log line\n"
            return original_popen(
                (sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", response),
                **kwargs,
            )

        with patch.object(Diagnostic_collector.subprocess, "Popen", side_effect=fake_docker):
            snapshot = Diagnostic_collector.collect_service("gateway", "spas-gateway-1")
        self.assertTrue(snapshot["state_collected"])
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["availability"], "available")
        self.assertEqual(len(inspect_templates), 1)
        self.assertIn(".State.Running", inspect_templates[0])
        self.assertIn(".State.Health.Status", inspect_templates[0])
        self.assertNotIn("{{json .State}}", inspect_templates[0])

    def test_collect_cycle_overlaps_first_batch(self):
        # Eight slow Docker calls should overlap. Otherwise a larger allowed
        # service list can make early snapshots stale before the cycle ends.
        services = {f"service{i}": f"container{i}" for i in range(32)}
        maximum_age_before_next_cycle = (
            ((len(services) + 7) // 8) * 2 * Diagnostic_collector.COMMAND_TIMEOUT_SECONDS
            + Diagnostic_collector.POLL_INTERVAL_SECONDS
        )
        self.assertLess(maximum_age_before_next_cycle, Diagnostics.MAX_SNAPSHOT_AGE_SECONDS)
        barrier = threading.Barrier(8, timeout=3)
        original_pool = Diagnostic_collector.ThreadPoolExecutor

        def bounded_pool(*args, **kwargs):
            maximum = kwargs.get("max_workers", args[0] if args else None)
            self.assertLessEqual(maximum, 8)
            return original_pool(*args, **kwargs)

        def collect(service, _container):
            barrier.wait()
            return {
                "service": service, "observed_at": time.time(), "state": "running",
                "availability": "available", "logs": "", "logs_truncated": False,
                "state_collected": True, "logs_collected": True,
            }

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o750)
            output = parent / "diagnostics"
            output.mkdir(mode=0o750)
            with patch.object(Diagnostic_collector, "collect_service", side_effect=collect), patch.object(
                Diagnostic_collector, "ThreadPoolExecutor", side_effect=bounded_pool,
            ):
                with Diagnostic_collector.open_output(output) as output_fd:
                    Diagnostic_collector.collect_cycle(output_fd, services)
            for service in services:
                snapshot = json.loads((output / service / "snapshot.json").read_text())
                self.assertEqual(snapshot["service"], service)
                self.assertEqual(snapshot["state"], "running")


if __name__ == "__main__":
    unittest.main()
