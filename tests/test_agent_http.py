import asyncio
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse, Response

from agents.demo import DemoResource
from agents.http import HttpAgent
from apps.cli.demo_agents import run_demo
from core.incidents.models import IncidentStatus
from core.orchestrator.engine import Orchestrator
from shared.contracts import (
    AgentCompletionUnknownError,
    AgentMessage,
    AgentResult,
    RoleEnum,
    StageEnum,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["message_id", "incident_id", "sender", "receiver", "stage"])
async def test_standalone_http_agent_rejects_mismatched_result(live_server, field):
    """Transport clients must be safe to use without the Orchestrator."""
    app = FastAPI()

    @app.post("/agent")
    async def process(message: AgentMessage):
        data = AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=message.receiver,
            stage=message.stage,
            is_success=True,
        ).model_dump(mode="json")
        data[field] = {
            "message_id": str(message.incident_id),
            "incident_id": str(message.message_id),
            "sender": RoleEnum.REMEDIATION.value,
            "receiver": RoleEnum.DIAGNOSTIC.value,
            "stage": StageEnum.VERIFICATION.value,
        }[field]
        return data

    async with live_server(app) as (host, port):
        agent = HttpAgent(RoleEnum.DIAGNOSTIC, f"http://{host}:{port}/agent")
        message = AgentMessage(
            incident_id=uuid4(),
            receiver=RoleEnum.DIAGNOSTIC,
            stage=StageEnum.DIAGNOSIS,
            task_description="Independent diagnostic request",
        )
        with pytest.raises(ValueError):
            await agent.process(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_local_http_agents_observe_and_change_actual_demo_state(repair_succeeds):
    report = await run_demo(repair_succeeds=repair_succeeds)
    assert report["resource_healthy"] is repair_succeeds
    assert report["repair_calls"] == 1
    assert report["agent_calls"] == ["diagnosis", "remediation", "verification"]
    incident = report["incident"]
    assert incident["status"] == ("resolved" if repair_succeeds else "failed")
    assert [item["stage"] for item in incident["history"]] == [
        "triage",
        "diagnosis",
        "remediation",
        "verification",
    ]
    assert incident["verification"]["resolved"] is repair_succeeds
    assert incident["history"][1]["output_data"]["metrics"]["healthy"] is False
    assert incident["history"][3]["output_data"]["metrics"]["healthy"] is repair_succeeds


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "json", "schema", "correlation"])
async def test_invalid_http_results_fail_without_committing(live_server, failure):
    app = FastAPI()

    @app.post("/agent")
    async def process(message: AgentMessage):
        if failure == "status":
            return JSONResponse({"error": "Unavailable"}, status_code=503)
        if failure == "json":
            return Response("{not json", media_type="application/json")
        if failure == "schema":
            return JSONResponse({"is_success": True})
        return AgentResult(
            message_id=message.incident_id,  # Deliberately mismatched correlation.
            incident_id=message.incident_id,
            sender=message.receiver,
            stage=message.stage,
            is_success=True,
        )

    async with live_server(app) as (host, port):
        engine = Orchestrator(
            agents={RoleEnum.TRIAGE: HttpAgent(RoleEnum.TRIAGE, f"http://{host}:{port}/agent")}
        )
        incident = await engine.create_incident("HTTP error", "Invalid result")
        await engine.process_incident(incident.incident_id)
        assert incident.status == IncidentStatus.FAILED
        assert incident.history == [] and incident.metrics_log == {}
        assert incident.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cancel", "timeout", "deadline"])
