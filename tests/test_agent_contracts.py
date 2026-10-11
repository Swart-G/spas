
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum


def test_agent_message_generates_unique_ids():
    incident_id = uuid4()

    first = AgentMessage(
        incident_id=incident_id,
        receiver=RoleEnum.TRIAGE,
        task_description="Analyze incident",
    )
    second = AgentMessage(
        incident_id=incident_id,
        receiver=RoleEnum.TRIAGE,
        task_description="Analyze incident",
    )

    assert first.message_id != second.message_id
    assert first.incident_id == second.incident_id
    assert first.sender == RoleEnum.ORCHESTRATOR


def test_agent_message_is_immutable():
    message = AgentMessage(
        incident_id=uuid4(),
        receiver=RoleEnum.TRIAGE,
        task_description="Analyze incident",
    )

    with pytest.raises(ValidationError):
        message.task_description = "Changed task"


def test_agent_result_correlates_with_message():
    message_id = uuid4()
    incident_id = uuid4()

    result = AgentResult(
        message_id=message_id,
        incident_id=incident_id,
        sender=RoleEnum.TRIAGE,
        is_success=True,
        output_data={"severity": "CRITICAL"},
    )

    assert result.message_id == message_id
    assert result.incident_id == incident_id
    assert result.receiver == RoleEnum.ORCHESTRATOR
    assert result.output_data["severity"] == "CRITICAL"


def test_agent_result_is_immutable():
    result = AgentResult(
        message_id=uuid4(),
        incident_id=uuid4(),
        sender=RoleEnum.TRIAGE,
        is_success=True,
    )

    with pytest.raises(ValidationError):
        result.is_success = False
