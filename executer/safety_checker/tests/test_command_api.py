import json
import logging

import httpx
import pytest
from conftest import completion
from test_commands import ALLOW, DENY

BODY = {"command": ["pwd"], "context": {"incident_id": "i", "reason": "diagnose"}}


@pytest.mark.parametrize("verdict", [ALLOW, DENY])
async def test_command_api_structured_decisions(api_for, verdict):
    async with api_for(lambda _: httpx.Response(200, json=completion(json.dumps(verdict)))) as api:
        result = await api.post("/v1/commands/analyze", json=BODY)
    assert result.status_code == 200
    data = result.json()
    assert data["decision"] == verdict["decision"] and data["risk"] == verdict["risk"]
    assert data["schema_version"] == "2.0" and data["rules"]["decision"] == "ALLOW"


async def test_rule_gate_works_without_llm_configuration(api_for):
    async with api_for(lambda _: pytest.fail("model bypassed rules"), model=None) as api:
        result = await api.post("/v1/commands/analyze", json=BODY | {"command": ["rm", "-rf", "/"]})
    assert result.status_code == 200 and result.json()["decision"] == "DENY"
    assert result.json()["source"] == "rules"


async def test_rule_allow_without_model_is_not_permission(api_for):
    async with api_for(lambda _: pytest.fail("unconfigured model called"), model=None) as api:
        result = await api.post("/v1/commands/analyze", json=BODY)
    assert result.status_code == 503 and result.json()["decision"] == "DENY"
    assert result.json()["error_code"] == "not_configured"


@pytest.mark.parametrize(
    "extra",
    [{"skip_rules": True}, {"decision": "ALLOW"}, {"policy": {"readable_paths": ["/etc/shadow"]}}],
)
async def test_caller_cannot_supply_policy_or_prior_permission(api_for, extra):
    async with api_for(lambda _: pytest.fail("invalid request reached model")) as api:
        response = await api.post("/v1/commands/analyze", json=BODY | extra)
    assert response.status_code == 422


async def test_legacy_string_route_is_not_an_alternate_command_gate(api_for):
    async with api_for(lambda _: pytest.fail("legacy route bypassed rules")) as api:
        result = await api.post("/v1/analyze", json={"target": "command", "text": "rm -rf /"})
    assert result.status_code == 422


async def test_command_logs_have_no_context_reason_or_output(api_for, caplog):
    caplog.set_level(logging.INFO, logger="safety_analyzer.commands")
    sensitive = {**ALLOW, "reason": "PRIVATE-MODEL-REASON"}
    async with api_for(
        lambda _: httpx.Response(200, json=completion(json.dumps(sensitive)))
    ) as api:
        response = await api.post(
            "/v1/commands/analyze",
            json={
                "command": ["pwd"],
                "context": {
                    "incident_id": "PRIVATE-ID",
                    "reason": "PRIVATE-TASK",
                    "observations": ["PRIVATE-SECRET"],
                },
            },
        )
    assert response.status_code == 200
    records = [r.message for r in caplog.records if r.name == "safety_analyzer.commands"]
    assert len(records) == 1 and "PRIVATE" not in records[0]
