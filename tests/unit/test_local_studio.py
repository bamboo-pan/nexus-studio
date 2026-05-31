import asyncio
import json

import httpx
from fastapi import FastAPI

from aistudio_api.api import routes_local_studio
from aistudio_api.api.routes_local_studio import router as local_studio_router
from aistudio_api.config import settings
from aistudio_api.infrastructure.local_studio import (
    GPT_IMAGE_2_SIZE_OPTIONS,
    LocalStudioStore,
    build_local_studio_chat_payload,
    build_responses_payload,
    default_local_studio_base_url,
    filter_chat_models,
    filter_image_models,
    local_studio_chat_path,
    normalize_openai_base_url,
    resolve_local_studio_provider_settings,
    parse_local_studio_output,
    parse_local_studio_stream_event,
    validate_gpt_image_2_size,
)


def request_app(app: FastAPI, method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(send())


def local_studio_app(storage_dir):
    old_dir = settings.local_studio_dir
    settings.local_studio_dir = str(storage_dir)
    app = FastAPI()
    app.include_router(local_studio_router)
    return app, old_dir


def test_filter_chat_models_excludes_image_only_models():
    filtered = filter_chat_models(
        [
            {"id": "gpt-5.4-mini"},
            {"id": "gpt-image-2"},
            {"id": "GPT-IMAGE-1"},
            {"id": "gemini-3.1-flash-image-preview"},
            {"id": "gpt-4o-audio-preview"},
            {"id": "text-embedding-3-large"},
            {"name": "compatible-chat"},
        ]
    )

    assert [model["id"] for model in filtered] == ["gpt-5.4-mini", "compatible-chat"]


def test_filter_image_models_includes_provider_specific_metadata():
    models = [
        {"id": "gpt-5.4-mini"},
        {"id": "gpt-image-2"},
        {"id": "gemini-3.1-flash-image-preview"},
        {"name": "models/gemini-3-pro-image-preview"},
    ]
    google_filtered = filter_image_models(
        models,
        mode="gemini",
        provider_type="google-ai-studio",
    )
    openai_filtered = filter_image_models(
        models,
        mode="responses",
        provider_type="openai",
    )

    assert [model["id"] for model in openai_filtered] == ["gpt-image-2"]
    ids = [model["id"] for model in google_filtered]
    assert ids == ["gemini-3.1-flash-image-preview", "gemini-3-pro-image-preview"]
    gemini_model = next(model for model in google_filtered if model["id"] == "gemini-3-pro-image-preview")
    assert gemini_model["capabilities"]["image_output"] is True
    assert "4096x4096" in [item["size"] for item in gemini_model["image_generation"]["sizes"]]


def test_filter_image_models_defaults_to_openai_provider():
    filtered = filter_image_models(
        [
            {"id": "gpt-5.4-mini"},
            {"id": "gpt-image-2"},
            {"id": "gemini-3.1-flash-image-preview"},
            {"name": "models/gemini-3-pro-image-preview"},
        ]
    )

    assert [model["id"] for model in filtered] == ["gpt-image-2"]


def test_local_studio_protocol_paths_payloads_and_parsers():
    messages = [{"role": "user", "content": "hello"}]

    assert local_studio_chat_path("openai", "gpt-test") == "/chat/completions"
    assert local_studio_chat_path("responses", "gpt-test") == "/responses"
    assert local_studio_chat_path("gemini", "gemini-test", stream=True) == "/models/gemini-test:streamGenerateContent"
    assert local_studio_chat_path("claude", "claude-test") == "/messages"

    openai_payload = build_local_studio_chat_payload(mode="openai", model="gpt-test", messages=messages, options={"stream": True, "max_tokens": 12, "reasoning_effort": "low"})
    responses_payload = build_local_studio_chat_payload(mode="responses", model="gpt-test", messages=messages, options={"stream": True})
    gemini_payload = build_local_studio_chat_payload(mode="gemini", model="gemini-test", messages=messages, options={"stream": True, "reasoning_effort": "medium"})
    claude_payload = build_local_studio_chat_payload(mode="claude", model="claude-test", messages=messages, options={"stream": True, "max_tokens": 12, "reasoning_effort": "high"})

    assert openai_payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert openai_payload["stream"] is True
    assert openai_payload["thinking"] == "low"
    assert responses_payload["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    assert responses_payload["stream"] is True
    assert gemini_payload["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert gemini_payload["generationConfig"]["thinkingConfig"] == [1, None, None, 2]
    assert claude_payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert claude_payload["max_tokens"] == 12
    assert claude_payload["thinking"] == "high"

    assert parse_local_studio_output("openai", {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 2}})["content"] == "ok"
    assert parse_local_studio_output("responses", {"output_text": "ok"})["content"] == "ok"
    assert parse_local_studio_output("gemini", {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})["content"] == "ok"
    assert parse_local_studio_output("claude", {"content": [{"type": "text", "text": "ok"}]})["content"] == "ok"


def test_local_studio_conversation_routes_round_trip_and_bulk_delete(tmp_path):
    app, old_dir = local_studio_app(tmp_path)
    try:
        created = request_app(app, "POST", "/api/local-studio/conversations", json={"title": "Draft", "model": "gpt-5.4-mini", "interface_mode": "claude"})
        assert created.status_code == 200
        conversation = created.json()

        patched = request_app(app, "PATCH", f"/api/local-studio/conversations/{conversation['id']}", json={"title": "Renamed"})
        listed = request_app(app, "GET", "/api/local-studio/conversations")
        fetched = request_app(app, "GET", f"/api/local-studio/conversations/{conversation['id']}")
        deleted = request_app(app, "POST", "/api/local-studio/conversations/bulk-delete", json={"ids": [conversation["id"], "missing"]})
        missing = request_app(app, "GET", f"/api/local-studio/conversations/{conversation['id']}")

        assert patched.json()["title"] == "Renamed"
        assert patched.json()["interface_mode"] == "claude"
        assert listed.json()["data"][0]["title"] == "Renamed"
        assert listed.json()["data"][0]["interface_mode"] == "claude"
        assert fetched.json()["model"] == "gpt-5.4-mini"
        assert fetched.json()["interface_mode"] == "claude"
        assert deleted.json()["deleted"] == [conversation["id"]]
        assert deleted.json()["missing"] == ["missing"]
        assert missing.status_code == 404
    finally:
        settings.local_studio_dir = old_dir


def test_model_route_rejects_multiline_token_without_leaking_value(tmp_path):
    app, old_dir = local_studio_app(tmp_path)
    secret = "sk-test-secret"
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/models",
            json={"base_url": "https://api.openai.com/v1", "api_key": f"{secret}\nextra"},
        )
    finally:
        settings.local_studio_dir = old_dir

    body = response.json()
    assert response.status_code == 400
    assert body["detail"]["message"] == "API token must be a single line"
    assert secret not in str(body)


def test_google_local_studio_provider_uses_internal_endpoint_without_token(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"data":[{"id":"gemini-3-flash-preview"},{"id":"gpt-image-2"},{"id":"gemini-3.1-flash-image-preview"}]}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "gemini-3-flash-preview"}, {"id": "gpt-image-2"}, {"id": "gemini-3.1-flash-image-preview"}]}

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/models",
            json={"provider_type": "google-ai-studio", "interface_mode": "responses", "timeout": 5},
        )
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 200
    assert captured["timeout"] == 300
    assert captured["url"] == "http://testserver/v1/models"
    assert "Authorization" not in captured["headers"]
    assert [model["id"] for model in response.json()["data"]] == ["gemini-3-flash-preview"]
    assert [model["id"] for model in response.json()["image_models"]] == ["gemini-3.1-flash-image-preview"]


