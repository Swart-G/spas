"""Opt-in real model checks; never substitute a stub for model quality evidence."""

import os
from pathlib import Path

import httpx
import pytest

from safety_analyzer.command_evaluation import build_parser, run
from safety_analyzer.config import Settings
from safety_analyzer.evaluation import evaluate, load_dataset, release_gate
from safety_analyzer.gateway import LLMGateway
from safety_analyzer.service import Analyzer

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("SAFETY_RUN_LIVE") != "1", reason="real model not opted in"),
]
DATASET = Path(__file__).resolve().parents[1] / "datasets" / "safety-v1.jsonl"
COMMAND_DATASET = DATASET.with_name("quality-v1.jsonl")


async def test_real_model_false_allow_and_stability():
    settings = Settings()
    if settings.command_api == "decisions":
        pytest.skip("Native Choice models use the command evaluation, not content Chat Completions")
    assert settings.configured, "configure the real Gateway before running live tests"
    assert settings.model != "stub", "a stub is not a model quality evaluation"
    cases = load_dataset(DATASET)
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        report = await evaluate(Analyzer(LLMGateway(settings, client)), cases, repeats=3)
    assert report["false_allow_runs"] == 0, f"false allow: {report['false_allow_runs']}"
    assert release_gate(report), {
        key: report[key] for key in ["false_block_rate", "error_rate", "unstable_case_rate"]
    }


async def test_real_command_model_false_allow_and_stability():
    settings = Settings()
    assert settings.configured and settings.model != "stub"
    # Deployment scopes must deliberately match the dataset policy. Persist both artifacts.
    args = build_parser().parse_args(
        [
            "--dataset",
            str(COMMAND_DATASET),
            "--repeats",
            "5",
            "--seed",
            "42",
            "--output",
            str(DATASET.parents[1] / "reports/live-command-evaluation.json"),
        ]
    )
    args.backend_revision = os.getenv("SAFETY_BACKEND_REVISION")
    assert await run(args) == 0, "See reports/live-command-evaluation.json and .md for failed gates"
