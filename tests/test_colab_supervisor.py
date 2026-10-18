import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.manager import account_key
from llm.gateway.colab.lifetime import vm_time
from llm.gateway.colab.manager import ColabManager
from llm.gateway.colab.supervisor import RuntimeSupervisor
from llm.gateway.core.errors import GatewayError


@pytest.fixture
async def automatic(tmp_path):
    store = CredentialStore(tmp_path)
    store.put(
        account_key("gpu"),
        {
            "provider": "colab_managed",
            "account": "gpu",
            "stage": "READY",
            "runtime_name": "runtimes/old",
            "runtime_started_at": 100,
            "settings": {
                "google_account": "google",
                "accelerator": "T4",
                "model_repo": "team/tiny",
                "auto_create_runtime": True,
                "vm_lifetime_hours": 12,
            },
        },
    )
    async with httpx.AsyncClient() as client:
        manager = ColabManager(store, client)
        manager.api.get = AsyncMock()
        manager.start = AsyncMock()
        yield manager, RuntimeSupervisor(manager, lambda: {"gpu"}, interval=0.01)


def test_vm_timer_is_an_estimate_and_never_uses_proxy_token_expiration():
    record = {
        "runtime_started_at": 100,
        "stage": "READY",
        "settings": {"vm_lifetime_hours": 12},
        "connectionInfo": {"expireTime": "2100-01-01T00:00:00Z"},
    }
    assert vm_time(record, now=3700)["remaining_seconds"] == 11 * 3600
    assert vm_time(record, now=3700)["source"] == "estimate"
    record["stage"] = "EXPIRED"
    assert vm_time(record, now=3700)["remaining_seconds"] == 0
    assert vm_time({"settings": {}}, now=3700)["remaining_seconds"] is None


async def test_existing_vm_is_not_replaced_even_when_estimate_reached_zero(automatic):
    manager, supervisor = automatic
    await supervisor.tick("gpu")
    manager.api.get.assert_awaited_once()
    manager.start.assert_not_awaited()


async def test_transient_setup_failure_retries_on_the_allocated_vm_after_backoff(automatic):
    manager, supervisor = automatic
    await manager.patch("gpu", runtime_name=None, stage="EXPIRED")

    async def fail_after_allocation(*args, **kwargs):
        await manager.patch("gpu", runtime_name="runtimes/new", stage="FAILED")
        raise GatewayError("private timeout detail", code="provider_timeout", status=504)

    manager.start.side_effect = fail_after_allocation
    await supervisor.tick("gpu")
    record = manager.record("gpu")
    assert record["runtime_name"] == "runtimes/new"
    assert record["auto_next_retry_at"] > time.time()
    assert not record["auto_creation_paused"]
    await supervisor.tick("gpu")
    assert manager.start.await_count == 1
    manager.api.get.assert_not_awaited()

    await manager.patch("gpu", auto_next_retry_at=time.time() - 1)

    async def recover(*args, **kwargs):
        assert manager.record("gpu")["runtime_name"] == "runtimes/new"
        await manager.patch("gpu", stage="READY")

    manager.start.side_effect = recover
    await supervisor.tick("gpu")
    assert manager.start.await_count == 2
    manager.api.get.assert_awaited_once_with("google", "runtimes/new")
    record = manager.record("gpu")
    assert record["stage"] == "READY"
    assert record["auto_failures"] == 0 and record["auto_last_error"] is None
    await supervisor.tick("gpu")
    assert manager.start.await_count == 2


@pytest.mark.parametrize("stage", ["INTERRUPTED", "VERIFYING"])
async def test_incomplete_setup_is_resumed_after_gateway_restart(automatic, stage):
    manager, supervisor = automatic
    await manager.patch("gpu", stage=stage)
    await supervisor.tick("gpu")
    manager.start.assert_awaited_once_with("gpu", automatic=True)
    assert manager.record("gpu")["runtime_name"] == "runtimes/old"


