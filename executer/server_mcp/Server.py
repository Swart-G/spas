"""MCP-сервер: команда + контекст → Safety Checker → Linux shell → результат."""

import argparse
import asyncio
import json
import sys
from typing import Annotated
from mcp.server import MCPServer
from mcp_types import CallToolResult, TextContent
from .Models import Command, ExecutionContext, ExecutionResult
from .Runner import run_shell
from .Safety_contract import SafetyCheckerPort, SafetyDecision


def create_server(
    checker: SafetyCheckerPort | None = None, *, allow_unchecked: bool = False,
) -> MCPServer:
    """Подключает переданный извне checker (или None) и возвращает MCP-сервер, который использует его для проверки команд перед их выполнением."""
    if checker is not None and allow_unchecked:
        raise ValueError('Choose either a connected Safety Checker or manual execution without one')
    server = MCPServer('Executor MCP')

    @server.tool()
    async def execute_command(command: Command, context: ExecutionContext) -> Annotated[CallToolResult, ExecutionResult]:
        """Execute a Linux shell command using the server's configured safety mode."""
        if allow_unchecked:
            result = await run_shell(command)
        elif checker is None:
            result = ExecutionResult(
                status='error', error='Safety Checker is not connected; command was not started',
            )
        else:
            try:
                async with asyncio.timeout(5):
                    raw_decision = await checker.check(command, context)
                decision = SafetyDecision.model_validate(raw_decision)
            except Exception:
                result = ExecutionResult(status='error', error='Safety Checker unavailable or invalid response; command was not started')
            else:
                if decision.decision == 'ALLOW':
                    result = await run_shell(command)
                else:
                    result = ExecutionResult(
                        status='denied' if decision.decision == 'DENY' else 'approval_required',
                        error=decision.reason,
                    )
                result = result.model_copy(update={'safety_checked': True})
        data = result.model_dump(mode='json')
        return CallToolResult(
            structured_content=data,
            content=[TextContent(type='text', text=json.dumps(data, ensure_ascii=False))],
            is_error=result.status != 'succeeded',
        )
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description='Local Linux command executor over MCP stdio')
    parser.add_argument(
        '--without-safety', action='store_true',
        help='Execute real local commands without Safety Checker for manual development',
    )
    args = parser.parse_args()
    if args.without_safety:
        print('Manual mode: real Linux commands; Safety Checker is not used.', file=sys.stderr)
    create_server(allow_unchecked=args.without_safety).run(transport='stdio')


if __name__ == '__main__':
    main()
