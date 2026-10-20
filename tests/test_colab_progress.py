import ast
import asyncio
import io
import json
import subprocess
import sys
import time

import pytest
from rich.console import Console

from llm.gateway.colab.progress import StartupDisplay, valid_progress
from llm.gateway.colab.scripts import bootstrap_script
from llm.gateway.core.errors import GatewayError


def display(clock):
    output = io.StringIO()
    console = Console(
        file=output, force_terminal=True, no_color=False, color_system="truecolor", width=100
    )
    return StartupDisplay(console, "gpu", "team/model", clock=lambda: clock[0]), output


def test_timers_advance_during_remote_silence_and_freeze_completed_steps():
    clock = [10.0]
    progress, output = display(clock)
    with progress:
        progress.update({"step": "allocate", "status": "running"})
        clock[0] = 12.5
        progress.console.print(progress.render())
        assert "2.5 с" in output.getvalue()
        progress.update({"step": "allocate", "status": "done"})
        progress.update(
            {
                "step": "model_download",
                "status": "running",
                "unit": "bytes",
                "completed": 1024,
                "total": 4096,
            }
        )
        clock[0] = 13.5
        progress.update(
            {
                "step": "model_download",
                "status": "running",
                "unit": "bytes",
                "completed": 2048,
                "total": 4096,
            }
        )
        progress.console.print(progress.render())
        text = output.getvalue()
        assert "50%" in text and "2.0 КиБ / 4.0 КиБ" in text
        assert "1.0 КиБ/с" in text
        assert progress.rows["allocate"].ended == 12.5
        progress.update({"step": "model_download", "status": "done"})
        clock[0] = 20
    assert progress.ended == 20
    assert progress.rows["model_download"].ended == 13.5
    assert "Готово" in output.getvalue()
    # Label foreground is bright white (ANSI 97), including waiting rows.
    assert "\x1b[97m" in output.getvalue()


@pytest.mark.parametrize(
    "error,status",
    [
        (GatewayError("failed"), "failed"),
        (asyncio.CancelledError(), "cancelled"),
        (KeyboardInterrupt(), "cancelled"),
    ],
)
def test_error_or_cancellation_retains_completed_work_and_failed_step(error, status):
    clock = [0.0]
    progress, output = display(clock)
    with pytest.raises(type(error)):
        with progress:
            progress.update({"step": "server", "status": "done"})
            progress.update({"step": "route", "status": "running"})
            clock[0] = 4
            raise error
    assert progress.rows["server"].status == "done"
    assert progress.rows["route"].status == status
    assert progress.ended == 4


def test_progress_only_accepts_fixed_labels_and_finite_counters():
    event = valid_progress(
        {
            "step": "model_download",
            "status": "running",
            "completed": 12,
            "total": float("inf"),
            "unit": "bytes",
            "message": "private-secret",
            "filename": "private-secret",
        }
    )
    assert event == {
        "type": "progress",
        "step": "model_download",
        "status": "running",
        "completed": 12,
        "unit": "bytes",
    }
    assert valid_progress({"step": "private-secret", "status": "running"}) is None


def test_installer_forwards_live_counters_and_keeps_subprocess_logs_private():
    code = bootstrap_script({})
    function = next(
        node
        for node in ast.walk(ast.parse(code))
        if isinstance(node, ast.FunctionDef) and node.name == "run_child"
    )
    events = []
    namespace = {
        "subprocess": subprocess,
        "json": json,
        "emit": lambda event: events.append((time.monotonic(), event)),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "run_child", "exec"), namespace)
    started = time.monotonic()
    child = """import json,time
event={'step':'model_download','status':'running','completed':5,'total':10,'unit':'bytes'}
print('SPAS_PROGRESS '+json.dumps(event),flush=True)
time.sleep(.3)
print('private subprocess log')
print('{"snapshot":"cached","commit":"test"}')
"""
    result = namespace["run_child"]([sys.executable, "-c", child], step="model_download")
    assert result.returncode == 0
    counters = [(when, event) for when, event in events if "completed" in event]
    assert counters and counters[0][0] - started < 0.3
    assert events[-1][1]["status"] == "done"
    assert "SPAS_PROGRESS" not in result.stdout
    assert "private" not in str(events)


def test_retry_countdown_keeps_elapsed_time_and_resets_attempt_speed():
    clock = [0.0]
    progress, output = display(clock)
    with progress:
        progress.update(
            {
                "step": "model_download",
                "status": "running",
                "attempt": 1,
                "max_attempts": 3,
                "completed": 100,
                "total": 200,
                "unit": "bytes",
            }
        )
        clock[0] = 180
        progress.update(
            {
                "step": "model_download",
                "status": "retrying",
                "attempt": 1,
                "max_attempts": 3,
                "reason": "stalled",
                "retry_delay": 5,
            }
        )
        clock[0] = 182
        progress.console.print(progress.render())
        assert "Скачивание не продвигается" in output.getvalue()
        assert "Повтор через 3 с" in output.getvalue()
        assert progress.rows["model_download"].ended is None
        progress.update(
            {
                "step": "model_download",
                "status": "running",
                "attempt": 2,
                "max_attempts": 3,
                "completed": 0,
                "total": 0,
                "unit": "files",
            }
        )
        assert progress.rows["model_download"].rate == 0
        assert progress.rows["model_download"].started == 0
        assert progress.rows["model_download"].reason == ""