async def test_control_plane_recovery_clears_stale_auto_error_without_restarting_model(automatic):
    manager, supervisor = automatic
    manager.api.get.side_effect = GatewayError("temporary failure", code="provider_unavailable")
    await supervisor.tick("gpu")
    assert manager.record("gpu")["auto_last_error"] == "provider_unavailable"
    await manager.patch("gpu", auto_next_retry_at=time.time() - 1)
    manager.api.get.side_effect = None
    await supervisor.tick("gpu")
    record = manager.record("gpu")
    assert record["auto_last_error"] is None
    assert record["auto_failures"] == record["auto_next_retry_at"] == 0
    manager.start.assert_not_awaited()


async def test_missing_vm_triggers_full_start_only_when_opted_in(automatic):
    manager, supervisor = automatic
    manager.api.get.side_effect = GatewayError("expired", code="colab_runtime_missing")
    await supervisor.tick("gpu")
    manager.start.assert_awaited_once_with("gpu", automatic=True)
    manager.start.reset_mock()
    await manager.set_runtime_policy("gpu", enabled=False, lifetime_hours=24)
    assert manager.record("gpu")["stage"] == "EXPIRED"
    await supervisor.tick("gpu")
    manager.start.assert_not_awaited()


async def test_manual_pause_prevents_recreation_and_policy_can_resume_it(automatic):
    manager, supervisor = automatic
    await manager.patch("gpu", auto_creation_paused=True, runtime_name=None, stage="DELETED")
    await supervisor.tick("gpu")
    manager.start.assert_not_awaited()
    await manager.set_runtime_policy("gpu", enabled=True, lifetime_hours=12)
    await supervisor.tick("gpu")
    manager.start.assert_awaited_once_with("gpu", automatic=True)


@pytest.mark.parametrize(
    "code,paused", [("rate_limited", False), ("colab_model_load_failed", True)]
)
async def test_quota_uses_backoff_but_model_failure_pauses_automation(automatic, code, paused):
    manager, supervisor = automatic
    await manager.patch("gpu", runtime_name=None)
    manager.start.side_effect = GatewayError("private-error-body", code=code)
    await supervisor.tick("gpu")
    record = manager.record("gpu")
    assert record["auto_last_error"] == code and record["auto_creation_paused"] == paused
    if not paused:
        assert record["auto_next_retry_at"] > time.time()
    await supervisor.tick("gpu")
    assert manager.start.await_count == 1


async def test_supervisors_cannot_allocate_same_account_concurrently(automatic):
    manager, supervisor = automatic
    await manager.patch("gpu", runtime_name=None)
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(*args, **kwargs):
        entered.set()
        await release.wait()
        await manager.patch("gpu", runtime_name="runtimes/new", stage="READY")

    manager.start.side_effect = start
    first = asyncio.create_task(supervisor.tick("gpu"))
    await entered.wait()
    await RuntimeSupervisor(manager, lambda: {"gpu"}).tick("gpu")
    release.set()
    await first
    assert manager.start.await_count == 1


async def test_disabled_policy_cancels_inflight_background_start(automatic):
    manager, supervisor = automatic
    await manager.patch("gpu", runtime_name=None)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def start(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    manager.start.side_effect = start
    task = asyncio.create_task(supervisor.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await manager.set_runtime_policy("gpu", enabled=False, lifetime_hours=12)
        await asyncio.wait_for(cancelled.wait(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_disabled_policy_waits_for_pending_assignment_cleanup(automatic):
    manager, supervisor = automatic
    await manager.patch("gpu", runtime_name=None)
    entered, cleaning, release, interrupted = (asyncio.Event() for _ in range(4))

    async def start(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            # A Colab assignment runs in a thread: cancellation must wait for
            # its result before releasing the deployment control lock.
            cleaning.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            raise

    manager.start.side_effect = start
    task = asyncio.create_task(supervisor.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await manager.set_runtime_policy("gpu", enabled=False, lifetime_hours=12)
        await asyncio.wait_for(cleaning.wait(), 1)
        await asyncio.sleep(supervisor.interval * 4)
        assert not interrupted.is_set()
        task.cancel()
        await asyncio.sleep(supervisor.interval * 2)
        assert not task.done() and not interrupted.is_set()
    finally:
        release.set()
        if not task.cancelling():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
