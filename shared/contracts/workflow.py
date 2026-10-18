"""Validated output required to advance an incident to its next stage."""

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.snapshots import SnapshotData


class _StageData(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True, extra="forbid")

    metrics: SnapshotData = Field(default_factory=dict, validate_default=True)


class DiagnosisData(_StageData):
    root_cause: str = Field(min_length=1)


class RemediationData(_StageData):
    action_taken: str = Field(min_length=1)


class VerificationData(_StageData):
    resolved: bool
    details: str = Field(min_length=1)
