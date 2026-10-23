"""Реальные MCP-сеансы в Docker: argv, checker, права и ограничение вывода."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .Runner import run_command

LAUNCHER = Path(__file__).with_name("Run_docker.sh").resolve()
CONTEXT = {"incident_id": "docker-smoke", "reason": "Verify execution restrictions"}


@asynccontextmanager
async def connect(*args, env=None, errlog=sys.stderr):
    environment = os.environ.copy()
    # Проверки используют свою конфигурацию, не настройки оператора.
    for key in ("EXECUTOR_POLICY_FILE", "EXECUTOR_INPUTS_DIR"):
        environment.pop(key, None)
    environment.update(env or {})
    params = StdioServerParameters(command="/bin/bash", args=[str(LAUNCHER), *args], env=environment)
    async with stdio_client(params, errlog=errlog) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield session


async def call(session, argv):
    result = await session.call_tool("execute_command", {"command": argv, "context": CONTEXT})
    data = result.structured_content
    assert data and data["request_id"], result
    assert json.loads(result.content[0].text) == data
    return data


async def main():
    local = await run_command(("pwd",))
    assert local.status == "error" and not local.stdout
    print("OK: no host execution fallback", flush=True)

    async with connect() as session:
        tools = await session.list_tools()
        assert [tool.name for tool in tools.tools] == [
            "execute_command", "get_service_logs", "get_service_state", "check_service_availability",
        ], tools
        result = await call(session, ["pwd"])
        assert result["stdout"] == "/workspace\n" and result["safety_checked"], result
        result = await call(session, ["uname", "-s"])
        assert result["stdout"] == "Linux\n", result
        for argv in (["bash", "-c", "id"], ["pwd", "&&", "uname"], ["cat", "/etc/passwd"], ["docker", "ps"]):
            result = await call(session, argv)
            assert result["status"] == "denied" and not result["stdout"] and result["safety_checked"], result
        result = await session.call_tool("execute_command", {"command": "pwd", "context": CONTEXT})
        assert result.is_error and not result.structured_content, result
        results = await asyncio.gather(*(call(session, ["pwd"]) for _ in range(8)))
        assert len({r["request_id"] for r in results}) == 8
        assert all(r["status"] == "succeeded" or "busy" in (r["error"] or "") for r in results), results
        print("OK: checked MCP, strings/injections denied, concurrent request IDs", flush=True)

    # Реальный SDK пишет ошибки уже после возврата call_tool: проверяем stderr
    # отдельного контейнера, а не только форматтер или прямой вызов инструмента.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        async with connect(errlog=errors) as session:
            result = await session.call_tool("PRIVATE-INPUT-MARKER", {})
            assert result.is_error and "PRIVATE-INPUT-MARKER" not in str(result), result
            result = await call(session, ["pwd"])
            assert result["status"] == "succeeded", result
        errors.seek(0)
        records = errors.read()
        assert "PRIVATE-INPUT-MARKER" not in records, records
        assert any(json.loads(line)["event"] == "mcp_sdk_event" for line in records.splitlines()), records
    print("OK: SDK errors redacted in real MCP stderr; next request succeeds", flush=True)

    with tempfile.TemporaryDirectory(prefix="spas-inputs-") as directory:
        source = Path(directory)
        source.chmod(0o755)
        (source / "sample.txt").write_text("input-data\n")
        (source / "sample.txt").chmod(0o444)
        (source / "escape").symlink_to("/etc/passwd")
        terminal_payload = "Привет 👩‍💻\n\ttext\x1b[2J\x1b]0;untrusted\x07\x9b31m\u202eevil\r"
        (source / "terminal.txt").write_text(terminal_payload)
        (source / "terminal.txt").chmod(0o444)
        policy = source / "policy.json"
        policy.write_text(json.dumps({"readable_paths": ["/inputs/sample.txt", "/inputs/escape", "/inputs/terminal.txt"]}))
        policy.chmod(0o444)
        diagnostic = source / "diagnostics" / "gateway"
        diagnostic.mkdir(parents=True)
        (source / "diagnostics").chmod(0o750)
        diagnostic.chmod(0o750)
        (diagnostic / "snapshot.json").write_text(json.dumps({
            "service": "gateway", "observed_at": time.time(), "state": "running",
            "availability": "available", "logs": "gateway healthy\n", "logs_truncated": False,
            "state_collected": True, "logs_collected": True,
        }))
        (diagnostic / "snapshot.json").chmod(0o640)
        async with connect(env={"EXECUTOR_INPUTS_DIR": directory, "EXECUTOR_POLICY_FILE": str(policy)}) as session:
            result = await call(session, ["cat", "/inputs/sample.txt"])
            assert result["stdout"] == "input-data\n", result
            result = await call(session, ["cat", "/inputs/escape"])
            assert result["status"] == "denied" and not result["stdout"], result
            result = await call(session, ["cat", "/inputs/policy.json"])
            assert result["status"] == "denied", result
            result = await call(session, ["cat", "/inputs/terminal.txt"])
            assert result["stdout"] == terminal_payload, result
            for name, expected in (
                ("get_service_logs", "gateway healthy\n"),
                ("get_service_state", "running"),
                ("check_service_availability", "available"),
            ):
                reply = await session.call_tool(name, {"service": "gateway", "context": CONTEXT})
                assert not reply.is_error and reply.structured_content["output"] == expected, reply
            reply = await session.call_tool("get_service_logs", {"service": "../escape", "context": CONTEXT})
            assert reply.is_error, reply
        print("OK: exact read scope and symlink rejection", flush=True)
        print("OK: diagnostic MCP tools read only bounded service snapshots", flush=True)

        # Ручной интерфейс экранирует терминальные команды, MCP выше сохраняет данные.
        manual_env = os.environ.copy()
        manual_env.update(EXECUTOR_INPUTS_DIR=directory, EXECUTOR_POLICY_FILE=str(policy))
        manual = await asyncio.create_subprocess_exec(
            "/bin/bash", str(LAUNCHER), "--manual", env=manual_env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await manual.communicate(b"cat /inputs/terminal.txt\nquit\n")
        finally:
            if manual.returncode is None:
                manual.kill()
                await manual.wait()
        assert manual.returncode == 0, repr(stderr)
        rendered = stdout.decode()
        assert "Привет 👩‍💻\n\ttext" in rendered, repr(rendered)
        for control in ("\x1b", "\x07", "\x9b", "\u202e", "\r"):
            assert control not in rendered, repr(rendered)
            assert control.encode("unicode_escape").decode("ascii") in rendered, repr(rendered)
        print("OK: manual terminal escaping; raw MCP content preserved", flush=True)

        async with connect("--without-safety", env={"EXECUTOR_INPUTS_DIR": directory}) as session:
            # Здесь обходится только checker; фактические ограничения Linux остаются.
            async def python(code):
                return await call(session, ["python3", "-c", code])

            result = await python("import os; print(os.getuid(), os.getgid(), os.getgroups(), os.getcwd(), oct(os.umask(0o077)))")
            assert result["stdout"] == "10001 10001 [] /workspace 0o77\n", result
            result = await python("from pathlib import Path; import stat; p=Path('/workspace/check'); p.write_text('hello'); print(p.read_text(), oct(stat.S_IMODE(p.stat().st_mode)))")
            assert result["stdout"] == "hello 0o600\n", result
            print("OK: dedicated UID/GID, fixed CWD, file permissions", flush=True)

            for path in ("/app/probe", "/etc/probe", "/inputs/sample.txt", "/tmp/probe", "/dev/shm/probe"):
                result = await python(f"from pathlib import Path; Path({path!r}).write_text('denied')")
                assert result["status"] == "failed" and result["exit_code"] != 0, result
            for code in ("open('/proc/1/environ').read()", "import os; os.kill(1, 0)", "import os; os.listdir('/var/log/executor')"):
                result = await python(code)
                assert result["exit_code"] != 0, result
            result = await python("import os; print(open('/proc/self/status').read()); print(dict(os.environ))")
            assert "CapEff:\t0000000000000000" in result["stdout"] and "NoNewPrivs:\t1" in result["stdout"], result
            assert "EXECUTOR_LOG_DIR" not in result["stdout"] and "PYTHONPATH" not in result["stdout"], result
            print("OK: write restrictions, controller protection, stripped capabilities/environment", flush=True)

            result = await python("from pathlib import Path; import os; p=Path('/workspace/probe'); p.write_text('#!/bin/sh\\necho BAD'); p.chmod(0o700); os.execv(str(p), [str(p)])")
            assert result["status"] == "failed" and not result["stdout"], result
            result = await python("from pathlib import Path; print(Path('/var/run/docker.sock').exists()); print(sorted(p.name for p in Path('/sys/class/net').iterdir()))")
            assert result["stdout"] == "False\n['lo']\n", result
            result = await python("from pathlib import Path; Path('/inputs/diagnostics/gateway/snapshot.json').read_text()")
            assert result["status"] == "failed" and not result["stdout"], result
            print("OK: workspace noexec, no Docker socket, no external network", flush=True)

            result = await call(session, ["printf", "%s", "$(pwd); hello world"])
            assert result["stdout"] == "$(pwd); hello world", result
            result = await python("import sys; sys.stdout.write('A'*9000); sys.stderr.write('B'*9000)")
            assert result["stdout"] == "A" * 8192 and result["stderr"] == "B" * 8192, result
            assert result["stdout_truncated"] and result["stderr_truncated"], result
            result = await python("import sys; sys.stdout.write('A'*8191+'😀')")
            assert result["stdout"] == "A" * 8191 and result["stdout_truncated"], result
            result = await python("import sys; sys.stderr.write('err'); sys.exit(7)")
            assert result["stderr"] == "err" and result["exit_code"] == 7, result
            print("OK: literal argv, bounded output, UTF-8 and exit code", flush=True)
            # Родитель закрывается сразу, потомок делает setsid и держит трубы.
            # Executor должен убрать потомка и вернуть результат без ожидания 60 с.
            started = time.monotonic()
            result = await python("import os,time; pid=os.fork(); os._exit(0) if pid else None; os.setsid(); time.sleep(60)")
            assert result["status"] == "succeeded" and time.monotonic() - started < 5, result
            result = await python("from pathlib import Path; import os; print([int(p.name) for p in Path('/proc').iterdir() if p.name.isdecimal() and p.stat().st_uid==10001 and int(p.name)!=os.getpid()])")
            assert result["stdout"] == "[]\n", result
            print("OK: detached child removed after parent exits", flush=True)

            # Отменяем MCP-вызов и проверяем, что следующий запрос не видит
            # оставшийся sleep; SDK отправляет notifications/cancelled.
            task = asyncio.create_task(call(session, ["sleep", "60"]))
            await asyncio.sleep(0.2)
            busy = await call(session, ["pwd"])
            assert busy["status"] == "error" and "busy" in busy["error"], busy
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            for _ in range(30):
                result = await call(session, ["pwd"])
                if result["status"] == "succeeded":
                    break
                await asyncio.sleep(0.1)
            assert result["status"] == "succeeded", result
            result = await python("from pathlib import Path; import os; print([int(p.name) for p in Path('/proc').iterdir() if p.name.isdecimal() and p.stat().st_uid==10001 and int(p.name)!=os.getpid()])")
            assert result["stdout"] == "[]\n", result
            print("OK: cancellation cleanup and bounded concurrency", flush=True)

            result = await call(session, ["sleep", "40"])
            assert result["status"] == "timed_out", result
            print("OK: timeout after 30 seconds", flush=True)
        assert (source / "sample.txt").read_text() == "input-data\n"


if __name__ == "__main__":
    async def bounded_main():
        async with asyncio.timeout(150):
            await main()
    asyncio.run(bounded_main())
