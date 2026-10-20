"""Opt-in VM replacement while Gateway is running, respecting manual stops and quotas."""

import asyncio
import time
from contextlib import AsyncExitStack

from ..core.errors import GatewayError
from .manager import DeploymentSettings


class RuntimeSupervisor:
    def __init__(self, manager, accounts, *, interval=15):
        self.manager, self.accounts, self.interval = manager, accounts, interval
        self.jobs = {}

    def enabled(self, account):
        try:
            record = self.manager.record(account)
        except GatewayError:
            return False
        return record["settings"].get("auto_create_runtime", False) and not record.get(
            "auto_creation_paused", False
        )

    async def tick(self, account):
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(0.5):
                    await stack.enter_async_context(
                        self.manager.store.lock("colab-auto:" + account)
                    )
            except TimeoutError:
                return
            record = self.manager.record(account)
            if not self.enabled(account) or record.get("auto_next_retry_at", 0) > time.time():
                return
            try:
                settings = DeploymentSettings.model_validate(record["settings"])
                if record.get("runtime_name"):
                    try:
                        # Control-plane GET only; never execute keepalive cells.
                        await self.manager.api.get(settings.google_account, record["runtime_name"])
                        # An allocated VM can outlive a failed or interrupted
                        # setup. Only a verified deployment is ready to serve.
                        if record.get("stage") == "READY":
                            if record.get("auto_last_error") or record.get("auto_failures"):
                                await self.manager.patch(
                                    account,
                                    auto_last_error=None,
                                    auto_next_retry_at=0,
                                    auto_failures=0,
                                )
                            return
                    except GatewayError as error:
                        if error.code != "colab_runtime_missing":
                            raise
                        await self.manager.patch(account, stage="EXPIRED", state="DISCONNECTED")
                await self.manager.start(account, automatic=True)
                await self.manager.patch(
                    account, auto_last_error=None, auto_next_retry_at=0, auto_failures=0
                )
            except (GatewayError, ValueError) as error:
                record = self.manager.record(account)
                failures = min(record.get("auto_failures", 0) + 1, 8)
                code = error.code if isinstance(error, GatewayError) else "invalid_config"
                transient = code in {
                    "rate_limited",
                    "provider_unavailable",
                    "provider_timeout",
                    "colab_spec_unavailable",
                    "colab_operation_failed",
                    "colab_connection_expired",
                    "colab_runtime_missing",
                }
                await self.manager.patch(
                    account,
                    auto_last_error=code,
                    auto_failures=failures,
                    auto_creation_paused=not transient or record.get("auto_creation_paused", False),
                    auto_next_retry_at=time.time() + min(60 * 2 ** (failures - 1), 900)
                    if transient
                    else 0,
                )

    async def run(self):
        try:
            while True:
                accounts = set(self.accounts())
                for account, task in list(self.jobs.items()):
                    if (
                        account not in accounts or not self.enabled(account)
                    ) and not task.cancelling():
                        task.cancel()
                    if task.done():
                        if not task.cancelled():
                            # Errors never include credentials in logs or responses.
                            task.exception()
                        del self.jobs[account]
                for account in accounts:
                    if account not in self.jobs and self.enabled(account):
                        self.jobs[account] = asyncio.create_task(self.tick(account))
                await asyncio.sleep(self.interval)
        finally:
            for task in self.jobs.values():
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)
