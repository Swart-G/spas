from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.messages import AgentResult
from shared.time import utc_now


class IncidentStatus(str, Enum):
    NEW = "new"
    INVESTIGATING = "investigating"
    REMEDIATING = "remediating"
    RESOLVED = "resolved"
    FAILED = "failed"


ALLOWED_TRANSITIONS = {
    IncidentStatus.NEW: {
        IncidentStatus.INVESTIGATING,
        IncidentStatus.FAILED,
    },
    IncidentStatus.INVESTIGATING: {
        IncidentStatus.REMEDIATING,
        IncidentStatus.RESOLVED,
        IncidentStatus.FAILED,
    },
    IncidentStatus.REMEDIATING: {
        IncidentStatus.RESOLVED,
        IncidentStatus.FAILED,
    },
    IncidentStatus.RESOLVED: set(),
    IncidentStatus.FAILED: set(),
}


class Incident(BaseModel):
    """Single source of truth for an incident's state."""

    model_config = ConfigDict(validate_assignment=True)

    incident_id: UUID = Field(default_factory=uuid4)
    title: str
    description: str
    status: IncidentStatus = IncidentStatus.NEW
    metrics_log: dict[str, Any] = Field(default_factory=dict)
    root_cause: str | None = None
    action_taken: str | None = None
    history: list[AgentResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def update_status(self, new_status: IncidentStatus) -> None:
        """Apply a status change only when the transition is allowed."""
        if new_status == self.status:
            return

        if new_status not in ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(
                f"Invalid status transition: "
                f"{self.status.value} -> {new_status.value}"
            )

        self.status = new_status
        self.updated_at = utc_now()
