import asyncio
import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.state import runtime_state
from aistudio_api.api.routes_request_logs import router as request_logs_router
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.replay import RequestReplayService
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.request_logs import RequestLogStore


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
            "cookie": "SID=secret",
        },
        body=body,
    )


class FakeReplaySession:
    async def send_hooked_request(self, *, body, url, headers, timeout_ms):
        return 200, b"ok"


class FakeStreamingSession:
    async def send_streaming_request(self, *, body, url, headers, timeout_ms):
        yield "status", 200


def test_request_log_store_persists_toggle_and_complete_request(tmp_path):
    store = RequestLogStore(tmp_path)

    assert store.status() == {"enabled": False, "count": 0, "group_count": 0}
    assert store.save(kind="generate_content", model="m", method="POST", url="u", headers={}, body="{}") is None

    status = store.set_enabled(True)
    assert status["enabled"] is True
    entry = store.save(
        kind="generate_content",
        model="gemini-test",
        method="POST",
        url="https://aistudio.google.com/rpc",
        headers={"content-type": "application/json", "x-test": 3},
        captured_headers={"host": "aistudio.google.com", "content-length": "12", "x-test": 3},
        body='{"hello":[1,true]}',
        transport="browser",
    )

    assert entry is not None
    assert entry["chain_id"] == entry["id"]
    assert entry["direction"] == "outbound"
    assert entry["phase"] == "upstream_request"
    assert store.count() == 1
    summary = store.list()[0]
    assert summary["id"] == entry["id"]
    assert summary["body_size"] == len('{"hello":[1,true]}'.encode())

    detail = store.get(entry["id"])
    assert detail["headers"] == {"content-type": "application/json", "x-test": "3"}
    assert detail["captured_headers"]["host"] == "aistudio.google.com"
    assert detail["body_raw"] == '{"hello":[1,true]}'
    assert detail["body_json"] == {"hello": [1, True]}
    assert detail["body_parse_error"] is None

    updated = store.attach_response(entry["id"], status_code=200, response_headers={"content-type": "application/json"}, response_body='{"ok":true}', elapsed_ms=12.3456)
    assert updated["status_code"] == 200
    assert updated["elapsed_ms"] == 12.346
    assert updated["response_headers"] == {"content-type": "application/json"}
    assert updated["response_body_json"] == {"ok": True}
    assert updated["response_body_parse_error"] is None


def test_request_log_store_groups_exports_and_deletes_lifecycle(tmp_path):
    store = RequestLogStore(tmp_path)
    store.set_enabled(True)
    chain_id = "chain-alpha"

    store.save(
        kind="client_response",
        model="gemini-test",
        method="POST",
        url="http://testserver/v1/chat/completions",
        headers={},
        body="",
        chain_id=chain_id,
        phase="client_response",
        status_code=200,
        response_body='{"ok":true}',
        elapsed_ms=15.25,
    )
    store.save(
        kind="client_request",
        model="gemini-test",
        method="POST",
        url="http://testserver/v1/chat/completions",
        headers={"content-type": "application/json"},
        body='{"messages":[{"role":"user","content":"hi"}]}',
        chain_id=chain_id,
        phase="client_request",
    )
    store.save(
        kind="generate_content",
        model="gemini-test",
        method="POST",
        url="https://aistudio.google.com/rpc",
        headers={"content-type": "application/json"},
        body='{"outbound":true}',
        chain_id=chain_id,
        phase="upstream_request",
        status_code=200,
        response_body='{"candidate":"ok"}',
        elapsed_ms=11.5,
    )
    store.save(
        kind="generate_content",
        model="gemini-test",
        method="POST",
        url="https://aistudio.google.com/rpc",
        headers={},
        body="",
        chain_id=chain_id,
        direction="inbound",
        phase="upstream_response",
        status_code=200,
        response_body='{"candidate":"ok"}',
    )

    assert store.group_count() == 1
    summary = store.list_groups()[0]
    assert summary["id"] == chain_id
    assert summary["entry_count"] == 4
    assert summary["status_code"] == 200
    assert summary["elapsed_ms"] == 15.25
    assert [phase["phase"] for phase in summary["phases"]] == ["client_request", "upstream_request", "upstream_response", "client_response"]

    detail = store.get_group(chain_id)
    assert [entry["phase"] for entry in detail["entries"]] == ["client_request", "upstream_request", "upstream_response", "client_response"]
    assert detail["total_size"] == detail["body_size"] + detail["response_body_size"]

    exported = store.export_groups([chain_id, "missing-chain"])
    assert exported["data"][0]["id"] == chain_id
    assert exported["missing"] == ["missing-chain"]

    deleted = store.delete_group(chain_id)
    assert deleted["deleted_groups"] == 1
    assert deleted["deleted_entries"] == 4
    assert store.count() == 0
    assert store.group_count() == 0


