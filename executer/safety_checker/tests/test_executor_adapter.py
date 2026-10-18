import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import completion
from test_commands import ALLOW, DENY

from safety_analyzer.command_contracts import CommandContext
from safety_analyzer.executor import AnalyzerSafetyChecker

CONTEXT = SimpleNamespace(incident_id="i", reason="diagnose")


@pytest.mark.parametrize("verdict", [ALLOW, DENY])
async def test_executor_wire_contract_is_minimal(command_analyzer_for, verdict):
    async with command_analyzer_for(
        lambda _: httpx.Response(200, json=completion(json.dumps(verdict)))
    ) as analyzer:
        payload = await AnalyzerSafetyChecker(analyzer).check(["pwd"], CONTEXT)
    assert set(payload) == {"decision", "reason"} and payload["decision"] == verdict["decision"]


async def test_trusted_provider_supplies_entire_context(command_analyzer_for):
    async def provider(incident_id, reason):
        return CommandContext(
            incident_id=incident_id,
            reason=reason,
            task="PRIVATE-TASK",
            constraints=["No personal data disclosure"],
            history=[{"role": "user", "content": "Full prior history"}],
        )

    def handler(request):
        evidence = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert evidence["context"]["task"] == "PRIVATE-TASK"
        assert evidence["context"]["history"][0]["content"] == "Full prior history"
        return httpx.Response(200, json=completion(json.dumps(DENY)))

    async with command_analyzer_for(handler) as analyzer:
        result = await AnalyzerSafetyChecker(analyzer, provider).check(["pwd"], CONTEXT)
    assert result["decision"] == "DENY"


@pytest.mark.parametrize("kind", ["failure", "wrong_incident", "timeout"])
async def test_missing_or_unrelated_context_never_allows(command_analyzer_for, kind):
    import asyncio

    async def provider(incident_id, reason):
        if kind == "failure":
            raise RuntimeError("PRIVATE")
        if kind == "timeout":
            await asyncio.sleep(1)
        return CommandContext(incident_id="wrong", reason=reason)

    async with command_analyzer_for(
        lambda _: pytest.fail("bad context reached model"), rule_timeout_seconds=0.01
    ) as analyzer:
        result = await AnalyzerSafetyChecker(analyzer, provider).check(["pwd"], CONTEXT)
    assert result["decision"] == "DENY" and "PRIVATE" not in result["reason"]


@pytest.mark.parametrize("command", ["pwd", [], [1], ["rm", "-rf", "/"]])
async def test_invalid_or_forbidden_executor_command(command_analyzer_for, command):
    async with command_analyzer_for(lambda _: pytest.fail("bad argv reached model")) as analyzer:
        result = await AnalyzerSafetyChecker(analyzer).check(command, CONTEXT)
    assert result["decision"] == "DENY"
