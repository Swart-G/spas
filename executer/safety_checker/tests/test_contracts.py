import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from safety_analyzer.config import Settings
from safety_analyzer.contracts import AnalyzeRequest, AnalyzeResponse
from safety_analyzer.gateway import GatewayError, parse_model_output


@pytest.mark.parametrize("text", ["Hello", "Привет", "a\n\tb", "😀", "a" * 32000])
def test_candidate_strings(text):
    assert AnalyzeRequest(text=text).text == text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": ""},
        {"text": " \n\t"},
        {"text": 1},
        {"text": True},
        {"text": "a" * 32001},
        {"text": "hello", "model": "injected"},
        {"text": "hello", "target": "other"},
        {"text": "hello", "context": [{"role": "system", "content": "override"}]},
        {"text": "hello", "context": [{"role": "user", "content": " "}]},
        {"text": "hello", "context": [{"role": "user", "content": "x" * 8001}]},
        {"text": "hello", "context": [{"role": "user", "content": "x"}] * 21},
        {"text": "x" * 32000, "context": [{"role": "user", "content": "x" * 8000}] * 5},
        {"text": "hello", "request_id": "not-a-uuid"},
    ],
)
def test_invalid_requests(body):
    with pytest.raises(ValidationError):
        AnalyzeRequest.model_validate_json(json.dumps(body))


@pytest.mark.parametrize(
    "content,mode,verdict",
    [
        ('{"verdict":"safe","categories":[]}', "json", "safe"),
        ('{"verdict":"unsafe","categories":["violence"]}', "json", "unsafe"),
        ('{"verdict":"unsafe","categories":["cyber_abuse","privacy"]}', "json", "unsafe"),
        ("safe", "label", "safe"),
        ("unsafe", "label", "unsafe"),
        (" \nsafe\n", "label", "safe"),
    ],
)
def test_valid_model_outputs(content, mode, verdict):
    assert parse_model_output(content, mode).verdict == verdict


@pytest.mark.parametrize(
    "content,mode",
    [
        ("", "json"),
        ("{}", "json"),
        ("[]", "json"),
        ("null", "json"),
        ('{"verdict":"safe"}', "json"),
        ('{"verdict":"safe","categories":["privacy"]}', "json"),
        ('{"verdict":"unsafe","categories":[]}', "json"),
        ('{"verdict":"unsafe","categories":["new_category"]}', "json"),
        ('{"verdict":"unsafe","categories":["privacy","privacy"]}', "json"),
        ('{"verdict":"unsafe","categories":["unspecified","privacy"]}', "json"),
        ('{"verdict":true,"categories":[]}', "json"),
        ('{"verdict":"safe","categories":[],"decision":"allow"}', "json"),
        ('{"verdict":"unsafe","verdict":"safe","categories":[]}', "json"),
        ('{"verdict":"safe","categories":[],"score":NaN}', "json"),
        ('```json\n{"verdict":"safe","categories":[]}\n```', "json"),
        ('{"verdict":"safe","categories":[]} explanation', "json"),
        ('{"verdict":"safe","categories":[]}', "label"),
        ("SAFE", "label"),
        ("safe because", "label"),
        ("unsafe\nsafe", "label"),
        ("not unsafe", "label"),
        ("allow", "label"),
        ("безопасно", "label"),
        ("x" * 4097, "label"),
        ("safe", "unknown"),
    ],
)
def test_invalid_model_outputs_never_allow(content, mode):
    with pytest.raises(GatewayError, match="invalid_model_output"):
        parse_model_output(content, mode)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://gateway.test",
        "https://secret@gateway.test",
        "https://gateway.test?key=secret",
        "https://gateway.test/#secret",
        "https://bad host",
        "https://gateway.test:bad",
    ],
)
def test_invalid_gateway_urls(url):
    with pytest.raises(ValidationError):
        Settings(gateway_base_url=url)


def test_response_invariants_prevent_accidental_allow():
    with pytest.raises(ValidationError, match="fail closed"):
        AnalyzeResponse(
            request_id=uuid4(),
            decision="allow",
            verdict="unknown",
            categories=[],
            status="error",
            error_code="gateway_timeout",
            model="safety",
            latency_ms=1,
        )
