"""Human-readable evaluation artifact without incident text or credentials."""


def _rate(value):
    return "n/a" if value is None else f"{value:.2%}"


def markdown_report(report: dict) -> str:
    lines = [
        "# Safety Analyzer quality report",
        "",
        f"Evidence: **{report['evidence_type']}**; model/route: `{report['model']}`.",
        f"Quality gate: **{'PASS' if report['gate_passed'] else 'FAIL'}**.",
        "Control results test the evaluator, not model quality."
        if report["evidence_type"] == "control"
        else "Gateway results apply only to the recorded dataset, policy and backend revision.",
        "",
        f"Cases: {report['total_cases']}; repeats: {report['repeats']}; "
        f"runs: {report['total_runs']}; seed: {report['seed']}.",
        f"Dataset SHA-256: `{report['dataset_sha256']}`.",
        f"Backend revision (operator supplied): `{report['backend_revision'] or 'unknown'}`.",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key in (
        "accuracy",
        "macro_f1",
        "false_allow_rate",
        "false_allow_case_rate",
        "dangerous_false_allow_rate",
        "ambiguous_false_allow_rate",
        "model_false_allow_rate",
        "model_dangerous_false_allow_rate",
        "model_ambiguous_false_allow_rate",
        "false_block_rate",
        "error_rate",
        "model_coverage",
        "unstable_case_rate",
        "decision_unstable_case_rate",
        "pairwise_disagreement_rate",
    ):
        lines.append(f"| {key} | {_rate(report[key])} |")
    lines.extend(
        [
            "",
            "## Confusion matrix (errors are separate)",
            "",
            "| Expected | ALLOW | DENY | REQUIRE_APPROVAL | ERROR |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for decision, row in report["confusion_matrix"].items():
        lines.append("| " + decision + " | " + " | ".join(str(v) for v in row.values()) + " |")
    lines.extend(
        [
            "",
            "## Splits",
            "",
            "| Split | Cases | Model false allow | Error | Unstable |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for split, group in report["groups"]["split"].items():
        lines.append(
            f"| {split} | {group['total_cases']} | "
            f"{_rate(group['model_false_allow_rate'])} | {_rate(group['error_rate'])} | "
            f"{_rate(group['unstable_case_rate'])} |"
        )
    ci = report["model_dangerous_false_allow_case_ci95"]
    lines.extend(
        [
            "",
            f"Model dangerous-case false allow, 95% Wilson interval: "
            f"{_rate(ci['lower'])}–{_rate(ci['upper'])}, n={ci['n']} unique cases. "
            "Repeated calls are not independent samples; "
            "this interval describes this synthetic set.",
            "",
            "Failed gates: " + (", ".join(report["gate_failures"]) or "none") + ".",
            "",
            "False allow case IDs: " + (", ".join(report["false_allow_case_ids"]) or "none") + ".",
            "",
            "Unstable case IDs: " + (", ".join(report["unstable_case_ids"]) or "none") + ".",
            "",
            "See JSON for per-case repeats and label/language/family/tag breakdowns. "
            "Synthetic labels are provisional and require independent review. "
            "Zero observed errors do not demonstrate zero deployment risk.",
            "",
        ]
    )
    return "\n".join(lines)
