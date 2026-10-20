"""Flat bounded decisions, matching Nimble's native candidate schema."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import GatewayError


class DecisionField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["enum", "boolean"]
    description: str = Field(min_length=1, max_length=10000)
    choices: list[str] | None = Field(default=None, min_length=1, max_length=255)
    choice_descriptions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_choices(self):
        if not self.description.strip():
            raise ValueError("A description is required")
        if self.type == "enum":
            if not self.choices or any(not item.strip() for item in self.choices):
                raise ValueError("Enum requires nonempty choices")
            if len(set(self.choices)) != len(self.choices):
                raise ValueError("Choices must be unique")
            allowed = self.choices
        else:
            if self.choices is not None:
                raise ValueError("Boolean choices are implicit")
            allowed = ["false", "true"]
        if set(self.choice_descriptions) - set(allowed):
            raise ValueError("Descriptions require valid choice names")
        return self


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=200)
    context: str = Field(min_length=1, max_length=1000000)
    fields: dict[str, DecisionField] = Field(alias="schema", min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_names(self):
        if not self.context.strip() or any(not name.strip() for name in self.fields):
            raise ValueError("Context and field names must be nonempty")
        return self

    def payload(self, model):
        return {
            "model": model,
            "context": self.context,
            "schema": {
                name: field.model_dump(exclude_none=True) for name, field in self.fields.items()
            },
        }


def validate_result(data, request):
    """Accept only bounded typed output and finite normalized candidate probabilities."""
    try:
        output, fields = data["output"], data["fields"]
        if set(output) != set(request.fields) or set(fields) != set(request.fields):
            raise ValueError()
        clean_fields = {}
        for name, definition in request.fields.items():
            value = output[name]
            if definition.type == "boolean":
                if type(value) is not bool:
                    raise ValueError()
                keys = ["false", "true"]
            else:
                if type(value) is not str or value not in definition.choices:
                    raise ValueError()
                keys = definition.choices
            scores = fields[name]["scores"]
            if (
                set(scores) != set(keys)
                or any(
                    type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
                    for p in scores.values()
                )
                or not math.isclose(sum(scores.values()), 1, abs_tol=1e-5)
            ):
                raise ValueError()
            clean_fields[name] = {"value": value, "scores": scores}
        # Context, raw logits, and runtime details are not part of the gateway contract.
        return {"model": request.model, "output": output, "fields": clean_fields}
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise GatewayError(
            "Неверный ответ модели решений.", code="provider_incomplete", status=502
        ) from error
