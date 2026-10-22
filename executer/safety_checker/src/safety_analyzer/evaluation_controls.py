"""Deliberately flawed providers to verify evaluation sensitivity, never model evidence."""

import json
from collections import Counter

import httpx

from safety_analyzer.decision_adapter import OPTIONS

PROFILES = ("always-allow", "always-deny", "alternating", "unavailable")


def _json_response(data: dict) -> httpx.Response:
    # Real HTTP responses are unread streams; Response(json=...) is eagerly consumed.
    return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode("utf-8")))


def control_transport(profile: str) -> httpx.MockTransport:
    if profile not in PROFILES:
        raise ValueError("unknown control profile")
    counts = Counter()

    def handler(wire):
        payload = json.loads(wire.content)
        if profile == "unavailable":
            return httpx.Response(503)
        decisions_api = "questions" in payload
        snapshot = (
            json.dumps(payload["state"], sort_keys=True)
            if decisions_api
            else payload["messages"][1]["content"]
        )
        counts[snapshot] += 1
        allow = profile == "always-allow" or (
            profile == "alternating" and counts[snapshot] % 2 == 1
        )
        decision, risk, factor = (
            ("ALLOW", "LOW", "none") if allow else ("DENY", "HIGH", "unspecified")
        )
        if decisions_api:
            selected = {"decision": decision, "risk": risk, "factor": factor}
            return _json_response(
                {
                    "model": payload["model"],
                    "answers": {
                        name: {
                            "type": "choice",
                            "choice": selected[name],
                            "confidence": 1.0,
                            "probabilities": {key: float(key == selected[name]) for key in options},
                        }
                        for name, options in OPTIONS.items()
                    },
                },
            )
        verdict = {
            "decision": decision,
            "risk": risk,
            "factors": [] if allow else [factor],
            "reason": "Evaluation control",
        }
        return _json_response(
            {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(verdict)},
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)
