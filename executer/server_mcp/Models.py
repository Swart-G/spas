"""Контракт команды, контекста и результата Executor."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, StringConstraints

Command = Annotated[str, StringConstraints(min_length=1, max_length=4096, pattern=r'^[^\x00]+$')]
ContextText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class ExecutionContext(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    incident_id: ContextText
    reason: ContextText


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stdout: str = ''
    stderr: str = ''
    exit_code: int | None = None
    status: Literal['succeeded', 'failed', 'denied', 'approval_required', 'timed_out', 'error']
    error: str | None = None
    safety_checked: bool = False
