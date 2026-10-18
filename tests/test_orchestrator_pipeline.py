import asyncio
from uuid import uuid4

import pytest

from agents.base import BaseSlaveAgent
from core.incidents.models import Incident, IncidentStatus
from core.orchestrator.engine import Orchestrator
from shared.contracts.messages import AgentResult, RoleEnum, StageEnum

STAGES = list(StageEnum)
OUTPUTS = {
    StageEnum.TRIAGE: {"severity": "critical"},
    StageEnum.DIAGNOSIS: {
        "root_cause": "Connection pool exhausted",
        "metrics": {"connections": 100},
    },
    StageEnum.REMEDIATION: {"action_taken": "Reset connection pool"},
    StageEnum.VERIFICATION: {
        "resolved": True,
        "details": "Health check passed",
        "metrics": {"connections": 10},
    },
}


class RecordingAgent(BaseSlaveAgent):
    def __init__(self, role, messages):
        super().__init__(role)
        self.messages = messages

    async def process(self, message):
        self.messages.append(message)
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            stage=message.stage,
            is_success=True,
            output_data=OUTPUTS[message.stage],
        )


async def setup_pipeline():
    engine = Orchestrator(agent_timeout=1, cleanup_timeout=1)
    messages = []
    for role in (RoleEnum.TRIAGE, RoleEnum.DIAGNOSTIC, RoleEnum.REMEDIATION):
        engine.agents[role] = RecordingAgent(role, messages)
    incident = await engine.create_incident("DB timeout", "Connections exhausted")
    return engine, incident, messages


def role_for(stage):
    return {
        StageEnum.TRIAGE: RoleEnum.TRIAGE,
        StageEnum.DIAGNOSIS: RoleEnum.DIAGNOSTIC,
        StageEnum.REMEDIATION: RoleEnum.REMEDIATION,
        StageEnum.VERIFICATION: RoleEnum.DIAGNOSTIC,
    }[stage]


async def advance_to(engine, incident, stage):
    for _ in STAGES[: STAGES.index(stage)]:
        await engine.execute_next_step(incident.incident_id)
    assert incident.next_stage == stage


@pytest.mark.asyncio
async def test_full_cycle_passes_context_and_resolves_only_after_verification():
    engine, incident, messages = await setup_pipeline()
    result = await engine.process_incident(incident.incident_id)

    assert result is incident
    assert incident.status == IncidentStatus.RESOLVED
    assert incident.next_stage is None
    assert [message.stage for message in messages] == STAGES
    assert [result.stage for result in incident.history] == STAGES
    assert [message.receiver for message in messages] == [
        role_for(stage) for stage in STAGES
    ]
    assert len({message.message_id for message in messages}) == 4
    assert incident.metrics_log == {"severity": "critical", "connections": 10}
    assert incident.root_cause == OUTPUTS[StageEnum.DIAGNOSIS]["root_cause"]
    assert incident.action_taken == OUTPUTS[StageEnum.REMEDIATION]["action_taken"]
    assert incident.verification["resolved"] is True
    assert incident.last_error is None

    for index, message in enumerate(messages):
        assert message.payload["text"] == incident.description
        assert message.payload["title"] == incident.title
        assert len(message.payload["history"]) == index
        assert message.payload["history"] == [
            item.model_dump(mode="json") for item in incident.history[:index]
        ]
    assert messages[1].payload["metrics"] == {"severity": "critical"}
    assert messages[2].payload["root_cause"] == incident.root_cause
    assert messages[3].payload["action_taken"] == incident.action_taken
    assert messages[2].payload["metrics"]["connections"] == 100
    assert messages[3].payload["metrics"]["connections"] == 100
    with pytest.raises(TypeError):
        messages[3].payload["history"][0]["output_data"]["severity"] = "changed"
    with pytest.raises(TypeError):
        incident.verification["resolved"] = False


@pytest.mark.asyncio
async def test_steps_follow_status_transitions_and_resume_mid_cycle():
    engine, incident, messages = await setup_pipeline()
    for expected_status, expected_stage in [
        (IncidentStatus.INVESTIGATING, StageEnum.DIAGNOSIS),
        (IncidentStatus.REMEDIATING, StageEnum.REMEDIATION),
        (IncidentStatus.VERIFYING, StageEnum.VERIFICATION),
    ]:
        await engine.execute_next_step(incident.incident_id)
        assert incident.status == expected_status
        assert incident.next_stage == expected_stage
    await engine.process_incident(incident.incident_id)
    assert incident.status == IncidentStatus.RESOLVED
    assert len(messages) == 4
    await engine.process_incident(incident.incident_id)
    await engine.execute_next_step(incident.incident_id)
    assert len(messages) == 4


