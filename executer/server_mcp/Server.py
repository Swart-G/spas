"""MCP-сервер: команда, Safety Checker, argv без shell, результат."""

import argparse
import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp_types import CallToolResult, TextContent
from pydantic import Field

from .Models import Command, DiagnosticResult, ExecutionContext, ExecutionResult, ServiceName
from .Runner import run_command
from .Configuration import load_checker
from .Execution_policy import validate_read_paths
from .Diagnostics import diagnose_service
from .Safety_contract import SafetyCheckerPort, SafetyDecision
from .Logging import REQUEST_CONTEXT, setup_logging, shutdown_logging

logger = logging.getLogger("executor.server")


def tool_result(result: ExecutionResult | DiagnosticResult) -> CallToolResult:
    """Единый формат обычного ответа и отказа при занятом сервере."""
    data = result.model_dump(mode="json")
    return CallToolResult(
        structured_content=data,
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        is_error=result.status != "succeeded",
    )


class ExecutorMCPServer(MCPServer):
    """Журналируем вызов до валидации SDK, в том числе отклонённые аргументы."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._request_lock = asyncio.Lock()

    async def call_tool(self, name, arguments, context=None):
        request_id = uuid.uuid4().hex
        token = REQUEST_CONTEXT.set({"request_id": request_id})
        started = time.monotonic()
        status = "error"
        exit_code = None
        safety_checked = False
        # Имя неизвестного инструмента тоже может содержать пользовательские данные.
        known_tools = {"execute_command", "get_service_logs", "get_service_state", "check_service_availability"}
        logger.info("request_received", extra={"tool": name if name in known_tools else "unknown"})
        try:
            # Весь цикл, включая async checker, имеет один слот без очереди.
            # Между locked() и acquire нет await, допускающего второй вход.
            if self._request_lock.locked():
                logger.warning("request_busy")
                diagnostic_kinds = {
                    "get_service_logs": "logs", "get_service_state": "state",
                    "check_service_availability": "availability",
                }
                if name in diagnostic_kinds:
                    result = tool_result(DiagnosticResult(
                        status="error", kind=diagnostic_kinds[name],
                        service="unknown", request_id=request_id,
                        error="Executor is busy; retry after the current request finishes",
                    ))
                else:
                    result = tool_result(ExecutionResult(
                        status="error", error="Executor is busy; retry after the current request finishes",
                        request_id=request_id,
                    ))
            else:
                async with self._request_lock:
                    result = await super().call_tool(name, arguments, context)
            data = result.structured_content or {}
            status = data.get("status", "error")
            exit_code = data.get("exit_code")
            safety_checked = data.get("safety_checked", False)
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            logger.warning("request_cancelled")
            raise
        except UnexpectedToolError:
            logger.error("request_failed", exc_info=True)
            raise ToolError(f"Executor request failed; request_id={request_id}") from None
        except ToolError:
            status = "rejected"
            logger.warning("request_rejected", exc_info=True)
            # SDK/Pydantic может включать весь отвергнутый ввод в текст ошибки.
            # Не возвращаем его в контекст модели или ручной stderr-журнал.
            raise ToolError(
                f"Invalid tool or arguments; request_id={request_id}"
            ) from None
        except Exception:
            logger.error("request_failed", exc_info=True)
            raise ToolError(f"Executor request failed; request_id={request_id}") from None
        finally:
            safety_checked = (REQUEST_CONTEXT.get() or {}).get("safety_checked", safety_checked)
            level = logging.INFO if status == "succeeded" else logging.ERROR if status == "error" else logging.WARNING
            logger.log(level, "request_finished", extra={
                "status": status, "exit_code": exit_code,
                "safety_checked": safety_checked,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            })
            REQUEST_CONTEXT.reset(token)


def create_server(
    checker: SafetyCheckerPort | None = None,
    *,
    allow_unchecked: bool = False,
) -> MCPServer:
    """Создаёт MCP Executor с заданным режимом безопасности."""
    setup_logging()
    if checker is not None and allow_unchecked:
        logger.error("server_configuration_invalid")
        raise ValueError("Choose either a connected Safety Checker or manual execution without one")

    mode = "unchecked" if allow_unchecked else "blocked" if checker is None else "checked"
    logger.log(logging.INFO if mode == "checked" else logging.WARNING,
               "server_initialized", extra={"mode": mode})
    server = ExecutorMCPServer("Executor MCP")

    @server.tool()
    async def execute_command(
        command: Command,
        context: ExecutionContext,
    ) -> Annotated[CallToolResult, ExecutionResult]:
        """Execute literal argv in the restricted container. No shell operators or expansions."""
        request = dict(REQUEST_CONTEXT.get() or {})
        request_id = request["request_id"]
        # incident_id — произвольная строка от клиента. В журнал попадает
        # стабильная ссылка, а не её содержимое, которое может быть секретом.
        request["incident_ref"] = hashlib.sha256(context.incident_id.encode()).hexdigest()[:16]
        REQUEST_CONTEXT.set(request)
        logger.info("command_validated", extra={"command_length": sum(map(len, command)) + len(command) - 1})
        argv = tuple(command)
        safety_checked = False
        try:
            if allow_unchecked:
                logger.warning("safety_bypassed")
                result = await run_command(argv, request_id=request_id)
            elif checker is None:
                logger.error("safety_checker_missing")
                result = ExecutionResult(status="error", error="Safety Checker is not connected; command was not started")
            else:
                logger.info("safety_check_started")
                try:
                    async with asyncio.timeout(5):
                        raw_decision = await checker.check(list(argv), context)
                    decision = SafetyDecision.model_validate(raw_decision)
                except TimeoutError:
                    logger.error("safety_check_timeout", exc_info=True)
                    result = ExecutionResult(status="error", error="Safety Checker timed out; command was not started")
                except Exception:
                    logger.error("safety_check_failed", exc_info=True)
                    result = ExecutionResult(status="error", error="Safety Checker unavailable or invalid response; command was not started")
                else:
                    safety_checked = True
                    request["safety_checked"] = True
                    REQUEST_CONTEXT.set(dict(request))
                    logger.info("safety_decision", extra={"decision": decision.decision})
                    if decision.decision == "ALLOW":
                        # Snapshot не зависит от изменений списка внутри checker.
                        try:
                            validate_read_paths(argv)
                        except (OSError, ValueError):
                            logger.warning("read_scope_rejected", exc_info=True)
                            result = ExecutionResult(status="denied", error="Read path is outside the mounted scope, is a symlink or unsupported file type")
                        else:
                            result = await run_command(argv, request_id=request_id)
                    else:
                        result = ExecutionResult(
                            status="denied" if decision.decision == "DENY" else "approval_required",
                            error=decision.reason,
                        )
        except Exception:
            logger.error("internal_error", exc_info=True)
            result = ExecutionResult(status="error", error="Internal Executor error")

        result = result.model_copy(update={"request_id": request_id, "safety_checked": safety_checked})
        return tool_result(result)

    def diagnostic_tool(kind: str, service: str, context: ExecutionContext, lines: int = 100) -> CallToolResult:
        request = dict(REQUEST_CONTEXT.get() or {})
        request["incident_ref"] = hashlib.sha256(context.incident_id.encode()).hexdigest()[:16]
        REQUEST_CONTEXT.set(request)
        result = diagnose_service(kind, service, lines=lines)
        return tool_result(result.model_copy(update={"request_id": request["request_id"]}))

    @server.tool()
    async def get_service_logs(
        service: ServiceName,
        context: ExecutionContext,
        lines: Annotated[int, Field(strict=True, ge=1, le=200)] = 100,
    ) -> Annotated[CallToolResult, DiagnosticResult]:
        """Read recent bounded logs for an operator-allowlisted service snapshot."""
        return diagnostic_tool("logs", service, context, lines)

    @server.tool()
    async def get_service_state(
        service: ServiceName,
        context: ExecutionContext,
    ) -> Annotated[CallToolResult, DiagnosticResult]:
        """Read running/stopped/unknown state from a fresh operator snapshot."""
        return diagnostic_tool("state", service, context)

    @server.tool()
    async def check_service_availability(
        service: ServiceName,
        context: ExecutionContext,
    ) -> Annotated[CallToolResult, DiagnosticResult]:
        """Read Docker health availability from a fresh operator snapshot."""
        return diagnostic_tool("availability", service, context)

    return server


def configured_server(*, allow_unchecked: bool = False) -> MCPServer:
    """Единая сборка зависимостей для stdio и ручной консоли."""
    return create_server(
        checker=None if allow_unchecked else load_checker(),
        allow_unchecked=allow_unchecked,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Restricted Linux argv executor over MCP stdio")
    parser.add_argument("--without-safety", action="store_true",
                        help="Skip Safety Checker for isolated development; Docker restrictions remain")
    args = parser.parse_args()
    setup_logging()
    status = "stopped"
    try:
        server = configured_server(allow_unchecked=args.without_safety)
        logger.info("server_started", extra={"transport": "stdio"})
        server.run(transport="stdio")
    except KeyboardInterrupt:
        status = "interrupted"
    except Exception:
        status = "error"
        logger.error("server_failed", exc_info=True)
        raise SystemExit(1) from None
    finally:
        logger.info("server_stopped", extra={"status": status})
        shutdown_logging()


if __name__ == "__main__":
    main()
