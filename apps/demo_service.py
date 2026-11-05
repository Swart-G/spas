"""HTTP host for reference agents, independent of the orchestration library."""

from fastapi import FastAPI, HTTPException

from agents.demo import DemoDiagnosticAgent, DemoResource, DemoWorkerAgent
from shared.contracts import AgentMessage, AgentResult, RoleEnum


def create_demo_service(resource: DemoResource) -> FastAPI:
    app = FastAPI()
    agents = {
        RoleEnum.DIAGNOSTIC: DemoDiagnosticAgent(resource),
        RoleEnum.REMEDIATION: DemoWorkerAgent(resource),
    }

    @app.post("/agents/{role}", response_model=AgentResult)
    async def process(role: RoleEnum, message: AgentMessage) -> AgentResult:
        if role not in agents or message.receiver != role:
            raise HTTPException(400, "Message receiver does not match agent endpoint")
        try:
            return await agents[role].process(message)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return app
