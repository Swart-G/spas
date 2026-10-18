"""Real pinned MCP tool dispatch/validation; only the process runner is replaced."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import completion
from test_commands import ALLOW, DENY

from safety_analyzer.executor import AnalyzerSafetyChecker

REFERENCE = (
    Path(__file__).resolve().parents[1]
    / "reports/reference/00b54e3ebfb768ebfdf0ee69304879b3cb1c7c7b"
)
pytestmark = pytest.mark.gateway_contract


@pytest.mark.parametrize(
    "command,verdict,expected,calls",
    [
        (["pwd"], ALLOW, "succeeded", 1),
        (["pwd"], DENY, "denied", 0),
        (["rm", "-rf", "/"], ALLOW, "denied", 0),
        (["docker", "restart", "--", "backend"], ALLOW, "approval_required", 0),
        (["bash", "-c", "pwd"], ALLOW, "denied", 0),
    ],
)
async def test_real_mcp_never_executes_a_restricted_command(
    actual_executor, command_analyzer_for, monkeypatch, command, verdict, expected, calls
):
    actual = actual_executor
    runner = AsyncMock(
        return_value=actual.models.ExecutionResult(status="succeeded", stdout="fixture")
    )
    monkeypatch.setattr(actual.server, "run_command", runner)
    policy = actual.rules.SafetyPolicy(containers={"backend"})
    rules = actual.rules.RuleBasedSafetyChecker(policy)

    def model(_):
        if command != ["pwd"]:
            pytest.fail("model attempted to override actual Rule-Based Checker")
        return httpx.Response(200, json=completion(json.dumps(verdict)))

    async with command_analyzer_for(model, rules=rules) as analyzer:
        server = actual.server.create_server(checker=AnalyzerSafetyChecker(analyzer))
        result = await server.call_tool(
            "execute_command",
            {
                "command": command,
                "context": {"incident_id": "i", "reason": "diagnose"},
            },
        )
    assert result.structured_content["status"] == expected
    assert result.structured_content["safety_checked"] is True
    assert runner.await_count == calls
    if calls:
        assert runner.await_args.args[0] == tuple(command)


async def test_real_mcp_llm_outage_never_reaches_runner(
    actual_executor, command_analyzer_for, monkeypatch
):
    actual = actual_executor
    runner = AsyncMock()
    monkeypatch.setattr(actual.server, "run_command", runner)
    async with command_analyzer_for(lambda _: httpx.Response(503)) as analyzer:
        server = actual.server.create_server(checker=AnalyzerSafetyChecker(analyzer))
        result = await server.call_tool(
            "execute_command",
            {
                "command": ["pwd"],
                "context": {"incident_id": "i", "reason": "diagnose"},
            },
        )
    assert result.structured_content["status"] == "denied"
    runner.assert_not_awaited()
