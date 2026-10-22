import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import completion
from test_commands import ALLOW

from safety_analyzer.command_evaluation import (
    CommandCase,
    command_gate,
    evaluate_commands,
    gate_failures,
    load_cases,
    summarize_commands,
)
from safety_analyzer.quality_metrics import wilson

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "datasets/quality-v1.jsonl"
CASES = load_cases(DATASET)
POLICY = json.loads((ROOT / "datasets/command-policy.json").read_text())


def test_versioned_dataset_balance_and_family_split():
    manifest = json.loads(DATASET.with_suffix(".manifest.json").read_text())
    assert hashlib.sha256(DATASET.read_bytes()).hexdigest() == manifest["sha256"]
    assert len(CASES) == manifest["cases"] == 90
    assert (
        dict(Counter(c.label for c in CASES))
        == manifest["labels"]
        == {
            "safe": 30,
            "dangerous": 30,
            "ambiguous": 30,
        }
    )
    assert dict(Counter(c.split for c in CASES)) == manifest["splits"]
    assert sum(c.expected_rules == "ALLOW" for c in CASES) == 70
    assert {c.language for c in CASES} == {"en", "ru", "mixed"}
    assert all(c.rationale and c.family != "legacy" for c in CASES)
    for family in {c.family for c in CASES}:
        variants = [c for c in CASES if c.family == family]
        assert len({c.split for c in variants}) == 1
        assert {c.label for c in variants} == {"safe", "dangerous", "ambiguous"}
    for case in CASES:
        assert case.label not in case.request.context.incident_id
        assert case.id != case.request.context.incident_id


async def test_quality_dataset_labels_match_real_executor(actual_executor):
    real = actual_executor.rules.RuleBasedSafetyChecker(
        actual_executor.rules.SafetyPolicy(**POLICY)
    )
    for case in CASES:
        result = await real.check(case.request.command, case.request.context)
        assert result["decision"] == case.expected_rules, case.id


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.expected_rules != "ALLOW"], ids=lambda c: c.id
)
async def test_quality_rule_restrictions_never_call_native_model(command_analyzer_for, case):
    async with command_analyzer_for(
        lambda _: pytest.fail("rule bypass"), command_api="decisions", **POLICY
    ) as a:
        result = await a.analyze(case.request)
    assert result.decision == case.expected_rules and result.source == "rules"


def small_cohort():
    return [
        next(c for c in CASES if c.label == label and c.expected_rules == rule)
        for label, rule in [
            ("safe", "ALLOW"),
            ("dangerous", "ALLOW"),
            ("ambiguous", "ALLOW"),
            ("dangerous", "DENY"),
            ("ambiguous", "REQUIRE_APPROVAL"),
        ]
    ]


async def test_metrics_have_independently_calculated_denominators(command_analyzer_for):
    cases = small_cohort()
    seen = Counter()
    by_task = {c.request.context.task: c.id for c in cases}

    def handler(wire):
        body = json.loads(json.loads(wire.content)["messages"][1]["content"])
        ident = by_task[body["context"]["task"]]
        seen[ident] += 1
        if ident == cases[1].id and seen[ident] == 1:
            verdict = ALLOW
        else:
            approval = ident == cases[2].id
            verdict = {
                "decision": "REQUIRE_APPROVAL" if approval else "DENY",
                "risk": "MEDIUM" if approval else "HIGH",
                "factors": ["context_conflict"],
                "reason": "fixture",
            }
        return httpx.Response(200, json=completion(json.dumps(verdict)))

    async with command_analyzer_for(handler, **POLICY) as a:
        report = await evaluate_commands(a, cases, repeats=2)
    assert report["total_runs"] == 10
    assert report["safe_runs"] == 2 and report["unsafe_runs"] == 8
    assert report["false_allow_rate"] == 1 / 8
    assert report["dangerous_false_allow_rate"] == 1 / 4
    assert report["ambiguous_false_allow_rate"] == 0
    assert report["model_false_allow_rate"] == 1 / 4
    assert report["model_dangerous_false_allow_rate"] == 1 / 2
    assert report["model_ambiguous_false_allow_rate"] == 0
    assert report["false_block_rate"] == 1
    assert report["false_allow_case_rate"] == 1 / 4
    assert report["accuracy"] == 7 / 10
    assert report["unstable_case_rate"] == 1 / 5
    assert report["risky_flip_cases"] == 1
    assert report["pairwise_disagreement_rate"] == 1 / 5
    assert report["cases"][1]["majority_decision"] == "TIE"
    assert report["model_dangerous_false_allow_case_ci95"]["n"] == 1
    assert report["groups"]["label"]["safe"]["model_false_allow_rate"] is None
    assert report["classification"]["DENY"]["precision"] == 3 / 5
    assert report["classification"]["DENY"]["recall"] == 3 / 4
    # General tolerance cannot silently relax the separate model dangerous-case gate.
    assert "model_dangerous_false_allow_rate" in gate_failures(report, max_false_allow_rate=1)


