import asyncio
import json

import pytest

from aistudio_api.domain.errors import RequestError
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.client import AIStudioClient, image_replay_model_id
from aistudio_api.infrastructure.gateway.replay import RequestReplayService
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


TEXT_RESPONSE_RAW = json.dumps(
    [
        [
            [[[[[None, "native text ok"]]], 1]],
            None,
            [1, 3, 4],
            None,
            None,
            None,
            None,
            "resp_native_first_test",
        ]
    ]
)


def _captured_request() -> CapturedRequest:
    body = json.dumps(
        [
            "models/gemini-3.1-flash-lite",
            [[[[None, "template"]], "user"]],
            None,
            [],
            "template-snapshot",
        ]
    )
    return CapturedRequest(
        url="https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
        headers={
            "host": "aistudio.google.com",
            "content-length": "999",
            "content-type": "application/json+protobuf",
            "x-client-data": "abc",
        },
        body=body,
    )


class FakeReplaySession:
    def __init__(self):
        self.calls = []

    async def send_hooked_request(self, *, body, url, headers, timeout_ms):
        self.calls.append({"body": body, "url": url, "headers": headers, "timeout_ms": timeout_ms})
        return 200, b"ok"


class FakeStreamingSession:
    def __init__(self):
        self.calls = []

    async def send_streaming_request(self, *, body, url, headers, timeout_ms):
        self.calls.append({"body": body, "url": url, "headers": headers, "timeout_ms": timeout_ms})
        yield "status", 200


class FakeControlledErrorReplaySession:
    async def send_hooked_request(self, *, body, url, headers, timeout_ms):
        raise RequestError(503, "native UI worker unavailable: model picker not ready")


class FakeAccountNativeSession:
    has_account_auth = True

    def __init__(self, *, response_body: bytes | None = None):
        self.response_body = response_body or TEXT_RESPONSE_RAW.encode("utf-8")
        self.calls = []

    async def send_account_native_generate_content_body(self, *, body, timeout_ms, max_attempts=None):
        self.calls.append({"body": body, "timeout_ms": timeout_ms, "max_attempts": max_attempts})
        return 200, self.response_body


def test_browser_replay_uses_captured_request_url_and_headers_without_session_template():
    captured = _captured_request()
    session = FakeReplaySession()
    replay = RequestReplayService(session=session)

    status, raw = asyncio.run(replay.replay(captured, body="rewritten-body", timeout=7))

    assert status == 200
    assert raw == b"ok"
    assert session.calls == [
        {
            "body": "rewritten-body",
            "url": captured.url,
            "headers": {
                "content-type": "application/json+protobuf",
                "x-client-data": "abc",
            },
            "timeout_ms": 7000,
        }
    ]


def test_browser_replay_propagates_controlled_request_error():
    replay = RequestReplayService(session=FakeControlledErrorReplaySession())

    with pytest.raises(RequestError) as exc_info:
        asyncio.run(replay.replay(_captured_request(), body="rewritten-body", timeout=7))

    assert exc_info.value.status == 503
    assert "native UI worker unavailable" in exc_info.value.message


def test_streaming_replay_uses_captured_request_url_and_headers_without_session_template():
    captured = _captured_request()
    session = FakeStreamingSession()
    gateway = StreamingGateway(session=session)

    events = asyncio.run(
        _collect_stream_events(
            gateway.stream_chat(
                captured=captured,
                model="gemini-3.1-flash-lite",
                system_instruction=None,
            )
        )
    )

    assert events == [("usage", None), ("done", None)]
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == captured.url
    assert session.calls[0]["headers"] == {
        "content-type": "application/json+protobuf",
        "x-client-data": "abc",
    }
    assert json.loads(session.calls[0]["body"])[0] == "models/gemini-3.1-flash-lite"


def test_image_preview_model_ids_replay_with_current_rpc_model_ids():
    assert image_replay_model_id("gemini-3.1-flash-image-preview") == "gemini-3.1-flash-image"
    assert image_replay_model_id("models/gemini-3-pro-image-preview") == "models/gemini-3-pro-image"


