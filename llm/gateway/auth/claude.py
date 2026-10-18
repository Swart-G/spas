"""Keep Claude-native credentials encrypted; only the official runtime consumes them."""

import asyncio
import base64
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.util import find_spec
from pathlib import Path

from ..core.errors import GatewayError
from ..core.models import AccountState
from ..process import stop_process
from .manager import AuthManager, account_key

PROFILE_FILES = (".credentials.json", ".claude.json")
STRIP_PREFIXES = (
    "ANTHROPIC_",
    "OPENAI_",
    "SPAS_",
    "CLAUDE_CODE_USE_",
    "CLAUDE_CODE_OAUTH_",
)
STRIP_NAMES = {
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_SIMPLE",
    "CLAUDE_CODE_SAFE_MODE",
    "CLAUDECODE",
    "CLAUDE_CODE_RETRY_WATCHDOG",
    "CLAUDE_CODE_API_KEY",
    "CLAUDE_CODE_API_KEY_HELPER",
}


def subscription_environment(profile: Path) -> dict[str, str]:
    result = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(STRIP_PREFIXES) and key not in STRIP_NAMES
    }
    result.update(
        {
            "CLAUDE_CONFIG_DIR": str(profile),
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "CLAUDE_AGENT_SDK_CLIENT_APP": "spas-llm-gateway",
        }
    )
    return result


def sdk_environment(profile: Path) -> dict[str, str]:
    # The SDK overlays rather than replaces os.environ; explicitly clear removed values.
    result = subscription_environment(profile)
    for key in os.environ:
        if key not in result:
            result[key] = ""
    return result


def claude_binary() -> str:
    if binary := shutil.which("claude"):
        return binary
    if spec := find_spec("claude_agent_sdk"):
        if spec.origin:
            bundled = Path(spec.origin).parent / "_bundled/claude"
            if bundled.is_file():
                return str(bundled)
    raise GatewayError("Claude Code не установлен. Установите CLI или extra claude.")


class ClaudeProfiles:
    def __init__(self, auth: AuthManager) -> None:
        self.auth = auth

    @asynccontextmanager
    async def materialize(
        self, account: str, *, create: bool = False, allow_reauth: bool = False
    ) -> AsyncIterator[tuple[Path, dict]]:
        key = account_key(account)
        async with self.auth.store.lock(key):
            record = self.auth.store.get(key)
            if record is None and create:
                record = {
                    "kind": "account",
                    "account": account,
                    "provider": "claude_subscription",
                    "state": AccountState.REAUTH_REQUIRED,
                    "profile": {},
                }
            if record is None or record.get("provider") != "claude_subscription":
                raise GatewayError("Профиль Claude не подключён.", code="reauth_required")
            if not create and not allow_reauth and record["state"] == AccountState.REAUTH_REQUIRED:
                raise GatewayError("Нужен повторный вход в Claude.", code="reauth_required")
            memory_dir = Path("/dev/shm")
            parent = (
                str(memory_dir) if memory_dir.is_dir() and os.access(memory_dir, os.W_OK) else None
            )
            with tempfile.TemporaryDirectory(prefix="spas-claude-", dir=parent) as temporary:
                profile = Path(temporary)
                profile.chmod(0o700)
                for name, encoded in record.get("profile", {}).items():
                    if name not in PROFILE_FILES:
                        raise GatewayError("Профиль Claude содержит недопустимый файл.")
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except ValueError as error:
                        raise GatewayError("Профиль Claude повреждён.") from error
                    path = profile / name
                    path.write_bytes(data)
                    path.chmod(0o600)
                try:
                    yield profile, record
                finally:
                    # Preserve native refreshes after success, errors and cancellation.
                    files = {}
                    for name in PROFILE_FILES:
                        path = profile / name
                        if path.is_file() and not path.is_symlink():
                            files[name] = base64.b64encode(path.read_bytes()).decode()
                    record["profile"] = files
                    self.auth.store.put(key, record)

    @staticmethod
    async def native_status(profile: Path) -> dict:
        process = await asyncio.create_subprocess_exec(
            claude_binary(),
            "--safe-mode",
            "auth",
            "status",
            "--json",
            env=subscription_environment(profile),
            cwd=profile,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 20)
            data = json.loads(stdout)
            if not isinstance(data, dict):
                raise ValueError("Not a status object")
            return data
        except (ValueError, TimeoutError) as error:
            raise GatewayError("Не удалось проверить профиль Claude.") from error
        finally:
            await stop_process(process)

    @staticmethod
    def validate_subscription(status: dict) -> None:
        if status.get("loggedIn") is not True or status.get("authMethod") != "claude.ai":
            raise GatewayError(
                "Для подписочного адаптера нужен вход claude auth login через claude.ai.",
                code="reauth_required",
            )

    async def login(self, account: str) -> dict:
        async with self.materialize(account, create=True, allow_reauth=True) as (profile, record):
            process = await asyncio.create_subprocess_exec(
                claude_binary(),
                "auth",
                "login",
                env=subscription_environment(profile),
                cwd=profile,
                start_new_session=True,
            )
            try:
                returncode = await process.wait()
            finally:
                await stop_process(process)
            if returncode:
                raise GatewayError("Вход в Claude не завершён.", code="reauth_required")
            status = await self.native_status(profile)
            self.validate_subscription(status)
            record.update(
                state=AccountState.CONNECTED,
                email=status.get("email"),
                subscription_type=status.get("subscriptionType"),
            )
            record.pop("retry_at", None)
            return self.auth.public_account(record)

    async def import_profile(self, account: str, source: Path) -> dict:
        source = source.expanduser().resolve()
        async with self.materialize(account, create=True, allow_reauth=True) as (profile, record):
            found = False
            for name in PROFILE_FILES:
                candidate = source / name
                if name == ".claude.json" and not candidate.exists() and source.name == ".claude":
                    candidate = source.parent / name
                if candidate.is_file() and not candidate.is_symlink():
                    shutil.copyfile(candidate, profile / name)
                    (profile / name).chmod(0o600)
                    found = True
            if not found:
                raise GatewayError("В каталоге нет файлов авторизации Claude Code.")
            status = await self.native_status(profile)
            self.validate_subscription(status)
            record.update(
                state=AccountState.CONNECTED,
                email=status.get("email"),
                subscription_type=status.get("subscriptionType"),
            )
            record.pop("retry_at", None)
            return self.auth.public_account(record)

    async def health(self, account: str) -> dict:
        try:
            async with self.materialize(account, allow_reauth=True) as (profile, record):
                status = await self.native_status(profile)
                self.validate_subscription(status)
                return {
                    **self.auth.public_account(record),
                    "auth_method": "claude.ai",
                    "inference_verified": record.get("last_success_at") is not None,
                }
        except GatewayError as error:
            return {"account": account, "state": AccountState.REAUTH_REQUIRED, "code": error.code}