async def test_fail_closed_errors_are_not_correct_classifications(command_analyzer_for):
    async with command_analyzer_for(lambda _: httpx.Response(503), **POLICY) as a:
        report = await evaluate_commands(a, small_cohort(), repeats=2)
    assert report["accuracy"] == 4 / 10
    assert report["error_runs"] == 6 and report["model_coverage"] == 0
    assert report["confusion_matrix"]["DENY"] == {
        "ALLOW": 0,
        "DENY": 2,
        "REQUIRE_APPROVAL": 0,
        "ERROR": 2,
    }
    assert report["error_codes"] == {"gateway_unavailable": 6}
    assert not command_gate(report)


async def test_full_pass_shuffle_and_result_binding(command_analyzer_for):
    cases = small_cohort()
    order = []
    by_task = {c.request.context.task: c.id for c in cases}
    async with command_analyzer_for(
        lambda _: httpx.Response(200, json=completion(json.dumps(ALLOW))), **POLICY
    ) as a:

        async def analyze(request):
            order.append(by_task[request.context.task])
            return await a.analyze(request)

        await evaluate_commands(SimpleNamespace(analyze=analyze), cases, repeats=3, seed=42)
        outputs = {c.id: [await a.analyze(c.request) for _ in range(2)] for c in cases}
    for offset in range(0, 15, 5):
        assert set(order[offset : offset + 5]) == {c.id for c in cases}
    assert order[:5] != order[5:10]
    outputs[cases[0].id][0] = outputs[cases[1].id][0]
    with pytest.raises(ValueError, match="different command"):
        summarize_commands(cases, outputs, 2)


def test_intervals_use_unique_cases_and_missing_samples_are_null():
    assert wilson(0, 0)["upper"] is None
    assert wilson(0, 20)["upper"] == pytest.approx(0.161125158)
    assert wilson(0, 20)["n"] == 20
    with pytest.raises(ValueError):
        wilson(2, 1)


@pytest.mark.parametrize(
    "change,error",
    [
        ("duplicate", "duplicate command"),
        ("split", "cannot cross"),
        ("label", "contradicts"),
        ("rules", "cannot override"),
    ],
)
def test_loader_rejects_inconsistent_or_leaking_datasets(tmp_path, change, error):
    rows = [c.model_dump(mode="json") for c in CASES]
    if change == "duplicate":
        rows[1]["request"] = rows[0]["request"]
    elif change == "split":
        rows[0]["split"] = "holdout"
    elif change == "label":
        rows[0]["label"] = "dangerous"
    else:
        rows[0]["expected_rules"] = "DENY"
    path = tmp_path / "bad.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match=error):
        load_cases(path)


@pytest.mark.parametrize("api", ["chat_completions", "decisions"])
@pytest.mark.parametrize(
    "profile,expected",
    [
        ("always-allow", "model_dangerous_false_allow_rate"),
        ("always-deny", "false_block_rate"),
        ("alternating", "unstable_case_rate"),
        ("unavailable", "model_coverage"),
    ],
)
def test_control_cli_detects_known_failures_and_writes_redacted_artifacts(
    tmp_path, api, profile, expected
):
    output = tmp_path / "evaluation.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "safety_analyzer.command_evaluation",
            "--control",
            profile,
            "--api",
            api,
            "--policy-file",
            "datasets/command-policy.json",
            "--output",
            str(output),
            "--repeats",
            "2",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, completed.stderr
    report = json.loads(output.read_text())
    assert expected in report["gate_failures"]
    assert report["evidence_type"] == "control" and not report["gate_passed"]
    assert report["total_runs"] == 180 and report["expected_model_runs"] == 140
    assert report["rule_bypass_runs"] == report["rule_mismatch_runs"] == 0
    assert report["groups"]["split"]["holdout"]["total_cases"] == 30
    assert "Control results test the evaluator" in output.with_suffix(".md").read_text()
    for forbidden in ("local-control-only", "private", '"request"', '"reason"'):
        assert forbidden not in output.read_text()
    assert all("context" not in case for case in report["cases"])
    assert CASES[0].request.context.task not in output.read_text()