async def test_unconfirmed_remote_completion_blocks_retry(live_server, reason):
    app = FastAPI()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls = 0

    @app.post("/agent")
    async def process(message: AgentMessage):
        nonlocal calls
        calls += 1
        started.set()
        try:
            await release.wait()
            return AgentResult(
                message_id=message.message_id,
                incident_id=message.incident_id,
                sender=message.receiver,
                stage=message.stage,
                is_success=True,
            )
        finally:
            finished.set()

    async with live_server(app) as (host, port):
        agent = HttpAgent(
            RoleEnum.TRIAGE,
            f"http://{host}:{port}/agent",
            request_timeout=0.2 if reason == "timeout" else 1,
        )
        engine = Orchestrator(
            agents={RoleEnum.TRIAGE: agent}, agent_timeout=0.2 if reason == "deadline" else 10
        )
        incident = await engine.create_incident("Remote", "Remote cleanup is not acknowledged")
        active = asyncio.create_task(engine.execute_next_step(incident.incident_id))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            if reason == "cancel":
                active.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(active), timeout=1)
            else:
                await asyncio.wait_for(active, timeout=1)
            assert incident.status == IncidentStatus.FAILED
            assert not finished.is_set()  # Server is genuinely still running.
            await engine.process_incident(incident.incident_id)
            assert calls == 1
            assert incident.history == []
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=1)
            await engine.process_incident(incident.incident_id)
            assert incident.status == IncidentStatus.FAILED
            assert calls == 1
        finally:
            release.set()
            await asyncio.gather(active, return_exceptions=True)


@pytest.mark.parametrize("url", ["", "/relative", "ftp://localhost/agent"])
def test_http_agent_requires_absolute_http_url(url):
    with pytest.raises(ValueError, match="HTTP"):
        HttpAgent(RoleEnum.DIAGNOSTIC, url)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_http_agent_requires_finite_positive_timeout(timeout):
    with pytest.raises(ValueError, match="request_timeout"):
        HttpAgent(RoleEnum.DIAGNOSTIC, "http://localhost/agent", request_timeout=timeout)


def test_demo_resources_are_independent():
    first = DemoResource()
    second = DemoResource()
    first.calls.append("diagnosis")
    first.healthy = True
    assert not second.healthy and second.calls == []


@pytest.mark.asyncio
async def test_http_redirect_does_not_forward_a_worker_action(live_server):
    import httpx

    app = FastAPI()
    forwarded = 0

    @app.post("/agent")
    async def redirect():
        return RedirectResponse("/unexpected", status_code=307)

    @app.post("/unexpected")
    async def unexpected():
        nonlocal forwarded
        forwarded += 1
        return {}

    async with live_server(app) as (host, port):
        agent = HttpAgent(RoleEnum.REMEDIATION, f"http://{host}:{port}/agent")
        message = AgentMessage(
            incident_id=uuid4(),
            receiver=RoleEnum.REMEDIATION,
            stage=StageEnum.REMEDIATION,
            task_description="Must not forward this action",
        )
        with pytest.raises(httpx.HTTPStatusError):
            await agent.process(message)
        assert forwarded == 0


@pytest.mark.asyncio
async def test_http_agent_rejects_wrong_receiver_before_opening_connection():
    agent = HttpAgent(RoleEnum.DIAGNOSTIC, "http://127.0.0.1:1/unreachable")
    message = AgentMessage(
        incident_id=uuid4(), receiver=RoleEnum.REMEDIATION, task_description="Wrong agent"
    )
    with pytest.raises(ValueError, match="another role"):
        await agent.process(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cancel", "timeout"])
async def test_standalone_http_completion_is_unknown_until_server_finishes(live_server, reason):
    app = FastAPI()
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @app.post("/agent")
    async def process():
        started.set()
        try:
            await release.wait()
            return {}
        finally:
            finished.set()

    async with live_server(app) as (host, port):
        agent = HttpAgent(
            RoleEnum.DIAGNOSTIC,
            f"http://{host}:{port}/agent",
            request_timeout=0.2 if reason == "timeout" else 1,
        )
        message = AgentMessage(
            incident_id=uuid4(), receiver=RoleEnum.DIAGNOSTIC, task_description="Wait"
        )
        task = asyncio.create_task(agent.process(message))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            if reason == "cancel":
                task.cancel()
            with pytest.raises(AgentCompletionUnknownError):
                await asyncio.wait_for(task, timeout=1)
            assert not finished.is_set()
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=1)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
