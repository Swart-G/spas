"""Opt-in real model checks; never substitute a stub for model quality evidence."""

import os
from pathlib import Path

import httpx
import pytest

from safety_analyzer.command_evaluation import command_gate, evaluate_commands, load_cases
from safety_analyzer.commands import CommandAnalyzer, configured_rules
from safety_analyzer.config import Settings
from safety_analyzer.evaluation import evaluate, load_dataset, release_gate
from safety_analyzer.gateway import LLMGateway
from safety_analyzer.service import Analyzer

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("SAFETY_RUN_LIVE") != "1", reason="real model not opted in"),
]
DATASET = Path(__file__).resolve().parents[1] / "datasets" / "safety-v1.jsonl"
COMMAND_DATASET = DATASET.with_name("commands-v1.jsonl")


async def test_real_model_false_allow_and_stability():
    settings = Settings()
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
    # Live deployment must deliberately configure scopes matching the test policy.
    cases = load_cases(COMMAND_DATASET)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        report = await evaluate_commands(
            CommandAnalyzer(LLMGateway(settings, client), configured_rules(settings)), cases, 3
        )
    assert report["rule_bypass_runs"] == 0
    assert report["model_false_allow_runs"] == 0
    assert command_gate(report)
