"""Доверенный хостовый сборщик Docker-снимков для изолированного Executor MCP.

Запускается оператором на хосте; Docker socket никогда не передаётся агенту.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import stat
import subprocess
import time

from .Diagnostics import MAX_OUTPUT_BYTES, MAX_SNAPSHOT_BYTES, bounded_tail, reject_extended_acl
from .Execution_policy import COMMAND_GID, COMMAND_UID


NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
CONTAINER_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
COMMAND_TIMEOUT_SECONDS = 5
MAX_COLLECT_WORKERS = 8
POLL_INTERVAL_SECONDS = 30
# Docker's full .State includes Health.Log entries with unbounded probe output.
# Ask only for the fields used by the diagnostic tools.
INSPECT_FORMAT = (
    '{"Running":{{json .State.Running}},'
    '"Paused":{{json .State.Paused}},'
    '"Restarting":{{json .State.Restarting}},"Health":'
    '{{if .State.Health}}{"Status":{{json .State.Health.Status}}}{{else}}null{{end}}}'
)
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
ROOT_UID = os.stat("/").st_uid


def _validate_directory(fd: int, *, private: bool = False, input_root: bool = False) -> None:
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Diagnostic path is not a directory")
    if private:
        # The MCP controller reads as a supplemental host group. Command
        # subprocesses drop supplemental groups; owner UID must also differ.
        if (
            info.st_uid != os.geteuid() or info.st_gid != os.getegid()
            or info.st_uid == COMMAND_UID or info.st_gid == COMMAND_GID
            or mode & 0o027 or mode & 0o050 != 0o050
        ):
            raise ValueError("Diagnostic directory must be owned by the collector and mode 0750")
    elif input_root:
        if (
            info.st_uid != os.geteuid() or info.st_gid != os.getegid()
            or info.st_uid == COMMAND_UID or info.st_gid == COMMAND_GID
            or mode & 0o022 or mode & 0o050 != 0o050
        ):
            raise ValueError("Diagnostic input root must be owned by the collector and not writable by others")
    elif (mode & 0o022) and not (info.st_uid == ROOT_UID and mode & stat.S_ISVTX):
        raise ValueError("Diagnostic path has an unsafe writable ancestor")
    elif info.st_uid not in (ROOT_UID, os.geteuid()):
        raise ValueError("Diagnostic path has an untrusted owner")
    if private or input_root:
        reject_extended_acl(fd, directory=True)


@contextmanager
def open_output(directory: Path, *, create: bool = False):
    """Traverse from / with no-follow dirfds; keep the output inode pinned."""
    if not directory.is_absolute() or ".." in directory.parts or directory == Path("/"):
        raise ValueError("Diagnostic output must be an absolute directory path")
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for index, component in enumerate(directory.parts[1:], 1):
            last = index == len(directory.parts) - 1
            created = False
            if last and create:
                try:
                    os.mkdir(component, mode=0o750, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
            next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if created:
                # mkdir's mode is masked by umask 077 in the documented setup.
                # Only adjust the inode we just created, never an existing path.
                os.fchmod(fd, 0o750)
            _validate_directory(fd, private=last, input_root=index == len(directory.parts) - 2)
        yield fd
    finally:
        os.close(fd)


def _service_fd(output_fd: int, service: str) -> int:
    if not NAME_PATTERN.fullmatch(service):
        raise ValueError("Invalid service name")
    created = False
    try:
        os.mkdir(service, mode=0o750, dir_fd=output_fd)
        created = True
    except FileExistsError:
        pass
    fd = os.open(service, DIRECTORY_FLAGS, dir_fd=output_fd)
    try:
        if created:
            os.fchmod(fd, 0o750)
        _validate_directory(fd, private=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def load_services(path: Path) -> dict[str, str]:
    if not path.is_absolute():
        path = Path.cwd() / path
    if ".." in path.parts or path == Path("/"):
        raise ValueError("Invalid diagnostic collector config path")
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            _validate_directory(fd)
        config_fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=fd,
        )
        with os.fdopen(config_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ValueError("Diagnostic collector config must be a private operator-owned file")
            reject_extended_acl(stream.fileno(), directory=False)
            raw = stream.read(16385)
    finally:
        os.close(fd)
    if len(raw) > 16384:
        raise ValueError("Diagnostic collector config is too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"services"} or not isinstance(data["services"], dict):
        raise ValueError("Expected a services mapping")
    services = data["services"]
    if not 1 <= len(services) <= 32:
        raise ValueError("Configure 1-32 services")
    for service, container in services.items():
        if not isinstance(service, str) or not NAME_PATTERN.fullmatch(service):
            raise ValueError("Invalid service name")
        if not isinstance(container, str) or not CONTAINER_PATTERN.fullmatch(container):
            raise ValueError("Invalid container name")
    return services


def docker_output(argv: tuple[str, ...], limit: int, *, tail: bool = False) -> tuple[bytes, bool, bool]:
    """Ограничить память и время даже при огромной строке журнала."""
    process = subprocess.Popen(
        # Preserve Docker log order before keeping the newest bytes. Draining
        # two pipes independently could move a fresh stderr line ahead of old
        # stdout data and then discard it from the bounded tail.
        ("docker", *argv), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if tail else subprocess.PIPE,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    output = bytearray()
    truncated = False
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    try:
        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > limit:
                    truncated = True
                    if tail:
                        del output[:-limit]
                    else:
                        raise ValueError("Docker inspect response is too large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return bytes(output), truncated, process.wait(timeout=remaining) == 0
    except (TimeoutError, subprocess.TimeoutExpired):
        return b"", False, False
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        selector.close()
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def collect_service(service: str, container: str) -> dict[str, object]:
    observed_at = time.time()
    state, availability = "unknown", "unknown"
    state_collected = False
    try:
        raw, _, success = docker_output(
            ("inspect", "--type", "container", "--format", INSPECT_FORMAT, container), 4096,
        )
        if success:
            docker_state = json.loads(raw)
            if not isinstance(docker_state, dict) or any(
                not isinstance(docker_state.get(flag), bool)
                for flag in ("Running", "Paused", "Restarting")
            ):
                raise ValueError("Invalid Docker state")
            state_collected = True
            running = docker_state.get("Running") is True
            paused = docker_state["Paused"]
            restarting = docker_state["Restarting"]
            if restarting:
                state = "restarting"
            elif paused:
                state = "paused"
            else:
                state = "running" if running else "stopped"
            health = docker_state.get("Health") or {}
            if not isinstance(health, dict):
                health = {}
            if not running or paused or restarting or health.get("Status") == "unhealthy":
                availability = "unavailable"
            elif health.get("Status") == "healthy":
                availability = "available"
            # Без healthcheck факт запуска не доказывает доступность сервиса.
    except (OSError, ValueError, TypeError, KeyError):
        pass
    logs, logs_truncated, logs_collected = b"", False, False
    try:
        raw, logs_truncated, success = docker_output(
            ("logs", "--tail", "200", "--timestamps", container), MAX_OUTPUT_BYTES,
            tail=True,
        )
        if success:
            logs = raw
            logs_collected = True
    except OSError:
        pass
    decoded = logs.decode("utf-8", errors="replace")
    encoded = decoded.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        decoded = encoded[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="ignore")
        logs_truncated = True
    decoded, serialized_truncated = bounded_tail(decoded, 200)
    logs_truncated |= serialized_truncated
    return {
        "service": service, "observed_at": observed_at, "state": state,
        "availability": availability, "logs": decoded, "logs_truncated": logs_truncated,
        "state_collected": state_collected, "logs_collected": logs_collected,
    }


def _write_snapshot_fd(output_fd: int, service: str, snapshot: dict[str, object]) -> None:
    payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Diagnostic snapshot exceeds size limit")
    service_fd = _service_fd(output_fd, service)
    temporary = ".snapshot-" + secrets.token_hex(16)
    try:
        file_fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=service_fd,
        )
        try:
            with os.fdopen(file_fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o640)
                reject_extended_acl(stream.fileno(), directory=False)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "snapshot.json", src_dir_fd=service_fd, dst_dir_fd=service_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=service_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(service_fd)


def write_snapshot(directory: Path, service: str, snapshot: dict[str, object]) -> None:
    with open_output(directory) as output_fd:
        _write_snapshot_fd(output_fd, service, snapshot)


def clear_old_snapshots(output_fd: int) -> None:
    """Revoke all previous snapshots before loading a new service allowlist."""
    for name in os.listdir(output_fd):
        if not NAME_PATTERN.fullmatch(name):
            continue
        try:
            service_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=output_fd)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                continue
            raise
        try:
            _validate_directory(service_fd, private=True)
            try:
                os.unlink("snapshot.json", dir_fd=service_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(service_fd)


def collect_cycle(output_fd: int, services: dict[str, str]) -> None:
    """Collect bounded Docker results in parallel; publish from one writer."""
    with ThreadPoolExecutor(max_workers=min(MAX_COLLECT_WORKERS, len(services))) as pool:
        pending = {
            pool.submit(collect_service, service, container): service
            for service, container in services.items()
        }
        for future in as_completed(pending):
            _write_snapshot_fd(output_fd, pending[future], future.result())


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish bounded Docker diagnostics for Executor MCP")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    services = load_services(args.config)
    with open_output(args.output, create=True) as output_fd:
        lock_fd = os.open(
            ".collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=output_fd,
        )
        try:
            lock_info = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid()
                or lock_info.st_gid != os.getegid() or lock_info.st_nlink != 1
                or stat.S_IMODE(lock_info.st_mode) & 0o077
            ):
                raise ValueError("Invalid diagnostic collector lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            clear_old_snapshots(output_fd)
            while True:
                collect_cycle(output_fd, services)
                if not args.watch:
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            os.close(lock_fd)


if __name__ == "__main__":
    main()
