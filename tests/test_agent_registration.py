import asyncio
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.incidents.models import IncidentStatus
from core.orchestrator.engine import Orchestrator
from shared.contracts import Agent, AgentMessage, AgentResult, RoleEnum, StageEnum


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [RoleEnum.DIAGNOSTIC, RoleEnum.REMEDIATION])
async def test_bundled_agents_reject_messages_for_another_role(role):
    from apps.agent_setup import stub_agents

    message = AgentMessage(
        incident_id=uuid4(),
        receiver=RoleEnum.TRIAGE,
        stage=StageEnum.DIAGNOSIS if role == RoleEnum.DIAGNOSTIC else StageEnum.REMEDIATION,
        task_description="Wrong receiver",
        payload={"root_cause": "A diagnosis"},
    )
    with pytest.raises(ValueError, match="another role"):
        await stub_agents()[role].process(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", [None, "", "   ", 42, True, [], {}])
async def test_bundled_worker_requires_nonblank_text_diagnosis(cause):
    from agents.worker import WorkerAgent

    message = AgentMessage(
        incident_id=uuid4(),
        receiver=RoleEnum.REMEDIATION,
        stage=StageEnum.REMEDIATION,
        task_description="Invalid diagnosis",
        payload={"root_cause": cause},
    )
    with pytest.raises(ValueError, match="diagnosis"):
        await WorkerAgent().process(message)


class IndependentAgent:
    """An external implementation needs no project-specific base class."""

    def __init__(self, role):
        self.role = role
        self.messages = []

    async def process(self, message: AgentMessage) -> AgentResult:
        self.messages.append(message)
        output = {
            StageEnum.TRIAGE: {"severity": "critical"},
            StageEnum.DIAGNOSIS: {"root_cause": "Connection pool exhausted"},
            StageEnum.REMEDIATION: {"action_taken": "Reset connection pool"},
            StageEnum.VERIFICATION: {"resolved": True, "details": "Healthy"},
        }[message.stage]
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            stage=message.stage,
            is_success=True,
            output_data=output,
        )


def independent_agents():
    return {
        role: IndependentAgent(role)
        for role in (RoleEnum.TRIAGE, RoleEnum.DIAGNOSTIC, RoleEnum.REMEDIATION)
    }


@pytest.mark.asyncio
async def test_structural_agents_complete_cycle_without_inheritance():
    agents = independent_agents()
    assert all(isinstance(agent, Agent) for agent in agents.values())
    engine = Orchestrator(agents=agents)
    incident = await engine.create_incident("DB timeout", "Connections exhausted")
    await engine.process_incident(incident.incident_id)
    assert incident.status == IncidentStatus.RESOLVED
    assert [item.stage for item in incident.history] == list(StageEnum)
    assert len(agents[RoleEnum.DIAGNOSTIC].messages) == 2
    assert all(
        isinstance(item, AgentMessage) for agent in agents.values() for item in agent.messages
    )


def test_constructor_detaches_registry_and_exposes_read_only_view():
    agents = independent_agents()
    engine = Orchestrator(agents=agents)
    original = agents[RoleEnum.DIAGNOSTIC]
    agents.clear()
    assert engine.agents[RoleEnum.DIAGNOSTIC] is original
    with pytest.raises(TypeError):
        engine.agents[RoleEnum.DIAGNOSTIC] = IndependentAgent(RoleEnum.DIAGNOSTIC)
    with pytest.raises(TypeError):
        del engine.agents[RoleEnum.DIAGNOSTIC]


def test_registration_replacement_and_removal_are_explicit():
    engine = Orchestrator()
    original = IndependentAgent(RoleEnum.DIAGNOSTIC)
    replacement = IndependentAgent(RoleEnum.DIAGNOSTIC)
    engine.register_agent(original)
    with pytest.raises(ValueError, match="already registered"):
        engine.register_agent(replacement)
    assert engine.agents[RoleEnum.DIAGNOSTIC] is original
    engine.register_agent(replacement, replace=True)
    assert engine.agents[RoleEnum.DIAGNOSTIC] is replacement
    assert engine.unregister_agent(RoleEnum.DIAGNOSTIC) is replacement
    assert not engine.agents


@pytest.mark.parametrize("role", [None, "diagnostic_agent", RoleEnum.ORCHESTRATOR])
def test_registration_rejects_unsupported_roles(role):
    with pytest.raises(ValueError, match="role"):
        Orchestrator().register_agent(IndependentAgent(role))


