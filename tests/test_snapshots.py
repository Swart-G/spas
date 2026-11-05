import copy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.incidents.models import Incident
from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum


@pytest.fixture(params=["message", "result", "metrics"])
def snapshot(request):
    source = {"metrics": {"latencies": [10, {"p99": 50}]}}
    if request.param == "message":
        model = AgentMessage(
            incident_id=uuid4(),
            receiver=RoleEnum.TRIAGE,
            task_description="Analyze incident",
            payload=source,
        )
        field = "payload"
    elif request.param == "result":
        model = AgentResult(
            message_id=uuid4(),
            incident_id=uuid4(),
            sender=RoleEnum.TRIAGE,
            is_success=True,
            output_data=source,
        )
        field = "output_data"
    else:
        model = Incident(title="Timeout", description="DB timeout", metrics_log=source)
        field = "metrics_log"
    return model, field, source


def test_snapshot_is_detached_from_input(snapshot):
    model, field, source = snapshot
    source["metrics"]["latencies"][1]["p99"] = 999
    source["metrics"]["latencies"].append(1000)
    source["new"] = True

    assert getattr(model, field) == {"metrics": {"latencies": [10, {"p99": 50}]}}


def test_snapshot_rejects_nested_mutation(snapshot):
    model, field, _ = snapshot
    data = getattr(model, field)

    with pytest.raises(TypeError):
        data["new"] = True
    with pytest.raises(TypeError):
        data["metrics"]["p99"] = 999
    with pytest.raises(TypeError):
        data["metrics"]["latencies"].append(1000)
    with pytest.raises(TypeError):
        data["metrics"]["latencies"][1]["p99"] = 999


def test_snapshot_serialization_returns_independent_mutable_data(snapshot):
    model, field, _ = snapshot
    dumped = model.model_dump()
    dumped[field]["metrics"]["latencies"][1]["p99"] = 999

    assert getattr(model, field)["metrics"]["latencies"][1]["p99"] == 50
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model
    with pytest.raises(TypeError):
        getattr(restored, field)["metrics"]["latencies"].clear()


def test_snapshot_supports_deep_copy(snapshot):
    model, field, _ = snapshot
    copied = model.model_copy(deep=True)

    assert copied == model
    assert copy.deepcopy(getattr(model, field)) == getattr(model, field)
    with pytest.raises(TypeError):
        getattr(copied, field)["metrics"]["latencies"][0] = 999


def test_snapshot_defaults_are_immutable(snapshot):
    model, field, _ = snapshot
    default = type(model).model_validate(model.model_dump(exclude={field}))

    assert getattr(default, field) == {}
    with pytest.raises(TypeError):
        getattr(default, field)["new"] = True


def test_snapshot_cannot_be_reinitialized(snapshot):
    model, field, _ = snapshot
    data = getattr(model, field)
    data.__init__({"new": True})
    data["metrics"]["latencies"].__init__([999])

    assert data == {"metrics": {"latencies": [10, {"p99": 50}]}}


def test_snapshot_rejects_bulk_and_slice_mutation(snapshot):
    model, field, _ = snapshot
    data = getattr(model, field)
    latencies = data["metrics"]["latencies"]

    with pytest.raises(TypeError):
        data.update({"new": True})
    with pytest.raises(TypeError):
        data |= {"new": True}
    with pytest.raises(TypeError):
        latencies[:] = [999]
    with pytest.raises(TypeError):
        latencies += [999]

    assert data == {"metrics": {"latencies": [10, {"p99": 50}]}}


def test_snapshot_rejects_base_container_mutation(snapshot):
    model, field, _ = snapshot
    data = getattr(model, field)

    with pytest.raises(TypeError):
        dict.__setitem__(data["metrics"], "p99", 999)
    with pytest.raises(TypeError):
        dict.update(data, {"new": True})
    with pytest.raises(TypeError):
        list.append(data["metrics"]["latencies"], 999)
    with pytest.raises(TypeError):
        list.__setitem__(data["metrics"]["latencies"], 0, 999)

    assert data == {"metrics": {"latencies": [10, {"p99": 50}]}}


def test_metrics_assignment_creates_a_new_snapshot():
    incident = Incident(title="Timeout", description="DB timeout")
    original = incident.metrics_log
    source = {"metrics": {"latencies": [10]}}

    incident.metrics_log = source
    source["metrics"]["latencies"].append(999)

    assert original == {}
    assert incident.metrics_log == {"metrics": {"latencies": [10]}}
    with pytest.raises(TypeError):
        incident.metrics_log.update({"severity": "CRITICAL"})


def test_snapshot_rejects_non_json_mutable_values():
    with pytest.raises(ValidationError):
        AgentResult(
            message_id=uuid4(),
            incident_id=uuid4(),
            sender=RoleEnum.TRIAGE,
            is_success=True,
            output_data={"value": bytearray(b"mutable")},
        )


def test_snapshot_rejects_cyclic_data():
    data = {}
    data["self"] = data

    with pytest.raises(ValidationError):
        Incident(title="Timeout", description="DB timeout", metrics_log=data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_snapshot_rejects_non_finite_numbers(snapshot, value):
    model, field, _ = snapshot
    data = model.model_dump()
    data[field] = {"nested": [{"value": value}]}

    with pytest.raises(ValidationError, match="finite numbers"):
        type(model).model_validate(data)


def test_snapshot_preserves_json_values_and_shared_subtrees(snapshot):
    model, field, _ = snapshot
    shared = {"values": [None, True, False, 1, 1.5, "метрика"]}
    source = {"first": shared, "second": shared}
    data = model.model_dump()
    data[field] = source
    result = type(model).model_validate(data)

    assert result.model_dump()[field] == source
    assert type(model).model_validate_json(result.model_dump_json()) == result
    shared["values"].append("changed")
    assert len(getattr(result, field)["first"]["values"]) == 6
    assert len(getattr(result, field)["second"]["values"]) == 6


def test_excessive_nesting_raises_validation_error():
    data = {}
    for _ in range(1200):
        data = {"nested": data}

    with pytest.raises(ValidationError):
        Incident(title="Timeout", description="DB timeout", metrics_log=data)
