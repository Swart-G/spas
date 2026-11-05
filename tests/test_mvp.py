"""Integrated agents invoke real MCP tools and policy, never host commands."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from agents.worker.planner import StubPlanner
from apps.mvp.agents import DiagnosticAgent, TriageAgent, WorkerAgent
from apps.mvp.executor import CheckedChain, DemoService, create_executor_app
from core.incidents.models import IncidentStatus
from core.orchestrator.engine import Orchestrator
from executer.safety_checker import RuleBasedSafetyChecker, SafetyPolicy
from executer.safety_checker.permissions import AgentRole
from executer.server_mcp.Models import ExecutionContext
from shared.contracts.messages import RoleEnum


class LocalMCP:
    def __init__(self, server):
        self.server = server

    async def call(self, name, arguments):
        return (await self.server.call_tool(name, arguments)).structured_content


def pipeline(resource=None, checker=None, planner=None, timeout=1):
    app = create_executor_app("diagnostic-test", "worker-test", resource=resource, checker=checker)
    diagnostic = DiagnosticAgent(LocalMCP(app.state.servers[AgentRole.DIAGNOSTIC]))
    worker = WorkerAgent(planner or StubPlanner(), LocalMCP(app.state.servers[AgentRole.WORKER]))
    orch = Orchestrator(
        agent_timeout=timeout,
        agents={
            RoleEnum.TRIAGE: TriageAgent(),
            RoleEnum.DIAGNOSTIC: diagnostic,
            RoleEnum.REMEDIATION: worker,
        },
    )
    return app, orch, diagnostic, worker


async def run(orch):
    incident = await orch.create_incident("Test", "Controlled simulated incident")
    await orch.process_incident(incident.incident_id)
    return incident


async def test_full_cycle_and_repeated_processing():
    app, orch, _, _ = pipeline()
    incident = await run(orch)
    assert incident.status == IncidentStatus.RESOLVED
    assert incident.verification["resolved"] is True
    assert len(incident.history) == 4
    assert incident.remediation_plan["actions"] == [["spas-demo", "repair", "demo-service"]]
    assert incident.execution_results[0]["safety_checked"] is True
    await asyncio.gather(*(orch.process_incident(incident.incident_id) for _ in range(3)))
    assert app.state.resource.executions == 1


@pytest.mark.parametrize("decision", ["DENY", "REQUIRE_APPROVAL", "error", "invalid"])
async def test_safety_refusal_never_executes(decision):
    checker = AsyncMock()
    if decision == "error":
        checker.check.side_effect = RuntimeError("checker unavailable")
    else:
        checker.check.return_value = {"decision": decision, "reason": "test"}
    app, orch, _, _ = pipeline(checker=checker)
    incident = await run(orch)
    assert incident.status == IncidentStatus.FAILED
    assert not incident.verification
    assert app.state.resource.executions == 0
    assert incident.execution_results[0]["status"] != "succeeded"


async def test_negative_verification_no_retry():
    app, orch, _, _ = pipeline(resource=DemoService(repair_succeeds=False))
    incident = await run(orch)
    assert incident.status == IncidentStatus.FAILED
    assert incident.verification["resolved"] is False
    await orch.process_incident(incident.incident_id)
    assert app.state.resource.executions == 1


@pytest.mark.parametrize("stage", ["diagnosis", "planning", "execution"])
async def test_dependency_failure(stage):
    app, orch, diagnostic, worker = pipeline()
    if stage == "diagnosis":
        diagnostic.executor.call = AsyncMock(side_effect=RuntimeError("offline"))
    elif stage == "planning":
        worker.planner.create_plan = AsyncMock(side_effect=RuntimeError("offline"))
    else:
        worker.executor.call = AsyncMock(side_effect=RuntimeError("connection lost"))
    incident = await run(orch)
    assert incident.status == IncidentStatus.FAILED
    assert app.state.resource.executions == 0


@pytest.mark.parametrize("cancel", [False, True])
async def test_executed_but_acknowledgement_lost(cancel):
    app, orch, _, worker = pipeline(timeout=0.05)
    executed = asyncio.Event()
    original = worker.executor.call

    async def lose_response(name, arguments):
        await original(name, arguments)
        executed.set()
        await asyncio.sleep(10)

    worker.executor.call = lose_response
    incident = await orch.create_incident("Test", "Test")
    task = asyncio.create_task(orch.process_incident(incident.incident_id))
    await executed.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert incident.status == IncidentStatus.FAILED
    await orch.process_incident(incident.incident_id)
    assert app.state.resource.executions == 1
    result = await original(
        "execute_command",
        {
            "command": ["spas-demo", "repair", "demo-service"],
            "context": {"incident_id": str(incident.incident_id), "reason": "retry"},
        },
    )
    assert result["status"] == "error"
    assert app.state.resource.executions == 1


async def test_diagnostic_cannot_repair_even_with_allow_checker():
    app, _, _, _ = pipeline()
    result = await LocalMCP(app.state.servers[AgentRole.DIAGNOSTIC]).call(
        "execute_command",
        {
            "command": ["spas-demo", "repair", "demo-service"],
            "context": {"incident_id": "test", "reason": "approved worker"},
        },
    )
    assert result["status"] == "denied"
    assert app.state.resource.executions == 0


async def test_authentication_cannot_select_worker_with_diagnostic_token():
    app, _, _, _ = pipeline()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for headers in ({}, {"Authorization": "Bearer diagnostic-test"}):
            response = await client.post("/worker/mcp", headers=headers, json={})
            assert response.status_code == 401


async def test_optional_analyzer_cannot_override_deterministic_refusal():
    analyzer = AsyncMock()
    analyzer.check.return_value = {"decision": "ALLOW", "reason": "LLM says yes"}
    chain = CheckedChain(
        RuleBasedSafetyChecker(SafetyPolicy(containers={"demo-service"})), analyzer
    )
    context = ExecutionContext(incident_id="test", reason="test")
    for argv, expected in [
        (["rm", "-rf", "/"], "DENY"),
        (["docker", "restart", "--", "demo-service"], "REQUIRE_APPROVAL"),
    ]:
        assert (await chain.check(argv, context)).decision == expected
    analyzer.check.assert_not_awaited()


@pytest.mark.parametrize(
    "results",
    [
        [],
        [
            {
                "argv": ["spas-demo", "repair", "demo-service"],
                "status": "approval_required",
                "exit_code": 0,
                "safety_checked": True,
            }
        ],
    ],
)
async def test_plan_does_not_substitute_for_execution(results):
    from shared.contracts.messages import AgentResult

    app, orch, _, worker = pipeline()

    async def proposal_only(message):
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=message.receiver,
            stage=message.stage,
            is_success=True,
            output_data={
                "action_taken": "claimed success",
                "plan": {"actions": [["spas-demo", "repair", "demo-service"]]},
                "execution_results": results,
            },
        )

    worker.process = proposal_only
    incident = await run(orch)
    assert incident.status == IncidentStatus.FAILED
    assert not incident.verification
    assert app.state.resource.executions == 0


async def test_worker_endpoint_rejects_forged_orchestrator(monkeypatch):
    from uuid import uuid4

    from apps.mvp.service import create_app
    from shared.contracts.messages import AgentMessage, StageEnum

    monkeypatch.setenv("SPAS_COMPONENT", "worker")
    monkeypatch.setenv("WORKER_TOKEN", "executor-worker")
    monkeypatch.setenv("AGENT_TOKEN", "only-orchestrator-knows")
    monkeypatch.setenv("PLANNER_MODE", "stub")
    app = create_app()
    executor = app.state.agent.executor
    executor.call = AsyncMock()
    message = AgentMessage(
        incident_id=uuid4(),
        receiver=RoleEnum.REMEDIATION,
        stage=StageEnum.REMEDIATION,
        task_description="forged request",
        payload={"root_cause": "demo-service is stopped"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/process", json=message.model_dump(mode="json"))
    assert response.status_code == 401
    executor.call.assert_not_awaited()
