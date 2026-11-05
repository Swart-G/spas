"""Real HTTP + MCP round trip across four independently running processes."""

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest


@pytest.fixture
def deployed(tmp_path):
    processes = []
    logs = []

    def start(component, **settings):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        log = (tmp_path / f"{component}.log").open("w+")
        logs.append(log)
        env = {
            **os.environ,
            "SPAS_COMPONENT": component,
            "EXECUTOR_LOG_DIR": str(tmp_path / "audit"),
            "AGENT_TOKEN": "agent-test",
            "DIAGNOSTIC_AGENT_TOKEN": "agent-test",
            "WORKER_AGENT_TOKEN": "agent-test",
            **settings,
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.mvp.service:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        processes.append(process)
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            for _ in range(200):
                try:
                    if client.get(url + "/health").status_code == 200:
                        return url
                except httpx.TransportError:
                    pass
                if process.poll() is not None:
                    break
                time.sleep(0.05)
        log.seek(0)
        pytest.fail(log.read())

    try:
        executor = start("executor", DIAGNOSTIC_TOKEN="diagnostic", WORKER_TOKEN="worker")
        diagnostic = start("diagnostic", DIAGNOSTIC_TOKEN="diagnostic", EXECUTOR_URL=executor)
        worker = start("worker", WORKER_TOKEN="worker", EXECUTOR_URL=executor, PLANNER_MODE="stub")
        orchestrator = start(
            "orchestrator", DIAGNOSTIC_URL=diagnostic + "/process", WORKER_URL=worker + "/process"
        )
        yield orchestrator, logs
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()


def test_network_pipeline(deployed):
    url, logs = deployed
    with httpx.Client(base_url=url, timeout=55, trust_env=False) as client:
        created = client.post("/incidents", json={})
        assert created.status_code == 201, created.text
        incident_id = created.json()["incident_id"]
        result = client.post(f"/incidents/{incident_id}/process")
        assert result.status_code == 200, result.text
        body = result.json()
        diagnostics = []
        for log in logs:
            log.seek(0)
            diagnostics.append(log.read())
        assert body["status"] == "resolved", (body, diagnostics)
        assert body["verification"]["resolved"]
        assert body["execution_results"][0]["status"] == "succeeded"
        assert body["execution_results"][0]["safety_checked"]
        assert client.post(f"/incidents/{incident_id}/process").json() == body
