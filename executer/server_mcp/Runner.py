"""Прямой запуск argv без shell, с ограничением вывода и времени."""

import asyncio
import codecs
import logging
import os
import signal
import time

from .Models import ExecutionResult
from .Execution_policy import COMMAND_UID, subprocess_options


OUTPUT_LIMIT_BYTES = 8192
READ_CHUNK_BYTES = 4096
CLEANUP_TIMEOUT_SECONDS = 3
# Один UID на контейнер: последовательный запуск позволяет убрать и setsid-потомков.
_EXECUTION_LOCK = asyncio.Lock()

logger = logging.getLogger("executor.runner")


async def read_limited(
    stream: asyncio.StreamReader,
) -> tuple[str, bool]:
    """Читает до EOF; ограничивает и сохранённые байты, и итоговый UTF-8 текст."""

    saved = bytearray()
    truncated = False

    while True:
        chunk = await stream.read(READ_CHUNK_BYTES)

        if not chunk:
            break

        remaining = OUTPUT_LIMIT_BYTES - len(saved)
        saved.extend(chunk[:remaining])

        if len(chunk) > remaining:
            truncated = True

    # При усечении не превращаем обрезанный многобайтовый символ в лишний U+FFFD.
    # При настоящем EOF неполный/некорректный UTF-8 заменяется на U+FFFD (�).
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(saved, final=not truncated)

    # Замена одного некорректного байта на � занимает три байта UTF-8.
    # Повторно ограничиваем результат, чтобы он тоже помещался в лимит.
    encoded = text.encode("utf-8")
    if len(encoded) > OUTPUT_LIMIT_BYTES:
        text = encoded[:OUTPUT_LIMIT_BYTES].decode("utf-8", errors="ignore")
        truncated = True

    return text, truncated


def kill_process_group(pid: int) -> None:
    """Принудительно завершает группу процессов."""

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_command_processes() -> int:
    """Убрать процессы выделенного UID, включая сменившие session/process group.

    Вызывается только после проверки Docker-профиля, под общей execution-lock.
    pidfd защищает от отправки сигнала другому процессу при переиспользовании PID.
    """
    signalled = 0
    for entry in os.scandir("/proc"):
        if not entry.name.isdecimal():
            continue
        fd = None
        try:
            fd = os.pidfd_open(int(entry.name))
            with open(f"/proc/{entry.name}/status") as stream:
                fields = dict(line.split(":", 1) for line in stream if ":" in line)
            if int(fields["Uid"].split()[0]) == COMMAND_UID and not fields["State"].lstrip().startswith("Z"):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
                signalled += 1
        except (ProcessLookupError, FileNotFoundError):
            pass
        finally:
            if fd is not None:
                os.close(fd)
    return signalled


async def finish_processes(process, output) -> None:
    """Завершить потомков и дождаться закрытия труб с ограничением времени."""
    async def cleanup():
        kill_process_group(process.pid)
        # Проверяем до отсутствия живых процессов UID, а не фиксированное число
        # раз: потомок мог успеть fork во время обхода. Общий лимит — 3 секунды.
        while kill_command_processes():
            await asyncio.sleep(0.01)
        await asyncio.shield(output)

    try:
        await asyncio.wait_for(cleanup(), CLEANUP_TIMEOUT_SECONDS)
    except Exception:
        # Не оставляем неизвестные процессы жить до следующего запроса. Выход
        # контроллера завершает контейнер вместе с его PID namespace.
        logger.critical("process_cleanup_failed", exc_info=True)
        os._exit(70)


async def run_command(
    argv: tuple[str, ...],
    timeout_seconds: float = 30,
    *,
    request_id: str = "unknown",
) -> ExecutionResult:
    """Без неограниченной очереди: соседний запрос получает явный busy-ответ."""
    if _EXECUTION_LOCK.locked():
        logger.warning("execution_busy", extra={"request_id": request_id})
        return ExecutionResult(status="error", error="Executor is busy; retry after the current command finishes")
    async with _EXECUTION_LOCK:
        return await _run_command(argv, timeout_seconds, request_id=request_id)


