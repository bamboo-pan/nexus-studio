import asyncio
import json

import pytest

from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache
from aistudio_api.config import DEFAULT_WARMUP_TEXT_MODEL
from aistudio_api.api.app import _warmup_with_retries
from aistudio_api.domain.errors import AuthError
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.capture import CapturedRequest, RequestCaptureService


class FakeBrowserSession:
    def __init__(self):
        self.template_calls = []
        self.template_kwargs = []
        self.snapshot_calls = []

    async def capture_template(self, model, **kwargs):
        self.template_calls.append(model)
        self.template_kwargs.append(kwargs)
        call_number = len(self.template_calls)
        template_body = json.dumps(
            [
                "models/gemini-3-flash-preview",
                [[[[None, "template"]], "user"]],
                None,
                [],
                "template-snapshot",
            ]
        )
        return {
            "url": "https://example.test/GenerateContent",
            "headers": {"content-type": "application/json+protobuf", "x-template-call": str(call_number)},
            "body": template_body,
        }

    async def generate_snapshot(self, contents):
        self.snapshot_calls.append(contents)
        return "fresh-snapshot"


class FlakyNavigationBrowserSession(FakeBrowserSession):
    async def capture_template(self, model, **kwargs):
        if not self.template_calls:
            self.template_calls.append(model)
            self.template_kwargs.append(kwargs)
            raise RuntimeError('Page.goto: Timeout 60000ms exceeded while navigating to "https://aistudio.google.com/"')
        return await super().capture_template(model, **kwargs)


class AlwaysTimeoutWarmupSession(FakeBrowserSession):
    def __init__(self):
        super().__init__()
        self.ensure_context_calls = []

    async def ensure_context(self, **kwargs):
        self.ensure_context_calls.append(kwargs)

    async def capture_template(self, model, **kwargs):
        self.template_calls.append(model)
        self.template_kwargs.append(kwargs)
        raise RuntimeError(
            'Page.goto: Timeout 60000ms exceeded. navigating to "https://aistudio.google.com/", waiting until "commit"'
        )


class WarmupSession:
    def __init__(self):
        self.ensure_context_calls = []

    async def ensure_context(self, **kwargs):
        self.ensure_context_calls.append(kwargs)


class WarmupCaptureService:
    def __init__(self):
        self.warmup_calls = []

    async def warmup(self, **kwargs):
        self.warmup_calls.append(kwargs)
        return CapturedRequest(**await FakeBrowserSession().capture_template(kwargs["model"]))


class WarmupReplayService:
    def __init__(self, status=200, raw=b"ok"):
        self.status = status
        self.raw = raw
        self.calls = []

    async def replay(self, captured, body, timeout=None, **kwargs):
        self.calls.append({"captured": captured, "body": body, "timeout": timeout, **kwargs})
        return self.status, self.raw


class FailingWarmupCaptureService:
    async def warmup(self, **kwargs):
        raise RuntimeError("template warmup failed")


def test_capture_rewrites_template_with_requested_model():
    service = RequestCaptureService(FakeBrowserSession(), SnapshotCache(ttl=60, max_size=10))

    captured = asyncio.run(service.capture("draw a large image", model="gemini-3.1-flash-image-preview"))

    assert captured is not None
    assert captured.model == "models/gemini-3.1-flash-image-preview"
    body = json.loads(captured.body)
    assert body[0] == "models/gemini-3.1-flash-image-preview"
    assert body[4] == "fresh-snapshot"


def test_capture_template_cache_can_be_cleared():
    session = FakeBrowserSession()
    service = RequestCaptureService(session, SnapshotCache(ttl=60, max_size=10))

    first = asyncio.run(service.capture("first prompt", model="gemini-3.1-flash-lite"))
    second = asyncio.run(service.capture("second prompt", model="gemini-3.1-flash-lite"))

    assert first.headers["x-template-call"] == "1"
    assert second.headers["x-template-call"] == "1"
    assert session.template_calls == ["gemini-3.1-flash-lite"]

    service.clear_templates()
    third = asyncio.run(service.capture("third prompt", model="gemini-3.1-flash-lite"))

    assert third.headers["x-template-call"] == "2"
    assert session.template_calls == ["gemini-3.1-flash-lite", "gemini-3.1-flash-lite"]


def test_capture_template_retries_transient_aistudio_navigation_failure():
    session = FlakyNavigationBrowserSession()
    service = RequestCaptureService(session, SnapshotCache(ttl=60, max_size=10))

    captured = asyncio.run(service.capture("hello", model="gemini-3.1-flash-lite"))

    assert captured.headers["x-template-call"] == "2"
    assert session.template_calls == ["gemini-3.1-flash-lite", "gemini-3.1-flash-lite"]
    assert session.template_kwargs == [{}, {}]