@pytest.mark.asyncio
async def test_concurrent_full_cycles_call_each_stage_once():
    engine, incident, messages = await setup_pipeline()
    await asyncio.gather(
        *(engine.process_incident(incident.incident_id) for _ in range(5))
    )
    assert incident.status == IncidentStatus.RESOLVED
    assert [message.stage for message in messages] == STAGES


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(
    "failure",
    [
        "exception",
        "business",
        "timeout",
        "correlation",
        "incident",
        "sender",
        "receiver",
        "stage",
    ],
)
async def test_stage_failures_stop_pipeline_without_repeating_actions(stage, failure):
    engine, incident, messages = await setup_pipeline()
    await advance_to(engine, incident, stage)
    engine.agent_timeout = 0.01

    class FailingAgent(RecordingAgent):
        async def process(self, message):
            result = await super().process(message)
            if failure == "exception":
                raise RuntimeError("Agent failed")
            if failure == "business":
                return result.model_copy(
                    update={"is_success": False, "error_message": "Service unavailable"}
                )
            if failure == "timeout":
                await asyncio.Event().wait()
            if failure == "correlation":
                return result.model_copy(update={"message_id": uuid4()})
            if failure == "incident":
                return result.model_copy(update={"incident_id": uuid4()})
            if failure == "sender":
                return result.model_copy(update={"sender": RoleEnum.ORCHESTRATOR})
            if failure == "receiver":
                return result.model_copy(update={"receiver": RoleEnum.TRIAGE})
            if failure == "stage":
                wrong_stage = (
                    StageEnum.VERIFICATION
                    if stage == StageEnum.TRIAGE
                    else StageEnum.TRIAGE
                )
                return result.model_copy(update={"stage": wrong_stage})

    role = role_for(stage)
    engine.agents[role] = FailingAgent(role, messages)
    await engine.process_incident(incident.incident_id)

    assert incident.status == IncidentStatus.FAILED
    assert incident.next_stage == stage
    assert incident.last_error
    index = STAGES.index(stage)
    assert len(incident.history) == index + (failure == "business")
    assert [message.stage for message in messages] == STAGES[: index + 1]
    assert incident.verification == {}
    await engine.process_incident(incident.incident_id)
    assert len(messages) == index + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "output"),
    [
        (StageEnum.DIAGNOSIS, {}),
        (StageEnum.DIAGNOSIS, {"root_cause": "   "}),
        (StageEnum.DIAGNOSIS, {"root_cause": 42}),
        (StageEnum.REMEDIATION, {}),
        (StageEnum.REMEDIATION, {"action_taken": ""}),
        (StageEnum.REMEDIATION, {"action_taken": "Done", "resolved": True}),
        (StageEnum.VERIFICATION, {"resolved": "false", "details": "Wrong type"}),
        (StageEnum.VERIFICATION, {"resolved": 1, "details": "Wrong type"}),
        (StageEnum.VERIFICATION, {"resolved": True}),
        (StageEnum.VERIFICATION, {"resolved": True, "details": "  "}),
    ],
)
async def test_invalid_success_output_does_not_commit_partial_state(stage, output):
    engine, incident, messages = await setup_pipeline()
    await advance_to(engine, incident, stage)
    previous = incident.model_dump()

    class InvalidAgent(RecordingAgent):
        async def process(self, message):
            result = await super().process(message)
            return result.model_copy(update={"output_data": output})

    engine.agents[role_for(stage)] = InvalidAgent(role_for(stage), messages)
    await engine.process_incident(incident.incident_id)
    assert incident.status == IncidentStatus.FAILED
    assert incident.last_error
    current = incident.model_dump()
    for field in (
        "history",
        "metrics_log",
        "root_cause",
        "action_taken",
        "verification",
        "next_stage",
    ):
        assert current[field] == previous[field], field