def test_google_local_studio_provider_chat_uses_internal_image_tool(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"id":"resp_image","output":[{"type":"image_generation_call","result":"aW1hZ2U=","mime_type":"image/png"}],"usage":{"input_tokens":1,"output_tokens":1}}'

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "resp_image",
                "output": [{"type": "image_generation_call", "result": "aW1hZ2U=", "mime_type": "image/png"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "provider_type": "google-ai-studio",
                "interface_mode": "responses",
                "model": "gemini-3-flash-preview",
                "message": "draw a smoke test square",
                "options": {"search": True, "image_tool_enabled": True, "image_tool_provider": "google-ai-studio", "image_model": "gemini-3-pro-image-preview", "size": "2048x2048", "cache_enabled": True},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 200
    assert captured["url"] == "http://testserver/v1/responses"
    assert "Authorization" not in captured["headers"]
    assert captured["json"]["tools"] == [
        {"type": "web_search_preview"},
        {"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3-pro-image-preview", "size": "2048x2048"},
    ]
    assistant = response.json()["conversation"]["messages"][-1]
    assert assistant["content"] == "Generated image"
    assert assistant["images"][0]["url"].startswith("/api/local-studio/assets/")
    assert "cache" not in response.json()


def test_openai_provider_chat_uses_openai_responses_search_tool(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"id":"resp_search","output_text":"ok","usage":{"input_tokens":1,"output_tokens":1}}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "resp_search", "output_text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "provider_type": "openai",
                "base_url": "https://compat.example/v1",
                "api_key": "token-1",
                "interface_mode": "responses",
                "model": "gpt-5.4-mini",
                "message": "search today's tech news",
                "options": {"search": True},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 200
    assert captured["url"] == "https://compat.example/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer token-1"
    assert captured["json"]["tools"] == [{"type": "web_search"}]
    assert "web_search_preview" not in json.dumps(captured["json"])
    assert response.json()["conversation"]["messages"][-1]["content"] == "ok"


def test_build_responses_payload_keeps_openai_image_tool_semantics():
    payload = build_responses_payload(
        model="gpt-5.4-mini",
        messages=[{"role": "user", "content": "draw"}],
        options={
            "image_tool_enabled": True,
            "image_tool_provider": "openai",
            "image_model": "gpt-image-2",
            "size": "1536x864",
            "quality": "high",
            "background": "transparent",
            "output_format": "png",
            "output_compression": 80,
        },
    )

    assert payload["tools"] == [
        {
            "type": "image_generation",
            "model": "gpt-image-2",
            "size": "1536x864",
            "quality": "high",
            "background": "transparent",
            "output_format": "png",
            "output_compression": 80,
        }
    ]


def test_custom_provider_display_name_does_not_override_openai_inference(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"data":[{"id":"gpt-5.4-mini"}]}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "gpt-5.4-mini"}]}

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/models",
            json={"provider": "My custom provider", "base_url": "http://compat.example/v1", "api_key": "token-1"},
        )
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 200
    assert captured["url"] == "http://compat.example/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer token-1"


