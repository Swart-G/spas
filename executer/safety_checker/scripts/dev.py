#!/usr/bin/env python3
"""Start only local stubs, verify functional readiness, and clean up owned processes."""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def reserve(port: int) -> socket.socket:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def launch(stack: ExitStack, module: str, sock: socket.socket, log: Path, env: dict):
    output = stack.enter_context(log.open("w", encoding="utf-8"))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--factory",
            "--fd",
            str(sock.fileno()),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=env,
        pass_fds=(sock.fileno(),),
        stdout=output,
        stderr=subprocess.STDOUT,
    )
    stack.callback(stop, process)
    return process


def wait_ready(client: httpx.Client, url: str, processes: list[subprocess.Popen]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if any(p.poll() is not None for p in processes):
            raise RuntimeError("development process exited; inspect reports/dev/*.log")
        try:
            if client.get(url).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise RuntimeError("development readiness timeout; inspect reports/dev/*.log")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--gateway-port", type=int, default=8008)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    logs = ROOT / "reports" / "dev"
    logs.mkdir(parents=True, exist_ok=True)

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with ExitStack() as stack:
            # Reserve both ports before launching; never replace or kill existing services.
            gateway_sock = stack.enter_context(reserve(args.gateway_port))
            analyzer_sock = stack.enter_context(reserve(args.port))
            gateway_port = gateway_sock.getsockname()[1]
            analyzer_port = analyzer_sock.getsockname()[1]
            env = os.environ.copy()
            # Override all model settings: dev mode cannot accidentally contact a real Gateway.
            env.update(
                SAFETY_GATEWAY_BASE_URL=f"http://127.0.0.1:{gateway_port}/v1",
                SAFETY_GATEWAY_API_KEY="local-stub-only",
                SAFETY_MODEL="stub",
                SAFETY_OUTPUT_MODE="label",
                SAFETY_TIMEOUT_SECONDS="10",
                SAFETY_MAX_TOKENS="128",
                SAFETY_COMMAND_OUTPUT_MODE="json",
                SAFETY_COMMAND_MAX_TOKENS="512",
                SAFETY_COMMAND_TIMEOUT_SECONDS="4",
                SAFETY_RULE_TIMEOUT_SECONDS="0.5",
                SAFETY_READABLE_PATHS='["/workspace"]',
                SAFETY_CONTAINERS="[]",
                SAFETY_DENIED_EXECUTABLES="[]",
            )
            stub = launch(
                stack,
                "safety_analyzer.stub_gateway:create_stub_app",
                gateway_sock,
                logs / "stub.log",
                env,
            )
            analyzer = launch(
                stack, "safety_analyzer.app:create_app", analyzer_sock, logs / "analyzer.log", env
            )
            # These requests are exclusively loopback; no proxy or remote endpoint is needed.
            with httpx.Client(timeout=2, trust_env=False) as client:
                wait_ready(client, f"http://127.0.0.1:{gateway_port}/health", [stub, analyzer])
                wait_ready(
                    client, f"http://127.0.0.1:{analyzer_port}/health/ready", [stub, analyzer]
                )
                response = client.post(
                    f"http://127.0.0.1:{analyzer_port}/v1/analyze",
                    json={"text": "Hello", "target": "request"},
                )
                response.raise_for_status()
                actual = response.json()
                if (actual["decision"], actual["verdict"], actual["status"], actual["model"]) != (
                    "block",
                    "unsafe",
                    "ok",
                    "stub",
                ):
                    raise RuntimeError("stub functional check returned an unexpected decision")
                command = client.post(
                    f"http://127.0.0.1:{analyzer_port}/v1/commands/analyze",
                    json={
                        "command": ["pwd"],
                        "context": {
                            "incident_id": "dev-smoke",
                            "reason": "Inspect the working directory",
                        },
                    },
                )
                command.raise_for_status()
                assessment = command.json()
                if (assessment["decision"], assessment["source"], assessment["status"]) != (
                    "DENY",
                    "model",
                    "ok",
                ) or assessment["rules"]["decision"] != "ALLOW":
                    raise RuntimeError("contextual command check failed")
                denied = client.post(
                    f"http://127.0.0.1:{analyzer_port}/v1/commands/analyze",
                    json={
                        "command": ["rm", "-rf", "/"],
                        "context": {
                            "incident_id": "dev-smoke",
                            "reason": "Check an immutable rule deny",
                        },
                    },
                )
                denied.raise_for_status()
                if denied.json()["decision"] != "DENY" or denied.json()["source"] != "rules":
                    raise RuntimeError("mandatory rule gate failed")
            print(
                json.dumps(
                    {
                        "stub": True,
                        "functional_check": "passed",
                        "analyzer_port": analyzer_port,
                        "gateway_port": gateway_port,
                    }
                ),
                flush=True,
            )
            if not args.smoke:
                while all(p.poll() is None for p in [stub, analyzer]):
                    time.sleep(0.25)
                raise RuntimeError("development process stopped; inspect reports/dev/*.log")
    except KeyboardInterrupt:
        return
    except (OSError, RuntimeError, httpx.HTTPError) as exc:
        # Fixed setup error messages only; never print HTTP response bodies or process env.
        print(
            f"Development startup failed ({type(exc).__name__}); inspect reports/dev/*.log",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
