import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from aistudio_api.application import api_service
from aistudio_api.api.app import app
from aistudio_api.application.api_service import stats_response
from aistudio_api.api.responses import (
    chat_completion_response,
    normalize_usage,
    sse_chunk,
    sse_usage_chunk,
    to_gemini_parts,
    to_gemini_usage_metadata,
)
from aistudio_api.api.state import runtime_state


def test_favicon_route_avoids_browser_console_404():
    response = TestClient(app).get("/favicon.ico")

    assert response.status_code == 204


def test_sse_chunk_includes_null_usage_when_requested():
    payload = sse_chunk("chatcmpl-test", "models/gemma-4-31b-it", "你好", include_usage=True)
    data = json.loads(payload.removeprefix("data: ").strip())

    assert data["choices"][0]["delta"]["content"] == "你好"
    assert "usage" in data
    assert data["usage"] is None


def test_chat_request_stream_usage_is_enabled_by_default():
    schemas = pytest.importorskip("aistudio_api.api.schemas")
    req = schemas.ChatRequest(
        model="models/gemma-4-31b-it",
        messages=[{"role": "user", "content": "你好"}],
        stream=True,
        stream_options={},
    )

    assert req.stream_options.include_usage is True


def test_sse_usage_chunk_matches_openai_style_shape():
    payload = sse_usage_chunk(
        "chatcmpl-test",
        "models/gemma-4-31b-it",
        {
            "prompt_tokens": 5,
            "completion_tokens": 161,
            "total_tokens": 166,
            "completion_tokens_details": {"reasoning_tokens": 153},
        },
    )
    data = json.loads(payload.removeprefix("data: ").strip())

    assert data["choices"] == []
    assert data["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 161,
        "total_tokens": 166,
        "cached_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 153},
    }


def test_normalize_usage_defaults_missing_values_to_zero():
    assert normalize_usage(None) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def test_normalize_usage_preserves_cached_tokens():
    assert normalize_usage(
        {
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "total_tokens": 25,
            "cached_tokens": 12,
            "completion_tokens_details": {"reasoning_tokens": 2},
        }
    ) == {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_tokens": 25,
        "cached_tokens": 12,
        "prompt_tokens_details": {"cached_tokens": 12},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }


def test_to_gemini_usage_metadata_uses_visible_and_reasoning_tokens():
    assert to_gemini_usage_metadata(
        {
            "prompt_tokens": 9,
            "completion_tokens": 316,
            "total_tokens": 325,
            "cached_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 290, "visible_tokens": 26},
        }
    ) == {
        "promptTokenCount": 9,
        "candidatesTokenCount": 26,
        "thoughtsTokenCount": 290,
        "cachedContentTokenCount": 4,
        "totalTokenCount": 325,
    }