@pytest.mark.asyncio
async def test_negative_verification_records_evidence_and_never_repeats_worker():
    engine, incident, messages = await setup_pipeline()
    await advance_to(engine, incident, StageEnum.VERIFICATION)

    class NegativeVerification(RecordingAgent):
        async def process(self, message):
            result = await super().process(message)
            return result.model_copy(
                update={
                    "output_data": {
                        "resolved": False,
                        "details": "Service still unhealthy",
                    }
                }
            )

    engine.agents[RoleEnum.DIAGNOSTIC] = NegativeVerification(
        RoleEnum.DIAGNOSTIC, messages
    )
    await engine.process_incident(incident.incident_id)
    assert incident.status == IncidentStatus.FAILED
    assert incident.verification["resolved"] is False
    assert incident.last_error == "Service still unhealthy"
    assert len(incident.history) == 4
    await engine.process_incident(incident.incident_id)
    assert [message.stage for message in messages] == STAGES


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("cleanup_expires", [False, True])
async def test_cancellation_retains_stage_and_never_repeats_uncertain_worker(
    stage, cleanup_expires
):
    engine, incident, messages = await setup_pipeline()
    await advance_to(engine, incident, stage)
    previous_status = incident.status
    old_history = list(incident.history)
    engine.cleanup_timeout = 0.02 if cleanup_expires else 1
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    agent_task = None
    retry = None

    class CleaningAgent(RecordingAgent):
        async def process(self, message):
            nonlocal agent_task
            agent_task = asyncio.current_task()
            self.messages.append(message)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

    role = role_for(stage)
    engine.agents[role] = CleaningAgent(role, messages)
    active = asyncio.create_task(engine.process_incident(incident.incident_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        active.cancel()
        await asyncio.wait_for(cleaning.wait(), timeout=1)
        active.cancel()
        await asyncio.sleep(0)
        assert not agent_task.done()
        retry = asyncio.create_task(engine.process_incident(incident.incident_id))
        await asyncio.sleep(0)
        assert not retry.done()
        if not cleanup_expires:
            # The queued retry will use a healthy replacement only after cleanup.
            engine.agents[role] = RecordingAgent(role, messages)
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(active), timeout=1)
        if cleanup_expires or stage == StageEnum.REMEDIATION:
            assert incident.status == IncidentStatus.FAILED
            assert incident.next_stage == stage
            assert incident.history == old_history
            await asyncio.wait_for(retry, timeout=1)
            assert len(messages) == STAGES.index(stage) + 1
        else:
            # Resume from the interrupted read-only stage, not from Triage.
            await asyncio.wait_for(retry, timeout=1)
            assert incident.status == IncidentStatus.RESOLVED
            assert [message.stage for message in messages].count(stage) == 2
            assert [message.stage for message in messages].count(
                StageEnum.REMEDIATION
            ) == 1
        if cleanup_expires:
            assert not agent_task.done()
            release.set()
            await asyncio.gather(agent_task, return_exceptions=True)
            await engine.process_incident(incident.incident_id)
            assert incident.status == IncidentStatus.FAILED
        elif stage == StageEnum.TRIAGE:
            assert previous_status == IncidentStatus.NEW
    finally:
        release.set()
        await asyncio.gather(active, agent_task, return_exceptions=True)
        if retry is not None:
            if not retry.done():
                retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)


@pytest.mark.asyncio
async def test_default_stubs_complete_a_cycle():
    engine = Orchestrator()
    incident = await engine.create_incident("DB timeout", "Service unavailable")
    await engine.process_incident(incident.incident_id)
    assert incident.status == IncidentStatus.RESOLVED
    assert incident.root_cause and incident.action_taken
    assert incident.verification["resolved"] is True
    assert [result.stage for result in incident.history] == STAGES


@pytest.mark.asyncio
async def test_unknown_incident_in_full_cycle_raises():
    with pytest.raises(ValueError, match="Incident not found"):
        await Orchestrator().process_incident(uuid4())


@pytest.mark.asyncio
async def test_missing_agent_fails_without_starting_later_stages():
    engine, incident, messages = await setup_pipeline()
    del engine.agents[RoleEnum.DIAGNOSTIC]
    await engine.process_incident(incident.incident_id)
    assert incident.status == IncidentStatus.FAILED
    assert len(incident.history) == 1
    assert len(messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "status", "cause", "action"),
    [
        (StageEnum.REMEDIATION, IncidentStatus.NEW, None, None),
        (None, IncidentStatus.NEW, None, None),
        (StageEnum.REMEDIATION, IncidentStatus.REMEDIATING, None, None),
        (StageEnum.REMEDIATION, IncidentStatus.REMEDIATING, "   ", None),
        (StageEnum.VERIFICATION, IncidentStatus.VERIFYING, None, "Action"),
        (StageEnum.VERIFICATION, IncidentStatus.VERIFYING, "Cause", None),
        (StageEnum.VERIFICATION, IncidentStatus.VERIFYING, "Cause", "   "),
    ],
)
async def test_inconsistent_state_fails_before_calling_an_agent(
    stage, status, cause, action
):
    engine, incident, messages = await setup_pipeline()
    incident.next_stage = stage
    incident.status = status
    incident.root_cause = cause
    incident.action_taken = action
    await engine.process_incident(incident.incident_id)
    assert incident.status == IncidentStatus.FAILED
    assert incident.last_error
    assert not messages


