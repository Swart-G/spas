"""Exercise owned HTTP clients with real TCP inference and hostile proxy settings."""

import asyncio
import builtins
import json
import socket
from argparse import Namespace

import httpx
import pytest
import uvicorn

from safety_analyzer.app import create_app
from safety_analyzer.config import Settings
from safety_analyzer.evaluation import run
from safety_analyzer.stub_gateway import STUB_KEY, create_stub_app

PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
PROXY_ENVIRONMENTS = [
    pytest.param({"ALL_PROXY": "socks5://127.0.0.1:9"}, id="socks-all"),
    pytest.param({"HTTP_PROXY": "http://127.0.0.1:9"}, id="http"),
    pytest.param({"HTTPS_PROXY": "http://127.0.0.1:9"}, id="https"),
    pytest.param({"HTTP_PROXY": "socks5://127.0.0.1:9"}, id="socks-http"),
    pytest.param({"HTTPS_PROXY": "socks5://127.0.0.1:9"}, id="socks-https"),
    pytest.param({"ALL_PROXY": "socks5h://127.0.0.1:9"}, id="socks-dns"),
    pytest.param({"http_proxy": "http://127.0.0.1:9"}, id="lower-http"),
    pytest.param({"https_proxy": "socks5://127.0.0.1:9"}, id="lower-https"),
    pytest.param({"all_proxy": "socks5://127.0.0.1:9"}, id="lower-all"),
    pytest.param({"HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}, id="no-bypass"),
    pytest.param({"HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "*"}, id="bypass-all"),
    pytest.param(
        {"HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "other.invalid"}, id="bypass-other"
    ),
    pytest.param(
        {"http_proxy": "http://127.0.0.1:9", "no_proxy": "other.invalid"}, id="lower-bypass"
    ),
    pytest.param(
        {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "socks5://127.0.0.1:9",
            "ALL_PROXY": "socks5://127.0.0.1:9",
            "NO_PROXY": "",
        },
        id="combined",
    ),
]


@pytest.fixture
def proxy_environment(request, monkeypatch):
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in request.param.items():
        monkeypatch.setenv(name, value)
    original_import = builtins.__import__

    def without_socksio(name, *args, **kwargs):
        if name == "socksio" or name.startswith("socksio."):
            raise ImportError("socksio intentionally unavailable in this regression test")
        return original_import(name, *args, **kwargs)

    # Remain meaningful even if some other dependency later installs optional socksio.
    monkeypatch.setattr(builtins, "__import__", without_socksio)


@pytest.fixture
async def gateway_url(verdict):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(
            create_stub_app(verdict),
            log_level="error",
            access_log=False,
            timeout_graceful_shutdown=1,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("test Gateway failed to start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/v1"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            sock.close()


@pytest.mark.parametrize("proxy_environment", PROXY_ENVIRONMENTS, indirect=True)
@pytest.mark.parametrize("verdict", ["safe", "unsafe"])
async def test_startup_and_analysis_ignore_proxy_environment(
    proxy_environment, gateway_url, verdict
):
    settings = Settings(
        gateway_base_url=gateway_url,
        gateway_api_key=STUB_KEY,
        model="stub",
        output_mode="label",
        timeout_seconds=2,
    )
    # No injected HTTP client: test the same lifespan constructor used by uvicorn.
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        owned_client = app.state.analyzer.gateway.client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://analyzer.test", trust_env=False
        ) as api:
            assert (await api.get("/health/live")).status_code == 200
            assert (await api.get("/health/ready")).status_code == 200
            response = await api.post("/v1/analyze", json={"text": "Hello"})
        assert response.status_code == 200
        result = response.json()
        assert result["verdict"] == verdict and result["status"] == "ok"
        assert result["decision"] == ("allow" if verdict == "safe" else "block")
        assert result["error_code"] is None
    assert owned_client.is_closed


@pytest.mark.parametrize("proxy_environment", PROXY_ENVIRONMENTS, indirect=True)
@pytest.mark.parametrize("verdict", ["unsafe"])
async def test_evaluation_requests_ignore_proxy_environment(
    proxy_environment, gateway_url, monkeypatch, tmp_path
):
    monkeypatch.setenv("SAFETY_GATEWAY_BASE_URL", gateway_url)
    monkeypatch.setenv("SAFETY_GATEWAY_API_KEY", STUB_KEY)
    monkeypatch.setenv("SAFETY_MODEL", "stub")
    monkeypatch.setenv("SAFETY_OUTPUT_MODE", "label")
    monkeypatch.setenv("SAFETY_TIMEOUT_SECONDS", "2")
    dataset = tmp_path / "cases.jsonl"
    lines = [
        json.dumps(
            {
                "id": suite,
                "suite": suite,
                "language": "en",
                "tags": ["test"],
                "request": {"text": "candidate"},
                "expected_decision": decision,
            }
        )
        for suite, decision in [("benign", "allow"), ("false_allow", "block")]
    ]
    await asyncio.to_thread(dataset.write_text, "\n".join(lines), encoding="utf-8")
    output = tmp_path / "report.json"
    code = await run(
        Namespace(
            dataset=dataset,
            output=output,
            repeats=2,
            max_false_allow_rate=0,
            max_false_block_rate=0.1,
            max_unstable_case_rate=0,
            max_error_rate=0,
        )
    )
    report = json.loads(await asyncio.to_thread(output.read_text, encoding="utf-8"))
    assert report["total_runs"] == 4 and report["error_runs"] == 0
    assert report["false_allow_runs"] == 0
    assert code == 1 and not report["gate_passed"]  # Fixed unsafe stub blocks benign inputs.