@pytest.mark.parametrize("role", [None, "diagnostic_agent", RoleEnum.ORCHESTRATOR])
def test_constructor_rejects_unsupported_registry_keys(role):
    with pytest.raises(ValueError, match="role"):
        Orchestrator(agents={role: IndependentAgent(RoleEnum.DIAGNOSTIC)})


@pytest.mark.parametrize("agent_role", [RoleEnum.REMEDIATION, "diagnostic_agent", None])
def test_constructor_rejects_mismatched_or_untyped_agent_role(agent_role):
    with pytest.raises(ValueError, match="role"):
        Orchestrator(agents={RoleEnum.DIAGNOSTIC: IndependentAgent(agent_role)})


@pytest.mark.parametrize("process", [None, lambda message: None])
def test_registration_requires_async_process(process):
    agent = SimpleNamespace(role=RoleEnum.DIAGNOSTIC, process=process)
    with pytest.raises(TypeError, match="async process"):
        Orchestrator().register_agent(agent)


@pytest.mark.asyncio
async def test_missing_agent_does_not_fall_back_to_simulation():
    engine = Orchestrator()
    assert engine.agents == {}
    incident = await engine.create_incident("Failure", "No real agents configured")
    await engine.process_incident(incident.incident_id)
    assert incident.status == IncidentStatus.FAILED
    assert incident.history == []
    assert "No agent registered" in incident.last_error


@pytest.mark.asyncio
async def test_agent_role_is_rechecked_before_starting_a_call():
    agent = IndependentAgent(RoleEnum.TRIAGE)
    engine = Orchestrator(agents={RoleEnum.TRIAGE: agent})
    agent.role = RoleEnum.REMEDIATION
    incident = await engine.create_incident("Failure", "Plugin role changed")
    await engine.execute_next_step(incident.incident_id)
    assert incident.status == IncidentStatus.FAILED
    assert not agent.messages


@pytest.mark.asyncio
async def test_unregister_does_not_cancel_running_agent_or_allow_a_replacement_to_overlap():
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingAgent(IndependentAgent):
        async def process(self, message):
            started.set()
            await release.wait()
            return await super().process(message)

    original = BlockingAgent(RoleEnum.TRIAGE)
    replacement = IndependentAgent(RoleEnum.TRIAGE)
    engine = Orchestrator(agents={RoleEnum.TRIAGE: original})
    incident = await engine.create_incident("DB timeout", "Routing changes")
    active = asyncio.create_task(engine.execute_next_step(incident.incident_id))
    waiter = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert engine.unregister_agent(RoleEnum.TRIAGE) is original
        engine.register_agent(replacement)
        waiter = asyncio.create_task(engine.execute_next_step(incident.incident_id))
        await asyncio.sleep(0)
        assert not active.done() and not waiter.done()
        assert not replacement.messages
        release.set()
        await asyncio.wait_for(asyncio.gather(active, waiter), timeout=1)
        assert len(original.messages) == 1
        assert not replacement.messages
        assert incident.status == IncidentStatus.FAILED  # Diagnostic is intentionally absent.
        assert len(incident.history) == 1
    finally:
        release.set()
        await asyncio.gather(
            active, *([waiter] if waiter is not None else []), return_exceptions=True
        )


@pytest.mark.parametrize(
    ("blocked", "imports"),
    [
        (
            ("agents", "apps", "llm"),
            "from core.orchestrator.engine import Orchestrator; assert not Orchestrator().agents",
        ),
        (
            ("core", "apps", "llm"),
            (
                "from agents.diagnostic import DiagnosticAgent\n"
                "from agents.worker import WorkerAgent\n"
                "from agents.demo import DemoDiagnosticAgent, DemoWorkerAgent, DemoResource\n"
                "from agents.http import HttpAgent\n"
                "from shared.contracts import Agent, RoleEnum\n"
                "assert isinstance(DiagnosticAgent(), Agent)\n"
                "assert isinstance(WorkerAgent(), Agent)\n"
                "assert isinstance(DemoDiagnosticAgent(DemoResource()), Agent)\n"
                "assert isinstance(DemoWorkerAgent(DemoResource()), Agent)\n"
                "assert isinstance(HttpAgent(RoleEnum.DIAGNOSTIC, 'http://localhost'), Agent)"
            ),
        ),
        (
            ("core", "llm"),
            (
                "from apps.demo_service import create_demo_service\n"
                "from agents.demo import DemoResource\n"
                "assert create_demo_service(DemoResource())"
            ),
        ),
    ],
)
def test_dependency_direction_in_a_fresh_interpreter(blocked, imports):
    script = f"""
import importlib.abc
import sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {blocked!r}:
            raise AssertionError("Unexpected dependency: " + fullname)

sys.meta_path.insert(0, Blocker())
{imports}
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
