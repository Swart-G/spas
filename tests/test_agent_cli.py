import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from apps.cli.run_incident import load_agents, run
from shared.contracts import RoleEnum


@pytest.mark.asyncio
async def test_load_bundled_stub_factory():
    agents = await load_agents("apps.agent_setup:stub_agents")
    assert set(agents) == {RoleEnum.TRIAGE, RoleEnum.DIAGNOSTIC, RoleEnum.REMEDIATION}


@pytest.mark.asyncio
async def test_load_async_factory(monkeypatch):
    module = ModuleType("async_agent_application")

    async def factory():
        return {}

    module.factory = factory
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert await load_agents("async_agent_application:factory") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", ["", "module", ":factory", "module:", "module:a:b"])
async def test_invalid_factory_path_is_rejected(factory):
    with pytest.raises(ValueError, match="module:function"):
        await load_agents(factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [42, lambda: []])
async def test_factory_must_be_callable_and_return_a_mapping(monkeypatch, factory):
    module = ModuleType("invalid_agent_application")
    module.factory = factory
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(TypeError):
        await load_agents("invalid_agent_application:factory")


@pytest.mark.asyncio
async def test_failed_incident_still_writes_valid_json_stdout(monkeypatch, capsys):
    module = ModuleType("empty_agent_application")
    module.factory = lambda: {}
    monkeypatch.setitem(sys.modules, module.__name__, module)
    args = argparse.Namespace(
        stub=False,
        agent_factory="empty_agent_application:factory",
        agent_timeout=1,
        cleanup_timeout=1,
        title="Missing agents",
        description="No implicit simulation",
    )
    assert await run(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert "No agent registered" in report["last_error"]


PLUGIN = """
from shared.contracts import AgentMessage, AgentResult, RoleEnum, StageEnum

class ExternalAgent:
    def __init__(self, role):
        self.role = role

    async def process(self, message: AgentMessage) -> AgentResult:
        if message.payload["text"] == "crash" and message.stage == StageEnum.DIAGNOSIS:
            raise RuntimeError("External diagnostic failure")
        outputs = {
            StageEnum.TRIAGE: {"severity": "critical"},
            StageEnum.DIAGNOSIS: {"root_cause": "External diagnosis"},
            StageEnum.REMEDIATION: {"action_taken": "External action"},
            StageEnum.VERIFICATION: {
                "resolved": message.payload["text"] != "unhealthy",
                "details": "External health check",
            },
        }
        return AgentResult(
            message_id=message.message_id,
            incident_id=message.incident_id,
            sender=self.role,
            stage=message.stage,
            is_success=True,
            output_data=outputs[message.stage],
        )

def create_agents():
    return {role: ExternalAgent(role) for role in (
        RoleEnum.TRIAGE, RoleEnum.DIAGNOSTIC, RoleEnum.REMEDIATION
    )}
"""


@pytest.mark.parametrize(
    ("description", "status", "returncode"),
    [
        ("healthy", "resolved", 0),
        ("unhealthy", "failed", 1),
        ("crash", "failed", 1),
    ],
)
def test_cli_loads_independent_application_agents(tmp_path, description, status, returncode):
    (tmp_path / "external_application.py").write_text(PLUGIN)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.cli.run_incident",
            "--agent-factory",
            "external_application:create_agents",
            "--description",
            description,
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": os.pathsep.join((str(root), str(tmp_path)))},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == returncode, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == status
    if status == "resolved":
        assert [item["stage"] for item in report["history"]] == [
            "triage",
            "diagnosis",
            "remediation",
            "verification",
        ]


@pytest.mark.parametrize(
    "arguments", [[], ["--stub", "--agent-factory", "x:y"], ["--stub", "--agent-timeout", "0"]]
)
def test_cli_requires_an_explicit_valid_mode(arguments):
    result = subprocess.run(
        [sys.executable, "-m", "apps.cli.run_incident", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "error:" in result.stderr
