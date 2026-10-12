
import pytest

from core.incidents.models import Incident, IncidentStatus


def test_incident_is_created_with_new_status():
    incident = Incident(
        title="DB timeout",
        description="Database connections are timing out",
    )

    assert incident.status == IncidentStatus.NEW
    assert incident.incident_id is not None
    assert incident.created_at is not None
    assert incident.updated_at is not None
    assert incident.history == []
    assert incident.metrics_log == {}


def test_valid_status_transition():
    incident = Incident(
        title="DB timeout",
        description="Database timeout",
    )

    incident.update_status(IncidentStatus.INVESTIGATING)

    assert incident.status == IncidentStatus.INVESTIGATING


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        (IncidentStatus.NEW, IncidentStatus.RESOLVED),
        (IncidentStatus.NEW, IncidentStatus.REMEDIATING),
        (IncidentStatus.RESOLVED, IncidentStatus.NEW),
        (IncidentStatus.FAILED, IncidentStatus.INVESTIGATING),
    ],
)
def test_invalid_status_transitions_are_rejected(initial, target):
    incident = Incident(
        title="DB timeout",
        description="Database timeout",
        status=initial,
    )

    with pytest.raises(ValueError, match="Invalid status transition"):
        incident.update_status(target)

    assert incident.status == initial


def test_interrupted_investigation_can_return_to_new():
    incident = Incident(title="DB timeout", description="Database timeout")
    incident.update_status(IncidentStatus.INVESTIGATING)

    incident.update_status(IncidentStatus.NEW)

    assert incident.status == IncidentStatus.NEW
    incident.update_status(IncidentStatus.INVESTIGATING)
    assert incident.status == IncidentStatus.INVESTIGATING


def test_same_status_transition_is_a_noop():
    incident = Incident(
        title="DB timeout",
        description="Database timeout",
    )

    original_updated_at = incident.updated_at
    incident.update_status(IncidentStatus.NEW)

    assert incident.status == IncidentStatus.NEW
    assert incident.updated_at == original_updated_at
