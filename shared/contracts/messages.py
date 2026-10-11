from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from shared.time import utc_now


class RoleEnum(str, Enum):
    ORCHESTRATOR = "orchestrator"
    TRIAGE = "triage_agent"
    DIAGNOSTIC = "diagnostic_agent"
    REMEDIATION = "remediation_agent"


class AgentMessage(BaseModel):
    """Command sent by the orchestrator to an agent."""

    model_config = ConfigDict(frozen=True)

    message_id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    sender: RoleEnum = RoleEnum.ORCHESTRATOR
    receiver: RoleEnum
    task_description: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class AgentResult(BaseModel):
    """Structured result returned by an agent."""

    model_config = ConfigDict(frozen=True)

    result_id: UUID = Field(default_factory=uuid4)
    message_id: UUID
    incident_id: UUID
    sender: RoleEnum
    receiver: RoleEnum = RoleEnum.ORCHESTRATOR
    is_success: bool
    output_data: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)
