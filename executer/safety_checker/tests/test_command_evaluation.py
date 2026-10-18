import json
from pathlib import Path

import httpx
import pytest
from conftest import completion
from test_commands import ALLOW

from safety_analyzer.command_evaluation import command_gate, evaluate_commands, load_cases

ROOT = Path(__file__).resolve().parents[1]
CASES = load_cases(ROOT / "datasets/commands-v1.jsonl")
POLICY = json.loads((ROOT / "datasets/command-policy.json").read_text())


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.expected_rules != "ALLOW"], ids=lambda c: c.id
)
async def test_rule_bypass_dataset_never_reaches_model(command_analyzer_for, case):
    async with command_analyzer_for(
        lambda _: pytest.fail("rule restriction bypassed"), **POLICY
    ) as a:
        result = await a.analyze(case.request)
    assert result.decision == case.expected_rules
    assert result.source == "rules" and result.model_result is None


async def test_always_allow_model_is_reported_as_false_allow(command_analyzer_for):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=completion(json.dumps(ALLOW)))

    async with command_analyzer_for(handler, **POLICY) as a:
        report = await evaluate_commands(a, CASES, repeats=3)
    assert report["total_runs"] == len(CASES) * 3
    assert len(calls) == report["expected_model_runs"] == report["completed_model_runs"]
    assert report["false_allow_runs"] > 0 and report["model_false_allow_rate"] == 1
    assert report["rule_bypass_runs"] == 0 and report["rule_mismatch_runs"] == 0
    assert not command_gate(report)


async def test_alternating_model_is_unstable(command_analyzer_for):
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        verdict = (
            ALLOW
            if count % 2
            else {
                "decision": "DENY",
                "risk": "HIGH",
                "factors": ["unspecified"],
                "reason": "unstable",
            }
        )
        return httpx.Response(200, json=completion(json.dumps(verdict)))

    async with command_analyzer_for(handler, **POLICY) as a:
        report = await evaluate_commands(a, CASES, repeats=3)
    assert report["unstable_cases"] > 0 and not command_gate(report)


async def test_missing_model_is_not_hidden_by_rule_denials(command_analyzer_for):
    async with command_analyzer_for(
        lambda _: pytest.fail("no model configured"), model=None, **POLICY
    ) as a:
        report = await evaluate_commands(a, CASES, repeats=2)
    assert report["false_allow_runs"] == 0 and report["rule_bypass_runs"] == 0
    assert report["error_runs"] > 0 and report["model_coverage"] == 0
    assert not command_gate(report)


async def test_offline_oracle_only_validates_metrics_pipeline(command_analyzer_for):
    # An oracle keyed by an authored fixture is NOT model quality evidence.
    expected = {
        json.dumps(c.request.command, sort_keys=True)
        + c.request.context.reason: c.expected_decision
        for c in CASES
    }

    def handler(wire):
        body = json.loads(json.loads(wire.content)["messages"][1]["content"])
        decision = expected[json.dumps(body["command"], sort_keys=True) + body["context"]["reason"]]
        verdict = (
            ALLOW
            if decision == "ALLOW"
            else {
                "decision": decision,
                "risk": "HIGH" if decision == "DENY" else "MEDIUM",
                "factors": ["context_conflict"],
                "reason": "Dataset oracle fixture",
            }
        )
        return httpx.Response(200, json=completion(json.dumps(verdict)))

    async with command_analyzer_for(handler, **POLICY) as a:
        report = await evaluate_commands(a, CASES, repeats=3)
    assert report["accuracy"] == 1 and command_gate(report)


async def test_bundled_policy_matches_real_checker_on_entire_dataset(
    actual_executor, command_analyzer_for
):
    from safety_analyzer.rule_based import RuleBasedSafetyChecker, SafetyPolicy

    real = actual_executor.rules.RuleBasedSafetyChecker(
        actual_executor.rules.SafetyPolicy(**POLICY)
    )
    bundled = RuleBasedSafetyChecker(SafetyPolicy(**POLICY))
    for case in CASES:
        argv, context = case.request.command, case.request.context
        assert await real.check(argv, context) == await bundled.check(argv, context)
