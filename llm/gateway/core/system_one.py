"""System One requests and typed answers, independent of a provider's HTTP path."""

import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from .decisions import DecisionRequest
from .decisions import validate_result as validate_native
from .errors import GatewayError

Content = str | dict[str, JsonValue] | list[JsonValue]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instructions: Content | None = None


class ChoiceQuestion(Question):
    type: Literal["choice"]
    criteria: dict[str, Content | None] = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def valid_keys(self):
        if any(not key.strip() for key in self.criteria):
            raise ValueError("Option names cannot be blank")
        return self


class NoulQuestion(Question):
    type: Literal["noul"]
    criteria: dict[Literal["true", "false"], Content | None] | None = None

    @model_validator(mode="after")
    def meaningful(self):
        instructions = (
            self.instructions.strip() if isinstance(self.instructions, str) else self.instructions
        )
        if not instructions and not any((self.criteria or {}).values()):
            raise ValueError("Noul requires instructions or outcome descriptions")
        return self


class ScoreQuestion(Question):
    type: Literal["score"]
    criteria: list[Content] = Field(min_length=1, max_length=10)


class BoxQuestion(Question):
    type: Literal["box"]
    instructions: Content

    @model_validator(mode="after")
    def meaningful(self):
        if not self.instructions:
            raise ValueError("Describe the target for Box")
        return self


DecisionQuestion = Annotated[
    ChoiceQuestion | NoulQuestion | ScoreQuestion | BoxQuestion, Field(discriminator="type")
]
question_adapter = TypeAdapter(DecisionQuestion)


def contains_image(value):
    if isinstance(value, dict):
        return value.get("type") == "image_url" or any(contains_image(v) for v in value.values())
    return isinstance(value, list) and any(contains_image(item) for item in value)


class SystemOneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=200)
    state: str | dict[str, JsonValue] | list[JsonValue]
    questions: dict[str, DecisionQuestion] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_content(self):
        if not self.state or isinstance(self.state, str) and not self.state.strip():
            raise ValueError("State cannot be empty")
        if any(not key.strip() for key in self.questions):
            raise ValueError("Question ids cannot be blank")
        if len(self.model_dump_json().encode()) > 20 * 1024 * 1024:
            raise ValueError("Decision request exceeds 20 MiB")
        if any(q.type == "box" for q in self.questions.values()) and not contains_image(self.state):
            raise ValueError("Box requires an image in state")
        return self

    def payload(self, model):
        return {**self.model_dump(exclude_none=True), "model": model}


DecisionInput = DecisionRequest | SystemOneRequest


def parse_decision(data):
    return (SystemOneRequest if "questions" in data else DecisionRequest).model_validate(data)


def text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def from_native(request: DecisionRequest) -> SystemOneRequest:
    questions = {}
    for name, field in request.fields.items():
        if field.type == "boolean":
            questions[name] = {
                "type": "noul",
                "instructions": field.description,
                "criteria": field.choice_descriptions or None,
            }
        else:
            questions[name] = {
                "type": "choice",
                "instructions": field.description,
                "criteria": {key: field.choice_descriptions.get(key) for key in field.choices},
            }
    return SystemOneRequest(model=request.model, state=request.context, questions=questions)


def to_native(request: SystemOneRequest) -> DecisionRequest:
    if contains_image(request.model_dump()) or any(
        q.type == "box" for q in request.questions.values()
    ):
        raise GatewayError(
            "Для Nimble доступны текстовые режимы Choice, Noul и Score.",
            code="unsupported_parameter",
            status=400,
        )
    schema = {}
    for name, question in request.questions.items():
        field = {"description": text(question.instructions) if question.instructions else name}
        if question.type == "noul":
            field.update(
                type="boolean",
                choice_descriptions={
                    key: text(value)
                    for key, value in (question.criteria or {}).items()
                    if value is not None
                },
            )
        else:
            criteria = (
                question.criteria
                if question.type == "choice"
                else {str(index): value for index, value in enumerate(question.criteria)}
            )
            field.update(
                type="enum",
                choices=list(criteria),
                choice_descriptions={
                    key: text(value) for key, value in criteria.items() if value is not None
                },
            )
        schema[name] = field
    try:
        return DecisionRequest.model_validate(
            {"model": request.model, "context": text(request.state), "schema": schema}
        )
    except ValueError as error:
        raise GatewayError(
            "Схема превышает ограничения локального Nimble.",
            status=400,
            code="unsupported_parameter",
        ) from error