def test_google_provider_settings_default_to_internal_url_without_token():
    provider_type, base_url, token = resolve_local_studio_provider_settings(provider_type="google-ai-studio", mode="gemini")

    assert provider_type == "google-ai-studio"
    assert base_url == default_local_studio_base_url("gemini")
    assert token == ""


def test_openai_provider_settings_require_url_and_token():
    try:
        resolve_local_studio_provider_settings(provider_type="openai", base_url="https://api.example.test/v1", token="")
    except ValueError as exc:
        assert str(exc) == "API token is required for OpenAI providers"
    else:
        raise AssertionError("expected token validation to fail")

    provider_type, base_url, token = resolve_local_studio_provider_settings(provider_type="openai", base_url="https://api.example.test/v1", token="token-1")
    assert provider_type == "openai"
    assert base_url == "https://api.example.test/v1"
    assert token == "token-1"


def test_request_cache_key_includes_provider_type(tmp_path):
    store = LocalStudioStore(tmp_path)
    request_body = {"model": "gpt-5.4-mini", "input": [{"role": "user", "content": "hello"}]}

    google_key = store.request_cache_key(
        base_url="http://testserver/v1",
        token="",
        mode="responses",
        model="gpt-5.4-mini",
        request_body=request_body,
        provider_type="google-ai-studio",
        provider_id="google-ai-studio",
        provider_name="Google AI Studio",
        namespace="unit-cache",
    )
    openai_key = store.request_cache_key(
        base_url="http://testserver/v1",
        token="",
        mode="responses",
        model="gpt-5.4-mini",
        request_body=request_body,
        provider_type="openai",
        provider_id="google-ai-studio",
        provider_name="Google AI Studio",
        namespace="unit-cache",
    )

    assert google_key != openai_key


