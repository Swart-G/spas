import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import completion
from test_commands import ALLOW, DENY, command_request

from safety_analyzer.dispatch import CheckedExecutor


def reply(status="succeeded", **overrides):
    return SimpleNamespace(
        structured_content={
            "status": status,
            "safety_checked": True,
            "exit_code": 0,
            "stdout": "fixture",
            **overrides,
        },
        is_error=status != "succeeded",
    )


async def test_model_outage_does_not_dispatch_to_executor(command_analyzer_for):
    client = AsyncMock()
    async with command_analyzer_for(lambda _: httpx.Response(503)) as analyzer:
        result = await CheckedExecutor(analyzer, client).execute(command_request())
    assert not result.dispatch_attempted and result.assessment.status == "error"
    assert result.assessment.error_code == "gateway_unavailable"
    client.call_tool.assert_not_awaited()


@pytest.mark.parametrize(
    "command,verdict",
    [
        (["rm", "-rf", "/"], ALLOW),
        (["pwd"], DENY),
        (["docker", "restart", "--", "backend"], ALLOW),
    ],
)
async def test_no_dispatch_after_any_restriction(command_analyzer_for, command, verdict):
    client = AsyncMock()
    async with command_analyzer_for(
        lambda _: httpx.Response(200, json=completion(json.dumps(verdict))), containers=["backend"]
    ) as analyzer:
        result = await CheckedExecutor(analyzer, client).execute(command_request(command))
    client.call_tool.assert_not_awaited()
    assert not result.dispatch_attempted and result.execution_status == "not_started"


async def test_dispatch_binds_exact_argv_and_context_and_rechecks_every_request(
    command_analyzer_for,
):
    client = AsyncMock()
    client.call_tool.return_value = reply()
    request = command_request()
    original = request.fingerprint()

    def handler(_):
        request.command[:] = ["rm", "-rf", "/"]
        request.context.reason = "mutated context"
        return httpx.Response(200, json=completion(json.dumps(ALLOW)))

    async with command_analyzer_for(handler) as analyzer:
        result = await CheckedExecutor(analyzer, client).execute(request)
        second = await CheckedExecutor(analyzer, client).execute(request)
    assert result.assessment.input_sha256 == original and result.execution_status == "succeeded"
    client.call_tool.assert_awaited_once_with(
        "execute_command",
        {
            "command": ["pwd"],
            "context": {"incident_id": "incident-1", "reason": "Investigate the incident"},
        },
    )
    assert second.assessment.decision == "DENY" and not second.dispatch_attempted


@pytest.mark.parametrize("mode", ["outage", "timeout", "unchecked", "malformed"])
async def test_executor_failures_are_unknown_and_not_retried(command_analyzer_for, mode):
    client = AsyncMock()
    if mode == "outage":
        client.call_tool.side_effect = RuntimeError("PRIVATE connection failure")
    elif mode == "timeout":

        async def slow(*_):
            await asyncio.sleep(1)

        client.call_tool.side_effect = slow
    elif mode == "unchecked":
        client.call_tool.return_value = reply(safety_checked=False)
    else:
        client.call_tool.return_value = SimpleNamespace(structured_content={}, is_error=False)
    async with command_analyzer_for(
        lambda _: httpx.Response(200, json=completion(json.dumps(ALLOW)))
    ) as analyzer:
        result = await CheckedExecutor(analyzer, client, timeout_seconds=0.01).execute(
            command_request()
        )
    assert result.dispatch_attempted and result.execution_status == "unknown"
    assert result.error_code is not None and client.call_tool.await_count == 1
    assert "PRIVATE" not in result.model_dump_json()


async def test_host_bridge_preserves_actual_executor_rule_denial(
    actual_executor, command_analyzer_for, monkeypatch
):
    actual = actual_executor
    runner = AsyncMock()
    monkeypatch.setattr(actual.server, "run_command", runner)
    # Host permits docker ps, but the real isolated Executor's deployment prohibits Docker.
    rules = actual.rules.RuleBasedSafetyChecker(
        actual.rules.SafetyPolicy(denied_executables={"docker"})
    )
    server = actual.server.create_server(checker=rules)
    async with command_analyzer_for(
        lambda _: httpx.Response(200, json=completion(json.dumps(ALLOW)))
    ) as analyzer:
        result = await CheckedExecutor(analyzer, server).execute(command_request(["docker", "ps"]))
    assert result.assessment.decision == "ALLOW" and result.execution_status == "denied"
    assert result.execution.safety_checked
    runner.assert_not_awaited()
