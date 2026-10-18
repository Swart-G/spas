"""Структурированные журналы Executor: файл с ротацией и stderr для MCP."""

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
from pathlib import Path
import sys
import threading


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUEST_CONTEXT: ContextVar[dict | None] = ContextVar("executor_request", default=None)
_SETUP_LOCK = threading.Lock()
_SDK_LOG_STATE = None
MAX_LOG_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
# Только технические поля. command, reason, stdout/stderr и ключи сюда не входят.
FIELDS = (
    "request_id", "incident_ref", "pid", "mode", "transport", "status",
    "exit_code", "duration_ms", "command_length", "safety_checked", "decision",
    "timeout_seconds", "stdout_bytes", "stderr_bytes", "stdout_truncated",
    "stderr_truncated", "tool",
    "sdk_component",
)


class JsonFormatter(logging.Formatter):
    """Одна строка JSON; стек без сообщений исключений, исходного кода и locals."""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "server_pid": record.process,
            "event": record.getMessage(),
        }
        context = REQUEST_CONTEXT.get() or {}
        for key in FIELDS:
            value = getattr(record, key, context.get(key))
            if value is not None:
                data[key] = value[:160] if isinstance(value, str) else value
        if record.exc_info and record.exc_info[1] is not None:
            # Не используем formatException: сообщение ошибки может содержать
            # секрет, исходную команду или отвергнутые Pydantic входные данные.
            errors = []
            pending = [record.exc_info[1]]
            seen = set()
            while pending and len(seen) < 12 and len(errors) < 3:
                error = pending.pop(0)
                if id(error) in seen:
                    continue
                seen.add(id(error))
                if isinstance(error, BaseExceptionGroup):
                    # В AnyIO/MCP настоящая причина часто вложена в несколько
                    # ExceptionGroup. Не теряем её за стеками обёрток.
                    pending[0:0] = error.exceptions[:3]
                    continue
                frames = []
                tb = error.__traceback__
                while tb is not None:
                    frames.append({
                        "file": Path(tb.tb_frame.f_code.co_filename).name[:120],
                        "line": tb.tb_lineno,
                        "function": tb.tb_frame.f_code.co_name[:80],
                    })
                    tb = tb.tb_next
                errors.append({"type": type(error).__name__, "frames": frames[-12:]})
                cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
                if cause is not None:
                    pending.append(cause)
            data["exceptions"] = errors
        return json.dumps(data, ensure_ascii=False, allow_nan=False)


def _report_file_failure() -> None:
    """Не вызываем logging повторно из сломанного обработчика."""
    try:
        sys.stderr.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": "ERROR", "event": "log_file_write_failed", "server_pid": os.getpid(),
        }) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Закрытые права файла, включая файл, созданный после ротации."""

    failure_reported = False

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def handleError(self, record):
        # Стандартный handleError печатает record и args: не допускаем утечку
        # и не обрываем выполнение команды из-за заполненного диска.
        if not self.failure_reported:
            self.failure_reported = True
            _report_file_failure()


class StderrHandler(logging.StreamHandler):
    def handleError(self, record):
        # Недоступный stderr не должен ломать запись в файл или Executor.
        pass


class SDKLogHandler(logging.Handler):
    """SDK может включать ввод в msg/args: переносим только безопасные поля."""

    def emit(self, record):
        logging.getLogger("executor.sdk").log(
            record.levelno, "mcp_sdk_event", exc_info=record.exc_info,
            extra={"sdk_component": record.name[:160]},
        )


def setup_logging() -> logging.Logger:
    """Настроить один раз; каталог можно задать через EXECUTOR_LOG_DIR."""
    global _SDK_LOG_STATE
    with _SETUP_LOCK:
        logger = logging.getLogger("executor")
        if getattr(logger, "_executor_configured", False):
            return logger
        logger.setLevel(logging.INFO)
        logger.propagate = False
        formatter = JsonFormatter()
        console = StderrHandler(sys.stderr)
        console.setFormatter(formatter)
        logger.addHandler(console)
        try:
            log_dir = Path(os.environ.get("EXECUTOR_LOG_DIR") or PROJECT_ROOT / "logs" / "executor").expanduser()
            if Path("/etc/spas-executor").is_file():
                # PID одинаковых серверов в разных контейнерах может совпасть.
                log_dir = log_dir / socket.gethostname()
            log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Файл отдельный для каждого процесса: несколько MCP-клиентов
            # не конкурируют за ротацию одного и того же server.log.
            file_handler = PrivateRotatingFileHandler(
                log_dir / f"server-{os.getpid()}.log",
                maxBytes=MAX_LOG_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except (OSError, ValueError):
            logger.error("log_file_unavailable", exc_info=True)
        # MCPServer настраивает root-logger отдельно. Не даём mcp.* обойти
        # очистку и записать отвергнутые аргументы через этот второй канал.
        # Root и чужие библиотеки не меняем; при shutdown возвращаем настройки.
        sdk = logging.getLogger("mcp")
        _SDK_LOG_STATE = (sdk.handlers[:], sdk.level, sdk.propagate)
        sdk.handlers = [SDKLogHandler()]
        sdk.setLevel(logging.INFO)
        sdk.propagate = False
        logger._executor_configured = True
        return logger


def shutdown_logging() -> None:
    """Закрыть только обработчики Executor; не затрагивать логи приложения-хоста."""
    global _SDK_LOG_STATE
    with _SETUP_LOCK:
        if _SDK_LOG_STATE is not None:
            sdk = logging.getLogger("mcp")
            for handler in sdk.handlers:
                handler.close()
            sdk.handlers, sdk.level, sdk.propagate = _SDK_LOG_STATE
            _SDK_LOG_STATE = None
        logger = logging.getLogger("executor")
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger._executor_configured = False
