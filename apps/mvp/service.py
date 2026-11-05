"""One entry point for independently deployed MVP services."""

import hmac
import os
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from agents.http import HttpAgent
from agents.worker.planner import GatewayPlanner, StubPlanner
from apps.mvp.agents import DiagnosticAgent, TriageAgent, WorkerAgent
from apps.mvp.client import ExecutorClient
from apps.mvp.executor import create_executor_app
from core.orchestrator.engine import Orchestrator
from shared.contracts import AgentMessage, RoleEnum


def create_app():
    component = os.environ.get("SPAS_COMPONENT", "orchestrator")
    if component == "executor":
        return create_executor_app(os.environ["DIAGNOSTIC_TOKEN"], os.environ["WORKER_TOKEN"])
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok", "component": component}

    if component in {"diagnostic", "worker"}:
        executor = ExecutorClient(
            os.environ.get("EXECUTOR_URL", "http://executor:8000") + f"/{component}/mcp",
            os.environ[component.upper() + "_TOKEN"],
        )
        if component == "diagnostic":
            agent = DiagnosticAgent(executor)
        else:
            mode = os.environ.get("PLANNER_MODE", "stub")
            if mode not in {"stub", "gateway"}:
                raise ValueError("PLANNER_MODE must be stub or gateway")
            planner = (
                StubPlanner()
                if mode == "stub"
                else GatewayPlanner(
                    base_url=os.environ.get("GATEWAY_URL", "http://gateway:8008"),
                    model=os.environ["GATEWAY_MODEL"],
                    api_key=os.environ["GATEWAY_API_KEY"],
                    allow_insecure_http=True,
                    timeout=15,
                )
            )
            agent = WorkerAgent(planner, executor)

        agent_token = os.environ["AGENT_TOKEN"]
        if not agent_token:
            raise ValueError("AGENT_TOKEN must be nonempty")

        @app.post("/process")
        async def process(message: AgentMessage, request: Request):
            if not hmac.compare_digest(
                request.headers.get("authorization", "").encode("utf-8"),
                ("Bearer " + agent_token).encode("utf-8"),
            ):
                raise HTTPException(401, "Unauthorized")
            return await agent.process(message)

        app.state.agent = agent
        return app
    if component != "orchestrator":
        raise ValueError("Unknown SPAS_COMPONENT")
    orchestrator = Orchestrator(
        agent_timeout=45,
        cleanup_timeout=5,
        agents={
            RoleEnum.TRIAGE: TriageAgent(),
            RoleEnum.DIAGNOSTIC: HttpAgent(
                RoleEnum.DIAGNOSTIC,
                os.environ.get("DIAGNOSTIC_URL", "http://diagnostic:8000/process"),
                request_timeout=40,
                token=os.environ["DIAGNOSTIC_AGENT_TOKEN"],
            ),
            RoleEnum.REMEDIATION: HttpAgent(
                RoleEnum.REMEDIATION,
                os.environ.get("WORKER_URL", "http://worker:8000/process"),
                request_timeout=40,
                token=os.environ["WORKER_AGENT_TOKEN"],
            ),
        },
    )
    app.state.orchestrator = orchestrator

    class IncidentRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = Field(default="Demo service unavailable", min_length=1, max_length=200)
        description: str = Field(
            default="Controlled simulated outage", min_length=1, max_length=4000
        )

    @app.post("/incidents", status_code=201)
    async def create_incident(body: IncidentRequest):
        if len(orchestrator.storage) >= 10000:
            raise HTTPException(503, "In-memory incident capacity reached")
        return await orchestrator.create_incident(body.title, body.description)

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: UUID):
        if incident_id not in orchestrator.storage:
            raise HTTPException(404, "Unknown incident")
        return orchestrator.storage[incident_id]

    @app.post("/incidents/{incident_id}/process")
    async def process_incident(incident_id: UUID):
        await get_incident(incident_id)
        return await orchestrator.process_incident(incident_id)

    return app