@pytest.mark.parametrize(
    "option,value",
    [
        ("--repeats", "1"),
        ("--max-model-false-allow-rate", "nan"),
        ("--max-false-allow-rate", "1.01"),
    ],
)
def test_quality_cli_rejects_invalid_limits(option, value):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "safety_analyzer.command_evaluation",
            "--control",
            "always-allow",
            option,
            value,
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2


def test_gate_rejects_unknown_or_nonfinite_limits():
    for options in ({"max_error_rate": float("nan")}, {"unknown": 1}):
        with pytest.raises(ValueError):
            command_gate({}, **options)


def test_case_validation_does_not_mutate_request_labels():
    case = CommandCase.model_validate_json(CASES[0].model_dump_json())
    assert set(case.request.model_dump()) == {"command", "context", "request_id"}


async def test_holdout_errors_cannot_be_diluted_by_development(command_analyzer_for):
    by_task = {c.request.context.task: c.expected_decision for c in CASES}
    # Select two unique contextual tasks, rather than shared rule-regression tasks.
    wrong = {
        c.request.context.task
        for c in CASES
        if c.split == "holdout" and c.label == "safe" and not c.family.startswith("rules-")
    }
    wrong = set(sorted(wrong)[:2])

    def handler(wire):
        task = json.loads(json.loads(wire.content)["messages"][1]["content"])["context"]["task"]
        decision = "DENY" if task in wrong else by_task[task]
        verdict = (
            ALLOW
            if decision == "ALLOW"
            else {
                "decision": decision,
                "risk": "HIGH" if decision == "DENY" else "MEDIUM",
                "factors": ["unspecified"],
                "reason": "Quality test oracle; no model evidence",
            }
        )
        return httpx.Response(200, json=completion(json.dumps(verdict)))

    async with command_analyzer_for(handler, **POLICY) as a:
        report = await evaluate_commands(a, CASES, repeats=2)
    assert report["false_block_rate"] < 0.1
    assert report["accuracy"] > 0.9
    assert "split:holdout:false_block_rate" in gate_failures(report)
    assert "false_block_rate" not in gate_failures(report)


@pytest.mark.parametrize("vary_risk", [True, False])
async def test_stability_tracks_risk_but_ignores_reason_wording(command_analyzer_for, vary_risk):
    seen = Counter()

    def handler(wire):
        snapshot = json.loads(wire.content)["messages"][1]["content"]
        seen[snapshot] += 1
        verdict = {
            "decision": "DENY",
            "risk": "CRITICAL" if vary_risk and seen[snapshot] == 2 else "HIGH",
            "factors": ["unspecified"],
            "reason": f"Reason {seen[snapshot]}",
        }
        return httpx.Response(200, json=completion(json.dumps(verdict)))

    async with command_analyzer_for(handler, **POLICY) as a:
        report = await evaluate_commands(a, small_cohort(), repeats=2)
    assert report["decision_unstable_cases"] == 0
    assert report["unstable_cases"] == (3 if vary_risk else 0)


async def test_approval_for_dangerous_actions_fails_accuracy_gate(command_analyzer_for):
    by_task = {c.request.context.task: c.label for c in CASES}

    def handler(wire):
        task = json.loads(json.loads(wire.content)["messages"][1]["content"])["context"]["task"]
        verdict = (
            ALLOW
            if by_task[task] == "safe"
            else {
                "decision": "REQUIRE_APPROVAL",
                "risk": "MEDIUM",
                "factors": ["unspecified"],
                "reason": "Overuses human approval",
            }
        )
        return httpx.Response(200, json=completion(json.dumps(verdict)))

    async with command_analyzer_for(handler, **POLICY) as a:
        report = await evaluate_commands(a, CASES, repeats=2)
    assert report["false_allow_rate"] == report["false_block_rate"] == 0
    assert "accuracy" in gate_failures(report)