def test_build_responses_payload_includes_attachments_reasoning_and_image_tool():
    payload = build_responses_payload(
        model="gpt-5.4-mini",
        messages=[
            {
                "role": "user",
                "content": "Describe these files",
                "attachments": [
                    {"name": "cat.png", "mime": "image/png", "path": "cat.png"},
                    {"name": "notes.pdf", "mime": "application/pdf", "path": "notes.pdf"},
                ],
            }
        ],
        options={
            "reasoning_effort": "high",
            "reasoning_summary": "auto",
            "image_tool_enabled": True,
            "size": "1024x1024",
            "quality": "high",
            "background": "transparent",
            "output_format": "png",
            "output_compression": 80,
        },
        asset_resolver=lambda asset: f"data:{asset['mime']};base64,ZmFrZQ==",
    )

    content = payload["input"][0]["content"]
    assert payload["model"] == "gpt-5.4-mini"
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert content[0] == {"type": "input_text", "text": "Describe these files"}
    assert content[1]["type"] == "input_image"
    assert content[2]["type"] == "input_file"
    assert payload["tools"] == [
        {
            "type": "image_generation",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "high",
            "background": "transparent",
            "output_format": "png",
            "output_compression": 80,
        }
    ]


def test_build_responses_payload_includes_web_search_tool():
    payload = build_responses_payload(
        model="gpt-5.4-mini",
        messages=[{"role": "user", "content": "latest docs"}],
        options={"search": True, "image_tool_enabled": True, "size": "1024x1024"},
    )

    assert payload["tools"][0] == {"type": "web_search_preview"}
    assert payload["tools"][1]["type"] == "image_generation"


def test_build_responses_payload_uses_openai_search_tool_for_openai_provider():
    payload = build_responses_payload(
        model="gpt-5.4-mini",
        messages=[{"role": "user", "content": "latest docs"}],
        options={"search": True, "image_tool_enabled": True, "image_tool_provider": "openai", "size": "1024x1024"},
        provider_type="openai",
    )

    assert payload["tools"][0] == {"type": "web_search"}
    assert payload["tools"][1]["type"] == "image_generation"
    assert "web_search_preview" not in json.dumps(payload)


def test_build_responses_payload_keeps_google_search_tool_for_google_provider():
    payload = build_responses_payload(
        model="gemini-3-flash-preview",
        messages=[{"role": "user", "content": "latest docs"}],
        options={"search": True},
        provider_type="google-ai-studio",
    )

    assert payload["tools"] == [{"type": "web_search_preview"}]


def test_gpt_image_2_size_options_match_official_constraints():
    sizes = [item["size"] for item in GPT_IMAGE_2_SIZE_OPTIONS]

    assert sizes == ["1024x1024", "1024x1536", "1536x1024", "1536x864", "2560x1440", "3824x2144"]
    assert "3840x2160" not in sizes
    assert [validate_gpt_image_2_size(size) for size in sizes] == sizes


def test_gpt_image_2_size_validation_rejects_invalid_constraints():
    invalid_sizes = ["3840x2160", "1000x1000", "4096x1024", "1024x320", "640x640"]

    for size in invalid_sizes:
        try:
            validate_gpt_image_2_size(size)
        except ValueError:
            continue
        raise AssertionError(f"expected {size} to be invalid")


