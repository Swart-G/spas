"""SPAS System One Choice adapter, including native Nimble via Gateway conversion."""

import math
from typing import get_args

from safety_analyzer.command_contracts import CommandModelVerdict, Decision, Factor, Risk
from safety_analyzer.gateway import GatewayError

OPTIONS = {
    "decision": dict(
        zip(
            get_args(Decision),
            (
                "Low risk, within task and constraints, no risk factors",
                "Harmful intent, conflict, or high/critical risk",
                "Material uncertainty or medium risk; explicit approval required",
            ),
            strict=True,
        )
    ),
    "risk": dict(
        zip(
            get_args(Risk),
            (
                "No material risk",
                "Uncertainty or limited impact",
                "Significant harm",
                "Severe harm",
            ),
            strict=True,
        )
    ),
    "factor": {"none": "No risk factor", **{f: f.replace("_", " ") for f in get_args(Factor)}},
}


def decision_payload(model, request, rules, policy):
    state = request.model_dump(mode="json", exclude={"request_id"})
    state["rules"] = rules.model_dump()
    return {
        "model": model,
        "state": state,
        "questions": {
            name: {
                "type": "choice",
                "instructions": policy
                + "\nSelect "
                + name
                + (" (the primary risk factor)." if name == "factor" else "."),
                "criteria": options,
            }
            for name, options in OPTIONS.items()
        },
    }


def _bounded(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid probability")
    return value


def parse_decision(data, payload):
    try:
        if data["model"] != payload["model"] or set(data["answers"]) != set(OPTIONS):
            raise ValueError("unexpected model or questions")
        selected = {}
        for name, options in OPTIONS.items():
            answer = data["answers"][name]
            if answer["type"] != "choice" or answer["choice"] not in options:
                raise ValueError("invalid choice or refusal")
            scores = answer["probabilities"]
            if set(scores) != set(options):
                raise ValueError("incomplete probability distribution")
            total = sum(_bounded(p) for p in scores.values())
            if not math.isclose(total, 1, abs_tol=min(0.05, len(options) * 0.005 + 1e-6)):
                raise ValueError("invalid probability sum")
            _bounded(answer["confidence"])
            selected[name] = answer["choice"]
        factor = selected["factor"]
        return CommandModelVerdict(
            decision=selected["decision"],
            risk=selected["risk"],
            factors=[] if factor == "none" else [factor],
            reason="Gateway Choice classification: " + selected["decision"] + "; " + factor,
        )
    except (KeyError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise GatewayError("invalid_model_output") from exc
