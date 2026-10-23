"""Чтение ограниченных снимков сервисов, подготовленных доверенным оператором."""

import errno
import json
import os
from pathlib import Path
import stat
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import TypeAdapter

from .Execution_policy import COMMAND_GID, COMMAND_UID, subprocess_options
from .Models import DiagnosticResult, ServiceName


DIAGNOSTIC_ROOT = Path("/inputs/diagnostics")
MAX_SNAPSHOT_BYTES = 32768
MAX_OUTPUT_BYTES = 8192
MAX_SNAPSHOT_AGE_SECONDS = 120


class DiagnosticSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    observed_at: float = Field(ge=0)
    state: Literal["running", "stopped", "unknown"]
    availability: Literal["available", "unavailable", "unknown"]
    logs: str = Field(max_length=MAX_SNAPSHOT_BYTES)
    logs_truncated: bool
    state_collected: bool
    logs_collected: bool

    @field_validator("logs")
    @classmethod
    def valid_utf8(cls, value: str) -> str:
        value.encode("utf-8")
        return value


def reject_extended_acl(fd: int, *, directory: bool) -> None:
    """Named-user ACLs could grant the command UID access despite safe mode bits."""
    names = ("system.posix_acl_access", "system.posix_acl_default") if directory else ("system.posix_acl_access",)
    for name in names:
        try:
            os.getxattr(fd, name)
        except OSError as exc:
            if exc.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
                continue
            raise
        raise ValueError("Extended ACL on diagnostic snapshot path is not allowed")


def read_snapshot(service: str) -> DiagnosticSnapshot:
    """Открыть только regular snapshot без symlink на каждом уровне пути."""
    service = TypeAdapter(ServiceName).validate_python(service)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    root_fd = os.open(DIAGNOSTIC_ROOT, directory_flags)
    try:
        root_info = os.fstat(root_fd)
        if (
            root_info.st_uid == COMMAND_UID or root_info.st_gid == COMMAND_GID
            or stat.S_IMODE(root_info.st_mode) & 0o027
        ):
            raise ValueError("Insecure diagnostic directory")
        reject_extended_acl(root_fd, directory=True)
        service_fd = os.open(service, directory_flags, dir_fd=root_fd)
        try:
            service_info = os.fstat(service_fd)
            if (
                service_info.st_uid != root_info.st_uid
                or service_info.st_gid != root_info.st_gid
                or stat.S_IMODE(service_info.st_mode) & 0o027
            ):
                raise ValueError("Insecure diagnostic service directory")
            reject_extended_acl(service_fd, directory=True)
            snapshot_fd = os.open("snapshot.json", file_flags, dir_fd=service_fd)
            try:
                info = os.fstat(snapshot_fd)
                if (
                    not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT_BYTES
                    or info.st_uid != root_info.st_uid or info.st_gid != root_info.st_gid
                    or stat.S_IMODE(info.st_mode) & 0o027
                ):
                    raise ValueError("Invalid diagnostic snapshot")
                reject_extended_acl(snapshot_fd, directory=False)
                with os.fdopen(os.dup(snapshot_fd), "rb") as stream:
                    raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
                if len(raw) > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Diagnostic snapshot exceeds size limit")
            finally:
                os.close(snapshot_fd)
        finally:
            os.close(service_fd)
    finally:
        os.close(root_fd)
    snapshot = DiagnosticSnapshot.model_validate(json.loads(raw))
    if snapshot.service != service:
        raise ValueError("Diagnostic snapshot service mismatch")
    return snapshot


def bounded_tail(value: str, lines: int) -> tuple[str, bool]:
    selected = value.splitlines(keepends=True)
    by_lines = "".join(selected[-lines:])
    truncated = len(selected) > lines
    # Ограничиваем именно JSON-представление: управляющие символы могут
    # сериализоваться в шесть байтов при одном байте исходного журнала.
    if len(json.dumps(by_lines, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT_BYTES:
        low, high = 0, len(by_lines)
        while low < high:
            middle = (low + high) // 2
            size = len(json.dumps(by_lines[middle:], ensure_ascii=False).encode("utf-8"))
            if size > MAX_OUTPUT_BYTES:
                low = middle + 1
            else:
                high = middle
        by_lines = by_lines[low:]
        truncated = True
    return by_lines, truncated


def diagnose_service(
    kind: Literal["logs", "state", "availability"],
    service: str,
    *,
    lines: int = 100,
) -> DiagnosticResult:
    try:
        # Та же проверка защищённого Docker-профиля, что и перед запуском команд.
        subprocess_options()
    except (OSError, RuntimeError, ValueError, KeyError):
        return DiagnosticResult(
            status="error", kind=kind, service=service,
            error="Protected Docker execution is required; launch with Run_docker.sh",
        )
    try:
        snapshot = read_snapshot(service)
    except (OSError, ValueError, json.JSONDecodeError):
        return DiagnosticResult(
            status="error", kind=kind, service=service,
            error="No valid operator snapshot for this service",
        )
    age = time.time() - snapshot.observed_at
    if not 0 <= age <= MAX_SNAPSHOT_AGE_SECONDS:
        return DiagnosticResult(
            status="error", kind=kind, service=service, observed_at=snapshot.observed_at,
            error="Diagnostic snapshot is stale or has a future timestamp",
        )
    if (kind == "logs" and not snapshot.logs_collected) or (
        kind != "logs" and not snapshot.state_collected
    ):
        return DiagnosticResult(
            status="error", kind=kind, service=service, observed_at=snapshot.observed_at,
            error="Diagnostic data could not be collected from this service",
        )
    if kind == "logs":
        output, truncated = bounded_tail(snapshot.logs, lines)
        truncated |= snapshot.logs_truncated
    elif kind == "state":
        output, truncated = snapshot.state, False
    else:
        output, truncated = snapshot.availability, False
    return DiagnosticResult(
        status="succeeded", kind=kind, service=service, output=output,
        truncated=truncated, observed_at=snapshot.observed_at,
    )