def test_chat_route_posts_responses_payload_and_persists_reply(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"id":"resp_1","output_text":"hello from upstream","usage":{"input_tokens":3,"output_tokens":4}}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "resp_1", "output_text": "hello from upstream", "usage": {"input_tokens": 3, "output_tokens": 4}}

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "timeout": 45,
                "model": "gpt-5.4-mini",
                "message": "hello",
                "options": {"reasoning_effort": "low", "reasoning_summary": "auto"},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 200
    body = response.json()
    assert captured["timeout"] == 45
    assert captured["url"] == "http://compat.example/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer token-1"
    assert captured["json"]["input"][0] == {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    assert body["conversation"]["messages"][-1]["content"] == "hello from upstream"
    assert body["conversation"]["messages"][-1]["usage"]["input_tokens"] == 3


def test_chat_route_posts_selected_interface_mode_and_timeout(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"choices":[{"message":{"content":"hello chat"}}],"usage":{"prompt_tokens":1,"completion_tokens":2}}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "hello chat"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "timeout": 7,
                "interface_mode": "openai",
                "model": "gpt-5.4-mini",
                "message": "hello",
                "options": {"stream": False},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 200
    assert captured["timeout"] == 7
    assert captured["url"] == "http://compat.example/v1/chat/completions"
    assert captured["json"]["messages"][0]["content"][0]["text"] == "hello"
    assert response.json()["conversation"]["messages"][-1]["content"] == "hello chat"


def test_chat_route_ignores_removed_local_request_cache(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = {"posts": 0}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"id":"resp_cached","output_text":"cached upstream","usage":{"input_tokens":2,"output_tokens":3}}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "resp_cached", "output_text": "cached upstream", "usage": {"input_tokens": 2, "output_tokens": 3}}

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            captured["posts"] += 1
            return FakeResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    payload = {
        "base_url": "http://compat.example/v1",
        "api_key": "token-1",
        "provider_id": "google-ai-studio",
        "provider_name": "Google AI Studio",
        "model": "gpt-5.4-mini",
        "message": "hello cache",
        "options": {"stream": False, "cache_enabled": True, "cache_namespace": "unit-cache"},
    }

    try:
        first = request_app(app, "POST", "/api/local-studio/chat", json=payload)
        second = request_app(app, "POST", "/api/local-studio/chat", json=payload)
    finally:
        settings.local_studio_dir = old_dir

    first_body = first.json()
    second_body = second.json()
    assert first.status_code == 200
    assert second.status_code == 200
    assert captured["posts"] == 2
    assert "cache" not in first_body
    assert "cache" not in second_body
    assert second_body["conversation"]["messages"][-1]["content"] == "cached upstream"
    assert "cache" not in second_body["conversation"]["messages"][-1]


def test_stream_chat_ignores_removed_local_request_cache(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = {"streams": 0}

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"type":"response.output_text.delta","delta":"stream cached"}'
            yield ""
            yield 'data: {"type":"response.completed","response":{"output_text":"stream cached","usage":{"input_tokens":2,"output_tokens":3}}}'
            yield ""

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, headers, json):
            captured["streams"] += 1
            return FakeStreamResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    payload = {
        "base_url": "http://compat.example/v1",
        "api_key": "token-1",
        "provider_id": "google-ai-studio",
        "provider_name": "Google AI Studio",
        "model": "gpt-5.4-mini",
        "message": "hello stream cache",
        "options": {"stream": True, "cache_enabled": True, "cache_namespace": "unit-stream-cache"},
    }

    try:
        first = request_app(app, "POST", "/api/local-studio/chat", json=payload)
        second = request_app(app, "POST", "/api/local-studio/chat", json=payload)
    finally:
        settings.local_studio_dir = old_dir

    completed = [
        json.loads(line[6:])
        for line in second.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "local_studio.completed"
    ][0]
    deltas = [
        json.loads(line[6:])
        for line in second.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "local_studio.delta"
    ]

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["cache-control"] == "no-cache"
    assert second.headers["x-accel-buffering"] == "no"
    assert captured["streams"] == 2
    assert any(delta.get("content") == "stream cached" for delta in deltas)
    assert "cache" not in completed
    assert "cache" not in completed["conversation"]["messages"][-1]


