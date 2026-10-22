import asyncio
import gc
import time
import weakref

import pytest

from agents.base import BaseSlaveAgent
from core.incidents.models import IncidentStatus
from core.orchestrator.engine import Orchestrator
from shared.contracts.messages import AgentResult, RoleEnum


class ResourceAgent(BaseSlaveAgent):
    """A resource close operation must finish before another attempt starts."""

    def __init__(self):
        super().__init__(RoleEnum.TRIAGE)
        self.started = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.closed = asyncio.Event()
        self.cleanup_interrupted = asyncio.Event()
        self.task = None
        self.cleanup_task = None
        self.calls = 0

    async def close_resource(self):
        await self.release_cleanup.wait()
        self.closed.set()

    async def process(self, message):
        self.calls += 1
        if self.calls > 1:
            assert self.closed.is_set(), "A new attempt overlapped resource cleanup"
            return AgentResult(
                message_id=message.message_id,
                incident_id=message.incident_id,
                sender=self.role,
                is_success=True,
                output_data={"retry": True},
            )

        self.task = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaning.set()
            self.cleanup_task = asyncio.create_task(self.close_resource())
            try:
                await asyncio.shield(self.cleanup_task)
            except asyncio.CancelledError:
                self.cleanup_interrupted.set()
                raise


async def make_incident(agent_timeout=1, cleanup_timeout=1):
    orchestrator = Orchestrator(agent_timeout=agent_timeout, cleanup_timeout=cleanup_timeout)
    agent = ResourceAgent()
    orchestrator.register_agent(agent, replace=True)
    incident = await orchestrator.create_incident("Timeout", "Resource lifecycle")
    return orchestrator, agent, incident


async def drain(agent, *callers):
    agent.release_cleanup.set()
    tasks = [task for task in (*callers, agent.task, agent.cleanup_task) if task is not None]
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)


@pytest.mark.asyncio
async def test_execution_deadline_cannot_interrupt_cancelled_agent_cleanup():
    orchestrator, agent, incident = await make_incident(0.03, 1)
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    callers = [active]
    try:
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        active.cancel()
        await asyncio.wait_for(agent.cleaning.wait(), timeout=1)
        retry = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
        callers.append(retry)

        deadline_passed = asyncio.Event()
        asyncio.get_running_loop().call_later(0.06, deadline_passed.set)
        await asyncio.wait_for(deadline_passed.wait(), timeout=1)

        assert not agent.cleanup_interrupted.is_set()
        assert not active.done()
        assert not retry.done()
        assert incident.status == IncidentStatus.INVESTIGATING
        assert agent.calls == 1

        agent.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await active
        await asyncio.wait_for(retry, timeout=1)
        assert agent.task.done()
        assert agent.closed.is_set()
        assert agent.calls == 2
        assert len(incident.history) == 1
    finally:
        await drain(agent, *callers)


@pytest.mark.asyncio
async def test_repeated_caller_cancellation_does_not_interrupt_cleanup():
    orchestrator, agent, incident = await make_incident()
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        active.cancel()
        await asyncio.wait_for(agent.cleaning.wait(), timeout=1)
        active.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not agent.cleanup_interrupted.is_set()
        assert not active.done()
        assert incident.status == IncidentStatus.INVESTIGATING

        agent.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert agent.task.done()
        assert agent.closed.is_set()
        assert incident.status == IncidentStatus.NEW
        await orchestrator.execute_next_step(incident.incident_id)
        assert agent.calls == 2
    finally:
        await drain(agent, active)


@pytest.mark.asyncio
async def test_execution_timeout_waits_for_cleanup_outside_execution_deadline():
    orchestrator, agent, incident = await make_incident(0.01, 1)
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(agent.cleaning.wait(), timeout=1)
        assert not active.done()
        assert incident.status == IncidentStatus.INVESTIGATING
        assert not agent.cleanup_interrupted.is_set()

        agent.release_cleanup.set()
        await asyncio.wait_for(active, timeout=1)
        assert agent.task.done()
        assert agent.closed.is_set()
        assert incident.status == IncidentStatus.FAILED
        assert incident.history == []
        await orchestrator.execute_next_step(incident.incident_id)
        assert agent.calls == 1
    finally:
        await drain(agent, active)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cancel", "timeout"])
async def test_cleanup_timeout_blocks_retry_while_agent_is_still_running(reason):
    orchestrator, agent, incident = await make_incident(
        agent_timeout=1 if reason == "cancel" else 0.01,
        cleanup_timeout=0.03,
    )
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        if reason == "cancel":
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(active), timeout=0.2)
        else:
            await asyncio.wait_for(asyncio.shield(active), timeout=0.2)

        assert not agent.task.done()
        assert not agent.closed.is_set()
        assert not agent.cleanup_interrupted.is_set()
        assert incident.status == IncidentStatus.FAILED
        assert incident.history == []
        assert incident.metrics_log == {}
        await orchestrator.execute_next_step(incident.incident_id)
        assert agent.calls == 1

        # Even a direct external status overwrite must not overlap an active agent.
        incident.status = IncidentStatus.NEW
        await orchestrator.execute_next_step(incident.incident_id)
        assert agent.calls == 1
        incident.status = IncidentStatus.FAILED

        agent.release_cleanup.set()
        await asyncio.wait_for(asyncio.gather(agent.task, return_exceptions=True), timeout=1)
        await orchestrator.execute_next_step(incident.incident_id)
        assert agent.calls == 1
        assert incident.status == IncidentStatus.FAILED
    finally:
        await drain(agent, active)


