
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

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


@pytest.mark.asyncio
async def test_cancelled_agent_allows_incident_retry(orchestrator, incident):
    orchestrator.agent_timeout = 5
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class CancellableAgent(BaseSlaveAgent):
        async def process(self, message):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    orchestrator.agents[RoleEnum.TRIAGE] = CancellableAgent(RoleEnum.TRIAGE)
    task = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert incident.status == IncidentStatus.INVESTIGATING
    previous_updated_at = incident.updated_at

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert incident.status == IncidentStatus.NEW
    assert incident.updated_at >= previous_updated_at
    assert incident.history == []
    assert incident.metrics_log == {}

    agent = SuccessfulAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent
    await orchestrator.execute_next_step(incident.incident_id)

    assert agent.calls == 1
    assert len(incident.history) == 1
    assert incident.metrics_log["severity"] == "CRITICAL"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_reset_running_incident(orchestrator, incident):
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingAgent(SuccessfulAgent):
        async def process(self, message):
            started.set()
            await release.wait()
            return await super().process(message)

    orchestrator.agent_timeout = 1
    agent = BlockingAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        waiter = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert incident.status == IncidentStatus.INVESTIGATING
        assert not active.done()
    finally:
        release.set()
        await active

    assert agent.calls == 1
    assert len(incident.history) == 1


@pytest.mark.asyncio
async def test_agent_retained_data_cannot_change_history_or_metrics(orchestrator, incident):
    source = {"detected_metrics": {"latencies": [10, {"p99": 50}]}}

    class RetainingAgent(BaseSlaveAgent):
        async def process(self, message):
            self.result = AgentResult(
                message_id=message.message_id,
                incident_id=message.incident_id,
                sender=self.role,
                is_success=True,
                output_data=source,
            )
            return self.result

    agent = RetainingAgent(RoleEnum.TRIAGE)
    orchestrator.agents[RoleEnum.TRIAGE] = agent
    await orchestrator.execute_next_step(incident.incident_id)
    source["detected_metrics"]["latencies"][1]["p99"] = 999

    expected = {"detected_metrics": {"latencies": [10, {"p99": 50}]}}
    assert incident.history[0].output_data == expected
    assert incident.metrics_log == expected
    with pytest.raises(TypeError):
        agent.result.output_data["detected_metrics"]["latencies"].append(999)
    with pytest.raises(TypeError):
        incident.metrics_log["detected_metrics"]["latencies"][1]["p99"] = 999

    previous_metrics = incident.metrics_log
    incident.metrics_log = {**incident.metrics_log, "severity": "CRITICAL"}
    assert previous_metrics == expected
    assert incident.history[0].output_data == expected


@pytest.mark.asyncio
async def test_waiting_execution_retries_after_active_execution_is_cancelled(
    orchestrator, incident
):
    orchestrator.agent_timeout = 5
    started = asyncio.Event()

    class RetryAgent(SuccessfulAgent):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def process(self, message):
            self.attempts += 1
            if self.attempts == 1:
                started.set()
                await asyncio.Event().wait()
            return await super().process(message)

    agent = RetryAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    waiter = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    await asyncio.sleep(0)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    await asyncio.wait_for(waiter, timeout=1)

    assert agent.attempts == 2
    assert agent.calls == 1
    assert len(incident.history) == 1
    assert incident.status == IncidentStatus.INVESTIGATING


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["result", "uncancel", "error"])
async def test_agent_cannot_swallow_caller_cancellation(orchestrator, incident, outcome):
    orchestrator.agent_timeout = 5
    started = asyncio.Event()

    class SuppressingAgent(SuccessfulAgent):
        async def process(self, message):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if outcome == "error":
                    raise RuntimeError("Agent cleanup failed") from None
                if outcome == "uncancel":
                    asyncio.current_task().uncancel()
                return await super().process(message)

    orchestrator.agents[RoleEnum.TRIAGE] = SuppressingAgent()
    task = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Failed cleanup cannot establish that resources are safe for another attempt.
    expected_status = IncidentStatus.FAILED if outcome == "error" else IncidentStatus.NEW
    assert incident.status == expected_status
    assert incident.history == []
    assert incident.metrics_log == {}
    agent = SuccessfulAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent
    await orchestrator.execute_next_step(incident.incident_id)
    assert agent.calls == (0 if outcome == "error" else 1)