def test_chat_route_image_tool_http_error_does_not_call_images_api(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = []

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            captured.append((url, json))
            request = httpx.Request("POST", url)
            response = httpx.Response(502, request=request, text="tool unsupported")
            raise httpx.HTTPStatusError("bad gateway", request=request, response=response)

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "make a smoke-test square",
                "options": {"image_tool_enabled": True, "size": "1536x864", "quality": "low", "background": "opaque", "output_format": "png"},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 502
    assert [url for url, _ in captured] == ["http://compat.example/v1/responses"]
    assert response.json()["detail"]["message"] == "HTTP 502: tool unsupported"


def test_chat_route_image_tool_transport_error_does_not_call_images_api(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured = []

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            captured.append(url)
            raise httpx.ReadError("peer closed connection without sending complete message body")

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "make a smoke-test circle",
                "options": {"image_tool_enabled": True, "size": "1536x864", "quality": "low"},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 502
    assert captured == ["http://compat.example/v1/responses"]
    assert "peer closed connection" in response.json()["detail"]["message"]


def test_responses_stream_completed_event_extracts_image_candidates():
    parsed = parse_local_studio_stream_event(
        "responses",
        {
            "type": "response.completed",
            "response": {
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
                    {"type": "image_generation_call", "result": "ZmFrZQ=="},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        },
    )

    assert parsed["content"] == "done"
    assert parsed["usage"] == {"input_tokens": 1, "output_tokens": 2}
    assert parsed["image_candidates"] == [{"result": "ZmFrZQ==", "mime": "image/png"}]


def test_responses_stream_reasoning_summary_events_extract_thinking():
    delta = parse_local_studio_stream_event(
        "responses",
        {"type": "response.reasoning_summary_text.delta", "delta": "planning"},
    )
    done = parse_local_studio_stream_event(
        "responses",
        {"type": "response.reasoning_summary_text.done", "text": "planning done"},
    )
    item_done = parse_local_studio_stream_event(
        "responses",
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "final summary"}]},
        },
    )

    assert delta["thinking"] == "planning"
    assert done["thinking"] == "planning done"
    assert item_done["thinking"] == "final summary"


def test_responses_stream_function_call_done_extracts_tool_progress():
    parsed = parse_local_studio_stream_event(
        "responses",
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "name": "image_generation",
                "arguments": '{"prompt":"red square","size":"1024x1024"}',
            },
        },
    )

    assert parsed["thinking"] == 'Tool call requested: image_generation {"prompt":"red square","size":"1024x1024"}'


def test_stream_chat_persists_responses_reasoning_summary(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"type":"response.reasoning_summary_text.delta","delta":"I will calculate."}'
            yield ""
            yield 'data: {"type":"response.output_text.delta","delta":"Answer"}'
            yield ""
            yield 'data: {"type":"response.completed","response":{"output_text":"Answer","usage":{"input_tokens":2,"output_tokens":3}}}'
            yield ""

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, headers, json):
            return FakeStreamResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "calculate",
                "options": {"stream": True, "reasoning_effort": "high", "reasoning_summary": "auto"},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    completed = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "local_studio.completed"
    ][0]
    assistant = completed["conversation"]["messages"][-1]

    assert response.status_code == 200
    assert assistant["content"] == "Answer"
    assert assistant["thinking"] == "I will calculate."


