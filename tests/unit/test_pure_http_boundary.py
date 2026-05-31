import asyncio
import json

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.state import runtime_state
from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PURE_HTTP_GENERATE_CONTENT_UNSUPPORTED
from aistudio_api.infrastructure.gateway.pure_capture import PURE_HTTP_SNAPSHOT_UNSUPPORTED, PureHttpCaptureService
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


TEXT_RESPONSE_RAW = json.dumps(
    [
        [
            [[[[[None, "pure text ok"]]], 1]],
            None,
            [1, 3, 4],
            None,
            None,
            None,
            None,
            "resp_pure_http_test",
        ]
    ]
)


class FakeReplayService:
    def __init__(self):
        self.calls = []

    async def replay(self, captured, body: str, timeout: int | None = None):
        self.calls.append({"captured": captured, "body": body, "timeout": timeout})
        return 200, TEXT_RESPONSE_RAW.encode("utf-8")


def request_with_client(client, method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            return await http_client.request(method, url, **kwargs)

    old_busy_lock = runtime_state.busy_lock
    old_account_service = runtime_state.account_service
    old_rotator = runtime_state.rotator
    app.dependency_overrides[get_client] = lambda: client
    runtime_state.busy_lock = asyncio.Semaphore(3)
    runtime_state.account_service = None
    runtime_state.rotator = None
    try:
        return asyncio.run(send())
    finally:
        runtime_state.busy_lock = old_busy_lock
        runtime_state.account_service = old_account_service
        runtime_state.rotator = old_rotator
        app.dependency_overrides.pop(get_client, None)


def test_pure_http_streaming_route_returns_clear_unsupported_error():
    client = AIStudioClient(use_pure_http=True)

    response = request_with_client(
        client,
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3-flash-preview",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 501
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "Pure HTTP mode is experimental" in body["error"]["message"]
    assert "does not support streaming" in body["error"]["message"]


def test_pure_http_image_generation_route_returns_clear_unsupported_error():
    client = AIStudioClient(use_pure_http=True)

    response = request_with_client(
        client,
        "POST",
        "/v1/images/generations",
        json={"model": "gemini-3.1-flash-image-preview", "prompt": "draw", "size": "1024x1024"},
    )

    assert response.status_code == 501
    body = response.json()
    assert "Pure HTTP mode is experimental" in body["error"]["message"]
    assert "image generation" in body["error"]["message"]


def test_pure_http_plain_text_non_streaming_route_uses_experimental_http_path(monkeypatch):
    client = AIStudioClient(use_pure_http=True)
    replay = FakeReplayService()

    async def snapshot(prompt: str):
        return "snapshot-token"

    monkeypatch.setattr(client._capture_service, "_generate_snapshot", snapshot)
    client._replay_service = replay

    response = request_with_client(
        client,
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3-flash-preview",
            "messages": [{"role": "user", "content": "plain text only"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "pure text ok"
    modified_body = json.loads(replay.calls[0]["body"])
    generation_config = modified_body[3]
    assert generation_config[7] == "text/plain"
    assert len(generation_config) <= 16 or generation_config[16] is None


@pytest.mark.parametrize(
    "extra_kwargs",
    [
        {"capture_images": ["/tmp/image.png"]},
        {"tools": [[[]]]},
        {"system_instruction_content": AistudioContent(role="user", parts=[AistudioPart(text="system")])},
        {"generation_config_overrides": {"response_mime_type": "application/json"}},
        {"safety_off": True},
        {
            "contents": [
                AistudioContent(role="user", parts=[AistudioPart(text="first")]),
                AistudioContent(role="user", parts=[AistudioPart(text="second")]),
            ]
        },
    ],
)
def test_pure_http_client_rejects_unsupported_non_streaming_boundaries_before_capture(monkeypatch, extra_kwargs):
    client = AIStudioClient(use_pure_http=True)

    async def should_not_run(prompt: str):
        raise AssertionError("snapshot generation should not run")

    monkeypatch.setattr(client._capture_service, "_generate_snapshot", should_not_run)

    kwargs = {
        "model": "gemini-3-flash-preview",
        "capture_prompt": "hello",
        "contents": [AistudioContent(role="user", parts=[AistudioPart(text="hello")])],
    }
    kwargs.update(extra_kwargs)

    with pytest.raises(RequestError) as error:
        asyncio.run(client.generate_content(**kwargs))

    assert error.value.status == 501
    assert error.value.message == PURE_HTTP_GENERATE_CONTENT_UNSUPPORTED


def test_pure_http_missing_snapshot_returns_clear_unsupported_error(monkeypatch):
    service = PureHttpCaptureService(SnapshotCache())

    async def no_snapshot(prompt: str):
        return None

    monkeypatch.setattr(service, "_generate_snapshot", no_snapshot)

    with pytest.raises(RequestError) as error:
        asyncio.run(service.capture(prompt="hello", model="gemini-3-flash-preview"))

    assert error.value.status == 501
    assert error.value.message == PURE_HTTP_SNAPSHOT_UNSUPPORTED


def test_pure_http_gemini_streaming_route_returns_clear_unsupported_error():
    client = AIStudioClient(use_pure_http=True)

    response = request_with_client(
        client,
        "POST",
        "/v1beta/models/gemini-3-flash-preview:streamGenerateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
    )

    assert response.status_code == 501
    body = response.json()
    assert body["detail"]["type"] == "unsupported_feature"
    assert "Pure HTTP mode is experimental" in body["detail"]["message"]
    assert "does not support streaming" in body["detail"]["message"]


def test_pure_http_capture_rejects_images_before_snapshot_generation(monkeypatch):
    service = PureHttpCaptureService(SnapshotCache())

    async def should_not_run(prompt: str):
        raise AssertionError("snapshot generation should not run")

    monkeypatch.setattr(service, "_generate_snapshot", should_not_run)

    with pytest.raises(RequestError) as error:
        asyncio.run(service.capture(prompt="hello", model="gemini-3-flash-preview", images=["/tmp/image.png"]))

    assert error.value.status == 501
    assert "does not support image prompts" in error.value.message