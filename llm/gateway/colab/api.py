import asyncio
import re
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from ..auth.google import GoogleAuth
from ..auth.manager import AuthManager
from ..core.errors import GatewayError, upstream_error

BASE = "https://colaboratory.googleapis.com"


class ColabOperationError(GatewayError):
    """A confirmed terminal operation failure, distinct from a failed polling request."""


def resource_name(name, prefix):
    if not isinstance(name, str) or not re.fullmatch(prefix + r"/[A-Za-z0-9/_-]+", name):
        raise GatewayError("Colab вернул неверное имя ресурса.", code="colab_invalid_response")
    return name


class ColabAPI:
    def __init__(self, google: GoogleAuth, client: httpx.AsyncClient):
        self.google, self.client = google, client
        self.auth = AuthManager(google.store, client)

    async def request(self, account, method, path, **options):
        self.auth.check_cooldown(account)
        token = await self.google.access_token(account)
        self.auth.check_cooldown(account)
        try:
            response = await self.client.request(
                method,
                BASE + path,
                headers={"Authorization": "Bearer " + token},
                follow_redirects=False,
                timeout=30,
                **options,
            )
            if response.status_code == 403:
                raise GatewayError(
                    "Нет доступа к Colab API: проверьте allowlist проекта, "
                    "включение API и OAuth scope.",
                    code="colab_api_access_denied",
                    status=403,
                )
            if response.status_code == 404:
                raise GatewayError(
                    "Runtime Colab больше не существует.", code="colab_runtime_missing", status=404
                )
            if not 200 <= response.status_code < 300:
                error = upstream_error(
                    response.status_code, retry_after=response.headers.get("Retry-After")
                )
                if error.code == "rate_limited":
                    await self.auth.note_result(account, error)
                raise error
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid response")
            return data
        except (httpx.HTTPError, ValueError) as error:
            raise GatewayError(
                "Сервис управления Colab недоступен.", code="provider_unavailable"
            ) from error

    async def specs(self, account):
        data = await self.request(account, "GET", "/v1beta/runtimespecs")
        specs = data.get("runtimeSpecs")
        if not isinstance(specs, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("key"), dict)
            or any(
                not isinstance(item["key"].get(key), str)
                for key in ("variant", "accelerator", "shape")
            )
            for item in specs
        ):
            raise GatewayError(
                "Colab вернул неверный каталог runtime.", code="colab_invalid_response"
            )
        return specs

    async def subscription(self, account):
        return await self.request(account, "GET", "/v1beta/subscription")

    async def runtimes(self, account):
        data = await self.request(account, "GET", "/v1beta/runtimes")
        runtimes = data.get("runtimes", [])
        if not isinstance(runtimes, list) or any(not isinstance(item, dict) for item in runtimes):
            raise GatewayError(
                "Colab вернул неверный список runtime.", code="colab_invalid_response"
            )
        return runtimes

    async def create(self, account, spec, runtime_id, request_id, version=""):
        body = {"runtimeSpec": spec}
        if version:
            body["version"] = version
        return await self.request(
            account,
            "POST",
            "/v1beta/runtimes",
            params={"runtimeId": runtime_id, "requestId": request_id},
            json=body,
        )

    async def get(self, account, runtime):
        return await self.request(account, "GET", "/v1beta/" + resource_name(runtime, "runtimes"))

    async def delete(self, account, runtime):
        return await self.request(
            account, "DELETE", "/v1beta/" + resource_name(runtime, "runtimes")
        )

    async def wait(self, account, operation, *, wait_seconds=600):
        async with asyncio.timeout(wait_seconds):
            while True:
                if not isinstance(operation, dict):
                    raise GatewayError(
                        "Colab вернул неверную операцию.", code="colab_invalid_response"
                    )
                if operation.get("done"):
                    error = operation.get("error")
                    if error:
                        code = error.get("code") if isinstance(error, dict) else None
                        failure = ColabOperationError(
                            "Colab не выполнил операцию. "
                            "Проверьте доступность GPU и лимиты аккаунта.",
                            code="rate_limited" if code == 8 else "colab_operation_failed",
                            status=429 if code == 8 else 503,
                        )
                        if failure.code == "rate_limited":
                            await self.auth.note_result(account, failure)
                        raise failure
                    result = operation.get("response", {})
                    if not isinstance(result, dict):
                        raise GatewayError(
                            "Colab вернул неверный результат операции.",
                            code="colab_invalid_response",
                        )
                    return result
                name = resource_name(operation.get("name"), "operations")
                await asyncio.sleep(2)
                operation = await self.request(account, "GET", "/v1/" + name)

    @staticmethod
    def connection(runtime):
        try:
            info = runtime["connectionInfo"]
            token, url = info["token"], info["url"]
            parsed = urlsplit(url)
            _ = parsed.port
            expiry = datetime.fromisoformat(info["expireTime"].replace("Z", "+00:00"))
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or any(character.isspace() for character in url)
                or not isinstance(token, str)
                or not token
                or expiry.tzinfo is None
                or expiry.timestamp() <= datetime.now().timestamp()
            ):
                raise ValueError("Invalid connection")
            return {"url": url.rstrip("/"), "token": token}
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise GatewayError(
                "Colab не выдал действующее защищённое соединение.", code="colab_invalid_response"
            ) from error