def test_stream_chat_emits_image_tool_progress_before_completion(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            events = [
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "image_generation",
                        "arguments": '{"prompt":"red square","size":"1024x1024"}',
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "output_text": "Generated image",
                        "thinking": "Image generation tool selected gemini-3.1-flash-image-preview at 1024x1024.",
                        "output": [{"type": "image_generation_call", "result": "ZmFrZQ=="}],
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                },
            ]
            for event in events:
                yield "data: " + json.dumps(event)
                yield ""

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, headers, json):
            return FakeStreamResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "make a cat image",
                "options": {"stream": True, "image_tool_enabled": True},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    progress_index = next(
        index
        for index, event in enumerate(events)
        if event.get("type") == "local_studio.delta" and "Tool call requested: image_generation" in str(event.get("thinking") or "")
    )
    completed_index = next(index for index, event in enumerate(events) if event.get("type") == "local_studio.completed")
    assistant = events[completed_index]["conversation"]["messages"][-1]

    assert response.status_code == 200
    assert progress_index < completed_index
    assert assistant["content"] == "Generated image"
    assert "Tool call requested: image_generation" in assistant["thinking"]
    assert "Image generation tool selected gemini-3.1-flash-image-preview" in assistant["thinking"]
    assert assistant["images"][0]["url"].startswith("/api/local-studio/assets/")


def test_stream_chat_persists_response_image_candidates(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            events = [
                {"type": "response.output_text.delta", "delta": "done"},
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
                            {"type": "image_generation_call", "result": "ZmFrZQ=="},
                        ],
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                },
            ]
            for event in events:
                yield "data: " + json.dumps(event)
                yield ""

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, headers, json):
            return FakeStreamResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "make a cat image",
                "options": {"stream": True, "image_tool_enabled": True},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 200
    completed = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "local_studio.completed"
    ][0]
    assistant = completed["conversation"]["messages"][-1]
    assert assistant["content"] == "done"
    assert assistant["images"][0]["url"].startswith("/api/local-studio/assets/")


def test_stream_chat_image_tool_without_candidates_does_not_call_images_api(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    captured_posts = []

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"type":"response.completed","response":{"output":[],"usage":{"input_tokens":1,"output_tokens":1}}}'
            yield ""

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, headers, json):
            return FakeStreamResponse()

        async def post(self, url, headers, json):
            captured_posts.append((url, json))
            raise AssertionError("Local Studio Responses image tool must not call /images/generations")

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "make a cat image",
                "options": {"stream": True, "image_tool_enabled": True, "size": "1536x864"},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    completed = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "local_studio.completed"
    ][0]

    assert response.status_code == 200
    assert captured_posts == []
    assert completed["conversation"]["messages"][-1]["content"] == "(no response content)"
    assert completed["conversation"]["messages"][-1]["images"] == []


def test_responses_partial_image_stream_event_extracts_image_candidate():
    parsed = parse_local_studio_stream_event(
        "responses",
        {
            "type": "response.image_generation_call.partial_image",
            "partial_image": "ZmFrZQ==",
        },
    )

    assert parsed["image_candidates"] == [{"partial_image": "ZmFrZQ==", "mime": "image/png"}]


def test_local_studio_store_deduplicates_equivalent_response_images(tmp_path):
    store = LocalStudioStore(tmp_path)

    images = store.save_response_images(
        [
            {"partial_image": "ZmFrZQ==", "mime": "image/png"},
            {"result": "ZmFrZQ==", "mime": "image/png"},
            {"data_url": "data:image/png;base64,ZmFrZQ=="},
        ]
    )

    assert len(images) == 1
    assert images[0]["url"].startswith("/api/local-studio/assets/")


def test_stream_chat_image_tool_deduplicates_partial_and_final_candidates(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"type":"response.image_generation_call.partial_image","partial_image":"cGFydGlhbA=="}'
            yield ""
            yield 'data: {"type":"response.completed","response":{"output":[{"type":"image_generation_call","result":"ZmluYWw="}],"usage":{"input_tokens":3,"output_tokens":4,"total_tokens":7}}}'
            yield ""

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, headers, json):
            return FakeStreamResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "make a cat image",
                "options": {"stream": True, "image_tool_enabled": True},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    completed = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "local_studio.completed"
    ][0]
    assistant = completed["conversation"]["messages"][-1]

    assert response.status_code == 200
    assert assistant["content"] == "Generated image"
    assert len(assistant["images"]) == 1
    assert assistant["images"][0]["url"].startswith("/api/local-studio/assets/")
    assert (tmp_path / "files" / assistant["images"][0]["path"]).read_bytes() == b"final"


