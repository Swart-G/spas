"""выполнение Linux shell-команд."""

import asyncio
import os
import signal
from .Models import ExecutionResult


async def run_shell(command: str, timeout_seconds: float = 30) -> ExecutionResult:
    """Запускает Bash; допуск через Safety Checker определяет MCP-сервер."""
    try:
        process = await asyncio.create_subprocess_exec(
            '/bin/bash', '-c', command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return ExecutionResult(status='error', error='Could not start shell process')

    output = asyncio.create_task(process.communicate())
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), timeout_seconds)
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = await output
        if isinstance(exc, asyncio.CancelledError):
            raise
        timed_out = True
    return ExecutionResult(
        stdout=stdout.decode('utf-8', errors='replace'),
        stderr=stderr.decode('utf-8', errors='replace'),
        exit_code=process.returncode,
        status='timed_out' if timed_out else 'succeeded' if process.returncode == 0 else 'failed',
        error='Command exceeded execution timeout' if timed_out else None,
    )
