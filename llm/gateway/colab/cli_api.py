"""Official Colab CLI control plane; runtime execution still uses private Jupyter."""

import asyncio
import logging
import uuid
from contextlib import suppress

from ..auth.manager import AuthManager
from ..core.errors import GatewayError, upstream_error
from .api import ColabAPI, resource_name


class ColabCLIAPI:
    def __init__(self, google, client):
        self.google, self.client = google, client
        self.auth = AuthManager(google.store, client)

    async def call(self, account, operation):
        from colab_cli.client import Client, ColabRequestError, Prod, TooManyAssignmentsError
        from requests import Session

        class BoundedSession(Session):
            def request(self, *args, **kwargs):
                kwargs.update(timeout=30, allow_redirects=False)
                return super().request(*args, **kwargs)

        self.auth.check_cooldown(account)
        token = await self.google.access_token(account)
        self.auth.check_cooldown(account)

        def invoke():
            logger = logging.Logger("spas.colab.control", level=logging.CRITICAL + 1)
            with BoundedSession() as session:
                session.headers["Authorization"] = "Bearer " + token
                return operation(Client(Prod(), session, logger=logger))

        task = asyncio.create_task(asyncio.to_thread(invoke))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Keep the control lock until a pending assignment has finished. Its
            # stable notebook UUID allows the next explicit Start to recover it.
            with suppress(Exception):
                await task
            raise
        except TooManyAssignmentsError as error:
            limited = GatewayError("Достигнут лимит сессий Colab.", code="rate_limited", status=429)
            await self.auth.note_result(account, limited)
            raise limited from error
        except ColabRequestError as error:
            status = error.response.status_code
            if status in (401, 403):
                raise GatewayError(
                    "Повторите вход Google или проверьте доступ к Colab.",
                    code="reauth_required",
                    status=401,
                ) from error
            failure = upstream_error(status, retry_after=error.response.headers.get("Retry-After"))
            if failure.code == "rate_limited":
                await self.auth.note_result(account, failure)
            raise failure from error
        except GatewayError:
            raise
        except Exception as error:
            # Neither CLI debug output nor upstream response bodies may expose tokens.
            raise GatewayError(
                "Colab CLI не выполнил запрос.", code="provider_unavailable"
            ) from error

    async def specs(self, account):
        from colab_cli.client import Accelerator

        await self.call(account, lambda client: client.list_assignments())
        # The CLI has no eligibility catalogue. These are requestable types;
        # actual allocation is decided by Colab quota/capacity at Start.
        return [
            {
                "key": {"variant": "VARIANT_CPU", "accelerator": "NONE", "shape": "SHAPE_STANDARD"},
                "eligible": True,
                "availability": "on_start",
            }
        ] + [
            {
                "key": {"variant": "VARIANT_GPU", "accelerator": accelerator.value, "shape": shape},
                "eligible": True,
                "availability": "on_start",
            }
            for accelerator in Accelerator
            if accelerator.value in {"T4", "L4", "G4", "A100", "H100"}
            for shape in (
                ["SHAPE_STANDARD"]
                if accelerator.value == "L4"
                else ["SHAPE_STANDARD", "SHAPE_HIGHMEM"]
            )
        ]

    @staticmethod
    def options(spec):
        from colab_cli.client import Accelerator, Shape, Variant

        return {
            "variant": Variant.GPU if spec["variant"] == "VARIANT_GPU" else Variant.DEFAULT,
            "accelerator": Accelerator(spec["accelerator"]),
            "shape": Shape.HIGH_RAM if spec["shape"] == "SHAPE_HIGHMEM" else None,
        }

    @staticmethod
    def notebook(runtime):
        name = resource_name(runtime, "runtimes").split("/", 1)[1]
        try:
            return uuid.UUID(name.removeprefix("spas-"))
        except ValueError as error:
            raise GatewayError("Неверный идентификатор сессии Colab.") from error

    def spec_for(self, account, runtime):
        runtime_id = resource_name(runtime, "runtimes").split("/", 1)[1]
        for record in self.google.store.all_accounts():
            settings = record.get("settings", {})
            if record.get("runtime_id") == runtime_id and settings.get("google_account") == account:
                return {key: settings[key] for key in ("variant", "accelerator", "shape")}
        raise GatewayError("Сессия Colab не найдена.", code="colab_runtime_missing", status=404)

    @staticmethod
    def runtime(name, assignment):
        info = assignment.runtime_proxy_info
        return {
            "name": name,
            "endpoint": assignment.endpoint,
            "connectionInfo": {
                "url": info.url,
                "token": info.token,
                "expireTime": info.expires_at().isoformat(),
            },
        }

    async def create(self, account, spec, runtime_id, request_id, version=""):
        if version:
            raise GatewayError("Colab CLI использует актуальный образ; уберите версию runtime.")
        name = "runtimes/" + runtime_id
        assignment = await self.call(
            account, lambda client: client.assign(self.notebook(name), **self.options(spec))
        )
        return {"done": True, "response": self.runtime(name, assignment)}

    async def get(self, account, runtime):
        from colab_cli.client import GetAssignmentResponse

        spec = self.spec_for(account, runtime)
        assignment = await self.call(
            account,
            lambda client: client._get_assignment(self.notebook(runtime), **self.options(spec)),
        )
        # This is a GET only: it never posts an assignment when a runtime expired.
        if isinstance(assignment, GetAssignmentResponse):
            raise GatewayError("Runtime Colab истёк.", code="colab_runtime_missing", status=404)
        return self.runtime(runtime, assignment)

    async def delete(self, account, runtime):
        assigned = await self.get(account, runtime)
        await self.call(account, lambda client: client.unassign(assigned["endpoint"]))
        return {"done": True, "response": {}}

    async def wait(self, account, operation, *, wait_seconds=600):
        if not isinstance(operation, dict) or not operation.get("done"):
            raise GatewayError("Неверный ответ Colab CLI.", code="colab_invalid_response")
        return operation.get("response", {})

    async def subscription(self, account):
        info = await self.call(account, lambda client: client.get_consumption_user_info())
        return info.model_dump(mode="json")


class ColabControl:
    """Select the control plane from the encrypted Google account profile."""

    connection = staticmethod(ColabAPI.connection)

    def __init__(self, google, client):
        self.google = google
        self.api = ColabAPI(google, client)
        self.cli = ColabCLIAPI(google, client)

    def __getattr__(self, method):
        async def dispatch(account, *args, **kwargs):
            backend = (
                self.cli if self.google.record(account).get("auth_transport") == "cli" else self.api
            )
            return await getattr(backend, method)(account, *args, **kwargs)

        return dispatch
