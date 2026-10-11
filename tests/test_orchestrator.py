
import asyncio
from uuid import uuid4

import pytest

from agents.base import BaseSlaveAgent
from core.incidents.models import IncidentStatus
from core.orchestrator.engine import Orchestrator
from shared.contracts.messages import AgentResult, RoleEnum


class SuccessfulAgent(BaseSlaveAgent):
    """Агент, возвращающий успешный результат."""

    def __init__(self):
        super().__init__(RoleEnum.TRIAGE)
        self.calls = 0

    async def process(self, message):
        self.calls += 1
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            is_success=True,
            output_data={
                "severity": "CRITICAL",
                "timeout_ms": 5000,
            },
        )


class FailedAgent(BaseSlaveAgent):
    """Агент, возвращающий бизнес-ошибку."""

    async def process(self, message):
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            is_success=False,
            error_message="Diagnostic service unavailable",
        )


class CrashingAgent(BaseSlaveAgent):
    async def process(self, message):
        raise RuntimeError("Agent crashed")


class HangingAgent(BaseSlaveAgent):
    async def process(self, message):
        await asyncio.Event().wait()


class InvalidCorrelationAgent(BaseSlaveAgent):
    async def process(self, message):
        return AgentResult(
            message_id=uuid4(),  # Неверный message_id
            incident_id=message.incident_id,
            sender=self.role,
            is_success=True,
        )


@pytest.fixture
def orchestrator():
    return Orchestrator(agent_timeout=0.05)


import pytest_asyncio

@pytest_asyncio.fixture
async def incident(orchestrator):
    return await orchestrator.create_incident(
        title="DB timeout",
        description="PostgreSQL connections are dropping",
    )


@pytest.mark.asyncio
async def test_successful_triage_updates_metrics(
    orchestrator, incident
):
    agent = SuccessfulAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent

    await orchestrator.execute_next_step(incident.incident_id)

    updated = orchestrator.storage[incident.incident_id]

    assert updated.status == IncidentStatus.INVESTIGATING
    assert updated.metrics_log["severity"] == "CRITICAL"
    assert updated.metrics_log["timeout_ms"] == 5000
    assert len(updated.history) == 1
    assert updated.history[0].is_success is True
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_failed_result_marks_incident_failed(
    orchestrator, incident
):
    orchestrator.agents[RoleEnum.TRIAGE] = FailedAgent(
        RoleEnum.TRIAGE
    )

    await orchestrator.execute_next_step(incident.incident_id)

    updated = orchestrator.storage[incident.incident_id]

    assert updated.status == IncidentStatus.FAILED
    assert len(updated.history) == 1
    assert updated.history[0].is_success is False
    assert updated.history[0].error_message == (
        "Diagnostic service unavailable"
    )


@pytest.mark.asyncio
async def test_agent_exception_marks_incident_failed(
    orchestrator, incident
):
    orchestrator.agents[RoleEnum.TRIAGE] = CrashingAgent(
        RoleEnum.TRIAGE
    )

    await orchestrator.execute_next_step(incident.incident_id)

    updated = orchestrator.storage[incident.incident_id]

    assert updated.status == IncidentStatus.FAILED
    # Исключение не является AgentResult.
    assert updated.history == []


@pytest.mark.asyncio
async def test_agent_timeout_marks_incident_failed(
    orchestrator, incident
):
    orchestrator.agents[RoleEnum.TRIAGE] = HangingAgent(
        RoleEnum.TRIAGE
    )

    await orchestrator.execute_next_step(incident.incident_id)

    updated = orchestrator.storage[incident.incident_id]

    assert updated.status == IncidentStatus.FAILED
    assert updated.history == []


@pytest.mark.asyncio
async def test_invalid_correlation_marks_incident_failed(
    orchestrator, incident
):
    orchestrator.agents[RoleEnum.TRIAGE] = (
        InvalidCorrelationAgent(RoleEnum.TRIAGE)
    )

    await orchestrator.execute_next_step(incident.incident_id)

    updated = orchestrator.storage[incident.incident_id]

    assert updated.status == IncidentStatus.FAILED
    assert updated.history == []


@pytest.mark.asyncio
async def test_unknown_incident_raises_error(orchestrator):
    with pytest.raises(ValueError, match="Incident not found"):
        await orchestrator.execute_next_step(uuid4())


@pytest.mark.asyncio
async def test_repeated_execution_does_not_call_agent_twice(
    orchestrator, incident
):
    agent = SuccessfulAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent

    await orchestrator.execute_next_step(incident.incident_id)
    await orchestrator.execute_next_step(incident.incident_id)

    assert agent.calls == 1
    assert len(incident.history) == 1


@pytest.mark.asyncio
async def test_concurrent_execution_calls_agent_once(
    orchestrator, incident
):
    class SlowAgent(SuccessfulAgent):
        async def process(self, message):
            await asyncio.sleep(0.02)
            return await super().process(message)

    agent = SlowAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent

    await asyncio.gather(
        orchestrator.execute_next_step(incident.incident_id),
        orchestrator.execute_next_step(incident.incident_id),
    )

    assert agent.calls == 1
    assert len(incident.history) == 1
    assert incident.status == IncidentStatus.INVESTIGATING
