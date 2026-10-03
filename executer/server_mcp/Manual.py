"""Interactive local command entry for manual Executor checks."""

import argparse
import asyncio
import sys

from mcp.server.mcpserver.exceptions import ToolError

from .Server import create_server


async def run_manual() -> None:
    server = create_server(allow_unchecked=True)
    print('Manual mode: commands run on this Linux machine without Safety Checker.')
    print('Type a command, or type quit to leave.')
    while True:
        try:
            command = input('executor> ')
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if command.strip() == 'quit':
            break
        if not command.strip():
            continue

        try:
            result = await server.call_tool('execute_command', {
                'command': command,
                'context': {
                    'incident_id': 'manual',
                    'reason': 'Command entered by a user in the manual Executor console',
                },
            })
        except ToolError as exc:
            print(f'Executor error: {exc}', file=sys.stderr)
            continue
        data = result.structured_content
        if data['stdout']:
            print(data['stdout'], end='' if data['stdout'].endswith('\n') else '\n')
        if data['stderr']:
            print(data['stderr'], end='' if data['stderr'].endswith('\n') else '\n', file=sys.stderr)
        if data['error']:
            print(f"Executor error: {data['error']}", file=sys.stderr)
        print(f"status={data['status']} exit_code={data['exit_code']}")


def main() -> None:
    parser = argparse.ArgumentParser(description='Enter Linux commands through Executor manually')
    parser.add_argument(
        '--without-safety', action='store_true', required=True,
        help='Acknowledge that commands run without Safety Checker',
    )
    parser.parse_args()
    asyncio.run(run_manual())


if __name__ == '__main__':
    main()