@pytest.mark.asyncio
async def test_cancellation_during_timeout_cleanup_does_not_cancel_agent_twice():
    orchestrator, agent, incident = await make_incident(0.01, 1)
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(agent.cleaning.wait(), timeout=1)
        active.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not agent.cleanup_interrupted.is_set()
        assert not active.done()
        agent.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert agent.closed.is_set()
        assert agent.task.done()
        assert incident.status == IncidentStatus.FAILED
    finally:
        await drain(agent, active)


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_extend_cleanup_deadline():
    orchestrator, agent, incident = await make_incident(1, 0.04)
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    repeater = None
    try:
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        active.cancel()
        await asyncio.wait_for(agent.cleaning.wait(), timeout=1)

        async def repeat_cancellation():
            while not active.done():
                active.cancel()
                await asyncio.sleep(0.005)

        repeater = asyncio.create_task(repeat_cancellation())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(active), timeout=0.3)

        assert incident.status == IncidentStatus.FAILED
        assert not agent.task.done()
        assert not agent.cleanup_interrupted.is_set()
        await orchestrator.execute_next_step(incident.incident_id)
        assert agent.calls == 1
    finally:
        if repeater is not None:
            repeater.cancel()
            await asyncio.gather(repeater, return_exceptions=True)
        await drain(agent, active)


@pytest.mark.asyncio
async def test_already_cancelled_agent_cleanup_is_not_cancelled_again():
    orchestrator = Orchestrator(agent_timeout=0.01, cleanup_timeout=1)

    class SelfCancellingAgent(ResourceAgent):
        async def process(self, message):
            asyncio.current_task().cancel()
            return await super().process(message)

    agent = SelfCancellingAgent()
    orchestrator.register_agent(agent, replace=True)
    incident = await orchestrator.create_incident("Timeout", "Self cancellation")
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(agent.cleaning.wait(), timeout=1)
        deadline_passed = asyncio.Event()
        asyncio.get_running_loop().call_later(0.03, deadline_passed.set)
        await asyncio.wait_for(deadline_passed.wait(), timeout=1)

        assert not agent.cleanup_interrupted.is_set()
        assert not active.done()
        agent.release_cleanup.set()
        await asyncio.wait_for(active, timeout=1)
        assert agent.closed.is_set()
        assert agent.task.done()
        assert incident.status == IncidentStatus.FAILED
    finally:
        await drain(agent, active)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["result", "error"])
async def test_late_completion_after_cleanup_timeout_never_commits_a_result(outcome):
    orchestrator = Orchestrator(agent_timeout=1, cleanup_timeout=0.02)
    started = asyncio.Event()
    release = asyncio.Event()

    class LateAgent(BaseSlaveAgent):
        async def process(self, message):
            self.task = asyncio.current_task()
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                if outcome == "error":
                    raise RuntimeError("Late cleanup failure") from None
                return AgentResult(
                    message_id=message.message_id,
                    incident_id=message.incident_id,
                    sender=self.role,
                    is_success=True,
                    output_data={"late": True},
                )

    agent = LateAgent(RoleEnum.TRIAGE)
    orchestrator.register_agent(agent, replace=True)
    incident = await orchestrator.create_incident("Timeout", "Late result")
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(active), timeout=0.2)

        assert incident.status == IncidentStatus.FAILED
        assert not agent.task.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(agent.task, return_exceptions=True), timeout=1)
        await orchestrator.execute_next_step(incident.incident_id)
        assert incident.status == IncidentStatus.FAILED
        assert incident.history == []
        assert incident.metrics_log == {}
    finally:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(active, agent.task, return_exceptions=True), timeout=1
        )


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_cleanup_timeout_must_be_positive_and_finite(timeout):
    with pytest.raises(ValueError, match="cleanup_timeout"):
        Orchestrator(cleanup_timeout=timeout)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_execution_timeout_must_be_finite(timeout):
    with pytest.raises(ValueError, match="agent_timeout"):
        Orchestrator(agent_timeout=timeout)


@pytest.mark.asyncio
async def test_cleanup_failure_blocks_retry_even_when_agent_task_has_finished():
    orchestrator = Orchestrator(agent_timeout=1, cleanup_timeout=1)

    class FailingCleanupAgent(ResourceAgent):
        async def close_resource(self):
            await self.release_cleanup.wait()
            raise RuntimeError("Resource release failed")

    agent = FailingCleanupAgent()
    orchestrator.register_agent(agent, replace=True)
    incident = await orchestrator.create_incident("Failure", "Cleanup failure")
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        active.cancel()
        await asyncio.wait_for(agent.cleaning.wait(), timeout=1)
        agent.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await active

        assert agent.task.done()
        assert not agent.closed.is_set()
        assert incident.status == IncidentStatus.FAILED
        await orchestrator.execute_next_step(incident.incident_id)
        assert agent.calls == 1
        assert incident.history == []
    finally:
        await drain(agent, active)


