import math

import pytest
from pydantic import ValidationError

from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.errors import retry_after_seconds, upstream_payload_error


@pytest.mark.parametrize(
    "error,code,status",
    [
        ({"code": 429}, "rate_limited", 429),
        ({"code": "429"}, "rate_limited", 429),
        ({"status": 429}, "rate_limited", 429),
        ({"status_code": "401"}, "reauth_required", 503),
        ({"code": "invalid_api_key"}, "reauth_required", 503),
        ({"code": None, "type": "insufficient_quota"}, "rate_limited", 429),
        ({"code": "unknown", "type": "authentication_failed"}, "reauth_required", 503),
        ({"code": 400}, "provider_invalid_request", 400),
        ({"code": 402}, "provider_payment_required", 402),
        ({"code": 403}, "provider_access_denied", 403),
        ({"code": 413}, "provider_invalid_request", 413),
        ({"code": 422}, "provider_invalid_request", 400),
        ({"code": 504}, "provider_timeout", 504),
        ({"code": 524}, "provider_timeout", 504),
        ({"code": [429]}, "provider_unavailable", 503),
        ({"code": {"status": 401}}, "provider_unavailable", 503),
        ({"code": True}, "provider_unavailable", 503),
        ([], "provider_unavailable", 503),
        (None, "provider_unavailable", 503),
    ],
)
def test_embedded_errors_are_normalized_and_sanitized(error, code, status):
    if isinstance(error, dict):
        error = {**error, "message": "private-provider-body", "retry_after": 17}
    public = upstream_payload_error(error)
    assert public.code == code and public.status == status
    assert "private-provider-body" not in public.message
    if code == "rate_limited":
        assert public.retry_after == 17


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e309", "not-a-date"])
def test_nonfinite_retry_after_cannot_poison_account_state(value):
    assert retry_after_seconds(value) is None
    assert upstream_payload_error({"code": 429, "retry_after": value}).retry_after is None


@pytest.mark.parametrize("value,expected", [("0", 0), ("-1", 0), ("17", 17), ("0.5", 0.5)])
def test_retry_after_is_finite_and_nonnegative(value, expected):
    seconds = retry_after_seconds(value)
    assert math.isfinite(seconds) and seconds == expected


def test_real_http_status_takes_precedence_over_numeric_body_status():
    error = upstream_payload_error({"code": 429}, status=403)
    assert error.code == "provider_access_denied" and error.status == 403


@pytest.mark.parametrize("model", [" ", "bad\x1b[2J", "bad\x00value", "bad\ud800"])
def test_model_ids_reject_empty_and_terminal_control_sequences(model):
    with pytest.raises(ValidationError):
        BackendConfig(provider="openai_api", account="custom", model=model)


@pytest.mark.parametrize("alias", [" ", "x" * 201, "bad\x1b[2J"])
def test_route_names_are_valid_gateway_model_ids(alias):
    with pytest.raises(ValidationError):
        GatewayConfig(
            backends={
                "custom": BackendConfig(provider="openai_api", account="custom", model="model")
            },
            routes={alias: ["custom"]},
        )
