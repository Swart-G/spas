"""Local transport stub. Fixed unsafe output is not a safety classification model."""

import hmac
import json
from typing import Literal

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import Field

from safety_analyzer.contracts import StrictModel

STUB_KEY = "local-stub-only"  # Public development fixture, never a production credential.


class StubMessage(StrictModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: str


class StubResponseFormat(StrictModel):
    type: Literal["text", "json_object"]


class StubRequest(StrictModel):
    model: Literal["stub"]
    messages: list[StubMessage] = Field(min_length=1)
    temperature: float
    max_tokens: int
    n: Literal[1]
    stream: Literal[False]
    response_format: StubResponseFormat


def create_stub_app(verdict: Literal["safe", "unsafe"] = "unsafe") -> FastAPI:
    if verdict not in {"safe", "unsafe"}:
        raise ValueError("invalid stub verdict")
    app = FastAPI(title="Development Gateway stub: NOT a safety model")

    @app.get("/health")
    async def health():
        return {"status": "ok", "stub": True, "fixed_verdict": verdict}

    @app.post("/v1/chat/completions")
    async def completion(body: StubRequest, authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + STUB_KEY).encode()):
            return JSONResponse({"error": {"code": "invalid_gateway_key"}}, status_code=401)
        content = verdict
        if body.response_format.type == "json_object":
            candidate = json.loads(body.messages[-1].content)
            if isinstance(candidate, dict) and "command" in candidate:
                content = json.dumps(
                    {
                        "decision": "ALLOW" if verdict == "safe" else "DENY",
                        "risk": "LOW" if verdict == "safe" else "HIGH",
                        "factors": [] if verdict == "safe" else ["unspecified"],
                        "reason": "Fixed development stub verdict; not model quality evidence",
                    }
                )
            else:
                content = json.dumps(
                    {
                        "verdict": verdict,
                        "categories": [] if verdict == "safe" else ["unspecified"],
                    }
                )
        return {
            "model": "stub",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }

    return app
