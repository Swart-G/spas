import json
from pathlib import Path
from uuid import uuid4

import pytest

from safety_analyzer.contracts import AnalyzeResponse, Category
from safety_analyzer.evaluation import DatasetCase, load_dataset, release_gate, summarize

DATASET = Path(__file__).resolve().parents[1] / "datasets" / "safety-v1.jsonl"


def result(decision, error=False):
    return AnalyzeResponse(
        request_id=uuid4(),
        decision=decision,
        verdict="unknown" if error else "safe" if decision == "allow" else "unsafe",
        categories=[] if error or decision == "allow" else [Category.UNSPECIFIED],
        status="error" if error else "ok",
        error_code="gateway_timeout" if error else None,
        model="test",
        latency_ms=0,
    )


def cases():
    return [
        DatasetCase.model_validate_json(
            json.dumps(
                {
                    "id": suite,
                    "suite": suite,
                    "language": "en",
                    "tags": ["test"],
                    "request": {"text": "sample"},
                    "expected_decision": decision,
                }
            )
        )
        for suite, decision in [("benign", "allow"), ("false_allow", "block")]
    ]


def test_false_allow_has_unsafe_denominator_and_any_repeat_case_count():
    report = summarize(
        cases(),
        {
            "benign": [result("allow"), result("allow"), result("allow")],
            "false_allow": [result("block"), result("allow"), result("block")],
        },
        3,
    )
    assert report["false_allow_rate"] == pytest.approx(1 / 3)
    assert report["false_allow_case_rate"] == 1
    assert report["unstable_cases"] == 1
    assert not release_gate(report)


def test_all_blocking_stub_cannot_pass_release_gate():
    report = summarize(cases(), {c.id: [result("block")] * 3 for c in cases()}, 3)
    assert report["false_allow_rate"] == 0
    assert report["false_block_rate"] == 1 and not release_gate(report)


def test_all_error_blocks_are_unavailable_and_not_correct_classifications():
    report = summarize(cases(), {c.id: [result("block", error=True)] * 3 for c in cases()}, 3)
    assert report["false_allow_rate"] == 0
    assert report["accuracy"] == 0 and report["error_rate"] == 1
    assert not release_gate(report)


def test_perfect_repeated_predictions_pass_gate():
    report = summarize(cases(), {c.id: [result(c.expected_decision)] * 3 for c in cases()}, 3)
    assert release_gate(report) and report["accuracy"] == 1


@pytest.mark.parametrize("outputs,repeats", [({}, 3), ({"benign": []}, 3), ({}, 1)])
def test_incomplete_runs_do_not_validate_model(outputs, repeats):
    with pytest.raises(ValueError):
        summarize(cases(), outputs, repeats)


def test_dataset_coverage_and_separate_false_allow_suite():
    dataset = load_dataset(DATASET)
    assert len(dataset) >= 50
    assert {c.language for c in dataset} == {"en", "ru", "mixed"}
    assert {c.request.target for c in dataset} == {"request", "response", "command"}
    assert sum(c.suite == "false_allow" for c in dataset) >= 25
    tags = {tag for c in dataset for tag in c.tags}
    assert {"prompt_injection", "obfuscation", "context", "refusal", "destructive_action"} <= tags


def test_duplicate_dataset_ids_are_rejected(tmp_path):
    sample = DATASET.read_text(encoding="utf-8").splitlines()[0]
    path = tmp_path / "duplicate.jsonl"
    path.write_text(sample + "\n" + sample, encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_dataset(path)
