"""Public errors deliberately exclude upstream bodies and credentials."""

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any


class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_unavailable",
        status: int = 503,
        param: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.param = param
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error" if self.status == 400 else "gateway_error",
                "code": self.code,
                "param": self.param,
            }
        }


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            reset = parsedate_to_datetime(value)
            return max(0.0, (reset - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


_ERROR_STATUSES = {
    "subscription_sharing_usage_limit_exceeded": 429,
    "rate_limit": 429,
    "rate_limit_exceeded": 429,
    "insufficient_quota": 429,
    "authentication_failed": 401,
    "subscription_sharing_invalid_user": 401,
    "invalid_api_key": 401,
}


def upstream_payload_error(
    error: Any, *, status: int | None = None, retry_after: str | None = None
) -> GatewayError:
    details = error if isinstance(error, dict) else {"code": error}
    codes = [details.get("code"), details.get("type")]
    code = next(
        (value for value in codes if isinstance(value, str) and value in _ERROR_STATUSES), ""
    )
    if status is None:
        for value in [details.get("status"), details.get("status_code"), *codes]:
            if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isdecimal():
                value = int(value)
            if type(value) is int and 400 <= value <= 599:
                status = value
                break
    if retry_after is None and type(details.get("retry_after")) in (str, int, float):
        retry_after = str(details["retry_after"])
    return upstream_error(status or 503, code, retry_after)


def upstream_error(status: int, code: str = "", retry_after: str | None = None) -> GatewayError:
    if status == 429 or _ERROR_STATUSES.get(code) == 429:
        return GatewayError(
            "Лимит сервиса исчерпан. Проверьте квоту аккаунта или повторите запрос позже.",
            code="rate_limited",
            status=429,
            retry_after=retry_after_seconds(retry_after),
        )
    if status == 401 or _ERROR_STATUSES.get(code) == 401:
        return GatewayError(
            "Сервис отклонил авторизацию. Войдите снова или обновите API-ключ.",
            code="reauth_required",
        )
    if status == 403:
        return GatewayError(
            "Сервис ограничил доступ. Проверьте разрешения аккаунта "
            "и доступность модели в вашем регионе.",
            code="provider_access_denied",
            status=403,
        )
    if status in {400, 422}:
        return GatewayError(
            "Сервис отклонил запрос. Проверьте модель и параметры запроса.",
            code="provider_invalid_request",
            status=400,
        )
    if status == 402:
        return GatewayError(
            "На аккаунте сервиса недостаточно средств или исчерпана квота API.",
            code="provider_payment_required",
            status=402,
        )
    if status == 413:
        return GatewayError(
            "Запрос слишком большой для сервиса. Сократите текст или уменьшите изображения.",
            code="provider_invalid_request",
            status=413,
        )
    if status in {504, 524}:
        return GatewayError(
            "Сервис не ответил вовремя. Повторите запрос или увеличьте время ожидания.",
            code="provider_timeout",
            status=504,
        )
    return GatewayError(
        "Сервис временно недоступен. Повторите запрос позже.",
        retry_after=retry_after_seconds(retry_after),
    )
