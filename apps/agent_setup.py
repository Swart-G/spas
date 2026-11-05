"""Compose the bundled stubs outside the orchestration library."""

from agents.base import BaseSlaveAgent
from agents.diagnostic import DiagnosticAgent
from agents.worker import WorkerAgent
from core.orchestrator.engine import Orchestrator
from shared.contracts import Agent, RoleEnum


def stub_agents() -> dict[RoleEnum, Agent]:
    """Fresh stub instances for an explicitly simulated run."""
    return {
        RoleEnum.TRIAGE: BaseSlaveAgent(RoleEnum.TRIAGE),
        RoleEnum.DIAGNOSTIC: DiagnosticAgent(),
        RoleEnum.REMEDIATION: WorkerAgent(),
    }


def create_stub_orchestrator(
    *, agent_timeout: float = 10.0, cleanup_timeout: float = 10.0
) -> Orchestrator:
    return Orchestrator(
        agent_timeout=agent_timeout,
        cleanup_timeout=cleanup_timeout,
        agents=stub_agents(),
    )