@pytest.mark.asyncio
async def test_late_cleanup_confirmation_blocks_retry_after_event_loop_delay():
    orchestrator = Orchestrator(agent_timeout=1, cleanup_timeout=0.005)

    class BlockingCleanupAgent(ResourceAgent):
        async def close_resource(self):
            # Simulate a synchronous resource close that delays timeout callbacks.
            time.sleep(0.03)  # noqa: ASYNC251 - deliberately delay timeout delivery
            self.closed.set()

    agent = BlockingCleanupAgent()
    orchestrator.register_agent(agent, replace=True)
    incident = await orchestrator.create_incident("Delay", "Blocking resource close")
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active

        assert agent.closed.is_set()
        assert agent.task.done()
        assert incident.status == IncidentStatus.FAILED
        await orchestrator.execute_next_step(incident.incident_id)
        assert agent.calls == 1
    finally:
        await drain(agent, active)


@pytest.mark.asyncio
async def test_message_preparation_failure_does_not_leave_incident_investigating(monkeypatch):
    orchestrator, agent, incident = await make_incident()

    def fail_message(**kwargs):
        raise ValueError("Invalid task message")

    monkeypatch.setattr("core.orchestrator.engine.AgentMessage", fail_message)
    await orchestrator.execute_next_step(incident.incident_id)

    assert incident.status == IncidentStatus.FAILED
    assert not agent.started.is_set()
    assert incident.history == []


@pytest.mark.asyncio
@pytest.mark.parametrize("eager", [False, True])
async def test_execution_deadline_includes_eager_start_and_rejects_blocking_late_result(eager):
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if eager:
        if not hasattr(asyncio, "eager_task_factory"):
            pytest.skip("Eager task execution requires Python 3.12+")
        loop.set_task_factory(asyncio.eager_task_factory)

    class BlockingAgent(BaseSlaveAgent):
        async def process(self, message):
            time.sleep(0.03)  # noqa: ASYNC251 - deliberately delay timeout delivery
            return AgentResult(
                message_id=message.message_id,
                incident_id=message.incident_id,
                sender=self.role,
                is_success=True,
                output_data={"late": True},
            )

    try:
        orchestrator = Orchestrator(agent_timeout=0.005)
        orchestrator.register_agent(BlockingAgent(RoleEnum.TRIAGE), replace=True)
        incident = await orchestrator.create_incident("Delay", "Blocking agent execution")
        await orchestrator.execute_next_step(incident.incident_id)

        assert incident.status == IncidentStatus.FAILED
        assert incident.history == []
        assert incident.metrics_log == {}
    finally:
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("name", ["agent_timeout", "cleanup_timeout"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_runtime_timeout_changes_cannot_disable_deadlines(name, value):
    orchestrator = Orchestrator(agent_timeout=1, cleanup_timeout=1)
    with pytest.raises(ValueError, match=name):
        setattr(orchestrator, name, value)
    assert getattr(orchestrator, name) == 1


@pytest.mark.asyncio
async def test_timeout_configuration_change_does_not_extend_cleanup_already_in_progress():
    orchestrator, agent, incident = await make_incident(1, 0.03)
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        active.cancel()
        await asyncio.wait_for(agent.cleaning.wait(), timeout=1)
        orchestrator.cleanup_timeout = 5

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(active), timeout=0.2)
        assert incident.status == IncidentStatus.FAILED
        assert not agent.task.done()
        assert not agent.cleanup_interrupted.is_set()
    finally:
        await drain(agent, active)


@pytest.mark.asyncio
async def test_unawaited_late_cleanup_error_is_observed_and_agent_task_is_released():
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _, context: errors.append(context))
    orchestrator = Orchestrator(agent_timeout=1, cleanup_timeout=0.01)
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class LateFailureAgent(BaseSlaveAgent):
        async def process(self, message):
            self.task = asyncio.current_task()
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise RuntimeError("Late resource-close failure") from None

    agent = LateFailureAgent(RoleEnum.TRIAGE)
    orchestrator.register_agent(agent, replace=True)
    incident = await orchestrator.create_incident("Late failure", "Exception observation")
    active = asyncio.create_task(orchestrator.execute_next_step(incident.incident_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        reference = weakref.ref(agent.task)
        agent.task.add_done_callback(lambda _: finished.set())
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert incident.status == IncidentStatus.FAILED
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)

        # Do not await the failed task: its done callback must observe the error.
        agent.task = None
        del active
        # Let callbacks and their task references leave the event-loop queue
        # before collecting traceback cycles (including on Python 3.14).
        await asyncio.sleep(0)
        gc.collect()
        assert reference() is None
        assert errors == []
        assert incident.history == []
    finally:
        release.set()
        if getattr(agent, "task", None) is not None:
            await asyncio.gather(agent.task, return_exceptions=True)
        loop.set_exception_handler(previous_handler)
