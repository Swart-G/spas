"""Reproducible evaluator self-checks. Expected bad-provider gates must all fail."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from safety_analyzer.command_evaluation import build_parser, run
from safety_analyzer.evaluation_controls import PROFILES


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/quality-controls"))
    args = parser.parse_args()
    summaries = []
    expected = dict(
        zip(
            PROFILES,
            (
                "model_dangerous_false_allow_rate",
                "false_block_rate",
                "unstable_case_rate",
                "model_coverage",
            ),
            strict=True,
        )
    )
    for api in ("chat_completions", "decisions"):
        for profile in PROFILES:
            output = args.output_dir / f"{api}-{profile}.json"
            code = await run(
                build_parser().parse_args(
                    [
                        "--control",
                        profile,
                        "--api",
                        api,
                        "--repeats",
                        "5",
                        "--seed",
                        "42",
                        "--policy-file",
                        "datasets/command-policy.json",
                        "--output",
                        str(output),
                    ]
                )
            )
            report = json.loads(output.read_text())
            if (
                code != 1
                or expected[profile] not in report["gate_failures"]
                or report["rule_bypass_runs"]
                or report["rule_mismatch_runs"]
                or report["evidence_type"] != "control"
            ):
                raise SystemExit("Evaluation failed to detect a deliberately broken provider")
            summaries.append(
                {
                    key: report[key]
                    for key in (
                        "api",
                        "control_profile",
                        "total_runs",
                        "expected_model_runs",
                        "completed_model_runs",
                        "false_allow_runs",
                        "dangerous_false_allow_rate",
                        "ambiguous_false_allow_rate",
                        "model_false_allow_rate",
                        "false_block_rate",
                        "error_rate",
                        "accuracy",
                        "unstable_case_rate",
                        "risky_flip_cases",
                        "rule_bypass_runs",
                        "rule_mismatch_runs",
                        "gate_passed",
                        "gate_failures",
                    )
                }
            )
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence_type": "control",
        "evaluator_self_check_passed": True,
        "model_quality_validated": False,
        "dataset_sha256": report["dataset_sha256"],
        "prompt_sha256": report["prompt_sha256"],
        "implementation_sha256": report["implementation_sha256"],
        "seed": 42,
        "repeats": 5,
        "cases": 90,
        "profiles": summaries,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("Evaluator self-checks passed. These controls do not validate a real model.")


if __name__ == "__main__":
    asyncio.run(main())
