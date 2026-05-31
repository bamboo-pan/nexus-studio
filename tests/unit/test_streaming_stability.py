import asyncio
import json

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.application.account_service import AccountService
from aistudio_api.application.api_service import _build_gemini_streaming_response, _build_streaming_response
from aistudio_api.domain.errors import AuthError, RequestError
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.account.login_service import LoginService
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


class FakeStreamClient:
    def __init__(self, events=None, *, error=None):
        self.events = list(events or [])
        self.error = error
        self.calls = []

    async def stream_generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        for event in self.events:
            yield event


class CloseAwareStreamClient:
    def __init__(self):
        self.closed = False
        self.calls = []

    async def stream_generate_content(self, **kwargs):
        self.calls.append(kwargs)
        try:
            yield ("body", "hello")
            await asyncio.Event().wait()
        finally:
            self.closed = True


class AuthRetryStreamClient:
    def __init__(self):
        self.calls = []
        self.clear_calls = 0

    def clear_snapshot_cache(self):
        self.clear_calls += 1

    async def stream_generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise AuthError("stale auth")
        yield ("body", "hello")
        yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


class DisconnectingRequest:
    def __init__(self, states: list[bool]):
        self._states = list(states)
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        if self._states:
            return self._states.pop(0)
        return True


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


def stream_events(response: httpx.Response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


async def collect_body_iterator(response) -> list[str]:
    return [chunk async for chunk in response.body_iterator]


def gemini_normalized(cleanup_paths: list[str] | None = None) -> dict:
    return {
        "model": "gemini-3-flash-preview",
        "capture_prompt": "hello",
        "capture_images": None,
        "contents": [AistudioContent(role="user", parts=[AistudioPart(text="hello")])],
        "system_instruction": None,
        "tools": None,
        "temperature": None,
        "top_p": None,
        "top_k": None,
        "max_tokens": None,
        "generation_config_overrides": None,
        "cleanup_paths": cleanup_paths or [],
    }


def storage_state(cookie_name="sid", cookie_value="1"):
    return {"cookies": [{"name": cookie_name, "value": cookie_value, "domain": ".google.com", "path": "/"}]}


def account_runtime(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state(), tier="pro")
    service = AccountService(store, LoginService())
    return account, service, AccountRotator(store)


def test_openai_stream_error_chunk_has_sdk_compatible_shape_and_done_marker():
    client = FakeStreamClient(error=RequestError(502, "upstream stream failed"))

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

    assert response.status_code == 200
    body = response.text
    events = stream_events(response)
    assert events[-1]["error"] == {
        "message": "upstream stream failed",
        "type": "upstream_error",
        "param": None,
        "code": "upstream_error",
    }
    assert body.rstrip().endswith("data: [DONE]")


def test_openai_stream_empty_upstream_emits_error_chunk_and_done_marker():
    client = FakeStreamClient(events=[("usage", {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})])

    response = request_with_client(
        client,
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3.1-flash-lite",
            "stream": True,
            "messages": [{"role": "user", "content": "1+1"}],
        },
    )

    assert response.status_code == 200
    body = response.text
    events = stream_events(response)
    assert events[-1]["error"] == {
        "message": "AI Studio returned no response content",
        "type": "upstream_error",
        "param": None,
        "code": "upstream_error",
    }
    assert body.rstrip().endswith("data: [DONE]")


def test_openai_stream_close_cancels_upstream_and_cleans_temp_files(tmp_path):
    tmp_file = tmp_path / "uploaded.png"
    tmp_file.write_bytes(b"image")
    client = CloseAwareStreamClient()
    old_busy_lock = runtime_state.busy_lock
    runtime_state.busy_lock = asyncio.Semaphore(3)

    async def consume_then_close():
        response = _build_streaming_response(
            client=client,
            capture_prompt="hello",
            model="gemini-3-flash-preview",
            capture_images=None,
            contents=[AistudioContent(role="user", parts=[AistudioPart(text="hello")])],
            system_instruction=None,
            cleanup_paths=[str(tmp_file)],
        )
        iterator = response.body_iterator
        first = await anext(iterator)
        await iterator.aclose()
        return first

    try:
        first_chunk = asyncio.run(consume_then_close())
    finally:
        runtime_state.busy_lock = old_busy_lock

    assert "hello" in first_chunk
    assert client.closed is True
    assert not tmp_file.exists()


