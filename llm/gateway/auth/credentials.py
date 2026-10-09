"""Encrypted records and process locks for local, single-host deployments."""

import asyncio
import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..core.errors import GatewayError


class CredentialStore:
    def __init__(self, directory: Path, key: str | None = None) -> None:
        self.directory = directory.expanduser()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.records = self.directory / "credentials"
        self.records.mkdir(exist_ok=True, mode=0o700)
        self.records.chmod(0o700)
        key = key or os.environ.get("SPAS_CREDENTIAL_KEY")
        if key is None:
            key_path = self.directory / "master.key"
            with self._sync_lock("master-key"):
                if not key_path.exists():
                    if any(self.records.glob("*.enc")):
                        raise GatewayError("Ключ хранилища утрачен; восстановите master.key.")
                    self._atomic_write(key_path, Fernet.generate_key())
                key_path.chmod(0o600)
                key = key_path.read_text().strip()
        try:
            self.cipher = Fernet(key.encode())
        except (ValueError, TypeError) as error:
            raise GatewayError("Неверный ключ шифрования хранилища.") from error

    def _path(self, name: str, suffix: str = ".enc") -> Path:
        return self.records / (hashlib.sha256(name.encode()).hexdigest() + suffix)

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def get(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return json.loads(self.cipher.decrypt(path.read_bytes()))
        except (InvalidToken, ValueError) as error:
            raise GatewayError("Не удалось расшифровать хранилище аккаунтов.") from error

    def put(self, name: str, record: dict[str, Any]) -> None:
        data = json.dumps(record, ensure_ascii=False).encode()
        self._atomic_write(self._path(name), self.cipher.encrypt(data))

    def all_accounts(self) -> list[dict[str, Any]]:
        records = []
        for path in self.records.glob("*.enc"):
            try:
                value = json.loads(self.cipher.decrypt(path.read_bytes()))
            except (InvalidToken, ValueError) as error:
                raise GatewayError("Не удалось расшифровать хранилище аккаунтов.") from error
            if value.get("kind") == "account":
                records.append(value)
        return sorted(records, key=lambda record: record["account"])

    @contextmanager
    def _sync_lock(self, name: str) -> Iterator[None]:
        fd = os.open(self._path(name, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @asynccontextmanager
    async def lock(self, name: str) -> AsyncIterator[None]:
        # Nonblocking flock keeps the event loop responsive, including across processes.
        fd = os.open(self._path(name, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def host_id(self) -> str:
        with self._sync_lock("host"):
            record = self.get("host")
            if record is None:
                record = {"host_id": f"urn:uuid:{uuid.uuid4()}"}
                self.put("host", record)
            return record["host_id"]