def from_native_result(data, request: SystemOneRequest):
    native = validate_native(data, to_native(request))
    answers = {}
    for name, question in request.questions.items():
        field = native["fields"][name]
        scores = field["scores"]
        if question.type == "noul":
            answers[name] = {"type": "noul", "noul": scores["true"]}
            continue
        confidence = (
            1.0
            if len(scores) == 1
            else 1 + sum(p * math.log(p) for p in scores.values() if p > 0) / math.log(len(scores))
        )
        answer = {
            "type": question.type,
            "probabilities": scores,
            "confidence": min(1.0, max(0.0, confidence)),
        }
        if question.type == "choice":
            answer["choice"] = native["output"][name]
        else:
            answer["score"] = sum(int(key) * p for key, p in scores.items())
            answer["legend"] = {str(i): level for i, level in enumerate(question.criteria)}
        answers[name] = answer
    return {"model": request.model, "answers": answers}


def to_native_result(data, request: DecisionRequest):
    clean = validate_system_result(data, from_native(request))
    output, fields = {}, {}
    for name, answer in clean["answers"].items():
        if answer["type"] == "noul":
            value = answer["noul"] >= 0.5
            scores = {"false": 1 - answer["noul"], "true": answer["noul"]}
        else:
            value, scores = answer["choice"], answer["probabilities"]
            total = sum(scores.values())
            scores = {key: p / total for key, p in scores.items()}
        output[name] = value
        fields[name] = {"value": value, "scores": scores}
    return {"model": request.model, "output": output, "fields": fields}


def bounded(value, low=0, high=1):
    if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Invalid bounded number")
    return value


def probabilities(values, keys):
    if not isinstance(values, dict) or set(values) != set(keys):
        raise ValueError("Probabilities must cover every candidate")
    clean = {key: bounded(value) for key, value in values.items()}
    # Providers may round probabilities to two decimals.
    if not math.isclose(sum(clean.values()), 1, abs_tol=min(0.05, len(keys) * 0.005 + 1e-6)):
        raise ValueError("Invalid probability sum")
    return clean


def validate_system_result(data, request: SystemOneRequest):
    try:
        answers = data["answers"]
        if not isinstance(answers, dict) or set(answers) != set(request.questions):
            raise ValueError("Question ids do not match")
        cleaned = {}
        for name, question in request.questions.items():
            answer = answers[name]
            if answer["type"] == "refusal":
                cleaned[name] = {"type": "refusal"}
                continue
            if answer["type"] != question.type:
                raise ValueError("Answer type does not match")
            result = {"type": question.type}
            if question.type == "noul":
                result["noul"] = bounded(answer["noul"])
            elif question.type == "box":
                box = answer["box"]
                result["box"] = {
                    edge: bounded(box[edge]) for edge in ["top", "bottom", "left", "right"]
                }
                if box["top"] > box["bottom"] or box["left"] > box["right"]:
                    raise ValueError("Reversed box edges")
                result["confidence"] = bounded(answer["confidence"])
            else:
                keys = (
                    list(question.criteria)
                    if question.type == "choice"
                    else [str(index) for index in range(len(question.criteria))]
                )
                result["probabilities"] = probabilities(answer["probabilities"], keys)
                result["confidence"] = bounded(answer["confidence"])
                if question.type == "choice":
                    if answer["choice"] not in question.criteria:
                        raise ValueError("Unknown chosen candidate")
                    result["choice"] = answer["choice"]
                else:
                    result["score"] = bounded(answer["score"], 0, len(keys) - 1)
                    result["legend"] = {str(i): level for i, level in enumerate(question.criteria)}
            cleaned[name] = result
        result = {"model": request.model, "answers": cleaned}
        usage = data.get("usage")
        if usage is not None:
            result["usage"] = {}
            for key in ["input_tokens", "output_tokens"]:
                value = usage[key]
                if type(value) is not int or value < 0:
                    raise ValueError("Invalid token usage")
                result["usage"][key] = value
            if "cost" in usage:
                result["usage"]["cost"] = bounded(usage["cost"], high=float("inf"))
        return result
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise GatewayError(
            "Модель решений вернула ответ в неподдерживаемом формате.",
            code="provider_incomplete",
            status=502,
        ) from error