def test_capture_warmup_can_disable_internal_template_retry():
    session = FlakyNavigationBrowserSession()
    service = RequestCaptureService(session, SnapshotCache(ttl=60, max_size=10))

    with pytest.raises(RuntimeError, match="Page.goto"):
        asyncio.run(service.warmup(prompt="1", model="gemini-3.1-flash-lite", retry_template_capture=False))

    assert session.template_calls == ["gemini-3.1-flash-lite"]
    assert session.template_kwargs == [{}]
    assert session.snapshot_calls == []


def test_capture_warmup_does_not_store_reusable_prompt_snapshot():
    session = FakeBrowserSession()
    snapshot_cache = SnapshotCache(ttl=60, max_size=10)
    service = RequestCaptureService(session, snapshot_cache)

    asyncio.run(service.warmup(prompt="1", model="gemini-3.1-flash-lite"))

    assert session.template_calls == ["gemini-3.1-flash-lite"]
    assert len(session.snapshot_calls) == 1
    assert snapshot_cache._cache == {}


def test_client_switch_auth_clears_capture_templates(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    client = AIStudioClient()
    try:
        client._capture_service._templates["gemini-3.1-flash-lite"] = object()

        asyncio.run(client.switch_auth(str(auth_file)))

        assert client._capture_service._templates == {}
    finally:
        if client._session is not None:
            client._session._executor.shutdown(wait=False)


def test_client_warmup_prepares_default_text_capture_template():
    client = AIStudioClient()
    original_session = client._session
    session = WarmupSession()
    capture_service = WarmupCaptureService()
    try:
        client._session = session
        client._capture_service = capture_service
        replay_service = WarmupReplayService()
        client._replay_service = replay_service

        asyncio.run(client.warmup())

        assert session.ensure_context_calls == [{"navigation_timeout_ms": 30000, "chat_ready_timeout_ms": 30000}]
        assert capture_service.warmup_calls == [
            {
                "prompt": "1",
                "model": DEFAULT_WARMUP_TEXT_MODEL,
                "retry_template_capture": False,
                "navigation_timeout_ms": 30000,
                "chat_ready_timeout_ms": 30000,
                "botguard_timeout_ms": 15000,
                "template_capture_timeout_ms": 30000,
                "template_recovery_attempts": 1,
            }
        ]
        assert replay_service.calls[0]["kind"] == "warmup_probe"
        assert replay_service.calls[0]["model"] == DEFAULT_WARMUP_TEXT_MODEL
        assert replay_service.calls[0]["timeout"] == 30
    finally:
        if original_session is not None:
            original_session._executor.shutdown(wait=False)


def test_client_warmup_propagates_template_warmup_failure():
    session = WarmupSession()
    client = AIStudioClient(port=1)
    original_session = client._session
    try:
        client._session = session
        client._capture_service = FailingWarmupCaptureService()

        with pytest.raises(RuntimeError, match="template warmup failed"):
            asyncio.run(client.warmup())

        assert len(session.ensure_context_calls) == 1
    finally:
        client._session = None
        if original_session is not None:
            original_session._executor.shutdown(wait=False)


def test_client_warmup_fails_hard_when_generate_content_probe_is_forbidden():
    client = AIStudioClient(port=1)
    original_session = client._session
    try:
        client._session = WarmupSession()
        client._capture_service = WarmupCaptureService()
        client._replay_service = WarmupReplayService(status=403, raw=b'[[null,[7,"The caller does not have permission"]]]')

        with pytest.raises(AuthError, match="GenerateContent permission check failed"):
            asyncio.run(client.warmup())
    finally:
        client._session = None
        if original_session is not None:
            original_session._executor.shutdown(wait=False)


def test_startup_warmup_outer_retry_controls_template_attempt_count():
    session = AlwaysTimeoutWarmupSession()
    client = AIStudioClient(port=1)
    original_session = client._session
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    try:
        client._session = session
        client._capture_service = RequestCaptureService(session, SnapshotCache(ttl=60, max_size=10))
        client._replay_service = WarmupReplayService()

        with pytest.raises(RuntimeError, match="Page.goto"):
            asyncio.run(_warmup_with_retries(client.warmup, label="test", attempts=3, backoff_seconds=(0.1, 0.2), sleep=sleep))

        expected_context_kwargs = {"navigation_timeout_ms": 30000, "chat_ready_timeout_ms": 30000}
        expected_template_kwargs = {
            "navigation_timeout_ms": 30000,
            "chat_ready_timeout_ms": 30000,
            "botguard_timeout_ms": 15000,
            "template_capture_timeout_ms": 30000,
            "template_recovery_attempts": 1,
        }

        assert session.ensure_context_calls == [expected_context_kwargs, expected_context_kwargs, expected_context_kwargs]
        assert session.template_calls == [DEFAULT_WARMUP_TEXT_MODEL, DEFAULT_WARMUP_TEXT_MODEL, DEFAULT_WARMUP_TEXT_MODEL]
        assert session.template_kwargs == [expected_template_kwargs, expected_template_kwargs, expected_template_kwargs]
        assert session.snapshot_calls == []
        assert sleeps == [0.1, 0.2]
    finally:
        client._session = None
        if original_session is not None:
            original_session._executor.shutdown(wait=False)