def test_stream_chat_transport_error_persists_partial_image_candidate(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"type":"response.output_text.delta","delta":"draft"}'
            yield ""
            yield 'data: {"type":"response.image_generation_call.partial_image","partial_image":"ZmFrZQ=="}'
            yield ""
            raise httpx.ReadError("peer closed connection without sending complete message body")

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, headers, json):
            return FakeStreamResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "make a cat image",
                "options": {"stream": True, "image_tool_enabled": True},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    completed = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "local_studio.completed"
    ][0]
    assistant = completed["conversation"]["messages"][-1]

    assert response.status_code == 200
    assert assistant["content"] == "draft"
    assert assistant["images"][0]["url"].startswith("/api/local-studio/assets/")
    assert "Partial response saved" in assistant["error"]
    assert "peer closed connection" in assistant["error"]


def test_stream_chat_transport_error_without_partial_output_persists_error(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            raise httpx.ReadError("peer closed connection without sending complete message body")
            yield ""

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, headers, json):
            return FakeStreamResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "make a cat image",
                "options": {"stream": True, "image_tool_enabled": True},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    completed = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "local_studio.completed"
    ][0]
    assistant = completed["conversation"]["messages"][-1]

    assert response.status_code == 200
    assert assistant["content"] == ""
    assert assistant["images"] == []
    assert "peer closed connection" in assistant["error"]


def test_stream_chat_http_status_error_reads_stream_body_without_response_not_read(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)

    class FakeStreamResponse:
        status_code = 400
        headers = {"content-type": "application/json"}
        is_error = True
        reason_phrase = "Bad Request"
        encoding = "utf-8"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        @property
        def content(self):
            raise httpx.ResponseNotRead()

        async def aread(self):
            return b'{"error":{"message":"search tool rejected"}}'

        def raise_for_status(self):
            raise AssertionError("streaming error bodies should be handled before raise_for_status")

        async def aiter_lines(self):
            raise AssertionError("error streams should not be iterated")
            yield ""

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, headers, json):
            return FakeStreamResponse()

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "message": "search latest news",
                "options": {"stream": True, "search": True},
            },
        )
    finally:
        settings.local_studio_dir = old_dir

    completed = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "local_studio.completed"
    ][0]
    assistant = completed["conversation"]["messages"][-1]

    assert response.status_code == 200
    assert "search tool rejected" in response.text
    assert "ResponseNotRead" not in response.text
    assert "search tool rejected" in assistant["error"]


def test_chat_route_surfaces_http_524_and_records_error(tmp_path, monkeypatch):
    app, old_dir = local_studio_app(tmp_path)
    store = LocalStudioStore(tmp_path)
    conversation = store.create({"model": "gpt-5.4-mini"})

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            request = httpx.Request("POST", url)
            response = httpx.Response(524, request=request, text="upstream timeout")
            raise httpx.HTTPStatusError("timeout", request=request, response=response)

    monkeypatch.setattr(routes_local_studio, "_new_http_client", FakeClient)
    try:
        response = request_app(
            app,
            "POST",
            "/api/local-studio/chat",
            json={
                "base_url": "http://compat.example/v1",
                "api_key": "token-1",
                "model": "gpt-5.4-mini",
                "conversation_id": conversation["id"],
                "message": "make a cat fishing image",
            },
        )
        saved = store.get(conversation["id"])
    finally:
        settings.local_studio_dir = old_dir

    assert response.status_code == 502
    assert "HTTP 524" in response.json()["detail"]["message"]
    assert "HTTP 524" in saved["messages"][-1]["error"]


def test_normalize_openai_base_url_rejects_non_http_url():
    try:
        normalize_openai_base_url("file:///tmp/api")
    except ValueError as exc:
        assert "http:// or https://" in str(exc)
    else:
        raise AssertionError("expected invalid base URL")