def test_account_text_generate_content_uses_native_worker_before_capture(monkeypatch):
    client = AIStudioClient(port=0)
    session = FakeAccountNativeSession()
    client._session = session

    async def capture_should_not_run(*args, **kwargs):
        raise AssertionError("capture should not run for account-native text requests")

    monkeypatch.setattr(client, "capture_request", capture_should_not_run)

    output = asyncio.run(
        client.generate_content(
            model="gemini-3.5-flash",
            capture_prompt="Reply with exactly: native-first-ok",
            contents=[AistudioContent(role="user", parts=[AistudioPart(text="Reply with exactly: native-first-ok")])],
        )
    )

    sent_body = json.loads(session.calls[0]["body"])
    assert output.text == "native text ok"
    assert output.model == "gemini-3.5-flash"
    assert sent_body[0] == "models/gemini-3.5-flash"
    assert sent_body[1][0][0][0][1] == "Reply with exactly: native-first-ok"
    assert session.calls[0]["timeout_ms"] > 0
    assert session.calls[0]["max_attempts"] == 1


def test_account_text_stream_uses_native_worker_before_capture(monkeypatch):
    client = AIStudioClient(port=0)
    session = FakeAccountNativeSession()
    client._session = session

    async def capture_should_not_run(*args, **kwargs):
        raise AssertionError("capture should not run for account-native text streams")

    monkeypatch.setattr(client, "capture_request", capture_should_not_run)

    events = asyncio.run(
        _collect_stream_events(
            client.stream_generate_content(
                model="gemini-3.5-flash",
                capture_prompt="Reply with exactly: native-stream-ok",
                contents=[AistudioContent(role="user", parts=[AistudioPart(text="Reply with exactly: native-stream-ok")])],
            )
        )
    )

    assert events[:2] == [("body", "native text ok"), ("usage", {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4, "cached_tokens": None, "prompt_tokens_details": None, "completion_tokens_details": {"reasoning_tokens": 0, "visible_tokens": 3}})]
    assert events[-1] == ("done", None)
    assert len(session.calls) == 1
    assert session.calls[0]["max_attempts"] == 1


def test_account_text_stream_timeout_uses_request_budget_not_warmup_probe(monkeypatch):
    monkeypatch.setenv("AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS", "300")
    monkeypatch.setattr(settings, "warmup_probe_timeout_seconds", 300)
    monkeypatch.setattr(settings, "timeout_stream", 45)
    client = AIStudioClient(port=0)
    session = FakeAccountNativeSession()
    client._session = session

    events = asyncio.run(
        _collect_stream_events(
            client.stream_generate_content(
                model="gemini-3.5-flash",
                capture_prompt="Reply with exactly: native-stream-timeout-ok",
                contents=[AistudioContent(role="user", parts=[AistudioPart(text="Reply with exactly: native-stream-timeout-ok")])],
            )
        )
    )

    assert events[0] == ("body", "native text ok")
    assert session.calls[0]["timeout_ms"] == 120000
    assert session.calls[0]["max_attempts"] == 1


def test_account_generate_content_with_tools_keeps_captured_replay(monkeypatch):
    client = AIStudioClient(port=0)
    session = FakeAccountNativeSession()
    client._session = session
    replay = FakeReplaySession()
    client._replay_service = RequestReplayService(session=replay)
    captured = _captured_request()
    capture_calls = []

    async def capture_request(*args, **kwargs):
        capture_calls.append({"args": args, "kwargs": kwargs})
        return captured

    monkeypatch.setattr(client, "capture_request", capture_request)

    replay.calls.clear()
    output = asyncio.run(
        client.generate_content(
            model="gemini-3.5-flash",
            capture_prompt="search please",
            contents=[AistudioContent(role="user", parts=[AistudioPart(text="search please")])],
            tools=[[None, None, None, [None, [[]]]]],
        )
    )

    assert output.raw_response == "ok"
    assert len(capture_calls) == 1
    assert len(replay.calls) == 1
    assert session.calls == []


async def _collect_stream_events(stream):
    events = []
    async for event in stream:
        events.append(event)
    return events