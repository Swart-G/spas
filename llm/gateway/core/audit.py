import fcntl
import json
import logging
import os
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path


@contextmanager
def log_lock(path: str | Path) -> Iterator[None]:
    fd = os.open(os.fspath(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class SharedRotatingFileHandler(RotatingFileHandler):
    """Serialize writes and rotation using a stable lock file on the shared volume."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)

    def emit(self, record) -> None:
        with log_lock(self.baseFilename):
            if self.stream is not None:
                try:
                    current = os.stat(self.baseFilename)
                except FileNotFoundError:
                    current = None
                opened = os.fstat(self.stream.fileno())
                if current is None or (current.st_dev, current.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    self.stream.close()
                    self.stream = None
            # Reopen before checking rollover, so its size check uses the current file.
            if self.stream is None:
                self.stream = self._open()
            super().emit(record)


class RequestLog:
    """Only operational metadata. Never prompts, responses, headers or upstream bodies."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "requests.jsonl"
        with log_lock(self.path):
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            os.close(fd)
            self.path.chmod(0o600)
        self.handler = SharedRotatingFileHandler(
            self.path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8", delay=True
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.Logger("spas.gateway.requests")
        self.logger.addHandler(self.handler)

    def record(self, **metadata) -> None:
        self.logger.info(json.dumps(metadata, ensure_ascii=False))

    def recent(self, limit: int = 100) -> list[dict]:
        with log_lock(self.path), self.path.open(encoding="utf-8") as source:
            lines = deque(source, maxlen=limit)
        results = []
        for line in lines:
            try:
                results.append(json.loads(line))
            except ValueError:
                continue
        return results

    def close(self) -> None:
        self.handler.close()