def test_chat_completion_response_maps_function_calls_to_openai_tool_calls():
    response = chat_completion_response(
        model="models/gemma-4-31b-it",
        content="",
        function_calls=[{"name": "getWeather", "args": {"city": "Shanghai"}}],
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "getWeather"
    assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {"city": "Shanghai"}


def test_sse_chunk_can_emit_tool_calls_delta():
    payload = sse_chunk(
        "chatcmpl-test",
        "models/gemma-4-31b-it",
        "",
        tool_calls=[
            {
                "id": "call_test",
                "type": "function",
                "function": {"name": "getWeather", "arguments": "{\"city\":\"Shanghai\"}"},
            }
        ],
        include_usage=True,
    )
    data = json.loads(payload.removeprefix("data: ").strip())

    assert data["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    assert data["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "getWeather"
    assert data["usage"] is None


def test_sse_chunk_stringifies_tool_call_delta_arguments():
    payload = sse_chunk(
        "chatcmpl-test",
        "models/gemma-4-31b-it",
        "",
        tool_calls=[{"type": "function", "function": {"name": "getWeather", "arguments": {"city": "Shanghai"}}}],
    )
    data = json.loads(payload.removeprefix("data: ").strip())

    arguments = data["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {"city": "Shanghai"}


def test_to_gemini_parts_keeps_function_call_and_response_parts():
    parts = to_gemini_parts(
        "",
        function_calls=[{"name": "getWeather", "args": {"city": "Shanghai"}}],
        function_responses=[{"name": "getWeather", "args": {"temperature": "24C"}}],
    )

    assert parts == [
        {"functionCall": {"name": "getWeather", "args": {"city": "Shanghai"}}},
        {"functionResponse": {"name": "getWeather", "response": {"temperature": "24C"}}},
    ]


def test_to_gemini_parts_can_emit_thought_part():
    assert to_gemini_parts("答案", thinking="思考") == [
        {"text": "思考", "thought": True},
        {"text": "答案"},
    ]


def test_stats_response_aggregates_image_sizes_by_resolution():
    old_stats = runtime_state.model_stats
    runtime_state.model_stats = {
        "image-a": {
            "requests": 1,
            "success": 1,
            "rate_limited": 0,
            "errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 1,
            "image_sizes": {"1024x1024": 2},
            "last_used": "now",
        },
        "image-b": {
            "requests": 1,
            "success": 1,
            "rate_limited": 0,
            "errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 1,
            "image_sizes": {"1024x1024": 1, "1024x1792": 3},
            "last_used": "now",
        },
    }
    try:
        response = stats_response()
    finally:
        runtime_state.model_stats = old_stats

    assert response["totals"]["image_sizes"] == {"1024x1024": 3, "1024x1792": 3}
    assert response["totals"]["image_total"] == 6


def test_responses_search_image_fallback_splits_builtin_search_and_function_tool(monkeypatch):
    captured_tools = []
    captured_messages = []
    captured_image_request = {}

    async def fake_handle_chat(req, client, request=None):
        captured_tools.append(req.tools)
        captured_messages.append(req.messages)
        return {
            "choices": [
                {
                    "message": {
                        "content": 'Tool call requested: image_generation {"prompt":"red square with searched context","size":"1024x1024"}'
                    }
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }

    async def fake_handle_image_generation(req, client):
        captured_image_request["model"] = req.model
        captured_image_request["prompt"] = req.prompt
        captured_image_request["size"] = req.size
        return {"data": [{"b64_json": "ZmFrZQ==", "mime_type": "image/png"}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(api_service, "handle_chat", fake_handle_chat)
    monkeypatch.setattr(api_service, "handle_image_generation", fake_handle_image_generation)

    response = asyncio.run(
        api_service.handle_openai_responses(
            {
                "model": "gemini-3-flash-preview",
                "input": "Search for color symbolism and make it an image",
                "tools": [
                    {"type": "web_search_preview"},
                    {"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3.1-flash-image-preview"},
                ],
            },
            client=object(),
        )
    )

    assert len(captured_tools) == 1
    assert [tool.type for tool in captured_tools[0]] == ["web_search_preview"]
    assert any("Image generation tool selection protocol" in message.content for message in captured_messages[0] if message.role == "system")
    assert response["output"][0]["type"] == "web_search_call"
    assert response["output"][1]["type"] == "image_generation_call"
    assert captured_image_request == {
        "model": "gemini-3.1-flash-image-preview",
        "prompt": "red square with searched context",
        "size": "1024x1024",
    }


def test_responses_search_image_empty_decision_generates_explicit_image_request(monkeypatch):
    captured_tools = []
    captured_image_request = {}

    async def fake_handle_chat(req, client, request=None):
        captured_tools.append(req.tools)
        return {
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 0},
        }

    async def fake_handle_image_generation(req, client):
        captured_image_request["model"] = req.model
        captured_image_request["prompt"] = req.prompt
        captured_image_request["size"] = req.size
        return {"data": [{"b64_json": "ZmFrZQ==", "mime_type": "image/png"}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(api_service, "handle_chat", fake_handle_chat)
    monkeypatch.setattr(api_service, "handle_image_generation", fake_handle_image_generation)

    response = asyncio.run(
        api_service.handle_openai_responses(
            {
                "model": "gemini-3-flash-preview",
                "input": "Search recent visual references and create an image of a neon city skyline.",
                "tools": [
                    {"type": "web_search_preview"},
                    {"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3.1-flash-image-preview"},
                ],
            },
            client=object(),
        )
    )

    assert len(captured_tools) == 1
    assert [tool.type for tool in captured_tools[0]] == ["web_search_preview"]
    assert captured_image_request == {
        "model": "gemini-3.1-flash-image-preview",
        "prompt": "Search recent visual references and create an image of a neon city skyline.",
        "size": "1024x1024",
    }
    assert response["output_text"] == "Generated image"
    assert response["output"][0]["type"] == "web_search_call"
    assert response["output"][1]["type"] == "image_generation_call"


def test_responses_search_image_empty_decision_respects_explicit_no_image_request(monkeypatch):
    image_calls = []

    async def fake_handle_chat(req, client, request=None):
        return {
            "choices": [{"message": {"content": "nexus-text-ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }

    async def fake_handle_image_generation(req, client):
        image_calls.append(req)
        return {"data": [{"b64_json": "ZmFrZQ==", "mime_type": "image/png"}]}

    monkeypatch.setattr(api_service, "handle_chat", fake_handle_chat)
    monkeypatch.setattr(api_service, "handle_image_generation", fake_handle_image_generation)

    response = asyncio.run(
        api_service.handle_openai_responses(
            {
                "model": "gemini-3-flash-preview",
                "input": "Do not create an image. Reply with exactly: nexus-text-ok",
                "tools": [
                    {"type": "web_search_preview"},
                    {"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3.1-flash-image-preview"},
                ],
            },
            client=object(),
        )
    )

    assert image_calls == []
    assert response["output_text"] == "nexus-text-ok"
    assert response["output"][0]["type"] == "web_search_call"
    assert response["output"][1]["type"] == "message"


def test_responses_image_tool_retries_google_image_model_not_found(monkeypatch):
    image_models = []

    async def fake_handle_chat(req, client, request=None):
        return {
            "choices": [
                {
                    "message": {
                        "content": 'Tool call requested: image_generation {"prompt":"fallback square","size":"1024x1024"}'
                    }
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }

    async def fake_handle_image_generation(req, client):
        image_models.append(req.model)
        if req.model == "gemini-3.1-flash-image-preview":
            raise HTTPException(502, detail={"message": "HTTP 404: Requested entity was not found", "type": "upstream_error"})
        return {"data": [{"b64_json": "ZmFrZQ==", "mime_type": "image/png"}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(api_service, "handle_chat", fake_handle_chat)
    monkeypatch.setattr(api_service, "handle_image_generation", fake_handle_image_generation)

    response = asyncio.run(
        api_service.handle_openai_responses(
            {
                "model": "gemini-3-flash-preview",
                "input": "make an image",
                "tools": [{"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3.1-flash-image-preview"}],
            },
            client=object(),
        )
    )

    assert image_models == ["gemini-3.1-flash-image-preview", "gemini-3-pro-image-preview"]
    assert response["output"][0]["type"] == "image_generation_call"
    assert response["thinking"] == "Image generation tool selected gemini-3-pro-image-preview at 1024x1024."


def test_responses_image_tool_accepts_gemini_dalle_text_action(monkeypatch):
    captured_image_request = {}

    async def fake_handle_chat(req, client, request=None):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "action": "dalle.text2im",
                                "action_input": json.dumps(
                                    {
                                        "prompt": "A simple, solid blue square icon centered on a plain white background.",
                                    }
                                ),
                                "thought": "The user wants an image.",
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }

    async def fake_handle_image_generation(req, client):
        captured_image_request["model"] = req.model
        captured_image_request["prompt"] = req.prompt
        captured_image_request["size"] = req.size
        return {"data": [{"b64_json": "ZmFrZQ==", "mime_type": "image/png"}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(api_service, "handle_chat", fake_handle_chat)
    monkeypatch.setattr(api_service, "handle_image_generation", fake_handle_image_generation)

    response = asyncio.run(
        api_service.handle_openai_responses(
            {
                "model": "gemini-3-flash-preview",
                "input": "Create an image: a simple blue square icon on a plain white background.",
                "tools": [{"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3.1-flash-image-preview"}],
            },
            client=object(),
        )
    )

    assert captured_image_request == {
        "model": "gemini-3.1-flash-image-preview",
        "prompt": "A simple, solid blue square icon centered on a plain white background.",
        "size": "1024x1024",
    }
    assert response["output_text"] == "Generated image"
    assert response["output"][0]["type"] == "image_generation_call"


def test_responses_reasoning_effort_maps_to_chat_thinking(monkeypatch):
    captured = {}

    async def fake_handle_chat(req, client, request=None):
        captured["thinking"] = req.thinking
        return {
            "choices": [{"message": {"content": "ok"}}],
            "model": req.model,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(api_service, "handle_chat", fake_handle_chat)

    response = asyncio.run(
        api_service.handle_openai_responses(
            {
                "model": "gemini-3-flash-preview",
                "input": "reason briefly",
                "reasoning": {"effort": "high", "summary": "auto"},
            },
            client=object(),
        )
    )

    assert captured["thinking"] == "high"
    assert response["output_text"] == "ok"


async def collect_response_stream_payloads(response):
    return [payload async for payload in api_service._iter_sse_data_payloads(response)]


def test_responses_image_tool_streams_text_decision_before_completed(monkeypatch):
    captured = {}

    async def fake_handle_chat(req, client, request=None):
        captured["stream"] = req.stream

        async def body():
            yield 'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
            yield 'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n'
            yield 'data: [DONE]\n\n'

        return api_service.StreamingResponse(body(), media_type="text/event-stream")

    monkeypatch.setattr(api_service, "handle_chat", fake_handle_chat)

    response = asyncio.run(
        api_service.handle_openai_responses(
            {
                "model": "gemini-3-flash-preview",
                "input": "answer normally",
                "stream": True,
                "tools": [{"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3-pro-image-preview"}],
            },
            client=object(),
        )
    )
    payloads = asyncio.run(collect_response_stream_payloads(response))
    events = [json.loads(payload) for payload in payloads if payload != "[DONE]"]
    event_types = [event.get("type") for event in events]

    assert captured["stream"] is True
    assert event_types.index("response.output_text.delta") < event_types.index("response.completed")
    assert [event.get("delta") for event in events if event.get("type") == "response.output_text.delta"] == ["hel", "lo"]
    assert events[-1]["response"]["output_text"] == "hello"


def test_responses_image_tool_stream_generates_image_after_streamed_tool_decision(monkeypatch):
    captured_image_request = {}

    async def fake_handle_chat(req, client, request=None):
        assert req.stream is True

        async def body():
            yield 'data: {"choices":[{"delta":{"content":"Tool call requested: image_generation {\\"prompt\\":\\"red square\\",\\"size\\":\\"1024x1024\\"}"},"finish_reason":"stop"}]}\n\n'
            yield 'data: [DONE]\n\n'

        return api_service.StreamingResponse(body(), media_type="text/event-stream")

    async def fake_handle_image_generation(req, client):
        captured_image_request["model"] = req.model
        captured_image_request["prompt"] = req.prompt
        captured_image_request["size"] = req.size
        return {"data": [{"b64_json": "ZmFrZQ==", "mime_type": "image/png"}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(api_service, "handle_chat", fake_handle_chat)
    monkeypatch.setattr(api_service, "handle_image_generation", fake_handle_image_generation)

    response = asyncio.run(
        api_service.handle_openai_responses(
            {
                "model": "gemini-3-flash-preview",
                "input": "make an image",
                "stream": True,
                "tools": [{"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3-pro-image-preview"}],
            },
            client=object(),
        )
    )
    payloads = asyncio.run(collect_response_stream_payloads(response))
    events = [json.loads(payload) for payload in payloads if payload != "[DONE]"]
    completed = next(event for event in events if event.get("type") == "response.completed")

    assert captured_image_request == {"model": "gemini-3-pro-image-preview", "prompt": "red square", "size": "1024x1024"}
    assert any(event.get("type") == "response.function_call_arguments.done" for event in events)
    assert any(event.get("type") == "response.image_generation_call.partial_image" for event in events)
    assert completed["response"]["output_text"] == "Generated image"
    assert completed["response"]["output"][0]["type"] == "image_generation_call"


def test_responses_image_tool_stream_accepts_gemini_dalle_text_action(monkeypatch):
    captured_image_request = {}

    async def fake_handle_chat(req, client, request=None):
        assert req.stream is True

        async def body():
            chunks = [
                '{\n  "action": "dalle.text2im",\n  "action_input": "{ \\"',
                'prompt\\": \\"blue square\\" }",\n  "thought": "The user wants an image."\n}',
            ]
            for index, chunk in enumerate(chunks):
                choice = {"delta": {"content": chunk}}
                if index == len(chunks) - 1:
                    choice["finish_reason"] = "stop"
                yield f"data: {json.dumps({'choices': [choice]})}\n\n"
            yield "data: [DONE]\n\n"

        return api_service.StreamingResponse(body(), media_type="text/event-stream")

    async def fake_handle_image_generation(req, client):
        captured_image_request["model"] = req.model
        captured_image_request["prompt"] = req.prompt
        captured_image_request["size"] = req.size
        return {"data": [{"b64_json": "ZmFrZQ==", "mime_type": "image/png"}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(api_service, "handle_chat", fake_handle_chat)
    monkeypatch.setattr(api_service, "handle_image_generation", fake_handle_image_generation)

    response = asyncio.run(
        api_service.handle_openai_responses(
            {
                "model": "gemini-3-flash-preview",
                "input": "Create an image: a simple blue square icon on a plain white background.",
                "stream": True,
                "tools": [{"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3.1-flash-image-preview"}],
            },
            client=object(),
        )
    )
    payloads = asyncio.run(collect_response_stream_payloads(response))
    events = [json.loads(payload) for payload in payloads if payload != "[DONE]"]
    completed = next(event for event in events if event.get("type") == "response.completed")

    assert captured_image_request == {"model": "gemini-3.1-flash-image-preview", "prompt": "blue square", "size": "1024x1024"}
    assert not any(event.get("type") == "response.output_text.delta" for event in events)
    assert any(event.get("type") == "response.function_call_arguments.done" for event in events)
    assert any(event.get("type") == "response.image_generation_call.partial_image" for event in events)
    assert completed["response"]["output_text"] == "Generated image"
    assert completed["response"]["output"][0]["type"] == "image_generation_call"