async def _run_command(
    argv: tuple[str, ...],
    timeout_seconds: float = 30,
    *,
    request_id: str = "unknown",
) -> ExecutionResult:
    """Запускает сохранённые аргументы; никаких склеек строк или bash -c."""

    start_time = time.monotonic()

    try:
        options = subprocess_options()
    except (OSError, RuntimeError, ValueError, KeyError):
        logger.error("execution_policy_failed", extra={"request_id": request_id}, exc_info=True)
        return ExecutionResult(
            status="error",
            error="Protected Docker execution is required; launch with Run_docker.sh",
        )

    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=READ_CHUNK_BYTES,
        start_new_session=True,
        **options,
    ))
    cancelled_during_spawn = None
    try:
        # Отмена запроса не должна потерять ссылку на уже создаваемый процесс.
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError as exc:
            cancelled_during_spawn = exc
            while not spawn.done():
                try:
                    await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    continue
            process = spawn.result()

    except OSError:
        logger.error(
            "process_start_failed",
            extra={"request_id": request_id},
            exc_info=True,
        )

        if cancelled_during_spawn:
            raise cancelled_during_spawn
        return ExecutionResult(status="error", error="Could not start executable")

    logger.info(
        "process_started",
        extra={"request_id": request_id, "pid": process.pid},
    )

    output = asyncio.gather(
        read_limited(process.stdout),
        read_limited(process.stderr),
        process.wait(),
    )

    timed_out = False
    cancelled = cancelled_during_spawn
    try:
        if cancelled is not None:
            raise cancelled
        # Ждём основной процесс, затем убираем оставшихся потомков. Не ждём
        # EOF от демона бесконечно после успешного выхода его родителя.
        async def wait_exit():
            # asyncio Process.wait() может ждать ещё и закрытия унаследованных
            # потомком труб; returncode обновляется отдельно от их EOF.
            while process.returncode is None:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_exit(), timeout_seconds)
    except TimeoutError:
        timed_out = True
        logger.warning("process_timeout", extra={
            "request_id": request_id, "pid": process.pid, "timeout_seconds": timeout_seconds,
        })
    except asyncio.CancelledError as exc:
        cancelled = exc
        logger.warning("process_cancelled", extra={"request_id": request_id, "pid": process.pid})
    finally:
        cleanup = asyncio.create_task(finish_processes(process, output))
        # Повторная отмена MCP-запроса не прерывает обязательную очистку.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                if cancelled is None:
                    cancelled = exc
        cleanup.result()
    if cancelled is not None:
        # Сохраняем исходный CancelledError: AnyIO узнаёт своё cancel scope
        # по его метаданным и отменяет запрос, а не весь stdio-сервер.
        raise cancelled
    stdout_result, stderr_result, _ = output.result()

    elapsed_ms = (time.monotonic() - start_time) * 1000

    if timed_out:
        status = "timed_out"
    elif process.returncode == 0:
        status = "succeeded"
    else:
        status = "failed"

    result = ExecutionResult(
        stdout=stdout_result[0],
        stderr=stderr_result[0],
        stdout_truncated=stdout_result[1],
        stderr_truncated=stderr_result[1],
        exit_code=process.returncode,
        status=status,
        error=(
            "Command exceeded execution timeout"
            if timed_out
            else None
        ),
    )

    log_level = (
        logging.INFO
        if status == "succeeded"
        else logging.WARNING
    )

    logger.log(
        log_level,
        "process_finished",
        extra={
            "request_id": request_id, "pid": process.pid,
            "status": result.status, "exit_code": result.exit_code,
            "duration_ms": round(elapsed_ms, 1),
            "stdout_bytes": len(result.stdout.encode("utf-8")),
            "stderr_bytes": len(result.stderr.encode("utf-8")),
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        },
    )

    return result
