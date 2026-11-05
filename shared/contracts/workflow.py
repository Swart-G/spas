"""Validated output required to advance an incident to its next stage."""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.snapshots import SnapshotData


class _StageData(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True, extra="forbid")

    metrics: SnapshotData = Field(default_factory=dict, validate_default=True)


class DiagnosisData(_StageData):
    root_cause: str = Field(min_length=1)


class RemediationData(_StageData):
    action_taken: str = Field(min_length=1)
    plan: SnapshotData = Field(default_factory=dict, validate_default=True)
    execution_results: list[SnapshotData] = Field(default_factory=list)

    @model_validator(mode="after")
    def confirmed_execution(self):
        # Legacy demo agents have no executable plan. Once a plan is supplied,
        # every action requires a matching successful, checked execution receipt.
        if self.plan:
            actions = self.plan.get("actions")
            if not isinstance(actions, Sequence) or isinstance(actions, str):
                raise ValueError("Executable plan must contain an actions array")
            if len(actions) != len(self.execution_results):
                raise ValueError("Every planned action requires an execution result")
            for argv, result in zip(actions, self.execution_results, strict=True):
                if (
                    not argv
                    or result.get("argv") != argv
                    or result.get("status") != "succeeded"
                    or result.get("safety_checked") is not True
                    or type(result.get("exit_code")) is not int
                    or result["exit_code"] != 0
                ):
                    raise ValueError("Execution is not confirmed successful")
        elif self.execution_results:
            raise ValueError("Execution results require a plan")
        return self


class VerificationData(_StageData):
    resolved: bool
    details: str = Field(min_length=1)