@pytest.mark.asyncio
async def test_cancellation_waits_for_agent_cleanup_before_retry(orchestrator, incident):
    orchestrator.agent_timeout = 5
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()

    class CleaningAgent(SuccessfulAgent):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def process(self, message):
            self.attempts += 1
            if self.attempts == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release_cleanup.wait()
                    cleaned.set()
            assert cleaned.is_set()
            return await super().process(message)

    agent = CleaningAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    active.cancel()
    try:
        await asyncio.wait_for(cleaning.wait(), timeout=1)
        waiter = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
        await asyncio.sleep(0)
        assert not active.done()
        assert not waiter.done()
        assert incident.status == IncidentStatus.INVESTIGATING
        assert agent.attempts == 1
    finally:
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await active
    await asyncio.wait_for(waiter, timeout=1)

    assert agent.attempts == 2
    assert agent.calls == 1
    assert len(incident.history) == 1


@pytest.mark.asyncio
async def test_preexisting_cancellation_count_does_not_cancel_new_step(orchestrator, incident):
    agent = SuccessfulAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent

    async def run_after_caught_cancellation():
        task = asyncio.current_task()
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        assert task.cancelling() == 1
        await orchestrator.execute_next_step(incident.incident_id)

    await asyncio.create_task(run_after_caught_cancellation())

    assert agent.calls == 1
    assert incident.status == IncidentStatus.INVESTIGATING
    assert len(incident.history) == 1


@pytest.mark.asyncio
async def test_result_snapshot_is_validated_at_agent_boundary(orchestrator, incident):
    source = {"nested": {"values": [1]}}

    class UpdatingAgent(SuccessfulAgent):
        async def process(self, message):
            result = await super().process(message)
            # Pydantic's model_copy(update=...) deliberately skips validation.
            self.result = result.model_copy(update={"output_data": source})
            return self.result

    agent = UpdatingAgent()
    orchestrator.agents[RoleEnum.TRIAGE] = agent
    await orchestrator.execute_next_step(incident.incident_id)
    source["nested"]["values"].append(999)

    assert incident.history[0].output_data == {"nested": {"values": [1]}}
    assert incident.metrics_log == {"nested": {"values": [1]}}
    assert incident.history[0] is not agent.result
    with pytest.raises(TypeError):
        incident.history[0].output_data["nested"]["values"].append(999)


@pytest.mark.asyncio
async def test_structurally_similar_object_is_not_an_agent_result(orchestrator, incident):
    class UntypedAgent(SuccessfulAgent):
        async def process(self, message):
            result = await super().process(message)
            return SimpleNamespace(**result.model_dump())

    orchestrator.agents[RoleEnum.TRIAGE] = UntypedAgent()
    await orchestrator.execute_next_step(incident.incident_id)

    assert incident.status == IncidentStatus.FAILED
    assert incident.history == []
    assert incident.metrics_log == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("uncancel", [False, True])
async def test_suppressed_timeout_does_not_commit_agent_result(orchestrator, incident, uncancel):
    orchestrator.agent_timeout = 0.01

    class SuppressingAgent(SuccessfulAgent):
        async def process(self, message):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if uncancel:
                    asyncio.current_task().uncancel()
                return await super().process(message)

    orchestrator.agents[RoleEnum.TRIAGE] = SuppressingAgent()
    await asyncio.wait_for(orchestrator.execute_next_step(incident.incident_id), timeout=1)

    assert incident.status == IncidentStatus.FAILED
    assert incident.history == []
    assert incident.metrics_log == {}