@pytest.mark.asyncio
async def test_two_incidents_complete_independently():
    engine, first, messages = await setup_pipeline()
    second = await engine.create_incident("Other service", "Independent context")
    await asyncio.gather(
        engine.process_incident(first.incident_id),
        engine.process_incident(second.incident_id),
    )
    for incident in (first, second):
        assert incident.status == IncidentStatus.RESOLVED
        own = [
            message
            for message in messages
            if message.incident_id == incident.incident_id
        ]
        assert [message.stage for message in own] == STAGES
        assert all(message.payload["title"] == incident.title for message in own)
        assert all(
            result.incident_id == incident.incident_id for result in incident.history
        )


@pytest.mark.asyncio
async def test_cancelling_full_cycle_waiter_does_not_affect_active_cycle():
    engine, incident, messages = await setup_pipeline()
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingDiagnostic(RecordingAgent):
        async def process(self, message):
            if message.stage == StageEnum.DIAGNOSIS:
                started.set()
                await release.wait()
            return await super().process(message)

    engine.agents[RoleEnum.DIAGNOSTIC] = BlockingDiagnostic(
        RoleEnum.DIAGNOSTIC, messages
    )
    active = asyncio.create_task(engine.process_incident(incident.incident_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        waiter = asyncio.create_task(engine.process_incident(incident.incident_id))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not active.done()
        assert incident.status == IncidentStatus.INVESTIGATING
        release.set()
        await asyncio.wait_for(active, timeout=1)
        assert incident.status == IncidentStatus.RESOLVED
        assert [message.stage for message in messages] == STAGES
    finally:
        release.set()
        await asyncio.gather(active, return_exceptions=True)


@pytest.mark.asyncio
async def test_incident_round_trip_retains_stage_and_immutable_verification():
    engine, incident, messages = await setup_pipeline()
    await advance_to(engine, incident, StageEnum.VERIFICATION)
    restored = Incident.model_validate_json(incident.model_dump_json())
    assert restored == incident
    engine.storage[incident.incident_id] = restored
    await engine.process_incident(incident.incident_id)
    assert restored.status == IncidentStatus.RESOLVED
    assert len(messages) == 4
    final = Incident.model_validate_json(restored.model_dump_json())
    assert final == restored
    with pytest.raises(TypeError):
        final.verification["resolved"] = False


@pytest.mark.asyncio
async def test_eager_agents_complete_each_stage_once():
    if not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("Eager execution requires Python 3.12+")
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        engine, incident, messages = await setup_pipeline()
        await engine.process_incident(incident.incident_id)
        assert incident.status == IncidentStatus.RESOLVED
        assert [message.stage for message in messages] == STAGES
    finally:
        loop.set_task_factory(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", [StageEnum.DIAGNOSIS, StageEnum.REMEDIATION, StageEnum.VERIFICATION]
)
@pytest.mark.parametrize("outcome", ["result", "error"])
async def test_late_stage_completion_cannot_commit_after_cleanup_timeout(
    stage, outcome
):
    engine, incident, messages = await setup_pipeline()
    await advance_to(engine, incident, stage)
    previous = incident.model_dump()
    engine.cleanup_timeout = 0.01
    started = asyncio.Event()
    release = asyncio.Event()
    agent_task = None

    class LateAgent(RecordingAgent):
        async def process(self, message):
            nonlocal agent_task
            agent_task = asyncio.current_task()
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                if outcome == "error":
                    raise RuntimeError("Late cleanup failure") from None
                return await super().process(message)

    engine.agents[role_for(stage)] = LateAgent(role_for(stage), messages)
    active = asyncio.create_task(engine.process_incident(incident.incident_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(active), timeout=1)
        assert incident.status == IncidentStatus.FAILED
        release.set()
        await asyncio.gather(agent_task, return_exceptions=True)
        await engine.process_incident(incident.incident_id)
        current = incident.model_dump()
        assert incident.status == IncidentStatus.FAILED
        for field in (
            "history",
            "metrics_log",
            "root_cause",
            "action_taken",
            "verification",
            "next_stage",
        ):
            assert current[field] == previous[field], field
    finally:
        release.set()
        await asyncio.gather(active, agent_task, return_exceptions=True)
