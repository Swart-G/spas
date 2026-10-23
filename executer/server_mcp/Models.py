
"""Контракт команды, контекста и результата Executor."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


Argument = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=4096,
        pattern=r"^[^\x00]+$",
    ),
]


def command_size(argv: list[str]) -> list[str]:
    if sum(map(len, argv)) + len(argv) - 1 > 4096:
        raise ValueError("Command arguments exceed 4096 characters including separators")
    return argv


Command = Annotated[
    list[Argument],
    Field(min_length=1, max_length=4096, strict=True,
          description='Executable and literal arguments, e.g. ["uname", "-a"]. No shell strings.'),
    AfterValidator(command_size),
]

ContextText = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=2000,
    ),
]


class ExecutionContext(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    incident_id: ContextText
    reason: ContextText


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stdout: str = ""
    stderr: str = ""

    stdout_truncated: bool = Field(
        default=False,
        description="True if stdout was shortened to the output limit; only its beginning is returned.",
    )
    stderr_truncated: bool = Field(
        default=False,
        description="True if stderr was shortened to the output limit; only its beginning is returned.",
    )

    exit_code: int | None = None

    status: Literal[
        "succeeded",
        "failed",
        "denied",
        "approval_required",
        "timed_out",
        "error",
    ]

    error: str | None = None
    safety_checked: bool = False
    request_id: str | None = Field(
        default=None,
        description="Executor request identifier for finding related server log events.",
    )


ServiceName = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$"),
]


class DiagnosticResult(BaseModel):
    """Одинаковый ответ всех инструментов диагностики."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "error", "denied"]
    kind: Literal["logs", "state", "availability"]
    service: ServiceName
    output: str = ""
    truncated: bool = False
    observed_at: float | None = None
    error: str | None = None
    request_id: str | None = None
