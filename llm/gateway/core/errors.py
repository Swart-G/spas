"""Public errors deliberately exclude upstream bodies and credentials."""

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
        return max(0.0, float(value))
    except ValueError:
        try:
            reset = parsedate_to_datetime(value)
            return max(0.0, (reset - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def upstream_error(status: int, code: str = "", retry_after: str | None = None) -> GatewayError:
    if status == 429 or code in {"subscription_sharing_usage_limit_exceeded", "rate_limit"}:
        return GatewayError(
            "Лимит провайдера исчерпан. Проверьте Usage в настройках подписки.",
            code="rate_limited",
            status=429,
            retry_after=retry_after_seconds(retry_after),
        )
    if status == 401 or code in {"authentication_failed", "subscription_sharing_invalid_user"}:
        return GatewayError(
            "Авторизация провайдера недействительна. Выполните вход в этот аккаунт.",
            code="reauth_required",
        )
    if status == 403:
        return GatewayError(
            "Провайдер отклонил доступ по правилам аккаунта, региона или интеграции.",
            code="provider_access_denied",
            status=403,
        )
    if status == 400:
        return GatewayError(
            "Провайдер не поддерживает этот запрос или выбранную модель.",
            code="provider_invalid_request",
            status=400,
        )
    return GatewayError(
        "Провайдер временно недоступен.", retry_after=retry_after_seconds(retry_after)
    )