def test_openai_stream_disconnect_before_downstream_call_cleans_temp_files(tmp_path):
    tmp_file = tmp_path / "upload-before.png"
    tmp_file.write_bytes(b"image")
    client = FakeStreamClient(events=[("body", "should not run")])
    request = DisconnectingRequest([True])
    old_busy_lock = runtime_state.busy_lock
    runtime_state.busy_lock = asyncio.Semaphore(3)

    try:
        response = _build_streaming_response(
            client=client,
            capture_prompt="hello",
            model="gemini-3-flash-preview",
            capture_images=None,
            contents=[AistudioContent(role="user", parts=[AistudioPart(text="hello")])],
            system_instruction=None,
            cleanup_paths=[str(tmp_file)],
            request=request,
        )
        chunks = asyncio.run(collect_body_iterator(response))
    finally:
        runtime_state.busy_lock = old_busy_lock

    assert chunks == []
    assert client.calls == []
    assert request.calls == 1
    assert not tmp_file.exists()


def test_openai_stream_disconnect_during_downstream_closes_upstream_and_cleans_temp_files(tmp_path):
    tmp_file = tmp_path / "upload-during.png"
    tmp_file.write_bytes(b"image")
    client = CloseAwareStreamClient()
    request = DisconnectingRequest([False, True])
    old_busy_lock = runtime_state.busy_lock
    runtime_state.busy_lock = asyncio.Semaphore(3)

    try:
        response = _build_streaming_response(
            client=client,
            capture_prompt="hello",
            model="gemini-3-flash-preview",
            capture_images=None,
            contents=[AistudioContent(role="user", parts=[AistudioPart(text="hello")])],
            system_instruction=None,
            cleanup_paths=[str(tmp_file)],
            request=request,
        )
        chunks = asyncio.run(collect_body_iterator(response))
    finally:
        runtime_state.busy_lock = old_busy_lock

    assert chunks == []
    assert len(client.calls) == 1
    assert client.closed is True
    assert request.calls == 2
    assert not tmp_file.exists()


def test_openai_stream_auth_error_retries_with_fresh_capture_state():
    client = AuthRetryStreamClient()
    old_busy_lock = runtime_state.busy_lock
    runtime_state.busy_lock = asyncio.Semaphore(3)

    try:
        response = _build_streaming_response(
            client=client,
            capture_prompt="hello",
            model="gemini-3-flash-preview",
            capture_images=None,
            contents=[AistudioContent(role="user", parts=[AistudioPart(text="hello")])],
            system_instruction=None,
            cleanup_paths=[],
        )
        chunks = asyncio.run(collect_body_iterator(response))
    finally:
        runtime_state.busy_lock = old_busy_lock

    body = "".join(chunks)
    assert "hello" in body
    assert client.clear_calls == 1
    assert len(client.calls) == 2
    assert client.calls[0]["force_refresh_capture"] is False
    assert client.calls[1]["force_refresh_capture"] is True


