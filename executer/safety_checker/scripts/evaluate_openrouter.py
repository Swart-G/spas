"""Evaluate the selected Mercury model via a temporary real SPAS HTTP Gateway."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import uvicorn
from llm.gateway.config import GatewayConfig
from llm.gateway.core.errors import GatewayError
from llm.gateway.main import GatewayKeys, create_app

from safety_analyzer.command_evaluation import build_parser, load_cases, run
from safety_analyzer.commands import CommandAnalyzer, configured_rules
from safety_analyzer.config import Settings
from safety_analyzer.gateway import LLMGateway

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/openrouter-mercury.toml"
MODEL = "inception/mercury-decide:free"
ROUTE = "safety-mercury"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
GATEWAY_REVISION = "d311eedf6e75d1adcbe6668101e6bc69a1ed4734"


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def installed_gateway_revision():
    try:
        origin = json.loads(distribution("spas-llm-gateway").read_text("direct_url.json") or "{}")
        return origin.get("vcs_info", {}).get("commit_id")
    except (PackageNotFoundError, ValueError, TypeError):
        return None


def checked_config():
    config = GatewayConfig.load(CONFIG)
    if (
        set(config.backends) != {"mercury"}
        or config.routes != {ROUTE: ["mercury"]}
        or config.fallback_on
    ):
        raise ValueError("Mercury evaluation requires one backend and no fallback")
    backend = config.backends["mercury"]
    if (
        backend.provider != "openai_compatible"
        or backend.model != MODEL
        or backend.decision_url != ENDPOINT
        or backend.account != "safety-openrouter"
    ):
        raise ValueError("Unexpected model, endpoint or account")
    return config


@asynccontextmanager
async def gateway(api_key, directory):
    config = checked_config()
    keys = GatewayKeys(secrets.token_urlsafe(32))
    app = create_app(config, state_dir=directory, keys=keys)
    await app.state.auth.save_api_key("safety-openrouter", api_key, provider="openai_compatible")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                access_log=False,
                log_level="error",
                timeout_graceful_shutdown=1,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("Gateway startup failed")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}/v1", keys.client
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


async def evaluate(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "started_at": datetime.now(UTC).isoformat(),
        "provider": "OpenRouter.ai",
        "provider_model": MODEL,
        "provider_endpoint": ENDPOINT,
        "gateway_route": ROUTE,
        "gateway_revision": installed_gateway_revision(),
        "gateway_dependency_pin": GATEWAY_REVISION,
        "gateway_config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
        "model_weights_revision": None,
        "status": "not_run",
        "quality_metrics": None,
        "model_interval_seconds": args.model_interval_seconds,
    }
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        metadata["blockers"] = ["OPENROUTER_API_KEY is not configured"]
        write_json(args.output_dir / "preflight.json", metadata)
        print("Real model not run: configure OPENROUTER_API_KEY in environment settings.")
        return 2
    with TemporaryDirectory(prefix=".gateway-", dir=args.output_dir) as temporary:
        # Only encrypted Gateway credentials exist here; removed when the run ends.
        async with gateway(api_key, Path(temporary)) as (base_url, client_key):
            policy = json.loads((ROOT / "datasets/command-policy.json").read_text())
            settings = Settings(
                gateway_base_url=base_url,
                gateway_api_key=client_key,
                model=ROUTE,
                command_api="decisions",
                **policy,
            )
            cases = load_cases(ROOT / "datasets/quality-v1.jsonl")
            probe_cases = [c for c in cases if c.family == "tool-injection"]
            if len(probe_cases) != 3:
                raise ValueError("The three context probe cases are required")
            probe = []
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                analyzer = CommandAnalyzer(LLMGateway(settings, client), configured_rules(settings))
                # Same argv, different complete snapshots, including untrusted tool observations.
                for case in probe_cases:
                    if probe:
                        await asyncio.sleep(args.model_interval_seconds)
                    result = await analyzer.analyze(case.request)
                    probe.append(
                        {
                            "id": case.id,
                            "input_sha256": case.request.fingerprint(),
                            "context_fields": sorted(case.request.context.model_dump()),
                            "expected_decision": case.expected_decision,
                            **result.model_dump(
                                include={
                                    "decision",
                                    "risk",
                                    "status",
                                    "error_code",
                                    "latency_ms",
                                    "source",
                                }
                            ),
                        }
                    )
                    if result.status != "ok":
                        break  # Do not burn quota on 350 calls after an access/transport failure.
            metadata["probe"] = probe
            metadata["probe_gate_passed"] = all(
                row["status"] == "ok" and row["decision"] == row["expected_decision"]
                for row in probe
            )
            metadata["status"] = (
                "probe_completed" if all(row["status"] == "ok" for row in probe) else "probe_failed"
            )
            write_json(args.output_dir / "preflight.json", metadata)
            if metadata["status"] != "probe_completed":
                print("Real probe failed; see preflight.json. Full benchmark was not started.")
                return 2
            if args.probe_only:
                print("Real probe completed. Full quality/stability benchmark was not requested.")
                return 0 if metadata["probe_gate_passed"] else 1
            await asyncio.sleep(args.model_interval_seconds)
            evaluation = build_parser().parse_args(
                [
                    "--dataset",
                    str(ROOT / "datasets/quality-v1.jsonl"),
                    "--policy-file",
                    str(ROOT / "datasets/command-policy.json"),
                    "--repeats",
                    str(args.repeats),
                    "--seed",
                    "42",
                    "--model-interval-seconds",
                    str(args.model_interval_seconds),
                    "--output",
                    str(args.output_dir / "quality.json"),
                ]
            )
            code = await run(evaluation, settings=settings)
            report_path = args.output_dir / "quality.json"
            report = json.loads(report_path.read_text())
            report.update(
                {
                    key: metadata[key]
                    for key in (
                        "provider",
                        "provider_model",
                        "provider_endpoint",
                        "gateway_route",
                        "gateway_revision",
                        "gateway_dependency_pin",
                        "gateway_config_sha256",
                        "model_weights_revision",
                    )
                }
            )
            write_json(report_path, report)
            with report_path.with_suffix(".md").open("a", encoding="utf-8") as output:
                output.write(
                    f"\nProvider: OpenRouter.ai; model: `{MODEL}`. "
                    "Provider-managed weights revision is unknown.\n"
                )
            return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports/openrouter-mercury")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--model-interval-seconds", type=float, default=3.1)
    args = parser.parse_args()
    if args.repeats < 2 or not 0 <= args.model_interval_seconds <= 120:
        parser.error("at least two repeats and a finite model interval in [0, 120] are required")
    try:
        code = asyncio.run(evaluate(args))
    except (OSError, RuntimeError, ValueError, TimeoutError, GatewayError):
        parser.exit(2, "Gateway setup/evaluation failed; no execution is authorized.\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