def test_request_log_routes_manage_status_list_and_detail(tmp_path):
    store = RequestLogStore(tmp_path)
    runtime_state = SimpleNamespace(request_log_store=store)
    app = FastAPI()
    app.include_router(request_logs_router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime_state
    client = TestClient(app)

    assert client.get("/request-logs/status").json() == {"enabled": False, "count": 0, "group_count": 0}
    assert client.put("/request-logs/status", json={"enabled": True}).json()["enabled"] is True
    saved = store.save(kind="generate_image", model="image-model", method="POST", url="https://aistudio.google.com/rpc", headers={}, body="[]")

    listing = client.get("/request-logs").json()
    assert listing["enabled"] is True
    assert listing["total"] == 1
    assert listing["data"][0]["id"] == saved["id"]

    detail = client.get(f"/request-logs/{saved['id']}").json()
    assert detail["kind"] == "generate_image"
    assert detail["body_raw"] == "[]"
    group_detail = client.get(f"/request-logs/groups/{saved['chain_id']}").json()
    assert group_detail["entries"][0]["id"] == saved["id"]
    exported = client.post("/request-logs/export", json={"ids": [saved["chain_id"]]}).json()
    assert exported["data"][0]["id"] == saved["chain_id"]
    assert client.get("/request-logs/not-a-valid-id").status_code == 400


def test_request_log_routes_manage_lifecycle_groups(tmp_path):
    store = RequestLogStore(tmp_path)
    store.set_enabled(True)
    runtime_state = SimpleNamespace(request_log_store=store)
    app = FastAPI()
    app.include_router(request_logs_router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime_state
    client = TestClient(app)

    store.save(kind="client_request", model="m", method="POST", url="http://test/v1/chat/completions", headers={}, body="{}", chain_id="chain-one", phase="client_request")
    store.save(kind="client_response", model="m", method="POST", url="http://test/v1/chat/completions", headers={}, body="", chain_id="chain-one", phase="client_response", status_code=200, response_body='{"ok":true}')
    store.save(kind="client_request", model="m", method="POST", url="http://test/v1/responses", headers={}, body="{}", chain_id="chain-two", phase="client_request")

    listing = client.get("/request-logs").json()
    assert listing["total"] == 2
    assert listing["entry_total"] == 3
    assert {item["id"] for item in listing["data"]} == {"chain-one", "chain-two"}

    detail = client.get("/request-logs/groups/chain-one").json()
    assert [entry["phase"] for entry in detail["entries"]] == ["client_request", "client_response"]
    assert detail["status_code"] == 200

    exported = client.post("/request-logs/export", json={"ids": ["chain-one", "missing"]}).json()
    assert [item["id"] for item in exported["data"]] == ["chain-one"]
    assert exported["missing"] == ["missing"]

    deleted = client.post("/request-logs/groups/delete", json={"ids": ["chain-one"]}).json()
    assert deleted["deleted_groups"] == 1
    assert deleted["deleted_entries"] == 2
    assert store.group_count() == 1
    assert client.get("/request-logs/groups/chain-one").status_code == 404
    assert client.delete("/request-logs/groups/chain-two").json()["deleted_entries"] == 1


def test_replay_logs_actual_outbound_request_when_enabled(tmp_path):
    store = RequestLogStore(tmp_path)
    store.set_enabled(True)
    captured = _captured_request()
    replay = RequestReplayService(session=FakeReplaySession(), request_log_store=store)

    status, raw = asyncio.run(replay.replay(captured, body='{"rewritten":true}', kind="generate_content", model="gemini-test"))

    assert status == 200
    assert raw == b"ok"
    entries = {item["phase"]: store.get(item["id"]) for item in store.list()}
    detail = entries["upstream_request"]
    assert detail["kind"] == "generate_content"
    assert detail["model"] == "gemini-test"
    assert detail["transport"] == "browser"
    assert detail["headers"] == {"content-type": "application/json+protobuf", "cookie": "SID=secret"}
    assert detail["captured_headers"]["content-length"] == "999"
    assert detail["body_json"] == {"rewritten": True}
    assert detail["status_code"] == 200
    assert detail["response_body_raw"] == "ok"
    response_detail = entries["upstream_response"]
    assert response_detail["chain_id"] == detail["chain_id"]
    assert response_detail["status_code"] == 200
    assert response_detail["response_body_raw"] == "ok"


def test_replay_does_not_log_when_disabled(tmp_path):
    store = RequestLogStore(tmp_path)
    replay = RequestReplayService(session=FakeReplaySession(), request_log_store=store)

    asyncio.run(replay.replay(_captured_request(), body='{"rewritten":true}', kind="generate_content", model="gemini-test"))

    assert store.count() == 0


def test_streaming_logs_actual_outbound_request_when_enabled(tmp_path):
    store = RequestLogStore(tmp_path)
    store.set_enabled(True)
    gateway = StreamingGateway(session=FakeStreamingSession(), request_log_store=store)

    events = asyncio.run(
        _collect_stream_events(
            gateway.stream_chat(
                captured=_captured_request(),
                model="gemini-stream",
                system_instruction=None,
            )
        )
    )

    assert events == [("usage", None), ("done", None)]
    entries = {item["phase"]: store.get(item["id"]) for item in store.list()}
    detail = entries["upstream_request"]
    assert detail["kind"] == "stream_generate_content"
    assert detail["model"] == "gemini-stream"
    assert detail["transport"] == "browser_stream"
    assert json.loads(detail["body_raw"])[0] == "models/gemini-stream"
    assert detail["status_code"] == 200
    response_detail = entries["upstream_response"]
    assert response_detail["chain_id"] == detail["chain_id"]
    assert response_detail["status_code"] == 200


def test_api_exchange_logging_records_correlated_client_and_upstream_entries(tmp_path):
    store = RequestLogStore(tmp_path)
    store.set_enabled(True)

    class FakeClient:
        async def generate_content(self, **kwargs):
            runtime_state.request_log_store.save(
                kind="generate_content",
                model=kwargs.get("model"),
                method="POST",
                url="https://aistudio.google.com/rpc",
                headers={"content-type": "application/json"},
                body='{"outbound":true}',
                transport="fake",
            )
            return ModelOutput(candidates=[Candidate(text="ok")], usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            return await http_client.post(
                "/v1/chat/completions",
                json={"model": "gemini-3-flash-preview", "messages": [{"role": "user", "content": "hello"}]},
            )

    old_store = runtime_state.request_log_store
    old_busy_lock = runtime_state.busy_lock
    old_account_service = runtime_state.account_service
    old_rotator = runtime_state.rotator
    runtime_state.request_log_store = store
    runtime_state.busy_lock = asyncio.Semaphore(3)
    runtime_state.account_service = None
    runtime_state.rotator = None
    app.dependency_overrides[get_client] = lambda: FakeClient()
    try:
        response = asyncio.run(send())
    finally:
        app.dependency_overrides.pop(get_client, None)
        runtime_state.request_log_store = old_store
        runtime_state.busy_lock = old_busy_lock
        runtime_state.account_service = old_account_service
        runtime_state.rotator = old_rotator

    assert response.status_code == 200
    entries = [store.get(item["id"]) for item in store.list()]
    phases = {entry["phase"] for entry in entries}
    assert {"client_request", "upstream_request", "client_response"}.issubset(phases)
    chain_ids = {entry["chain_id"] for entry in entries}
    assert len(chain_ids) == 1
    client_request = next(entry for entry in entries if entry["phase"] == "client_request")
    client_response = next(entry for entry in entries if entry["phase"] == "client_response")
    upstream_request = next(entry for entry in entries if entry["phase"] == "upstream_request")
    assert client_request["body_json"]["messages"][0]["content"] == "hello"
    assert upstream_request["body_json"] == {"outbound": True}
    assert client_response["status_code"] == 200
    assert client_response["response_body_json"]["choices"][0]["message"]["content"] == "ok"


def test_api_exchange_logging_records_local_studio_and_redacts_token(tmp_path, monkeypatch):
    from aistudio_api.api import routes_local_studio

    store = RequestLogStore(tmp_path)
    store.set_enabled(True)
    secret = "sk-local-secret"

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"choices":[{"message":{"content":"ok"}}]}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            return FakeResponse()

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            return await http_client.post(
                "/api/local-studio/chat",
                json={"base_url": "http://compat.example/v1", "api_key": secret, "interface_mode": "openai", "model": "gpt-test", "message": "hello"},
            )

    old_store = runtime_state.request_log_store
    runtime_state.request_log_store = store
    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        before_ids = {item["id"] for item in store.list()}
        response = asyncio.run(send())
    finally:
        runtime_state.request_log_store = old_store

    assert response.status_code == 200
    entries = [store.get(item["id"]) for item in store.list() if item["id"] not in before_ids]
    phases = {entry["phase"] for entry in entries}
    assert {"client_request", "upstream_request", "upstream_response", "client_response"}.issubset(phases)
    chain_ids = {entry["chain_id"] for entry in entries}
    assert len(chain_ids) == 1
    raw = json.dumps(entries, ensure_ascii=False)
    assert secret not in raw
    client_request = next(entry for entry in entries if entry["phase"] == "client_request")
    upstream_request = next(entry for entry in entries if entry["phase"] == "upstream_request")
    assert client_request["body_json"]["api_key"] == "***"
    assert upstream_request["headers"]["Authorization"] == "Bearer ***"


async def _collect_stream_events(stream):
    events = []
    async for event in stream:
        events.append(event)
    return events