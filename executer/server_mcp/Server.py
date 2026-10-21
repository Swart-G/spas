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

from executer.safety_checker.permissions import AgentRole, permits_command

from .Configuration import load_checker
from .Execution_policy import validate_read_paths
from .Logging import REQUEST_CONTEXT, setup_logging, shutdown_logging
from .Models import Command, ExecutionContext, ExecutionResult
from .Runner import run_command
from .Safety_contract import SafetyCheckerPort, SafetyDecision

logger = logging.getLogger("executor.server")
SAFETY_CHECK_TIMEOUT_SECONDS = 5.0


def tool_result(result: ExecutionResult) -> CallToolResult:
    """Единый формат обычного ответа и отказа при занятом сервере."""
    data = result.model_dump(mode="json")
    return CallToolResult(
        structured_content=data,
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        is_error=result.status != "succeeded",
    )


class ExecutorMCPServer(MCPServer):
    """Журналируем вызов до валидации SDK, в том числе отклонённые аргументы."""

    def __init__(self, *args, agent_role: AgentRole, **kwargs):
        super().__init__(*args, **kwargs)
        self._agent_role = agent_role
        self._request_lock = asyncio.Lock()

    async def call_tool(self, name, arguments, context=None):
        request_id = uuid.uuid4().hex
        token = REQUEST_CONTEXT.set(
            {"request_id": request_id, "agent_role": self._agent_role.value}
        )
        started = time.monotonic()
        status = "error"
        exit_code = None
        safety_checked = False
        # Имя неизвестного инструмента тоже может содержать пользовательские данные.
        logger.info(
            "request_received", extra={"tool": name if name == "execute_command" else "unknown"}
        )
        try:
            # MCP SDK silently ignores unknown top-level tool arguments. Reject
            # them explicitly so role/approval fields cannot look authoritative.
            if name == "execute_command" and (
                not isinstance(arguments, dict) or set(arguments) - {"command", "context"}
            ):
                raise ToolError("Unexpected execute_command arguments")
            # Весь цикл, включая async checker, имеет один слот без очереди.
            # Между locked() и acquire нет await, допускающего второй вход.
            if self._request_lock.locked():
                logger.warning("request_busy")
                result = tool_result(
                    ExecutionResult(
                        status="error",
                        error="Executor is busy; retry after the current request finishes",
                        request_id=request_id,
                    )
                )
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
                "Invalid tool or arguments: expected command argv and incident_id/reason context; "
                f"request_id={request_id}"
            ) from None
        except Exception:
            logger.error("request_failed", exc_info=True)
            raise ToolError(f"Executor request failed; request_id={request_id}") from None
        finally:
            safety_checked = (REQUEST_CONTEXT.get() or {}).get("safety_checked", safety_checked)
            level = (
                logging.INFO
                if status == "succeeded"
                else logging.ERROR
                if status == "error"
                else logging.WARNING
            )
            logger.log(
                level,
                "request_finished",
                extra={
                    "status": status,
                    "exit_code": exit_code,
                    "safety_checked": safety_checked,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )
            REQUEST_CONTEXT.reset(token)


def create_server(
    checker: SafetyCheckerPort | None = None,
    *,
    allow_unchecked: bool = False,
    agent_role: AgentRole = AgentRole.DIAGNOSTIC,
) -> MCPServer:
    """Создаёт MCP Executor с заданным режимом безопасности."""
    if not isinstance(agent_role, AgentRole):
        raise ValueError("agent_role must be an AgentRole assigned by the application")
    if allow_unchecked and agent_role is not AgentRole.WORKER:
        raise ValueError("Unchecked development mode requires an explicit Worker role")
    setup_logging()
    if checker is not None and allow_unchecked:
        logger.error("server_configuration_invalid")
        raise ValueError("Choose either a connected Safety Checker or manual execution without one")

    mode = "unchecked" if allow_unchecked else "blocked" if checker is None else "checked"
    logger.log(
        logging.INFO if mode == "checked" else logging.WARNING,
        "server_initialized",
        extra={"mode": mode, "agent_role": agent_role.value},
    )
    server = ExecutorMCPServer("Executor MCP", agent_role=agent_role)

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
        logger.info(
            "command_validated", extra={"command_length": sum(map(len, command)) + len(command) - 1}
        )
        argv = tuple(command)
        safety_checked = False
        try:
            if not permits_command(agent_role, argv):
                logger.warning("agent_access_denied")
                result = ExecutionResult(
                    status="denied",
                    error="Operation is not permitted for this agent role",
                )
            elif allow_unchecked:
                logger.warning("safety_bypassed")
                result = await run_command(argv, request_id=request_id)
            elif checker is None:
                logger.error("safety_checker_missing")
                result = ExecutionResult(
                    status="error", error="Safety Checker is not connected; command was not started"
                )
            else:
                logger.info("safety_check_started")
                try:
                    async with asyncio.timeout(SAFETY_CHECK_TIMEOUT_SECONDS):
                        raw_decision = await checker.check(list(argv), context)
                    decision = SafetyDecision.model_validate(raw_decision)
                except TimeoutError:
                    logger.error("safety_check_timeout", exc_info=True)
                    result = ExecutionResult(
                        status="error", error="Safety Checker timed out; command was not started"
                    )
                except Exception:
                    logger.error("safety_check_failed", exc_info=True)
                    result = ExecutionResult(
                        status="error",
                        error=(
                            "Safety Checker unavailable or invalid response; "
                            "command was not started"
                        ),
                    )
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
                            result = ExecutionResult(
                                status="denied",
                                error=(
                                    "Read path is outside the mounted scope, is a symlink "
                                    "or unsupported file type"
                                ),
                            )
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

        result = result.model_copy(
            update={"request_id": request_id, "safety_checked": safety_checked}
        )
        return tool_result(result)

    return server


def configured_server(
    *,
    allow_unchecked: bool = False,
    agent_role: AgentRole = AgentRole.DIAGNOSTIC,
) -> MCPServer:
    """Единая сборка зависимостей для stdio и ручной консоли."""
    return create_server(
        checker=None if allow_unchecked else load_checker(),
        allow_unchecked=allow_unchecked,
        agent_role=agent_role,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Restricted Linux argv executor over MCP stdio")
    parser.add_argument(
        "--without-safety",
        action="store_true",
        help="Skip Safety Checker for isolated development; Docker restrictions remain",
    )
    parser.add_argument(
        "--agent-role",
        choices=[role.value for role in AgentRole],
        default=AgentRole.DIAGNOSTIC.value,
        help="Trusted connection role; never supplied by the agent tool call",
    )
    args = parser.parse_args()
    setup_logging()
    status = "stopped"
    try:
        server = configured_server(
            allow_unchecked=args.without_safety,
            agent_role=AgentRole(args.agent_role),
        )
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
