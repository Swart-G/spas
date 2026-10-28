"""The real Gateway/OpenRouter boundary, with remote inference replaced only in tests."""

import importlib.util
import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

pytest.importorskip("llm.gateway.main", reason="install the integration group")
from llm.gateway.auth.credentials import CredentialStore  # noqa: E402
from llm.gateway.auth.manager import AuthManager  # noqa: E402
from llm.gateway.config import GatewayConfig  # noqa: E402
from llm.gateway.main import GatewayKeys, create_app  # noqa: E402
from test_decision_adapter import REQUEST, answer  # noqa: E402

from safety_analyzer.commands import CommandAnalyzer, configured_rules  # noqa: E402
from safety_analyzer.config import Settings  # noqa: E402
from safety_analyzer.gateway import LLMGateway  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODEL = "inception/mercury-decide:free"
pytestmark = pytest.mark.gateway_contract


def runner_module():
    spec = importlib.util.spec_from_file_location(
        "evaluate_openrouter", ROOT / "scripts/evaluate_openrouter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "decision,risk,factor",
    [
        ("ALLOW", "LOW", "none"),
        ("DENY", "HIGH", "secret_exposure"),
        ("REQUIRE_APPROVAL", "MEDIUM", "insufficient_context"),
    ],
)
async def test_full_context_through_real_gateway_to_openrouter(tmp_path, decision, risk, factor):
    calls = []

    def remote(wire):
        body = json.loads(wire.content)
        calls.append(body)
        assert str(wire.url) == "https://openrouter.ai/api/alpha/decisions"
        assert wire.headers["Authorization"] == "Bearer provider-test-key"
        assert body["model"] == MODEL
        assert body["state"]["context"] == REQUEST.context.model_dump()
        assert body["state"]["command"] == REQUEST.command
        assert body["state"]["rules"]["decision"] == "ALLOW"
        assert set(body) == {"model", "state", "questions"}
        assert set(body["questions"]) == {"decision", "risk", "factor"}
        data = answer(decision, risk, factor)
        data["model"] = MODEL
        return httpx.Response(200, json=data)

    config = GatewayConfig.load(ROOT / "config/openrouter-mercury.toml")
    assert config.routes == {"safety-mercury": ["mercury"]} and config.fallback_on == []
    async with httpx.AsyncClient(trust_env=False) as client:
        await AuthManager(CredentialStore(tmp_path), client).save_api_key(
            "safety-openrouter", "provider-test-key", provider="openai_compatible"
        )
    app = create_app(
        config,
        state_dir=tmp_path,
        keys=GatewayKeys("client-" + "x" * 32),
        transport=httpx.MockTransport(remote),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), trust_env=False) as client:
            settings = Settings(
                gateway_base_url="http://gateway/v1",
                gateway_api_key="client-" + "x" * 32,
                model="safety-mercury",
                command_api="decisions",
            )
            result = await CommandAnalyzer(
                LLMGateway(settings, client), configured_rules(settings)
            ).analyze(REQUEST)
    assert result.status == "ok" and result.decision == decision and len(calls) == 1


def test_missing_secret_writes_preflight_without_inference(tmp_path):
    env = os.environ.copy()
    env.pop("OPENROUTER_API_KEY", None)
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_openrouter.py",
            "--probe-only",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr
    preflight = json.loads((tmp_path / "preflight.json").read_text())
    assert preflight["status"] == "not_run" and preflight["quality_metrics"] is None
    assert preflight["provider_model"] == MODEL
    assert not list(tmp_path.glob(".gateway-*"))
    assert not (tmp_path / "quality.json").exists()


@pytest.mark.parametrize(
    "before,after",
    [
        ('model = "inception/mercury-decide:free"', 'model = "different-model"'),
        ("fallback_on = []", 'fallback_on = ["provider_unavailable"]'),
        ("https://openrouter.ai/api/alpha/decisions", "https://unexpected.test/decisions"),
    ],
)
def test_changed_model_or_destination_is_rejected_before_credentials(tmp_path, before, after):
    module = runner_module()
    path = tmp_path / "changed.toml"
    path.write_text(module.CONFIG.read_text().replace(before, after))
    module.CONFIG = path
    with pytest.raises(ValueError):
        module.checked_config()


@pytest.mark.parametrize("outage", [False, True])
async def test_probe_owns_real_tcp_gateway_and_cleans_up_credentials(tmp_path, monkeypatch, outage):
    module = runner_module()
    calls = []
    selections = [
        ("ALLOW", "LOW", "none"),
        ("DENY", "HIGH", "context_conflict"),
        ("REQUIRE_APPROVAL", "MEDIUM", "insufficient_context"),
    ]

    def remote(wire):
        calls.append(wire)
        if outage:
            return httpx.Response(401)
        data = answer(*selections[len(calls) - 1])
        data["model"] = MODEL
        return httpx.Response(200, json=data)

    factory = module.create_app
    monkeypatch.setattr(
        module,
        "create_app",
        lambda *a, **kw: factory(*a, **kw, transport=httpx.MockTransport(remote)),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-test-key")
    code = await module.evaluate(
        Namespace(output_dir=tmp_path, probe_only=True, repeats=5, model_interval_seconds=0)
    )
    report = json.loads((tmp_path / "preflight.json").read_text())
    assert code == (2 if outage else 0)
    assert len(calls) == (1 if outage else 3)
    assert report["probe_gate_passed"] is (not outage)
    assert not list(tmp_path.glob(".gateway-*"))
    assert "provider-test-key" not in json.dumps(report)
