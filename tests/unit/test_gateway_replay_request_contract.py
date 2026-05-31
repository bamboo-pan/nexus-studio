import asyncio
import json

from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.client import image_replay_model_id
from aistudio_api.infrastructure.gateway.replay import RequestReplayService
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway


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


async def _collect_stream_events(stream):
    events = []
    async for event in stream:
        events.append(event)
    return events