def test_openai_stream_success_updates_active_account_stats(tmp_path):
    account, service, rotator = account_runtime(tmp_path / "accounts")
    client = FakeStreamClient(events=[("body", "hello"), ("usage", {"total_tokens": 2})])
    old_busy_lock = runtime_state.busy_lock
    old_account_service = runtime_state.account_service
    old_rotator = runtime_state.rotator
    runtime_state.busy_lock = asyncio.Semaphore(3)
    runtime_state.account_service = service
    runtime_state.rotator = rotator

    try:
        response = _build_streaming_response(
            client=client,
            capture_prompt="hello",
            model="gemini-3-flash-preview",
            capture_images=None,
            contents=[AistudioContent(role="user", parts=[AistudioPart(text="hello")])],
            system_instruction=None,
            cleanup_paths=[],
        )
        chunks = asyncio.run(collect_body_iterator(response))
    finally:
        runtime_state.busy_lock = old_busy_lock
        runtime_state.account_service = old_account_service
        runtime_state.rotator = old_rotator

    account_stats = rotator.get_all_stats()[account.id]
    assert "hello" in "".join(chunks)
    assert account_stats["requests"] == 1
    assert account_stats["success"] == 1
    assert account_stats["errors"] == 0


def test_gemini_streaming_emits_function_call_parts_and_finish_reason():
    client = FakeStreamClient(
        events=[
            ("tool_calls", [{"name": "lookup", "args": {"query": "weather"}}]),
            ("usage", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}),
        ]
    )

    response = request_with_client(
        client,
        "POST",
        "/v1beta/models/gemini-3-flash-preview:streamGenerateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "use tool"}]}]},
    )

    assert response.status_code == 200
    events = stream_events(response)
    function_call = events[0]["candidates"][0]["content"]["parts"][0]["functionCall"]
    finish = events[-1]["candidates"][0]
    assert function_call == {"name": "lookup", "args": {"query": "weather"}}
    assert finish["finishReason"] == "FUNCTION_CALL"
    assert events[-1]["usageMetadata"]["totalTokenCount"] == 3
    assert response.text.rstrip().endswith("data: [DONE]")


def test_gemini_stream_empty_upstream_emits_error_chunk_and_done_marker():
    client = FakeStreamClient(events=[("usage", {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})])

    response = request_with_client(
        client,
        "POST",
        "/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "1+1"}]}]},
    )

    assert response.status_code == 200
    events = stream_events(response)
    assert events[-1]["error"] == {
        "code": 502,
        "message": "AI Studio returned no response content",
        "status": "BAD_GATEWAY",
    }
    assert response.text.rstrip().endswith("data: [DONE]")


def test_gemini_stream_disconnect_during_downstream_closes_upstream_and_cleans_temp_files(tmp_path):
    tmp_file = tmp_path / "gemini-upload.png"
    tmp_file.write_bytes(b"image")
    client = CloseAwareStreamClient()
    request = DisconnectingRequest([False, True])
    old_busy_lock = runtime_state.busy_lock
    runtime_state.busy_lock = asyncio.Semaphore(3)

    try:
        response = _build_gemini_streaming_response(
            client=client,
            normalized=gemini_normalized([str(tmp_file)]),
            request=request,
        )
        chunks = asyncio.run(collect_body_iterator(response))
    finally:
        runtime_state.busy_lock = old_busy_lock

    assert chunks == []
    assert len(client.calls) == 1
    assert client.closed is True
    assert request.calls == 2
    assert not tmp_file.exists()


def test_gemini_stream_auth_error_retries_with_fresh_capture_state():
    client = AuthRetryStreamClient()
    old_busy_lock = runtime_state.busy_lock
    runtime_state.busy_lock = asyncio.Semaphore(3)

    try:
        response = _build_gemini_streaming_response(
            client=client,
            normalized=gemini_normalized(),
        )
        chunks = asyncio.run(collect_body_iterator(response))
    finally:
        runtime_state.busy_lock = old_busy_lock

    body = "".join(chunks)
    assert "hello" in body
    assert client.clear_calls == 1
    assert len(client.calls) == 2
    assert client.calls[0]["force_refresh_capture"] is False
    assert client.calls[1]["force_refresh_capture"] is True


def test_to_openai_tool_calls_stringifies_dict_arguments():
    from aistudio_api.api.responses import to_openai_tool_calls

    tool_call = to_openai_tool_calls([{"name": "lookup", "arguments": {"query": "weather"}}])[0]

    assert json.loads(tool_call["function"]["arguments"]) == {"query": "weather"}