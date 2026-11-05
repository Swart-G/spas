"""Run a complete incident through local HTTP agents and an explicitly simulated resource."""

import argparse
import asyncio
import json
import socket
from contextlib import asynccontextmanager

import uvicorn

from agents.base import BaseSlaveAgent
from agents.demo import DemoResource
from agents.http import HttpAgent
from apps.demo_service import create_demo_service
from core.incidents.models import IncidentStatus
from core.orchestrator.engine import Orchestrator
from shared.contracts import RoleEnum


@asynccontextmanager
async def local_server(resource: DemoResource):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
        server = uvicorn.Server(
            uvicorn.Config(
                create_demo_service(resource), log_level="error", timeout_graceful_shutdown=1
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("Demo server stopped during startup")
                    await asyncio.sleep(0.01)
            yield f"http://{host}:{port}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)


async def run_demo(*, repair_succeeds: bool = True) -> dict:
    resource = DemoResource(repair_succeeds=repair_succeeds)
    async with local_server(resource) as address:
        engine = Orchestrator(
            agents={
                RoleEnum.TRIAGE: BaseSlaveAgent(RoleEnum.TRIAGE),
                RoleEnum.DIAGNOSTIC: HttpAgent(
                    RoleEnum.DIAGNOSTIC, f"{address}/agents/diagnostic_agent"
                ),
                RoleEnum.REMEDIATION: HttpAgent(
                    RoleEnum.REMEDIATION, f"{address}/agents/remediation_agent"
                ),
            }
        )
        incident = await engine.create_incident(
            "Demo connection pool fault", "Local integration run"
        )
        await engine.process_incident(incident.incident_id)
    return {
        "incident": incident.model_dump(mode="json"),
        "resource_healthy": resource.healthy,
        "repair_calls": resource.repair_calls,
        "agent_calls": resource.calls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repair-fails", action="store_true", help="Demonstrate failed verification"
    )
    args = parser.parse_args()
    report = asyncio.run(run_demo(repair_succeeds=not args.repair_fails))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["incident"]["status"] == IncidentStatus.RESOLVED.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
