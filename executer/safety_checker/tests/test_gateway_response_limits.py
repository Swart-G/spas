"""Raw streaming limits must hold before decompression, allocation, or MCP dispatch."""

import gzip
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import completion
from test_commands import ALLOW, command_request
from test_decision_adapter import answer

import safety_analyzer.gateway as gateway_module
from safety_analyzer.contracts import AnalyzeRequest
from safety_analyzer.dispatch import CheckedExecutor

LIMIT = 32768


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture(params=["chat_completions", "decisions"])
def api(request):
    return request.param


def valid_body(api):
    envelope = answer() if api == "decisions" else completion(json.dumps(ALLOW))
    return json.dumps(envelope).encode()


async def denied_dispatch(
    factory, api, stream, headers, expected_error="invalid_model_output", status=200
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers=headers, stream=stream)

    executor = AsyncMock()
    async with factory(handler, command_api=api) as analyzer:
        result = await CheckedExecutor(analyzer, executor).execute(command_request())
    assert result.assessment.decision == "DENY"
    assert result.assessment.status == "error"
    assert result.assessment.error_code == expected_error
    assert result.assessment.model_result is None
    assert not result.dispatch_attempted and result.execution_status == "not_started"
    executor.call_tool.assert_not_awaited()
    assert len(calls) == 1 and calls[0].headers["Accept-Encoding"] == "identity"
    assert stream.closed


@pytest.mark.parametrize(
    "encoding",
    [
        "gzip",
        "GZip",
        "deflate",
        "br",
        "zstd",
        "unknown",
        "",
        "gzip, identity",
        "identity, gzip",
        "identity, identity",
    ],
)
async def test_unsupported_encoding_is_rejected_before_first_body_read(
    command_analyzer_for, api, encoding
):
    # 2 MiB expanded body fits in a tiny compressed wire response. It must never be read.
    stream = ObservedStream(gzip.compress(b" " * (2 * 1024 * 1024)))
    await denied_dispatch(command_analyzer_for, api, stream, {"Content-Encoding": encoding})
    assert stream.reads == 0


async def test_duplicate_content_encodings_are_not_interpreted_as_identity(
    command_analyzer_for, api
):
    stream = ObservedStream(b"not read")
    await denied_dispatch(
        command_analyzer_for,
        api,
        stream,
        [("Content-Encoding", "identity"), ("Content-Encoding", "gzip")],
    )
    assert stream.reads == 0


@pytest.mark.parametrize("length", ["32769", "99999999999999999999", "-1", "bad", "1, 1"])
async def test_invalid_or_oversized_content_length_is_rejected_before_body(
    command_analyzer_for, api, length
):
    stream = ObservedStream(valid_body(api))
    await denied_dispatch(command_analyzer_for, api, stream, {"Content-Length": length})
    assert stream.reads == 0


@pytest.mark.parametrize(
    "chunks,length",
    [
        ([b"x" * (LIMIT + 1)], None),
        ([b"x" * (LIMIT + 1)], "1"),
        ([b"x" * (LIMIT - 1), b"xx"], None),
        ([b"x" * LIMIT, b"x"], "32768"),
    ],
)
async def test_size_checked_before_buffer_extension_and_next_read(
    command_analyzer_for, api, chunks, length, monkeypatch
):
    extensions = []

    class BoundedBuffer(bytearray):
        def extend(self, chunk):
            # Assert the allocation invariant, rather than only the final DENY.
            assert len(self) + len(chunk) <= LIMIT, "oversized bytes copied into buffer"
            extensions.append(len(chunk))
            super().extend(chunk)

    monkeypatch.setattr(gateway_module, "bytearray", BoundedBuffer, raising=False)
    stream = ObservedStream(*chunks, AssertionError("must not request another chunk"))
    await denied_dispatch(
        command_analyzer_for, api, stream, {} if length is None else {"Content-Length": length}
    )
    assert extensions == [len(chunk) for chunk in chunks[:-1]]
    assert stream.reads == len(chunks)


@pytest.mark.parametrize("encoding", [None, "identity", "Identity", " identity\t"])
@pytest.mark.parametrize("advertise_length", [False, True])
async def test_exact_limit_uncompressed_response_accepts_fragmented_body(
    command_analyzer_for, api, encoding, advertise_length, monkeypatch
):
    body = valid_body(api)
    body += b" " * (LIMIT - len(body))
    stream = ObservedStream(body[:13], body[13:100], body[100:])
    headers = {} if encoding is None else {"Content-Encoding": encoding}
    if advertise_length:
        headers["Content-Length"] = str(LIMIT)

    async def no_decoding(*_args, **_kwargs):
        pytest.fail("decoded iteration must never be used")
        yield b""  # Make this an async iterator, matching aiter_bytes.

    # A client may otherwise advertise compression by default; the request must override it.
    monkeypatch.setattr(httpx.Response, "aiter_bytes", no_decoding)
    calls = []

    def handler(wire):
        calls.append(wire)
        return httpx.Response(200, headers=headers, stream=stream)

    async with command_analyzer_for(handler, command_api=api) as analyzer:
        analyzer.gateway.client.headers["Accept-Encoding"] = "gzip, deflate, br"
        result = await analyzer.analyze(command_request())
    assert result.status == "ok" and result.decision == "ALLOW"
    assert calls[0].headers["Accept-Encoding"] == "identity"
    assert stream.closed and stream.reads == 3


async def test_read_error_denies_and_closes_response(command_analyzer_for, api):
    stream = ObservedStream(valid_body(api)[:20], httpx.ReadError("private transport details"))
    await denied_dispatch(command_analyzer_for, api, stream, {}, "gateway_unavailable")


async def test_error_status_does_not_read_compressed_error_body(command_analyzer_for, api):
    stream = ObservedStream(gzip.compress(b"x" * 1024 * 1024))
    await denied_dispatch(
        command_analyzer_for,
        api,
        stream,
        {"Content-Encoding": "gzip"},
        "gateway_unavailable",
        status=503,
    )
    assert stream.reads == 0


async def test_legacy_content_analysis_also_rejects_compression_before_read(analyzer_for):
    stream = ObservedStream(gzip.compress(json.dumps(completion()).encode()))
    async with analyzer_for(
        lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)
    ) as analyzer:
        result = await analyzer.analyze(AnalyzeRequest(text="Inspect local diagnostics"))
    assert result.decision == "block" and result.error_code == "invalid_model_output"
    assert stream.reads == 0 and stream.closed
