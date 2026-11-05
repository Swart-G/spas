"""Regressions for malformed replies, credentials and execution bookkeeping."""

import time
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from apps.mvp.agents import DiagnosticAgent
from apps.mvp.executor import create_executor_app
from executer.safety_checker.permissions import AgentRole
from shared.contracts.messages import AgentMessage, RoleEnum, StageEnum
from shared.contracts.workflow import RemediationData


@pytest.mark.parametrize("argv", ["pwd", [1], [""], [["pwd"]]])
def test_invalid_argv_cannot_be_confirmed_as_executed(argv):
    with pytest.raises(ValidationError):
        RemediationData.model_validate(
            {
                "action_taken": "claimed success",
                "plan": {"actions": [argv]},
                "execution_results": [
                    {"argv": argv, "status": "succeeded", "safety_checked": True, "exit_code": 0}
                ],
            }
        )


@pytest.mark.parametrize(
    "fault", ["stale", "future", "missing_time", "wrong_kind", "unknown_state", "contradictory"]
)
@pytest.mark.parametrize("stage", [StageEnum.DIAGNOSIS, StageEnum.VERIFICATION])
async def test_invalid_diagnostic_evidence_fails_closed(fault, stage):
    async def call(tool, arguments):
        kind = {
            "get_service_state": "state",
            "get_service_logs": "logs",
            "check_service_availability": "availability",
        }[tool]
        result = {
            "status": "succeeded",
            "kind": kind,
            "service": "demo-service",
            "output": {"state": "running", "logs": "ok", "availability": "available"}[kind],
            "observed_at": time.time(),
        }
        if fault == "stale":
            result["observed_at"] -= 3600
        elif fault == "future":
            result["observed_at"] += 3600
        elif fault == "missing_time":
            result["observed_at"] = None
        elif fault == "wrong_kind":
            result["kind"] = "availability"
        elif fault == "unknown_state" and kind == "state":
            result["output"] = "unknown"
        elif fault == "contradictory" and kind == "state":
            result["output"] = "stopped"
        return result

    agent = DiagnosticAgent(AsyncMock(call=call))
    result = await agent.process(
        AgentMessage(
            incident_id=uuid4(),
            receiver=RoleEnum.DIAGNOSTIC,
            stage=stage,
            task_description="check",
        )
    )
    assert not result.is_success


async def test_non_ascii_authorization_is_rejected_not_server_error():
    app = create_executor_app("diagnostic", "worker")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post("/worker/mcp", headers={b"authorization": b"Bearer \xff"})
    assert response.status_code == 401


async def test_invalid_command_does_not_reserve_incident_and_replays_have_audit_id():
    app = create_executor_app("diagnostic", "worker")
    server = app.state.servers[AgentRole.WORKER]
    context = {"incident_id": "incident", "reason": "check"}
    with pytest.raises(ToolError):
        await server.call_tool("execute_command", {"command": [], "context": context})
    assert not app.state.ledger
    arguments = {"command": ["spas-demo", "repair", "demo-service"], "context": context}
    first = (await server.call_tool("execute_command", arguments)).structured_content
    second = (await server.call_tool("execute_command", arguments)).structured_content
    assert first["status"] == "succeeded"
    assert second["status"] == "error"
    assert second["request_id"] and first["request_id"] != second["request_id"]
    assert app.state.resource.executions == 1


async def test_agent_non_ascii_authorization_is_rejected(monkeypatch):
    from apps.mvp.service import create_app

    monkeypatch.setenv("SPAS_COMPONENT", "diagnostic")
    monkeypatch.setenv("DIAGNOSTIC_TOKEN", "diagnostic")
    monkeypatch.setenv("AGENT_TOKEN", "orchestrator")
    app = create_app()
    message = AgentMessage(
        incident_id=uuid4(),
        receiver=RoleEnum.DIAGNOSTIC,
        stage=StageEnum.DIAGNOSIS,
        task_description="check",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/process",
            json=message.model_dump(mode="json"),
            headers={b"authorization": b"Bearer \xff"},
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "receipt",
    [
        {"status": "succeeded", "safety_checked": "true", "exit_code": 0},
        {"status": "succeeded", "safety_checked": True, "exit_code": False},
        {"status": "succeeded", "safety_checked": True, "exit_code": "0"},
    ],
)
async def test_worker_does_not_coerce_execution_receipts(receipt):
    from agents.worker.planner import StubPlanner
    from apps.mvp.agents import WorkerAgent

    executor = AsyncMock()
    executor.call.return_value = receipt
    result = await WorkerAgent(StubPlanner(), executor).process(
        AgentMessage(
            incident_id=uuid4(),
            receiver=RoleEnum.REMEDIATION,
            stage=StageEnum.REMEDIATION,
            task_description="repair",
            payload={"root_cause": "demo-service is stopped"},
        )
    )
    assert not result.is_success
    assert result.output_data["execution_results"][0]["status"] == "unknown"
