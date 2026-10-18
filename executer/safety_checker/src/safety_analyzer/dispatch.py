"""Trusted host-side MCP dispatch. The isolated Executor still enforces its own rules."""

import asyncio
from typing import Literal, Protocol

from pydantic import Field, model_validator

from safety_analyzer.command_contracts import CommandAnalysisRequest, CommandAnalysisResponse
from safety_analyzer.commands import CommandAnalyzer
from safety_analyzer.contracts import StrictModel


class ExecutorReply(Protocol):
    structured_content: dict | None
    is_error: bool


class ExecutorClient(Protocol):
    async def call_tool(self, name: str, arguments: dict) -> ExecutorReply: ...


class ExecutionOutput(StrictModel):
    stdout: str = Field(default="", max_length=8192)
    stderr: str = Field(default="", max_length=8192)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    exit_code: int | None = None
    status: Literal["succeeded", "failed", "denied", "approval_required", "timed_out", "error"]
    error: str | None = Field(default=None, max_length=2000)
    safety_checked: bool
    request_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_success(self) -> "ExecutionOutput":
        if self.status in {"succeeded", "failed", "timed_out"} and not self.safety_checked:
            raise ValueError("unchecked execution must not be accepted")
        if self.status == "succeeded" and self.exit_code != 0:
            raise ValueError("unchecked or contradictory success must not be accepted")
        return self


class DispatchResult(StrictModel):
    assessment: CommandAnalysisResponse
    dispatch_attempted: bool
    execution_status: Literal[
        "not_started",
        "unknown",
        "succeeded",
        "failed",
        "denied",
        "approval_required",
        "timed_out",
        "error",
    ]
    execution: ExecutionOutput | None = None
    error_code: (
        Literal[
            "assessment_mismatch",
            "executor_timeout",
            "executor_unavailable",
            "invalid_executor_output",
        ]
        | None
    ) = None


class CheckedExecutor:
    """Only a fresh joint ALLOW reaches MCP; no token, caller verdict, retry or shell."""

    def __init__(
        self, analyzer: CommandAnalyzer, client: ExecutorClient, timeout_seconds: float = 35
    ):
        if not 0 < timeout_seconds <= 120:
            raise ValueError("invalid Executor timeout")
        self.analyzer, self.client, self.timeout_seconds = analyzer, client, timeout_seconds

    async def execute(self, request: CommandAnalysisRequest) -> DispatchResult:
        snapshot = CommandAnalysisRequest.model_validate_json(request.model_dump_json())
        assessment = await self.analyzer.analyze(snapshot.snapshot())
        if assessment.decision != "ALLOW" or assessment.status != "ok":
            return DispatchResult(
                assessment=assessment, dispatch_attempted=False, execution_status="not_started"
            )
        if assessment.input_sha256 != snapshot.fingerprint():
            return DispatchResult(
                assessment=assessment,
                dispatch_attempted=False,
                execution_status="not_started",
                error_code="assessment_mismatch",
            )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                reply = await self.client.call_tool(
                    "execute_command",
                    {
                        "command": list(snapshot.command),
                        "context": {
                            "incident_id": snapshot.context.incident_id,
                            "reason": snapshot.context.reason,
                        },
                    },
                )
        except TimeoutError:
            error = "executor_timeout"
        except Exception:
            error = "executor_unavailable"
        else:
            try:
                output = ExecutionOutput.model_validate(reply.structured_content)
                if reply.is_error != (output.status != "succeeded"):
                    raise ValueError("contradictory MCP status")
            except (ValueError, TypeError, AttributeError):
                error = "invalid_executor_output"
            else:
                return DispatchResult(
                    assessment=assessment,
                    dispatch_attempted=True,
                    execution_status=output.status,
                    execution=output,
                )
        # A lost reply cannot establish that the command did not start. Never retry it here.
        return DispatchResult(
            assessment=assessment,
            dispatch_attempted=True,
            execution_status="unknown",
            error_code=error,
        )
