"""Authenticated MCP endpoints, deterministic policy and an isolated demo resource."""

import hmac
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from starlette.responses import JSONResponse

from executer.safety_checker import RuleBasedSafetyChecker, SafetyPolicy
from executer.safety_checker.permissions import AgentRole
from executer.server_mcp.Models import DiagnosticResult, ExecutionResult
from executer.server_mcp.Safety_contract import SafetyDecision
from executer.server_mcp.Server import create_server


class CheckedChain:
    """An optional analyzer can only restrict deterministic ALLOW decisions."""

    def __init__(self, rules, analyzer=None):
        self.rules, self.analyzer = rules, analyzer

    async def check(self, command, context):
        argv = tuple(command)
        decision = SafetyDecision.model_validate(await self.rules.check(list(argv), context))
        if decision.decision == "ALLOW" and self.analyzer is not None:
            return SafetyDecision.model_validate(await self.analyzer.check(list(argv), context))
        return decision


@dataclass
class DemoService:
    """Explicit simulation: no subprocess, host service or Docker access."""

    healthy: bool = False
    executions: int = 0
    repair_succeeds: bool = True

    async def execute(self, argv, *, request_id):
        if argv != ("spas-demo", "repair", "demo-service"):
            return ExecutionResult(status="denied", error="Demo runner supports only repair")
        self.executions += 1
        self.healthy = self.repair_succeeds
        return ExecutionResult(status="succeeded", exit_code=0, stdout="Simulated repair applied")

    def diagnose(self, kind, service, *, lines=100):
        if service != "demo-service":
            return DiagnosticResult(status="denied", kind=kind, service=service)
        outputs = {
            "state": "running" if self.healthy else "stopped",
            "availability": "available" if self.healthy else "unavailable",
            "logs": "Simulated service ready" if self.healthy else "Simulated service stopped",
        }
        return DiagnosticResult(
            status="succeeded",
            kind=kind,
            service=service,
            output=outputs[kind],
            observed_at=time.time(),
        )


def create_executor_app(diagnostic_token, worker_token, *, resource=None, checker=None):
    if not diagnostic_token or not worker_token or diagnostic_token == worker_token:
        raise ValueError("Distinct nonempty Executor credentials are required")
    resource = resource if resource is not None else DemoService()
    rules = (
        checker
        if checker is not None
        else RuleBasedSafetyChecker(SafetyPolicy(demo_simulation=True, containers={"demo-service"}))
    )
    ledger = {}
    servers = {
        role: create_server(
            checker=CheckedChain(rules),
            agent_role=role,
            runner=resource.execute,
            diagnostic_provider=resource.diagnose,
            execution_ledger=ledger if role is AgentRole.WORKER else None,
        )
        for role in AgentRole
    }
    mounted = {
        role: server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            host="0.0.0.0",
            max_request_body_size=65536,
        )
        for role, server in servers.items()
    }

    @asynccontextmanager
    async def lifespan(app):
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            for subapp in mounted.values():
                await stack.enter_async_context(subapp.router.lifespan_context(subapp))
            yield

    app = FastAPI(lifespan=lifespan)
    app.state.resource, app.state.servers, app.state.ledger = resource, servers, ledger

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        role = request.url.path.split("/")[1]
        token = {"diagnostic": diagnostic_token, "worker": worker_token}.get(role)
        if token is None or not hmac.compare_digest(
            request.headers.get("authorization", "").encode("utf-8"),
            ("Bearer " + token).encode("utf-8"),
        ):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "isolated-simulation"}

    for role, subapp in mounted.items():
        app.mount("/" + role.value, subapp)
    return app
