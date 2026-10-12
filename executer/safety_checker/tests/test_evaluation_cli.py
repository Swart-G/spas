import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx

from safety_analyzer.evaluation import evaluate, load_dataset

ROOT = Path(__file__).resolve().parents[1]


def test_cli_reports_invalid_configuration_without_secrets(monkeypatch):
    monkeypatch.setenv("SAFETY_GATEWAY_BASE_URL", "https://private@gateway.test")
    completed = subprocess.run(
        [sys.executable, "-m", "safety_analyzer.evaluation"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "private" not in completed.stderr


def test_cli_rejects_single_repeat():
    completed = subprocess.run(
        [sys.executable, "-m", "safety_analyzer.evaluation", "--repeats", "1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "at least 2" in completed.stderr


def test_cli_rejects_nan_threshold():
    completed = subprocess.run(
        [sys.executable, "-m", "safety_analyzer.evaluation", "--max-error-rate", "nan"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2


async def test_dataset_repetitions_execute_all_cases(analyzer_for):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "unsafe"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    cases = await asyncio.to_thread(load_dataset, ROOT / "datasets" / "safety-v1.jsonl")
    async with analyzer_for(handler, output_mode="label") as analyzer:
        report = await evaluate(analyzer, cases, repeats=3)
    assert len(calls) == report["total_runs"] == 180
    assert report["false_allow_rate"] == 0 and report["false_block_rate"] == 1
    assert report["error_runs"] == 0 and report["unstable_cases"] == 0
    assert "text" not in report["cases"][0]
    assert json.dumps(report)
