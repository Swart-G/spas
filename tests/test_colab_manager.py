import ast
import asyncio
import base64
import json
import subprocess
import sys
import time
import types
import uuid
from contextlib import aclosing
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.google import COLAB_SCOPE
from llm.gateway.auth.manager import AuthManager, account_key
from llm.gateway.colab.api import ColabAPI
from llm.gateway.colab.manager import ColabManager, DeploymentSettings
from llm.gateway.colab.scripts import bootstrap_script, inference_script, status_script, stop_script
from llm.gateway.config import BackendConfig
from llm.gateway.core.errors import GatewayError
from llm.gateway.core.models import ChatRequest
from llm.gateway.providers.colab_managed import ColabManagedAdapter

SPEC = {"variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"}
MODEL = "team/tiny-instruct"


def script_data(code):
    call = ast.parse(code).body[1].value.args[0]
    return json.loads(base64.b64decode(ast.literal_eval(call.args[0])))


def completion(text="СПАС"):
    return {
        "choices": [{"index": 0, "message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }


class RuntimeService:
    def __init__(self):
        self.requests = []
        self.runtime = None
        self.connections = 0
        self.create_error = None
        self.operation_error = None
        self.eligible = True

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "colaboratory.googleapis.com"
        assert request.headers["Authorization"] == "Bearer google-access"
        path = request.url.path
        if path == "/v1beta/runtimespecs":
            return httpx.Response(
                200, json={"runtimeSpecs": [{"key": SPEC, "eligible": self.eligible}]}
            )
        if path == "/v1beta/runtimes" and request.method == "POST":
            uuid.UUID(request.url.params["requestId"], version=4)
            assert json.loads(request.content)["runtimeSpec"] == SPEC
            self.runtime = "runtimes/" + request.url.params["runtimeId"]
            if self.create_error:
                return httpx.Response(self.create_error, json={"secret": "upstream-secret"})
            return httpx.Response(200, json={"name": "operations/create-1"})
        if path == "/v1/operations/create-1":
            if self.operation_error:
                return httpx.Response(
                    200,
                    json={
                        "done": True,
                        "error": {"code": self.operation_error, "message": "upstream-secret"},
                    },
                )
            return httpx.Response(200, json={"done": True, "response": {"name": self.runtime}})
        if path.startswith("/v1beta/runtimes/"):
            if not self.runtime or path != "/v1beta/" + self.runtime:
                return httpx.Response(404, json={})
            if request.method == "DELETE":
                self.runtime = None
                return httpx.Response(200, json={"done": True, "response": {}})
            self.connections += 1
            return httpx.Response(
                200,
                json={
                    "name": self.runtime,
                    "connectionInfo": {
                        "url": "https://runtime.test/colab",
                        "token": f"runtime-token-{self.connections}",
                        "expireTime": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    },
                },
            )
        raise AssertionError((request.method, path))


class RuntimeBridge:
    def __init__(self):
        self.payloads = []
        self.tokens = []
        self.failure = None
        self.failure_detail = None
        self.inference_events = None
        self.waiting = None
        self.closed = 0
        self.during_inference = None

    def factory(self, client, connection):
        assert connection["url"] == "https://runtime.test/colab"
        self.tokens.append(connection["token"])
        return self

    async def execute(self, code, *, wait_seconds=1800):
        payload = script_data(code)
        self.payloads.append(payload)
        try:
            if "server_source" in payload:
                for stage in ["INSTALLING", "DOWNLOADING", "STARTING"]:
                    yield {"stage": stage}
                    if self.failure == stage:
                        yield {"stage": "FAILED", "phase": stage, "diagnostic": self.failure_detail}
                        return
                if self.waiting is not None:
                    self.waiting.set()
                    await asyncio.Future()
                yield {"stage": "READY", "snapshot_commit": "commit-123"}
            elif "payload" in payload:
                if self.during_inference is not None:
                    await self.during_inference()
                events = self.inference_events or [{"type": "response", "data": completion()}]
                for event in events:
                    if isinstance(event, Exception):
                        raise event
                    yield event
            elif "port" in payload:
                yield {"type": "models", "data": {"data": [{"id": MODEL}]}}
            else:
                yield {"stage": "STOPPED"}
        finally:
            self.closed += 1


@pytest.fixture
async def deployment(tmp_path):
    store = CredentialStore(tmp_path)
    store.put(
        account_key("google-main"),
        {
            "kind": "account",
            "provider": "google_colab",
            "account": "google-main",
            "client_id": "123.apps.googleusercontent.com",
            "client_secret": "desktop-secret",
            "scopes": [COLAB_SCOPE],
            "access_token": "google-access",
            "expires_at": time.time() + 3600,
            "state": "CONNECTED",
        },
    )
    service, bridge = RuntimeService(), RuntimeBridge()
    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        manager = ColabManager(store, client, bridge_factory=bridge.factory)
        settings = DeploymentSettings(google_account="google-main", model_repo=MODEL, **SPEC)
        await manager.configure("colab-main", settings, hf_token="private-hf-token")
        yield manager, service, bridge, settings


async def test_full_lifecycle_setup_inference_transport_stop_and_delete(deployment):
    manager, service, bridge, settings = deployment
    stages = []
    await manager.start("colab-main", stages.append)
    assert stages == [
        "ALLOCATING",
        "INSTALLING",
        "DOWNLOADING",
        "STARTING",
        "READY",
        "VERIFYING",
        "VERIFIED",
    ]
    record = manager.record("colab-main")
    assert record["stage"] == "READY" and record["state"] == "CONNECTED"
    assert record["snapshot_commit"] == "commit-123" and record["last_success_at"] > 0
    assert bridge.payloads[0]["hf_token"] == "private-hf-token"
    assert bridge.payloads[0]["server_key"] == record["server_key"]
    assert bridge.payloads[1]["payload"]["model"] == MODEL
    assert bridge.tokens[0] != bridge.tokens[1]
    assert await manager.models("colab-main") == [{"id": MODEL, "name": MODEL}]
    public = await manager.status("colab-main")
    assert public["available"] and public["model_available"]
    assert all(
        secret not in json.dumps(public)
        for secret in ("private-hf-token", record["server_key"], "google-access")
    )
    await manager.stop("colab-main")
    assert manager.record("colab-main")["stage"] == "STOPPED"
    assert service.runtime is not None
    await manager.start("colab-main")
    assert (
        sum(
            request.method == "POST" and request.url.path == "/v1beta/runtimes"
            for request in service.requests
        )
        == 1
    )
    await manager.delete("colab-main")
    assert manager.record("colab-main")["stage"] == "DELETED"
    assert manager.record("colab-main")["runtime_name"] is None
    assert service.runtime is None
    assert manager.record("colab-main")["server_key"] == record["server_key"]


async def test_runtime_expiry_is_reported_and_replaced_only_on_explicit_start(deployment):
    manager, service, bridge, _ = deployment
    await manager.start("colab-main")
    previous = service.runtime
    service.runtime = None
    status = await manager.status("colab-main")
    assert status["stage"] == "EXPIRED" and status["available"] is False
    assert service.runtime is None
    await manager.start("colab-main")
    assert service.runtime != previous and manager.record("colab-main")["stage"] == "READY"


async def test_runtime_creation_retries_with_same_idempotency_key_after_lost_response(deployment):
    manager, service, _, settings = deployment
    service.create_error = 503
    with pytest.raises(GatewayError):
        await manager.start("colab-main")
    first = manager.record("colab-main")["request_id"]
    assert first
    service.runtime = None
    service.create_error = None
    service.eligible = (
        False  # Existing operation is recoverable even if new capacity is unavailable.
    )
    await manager.start("colab-main")
    creates = [
        request
        for request in service.requests
        if request.method == "POST" and request.url.path == "/v1beta/runtimes"
    ]
    assert len(creates) == 2
    assert creates[0].url.params["requestId"] == creates[1].url.params["requestId"] == first
    assert creates[0].url.params["runtimeId"] == creates[1].url.params["runtimeId"]


async def test_start_recovers_assignment_when_create_response_was_lost(deployment):
    manager, service, _, _ = deployment
    service.create_error = 503
    with pytest.raises(GatewayError):
        await manager.start("colab-main")
    assigned = service.runtime
    assert assigned
    service.create_error = None
    await manager.start("colab-main")
    assert manager.record("colab-main")["runtime_name"] == assigned
    assert (
        sum(
            request.method == "POST" and request.url.path == "/v1beta/runtimes"
            for request in service.requests
        )
        == 1
    )


async def test_delete_recovers_assignment_when_create_response_was_lost(deployment):
    manager, service, _, _ = deployment
    service.create_error = 503
    with pytest.raises(GatewayError):
        await manager.start("colab-main")
    assert service.runtime is not None
    await manager.delete("colab-main")
    assert service.runtime is None


async def test_colab_api_cooldown_is_shared_by_deployments_using_the_same_google_account(
    deployment,
):
    manager, service, bridge, settings = deployment
    await manager.configure("colab-second", settings)
    service.create_error = 429
    with pytest.raises(GatewayError):
        await manager.start("colab-main")
    calls = len(service.requests)
    service.create_error = None
    with pytest.raises(GatewayError) as error:
        await manager.start("colab-second")
    assert error.value.code == "rate_limited"
    assert len(service.requests) == calls and not bridge.payloads
    google = manager.google.record("google-main")
    assert google["state"] == "RATE_LIMITED" and google["retry_at"] > time.time()


async def test_terminal_allocation_error_can_be_retried_but_has_no_model_install(deployment):
    manager, service, bridge, _ = deployment
    service.operation_error = 8
    with pytest.raises(GatewayError) as error:
        await manager.start("colab-main")
    assert error.value.code == "rate_limited"
    assert bridge.payloads == []
    assert manager.record("colab-main")["request_id"] is None
    google = manager.google.record("google-main")
    assert google["state"] == "RATE_LIMITED" and google["retry_at"] > time.time()
    service.operation_error = None
    await manager.patch("colab-main", retry_at=0)
    async with manager.store.lock(account_key("google-main")):
        google["retry_at"] = 0
        manager.store.put(account_key("google-main"), google)
    await manager.start("colab-main")
    assert manager.record("colab-main")["stage"] == "READY"


@pytest.mark.parametrize("error_code", [8, 13])
async def test_operation_quota_is_shared_across_google_deployments(deployment, error_code):
    manager, service, bridge, settings = deployment
    await manager.configure("colab-second", settings)
    # Also cover an already-completed operation returned by the initial allocation.
    with pytest.raises(GatewayError) as caught:
        await manager.api.wait("google-main", {"done": True, "error": {"code": error_code}})
    assert caught.value.code == ("rate_limited" if error_code == 8 else "colab_operation_failed")
    if error_code == 8:
        with pytest.raises(GatewayError) as blocked:
            await manager.start("colab-second")
        assert blocked.value.code == "rate_limited"
        assert service.requests == [] and bridge.payloads == []
    else:
        assert "retry_at" not in manager.google.record("google-main")
        await manager.start("colab-second")
        assert manager.record("colab-second")["stage"] == "READY"


@pytest.mark.parametrize(
    "phase, code",
    [
        ("INSTALLING", "colab_install_failed"),
        ("DOWNLOADING", "colab_download_failed"),
        ("STARTING", "colab_model_load_failed"),
    ],
)
async def test_model_setup_failure_retains_runtime_and_specific_stage(deployment, phase, code):
    manager, service, bridge, _ = deployment
    bridge.failure = phase
    with pytest.raises(GatewayError) as error:
        await manager.start("colab-main")
    assert error.value.code == code
    saved = manager.record("colab-main")
    assert saved["stage"] == "FAILED" and saved["last_error_stage"] == phase
    assert service.runtime == saved["runtime_name"]
    assert len(bridge.payloads) == 1 and bridge.closed == 1
    bridge.failure = None
    await manager.start("colab-main")
    assert manager.record("colab-main")["stage"] == "READY"


async def test_install_error_reports_safe_diagnostic_and_persists_it(deployment):
    manager, _, bridge, _ = deployment
    bridge.failure = "INSTALLING"
    bridge.failure_detail = {
        "step": "dependencies",
        "reason": "package_unavailable",
        "python": "3.13.15",
        "package": "fastapi==0.143.0",
        "exit_code": 1,
        "stderr": "private-hf-token private-server-key",
    }
    with pytest.raises(GatewayError) as error:
        await manager.start("colab-main")
    assert "fastapi==0.143.0" in error.value.message and "3.13.15" in error.value.message
    assert "private-" not in error.value.message
    detail = manager.record("colab-main")["last_error_detail"]
    assert detail["step"] == "dependencies" and "stderr" not in detail
    assert (await manager.status("colab-main"))["last_error_detail"] == detail


def test_generated_installer_repairs_pipless_venv_and_sanitizes_failure(tmp_path, monkeypatch):
    config = {
        **DeploymentSettings(google_account="google-main", model_repo=MODEL, **SPEC).model_dump(),
        "deployment_id": "test-install",
        "server_key": "private-server-key",
        "hf_token": "private-hf-token",
    }
    root = tmp_path / "test-install"
    (root / "venv/bin").mkdir(parents=True)
    (root / "venv/bin/python").touch()  # A previous ensurepip failure left a partial venv.
    calls, events = [], []

    class Process:
        def __init__(self, arguments, **options):
            calls.append(arguments)
            self.returncode = 0 if "venv" in arguments else 1

        def communicate(self, **options):
            if self.returncode == 0:
                assert "--without-pip" in calls[-1]  # This runtime has no ensurepip.
                return "", ""
            return (
                "",
                "ERROR: No matching distribution found for fastapi==0.143.0\n"
                "private-hf-token private-server-key https://private.example/token",
            )

    monkeypatch.setattr(subprocess, "Popen", Process)
    display = types.ModuleType("IPython.display")
    display.display = lambda data, **options: events.extend(data.values())
    monkeypatch.setitem(sys.modules, "IPython", types.ModuleType("IPython"))
    monkeypatch.setitem(sys.modules, "IPython.display", display)
    code = bootstrap_script(config).replace("Path('/content/spas')", f"Path({str(tmp_path)!r})")
    with pytest.raises(RuntimeError, match="SPAS setup failed"):
        exec(compile(code, "bootstrap", "exec"), {})
    assert calls[0][:3] == [sys.executable, "-m", "venv"]
    assert "--system-site-packages" in calls[0]
    assert calls[1][:5] == [sys.executable, "-m", "pip", "--python", str(root / "venv/bin/python")]
    failure = events[-1]
    assert failure["phase"] == "INSTALLING"
    assert failure["diagnostic"]["reason"] == "package_unavailable"
    assert failure["diagnostic"]["package"] == "fastapi==0.143.0"
    assert "private-" not in json.dumps(events)


async def test_cancelled_setup_retains_assignment_and_closes_execution(deployment):
    manager, service, bridge, _ = deployment
    bridge.waiting = asyncio.Event()
    task = asyncio.create_task(manager.start("colab-main"))
    await asyncio.wait_for(bridge.waiting.wait(), 4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.record("colab-main")["stage"] == "INTERRUPTED"
    assert service.runtime and bridge.closed == 1
    await manager.delete("colab-main")


async def test_pending_assignment_blocks_account_or_gpu_change(deployment):
    manager, service, _, settings = deployment
    service.create_error = 503
    with pytest.raises(GatewayError):
        await manager.start("colab-main")
    with pytest.raises(GatewayError, match="удалите текущий runtime"):
        await manager.configure("colab-main", settings.model_copy(update={"accelerator": "L4"}))


async def test_verification_success_preserves_existing_cooldown(deployment):
    manager, _, _, _ = deployment
    retry_at = time.time() + 3600
    bridge = deployment[2]

    async def rate_limit_concurrent_request():
        await manager.patch("colab-main", state="RATE_LIMITED", retry_at=retry_at)

    bridge.during_inference = rate_limit_concurrent_request
    await manager.start("colab-main")
    saved = manager.record("colab-main")
    assert saved["state"] == "RATE_LIMITED" and saved["retry_at"] == retry_at


async def test_saving_unchanged_deployment_keeps_running_server_ready(deployment):
    manager, _, _, settings = deployment
    await manager.start("colab-main")
    previous = manager.record("colab-main")
    await manager.configure("colab-main", settings, hf_token="updated-private-token")
    saved = manager.record("colab-main")
    assert saved["stage"] == "READY" and saved["runtime_name"] == previous["runtime_name"]
    assert saved["state"] == "CONNECTED" and saved["hf_token"] == "updated-private-token"


async def test_managed_stream_requires_transport_marker_and_kernel_success(deployment):
    manager, _, bridge, settings = deployment
    await manager.start("colab-main")
    provider = ColabManagedAdapter(
        manager.client,
        AuthManager(manager.store, manager.client),
        BackendConfig(provider="colab_managed", account="colab-main", model=MODEL),
        manager=manager,
    )
    bridge.inference_events = [
        {
            "type": "chunk",
            "data": {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
        },
        {"type": "chunk", "data": {"choices": [{"delta": {}, "finish_reason": "stop"}]}},
        {"type": "chunk", "data": {"choices": [], "usage": completion()["usage"]}},
        {"type": "transport_done"},
    ]
    request = ChatRequest(
        model="local", messages=[{"role": "user", "content": "hello"}], stream=True
    )
    async with aclosing(provider.stream(request)) as source:
        events = [event async for event in source]
    assert [event.kind for event in events] == ["delta", "done"]
    assert events[0].text == "hello" and events[1].usage.completion_tokens == 2
    assert bridge.payloads[-1]["payload"]["stream_options"] == {"include_usage": True}
    bridge.inference_events.pop()
    with pytest.raises(GatewayError) as error:
        await provider.generate(request)
    assert error.value.code == "provider_incomplete"
    bridge.inference_events.append({"type": "transport_done"})
    bridge.inference_events.append(GatewayError("kernel disconnected", code="provider_incomplete"))
    observed = []
    with pytest.raises(GatewayError):
        async with aclosing(provider.stream(request)) as source:
            async for event in source:
                observed.append(event.kind)
    assert "done" not in observed


@pytest.mark.parametrize("status, code", [(403, "colab_api_access_denied"), (429, "rate_limited")])
async def test_colab_control_api_errors_are_public_and_sanitized(deployment, status, code):
    manager, service, bridge, _ = deployment
    service.create_error = status
    with pytest.raises(GatewayError) as error:
        await manager.start("colab-main")
    assert error.value.code == code
    assert "upstream-secret" not in str(error.value)
    assert bridge.payloads == []


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "http://runtime.test"},
        {"url": "https://secret@runtime.test"},
        {"url": "https://runtime.test?token=x"},
        {"url": "https://runtime.test:invalid"},
        {"expireTime": "2020-01-01T00:00:00Z"},
        {"expireTime": "2099-01-01T00:00:00"},
        {"token": ""},
    ],
)
def test_runtime_connection_rejects_invalid_or_expired_credentials(changes):
    info = {
        "url": "https://runtime.test",
        "token": "runtime-token",
        "expireTime": "2099-01-01T00:00:00Z",
        **changes,
    }
    with pytest.raises(GatewayError) as error:
        ColabAPI.connection({"connectionInfo": info})
    assert error.value.code == "colab_invalid_response"


def test_remote_scripts_compile_and_keep_user_inputs_as_data():
    hostile = "');__import__('os').system('exit 99')\n#"
    config = {
        **DeploymentSettings(google_account="google-main", model_repo=MODEL, **SPEC).model_dump(),
        "deployment_id": uuid.uuid4().hex,
        "server_key": "private-server-key",
        "hf_token": "private-hf-token",
    }
    script = bootstrap_script(config)
    compile(script, "bootstrap", "exec")
    download = next(
        node.value.value
        for node in ast.walk(ast.parse(script))
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "download" for target in node.targets)
    )
    compile(download, "download", "exec")
    for code in (
        script,
        inference_script(8000, hostile, {"messages": [{"content": hostile}]}),
        status_script(8000, hostile),
        stop_script(config["deployment_id"]),
    ):
        compile(code, "cell", "exec")
        assert hostile not in code and "private-hf-token" not in code
    assert script_data(script)["hf_token"] == "private-hf-token"
