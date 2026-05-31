import asyncio
import base64
import json

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.state import runtime_state
from aistudio_api.application.api_service import _image_size_for_google_provider
from aistudio_api.domain.model_capabilities import clear_dynamic_model_capabilities, get_model_capabilities
from aistudio_api.domain.models import Candidate, GeneratedImage, ModelOutput


class FakeTextClient:
    def __init__(self, *, text: str = "ok", thinking: str = "", function_calls: list[dict] | None = None):
        self.text = text
        self.thinking = thinking
        self.function_calls = function_calls or []
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return ModelOutput(
            candidates=[Candidate(text=self.text, thinking=self.thinking, function_calls=self.function_calls)],
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        )


class FakeTextAndImageClient(FakeTextClient):
    async def stream_generate_content(self, **kwargs):
        self.calls.append(kwargs)
        yield ("thinking", self.thinking or "Selecting image tool")
        if self.text:
            yield ("body", self.text)
        if self.function_calls:
            yield ("tool_calls", self.function_calls)
        yield ("usage", {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})

    async def generate_image(self, **kwargs):
        self.calls.append({"image": kwargs})
        return ModelOutput(
            candidates=[Candidate(text="revised image", images=[GeneratedImage(mime="image/png", data=b"image-bytes", size=11)])],
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )


class FakeStreamClient:
    def __init__(self, events: list[tuple[str, object]] | None = None):
        self.calls = []
        self.events = events or [
            ("tool_calls", [{"name": "lookup", "args": {"query": "weather"}}]),
            ("usage", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}),
        ]

    async def stream_generate_content(self, **kwargs):
        self.calls.append(kwargs)
        for event in self.events:
            yield event


class ModelListClient(FakeTextClient):
    def __init__(self, models: list[str]):
        super().__init__()
        self.models = models
        self.list_calls = 0

    async def list_available_models(self):
        self.list_calls += 1
        return self.models


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


def response_stream_events(text: str) -> list[tuple[str | None, dict]]:
    events = []
    event_name = None
    data_lines = []
    for line in text.splitlines():
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                if data != "[DONE]":
                    events.append((event_name, json.loads(data)))
            event_name = None
            data_lines = []
            continue
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            events.append((event_name, json.loads(data)))
    return events


def reconstructed_function_call_arguments(events: list[tuple[str | None, dict]]) -> str:
    arguments_by_item_id: dict[str, str] = {}
    for event_name, data in events:
        if event_name == "response.output_item.added" and data.get("item", {}).get("type") == "function_call":
            item = data["item"]
            arguments_by_item_id[item["id"]] = item.get("arguments") or ""
        elif event_name == "response.function_call_arguments.delta":
            arguments_by_item_id[data["item_id"]] = arguments_by_item_id.get(data["item_id"], "") + data.get("delta", "")
        elif event_name == "response.function_call_arguments.done":
            arguments_by_item_id.setdefault(data["item_id"], data.get("arguments") or "")
    assert len(arguments_by_item_id) == 1
    return next(iter(arguments_by_item_id.values()))


def test_openai_responses_accepts_text_format_json_schema():
    client = FakeTextClient(text='{"ok":true}')

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": "return json",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "Answer",
                    "strict": True,
                    "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                }
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["output_text"] == '{"ok":true}'
    assert body["output"][0]["content"][0]["type"] == "output_text"
    call = client.calls[0]
    assert call["generation_config_overrides"]["response_mime_type"] == "application/json"
    assert call["generation_config_overrides"]["response_schema"] == [6, None, None, None, None, None, [["ok", [4]]]]
    assert call["sanitize_plain_text"] is False


def test_openai_models_refresh_registers_discovered_models():
    client = ModelListClient(["models/gemini-dynamic-preview"])
    old_client = runtime_state.client
    runtime_state.client = client
    clear_dynamic_model_capabilities()
    try:
        response = request_with_client(FakeTextClient(), "GET", "/v1/models?refresh=true")
        capabilities = get_model_capabilities("gemini-dynamic-preview", strict=True)
    finally:
        runtime_state.client = old_client
        clear_dynamic_model_capabilities()

    assert response.status_code == 200
    assert client.list_calls == 1
    assert capabilities.text_output is True
    assert any(model["id"] == "gemini-dynamic-preview" for model in response.json()["data"])


def test_openai_responses_accepts_output_text_history_blocks():
    client = FakeTextClient(text="fresh answer")

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": [
                {"role": "user", "content": "nihao"},
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "你好！有什么我可以帮你的吗？"}],
                },
                {"role": "user", "content": "看下今日头条新闻"},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["output_text"] == "fresh answer"
    contents = client.calls[0]["contents"]
    assert contents[1].role == "model"
    assert contents[1].parts[0].text == "你好！有什么我可以帮你的吗？"


def test_openai_responses_maps_function_calls_to_output_items():
    client = FakeTextClient(text="", function_calls=[{"name": "lookup", "args": {"query": "weather"}}])

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={"model": "gemini-3-flash-preview", "input": "call a tool"},
    )

    assert response.status_code == 200
    function_call = response.json()["output"][0]
    assert function_call["type"] == "function_call"
    assert function_call["name"] == "lookup"
    assert json.loads(function_call["arguments"]) == {"query": "weather"}


def test_openai_responses_accepts_function_call_history_and_output():
    client = FakeTextClient(text="follow-up answer")

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": [
                {"role": "user", "content": "search Google releases"},
                {
                    "id": "fc_test",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_test",
                    "name": "search_web",
                    "arguments": '{"query":"Google latest AI model release May 2026"}',
                },
                {
                    "id": "rs_test",
                    "type": "reasoning",
                    "status": "completed",
                    "content": [{"type": "reasoning_text", "text": "planning"}],
                    "summary": [],
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_test",
                    "output": '{"items":[{"title":"Google I/O 2026"}]}',
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "search_web",
                    "description": "Search the web",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["output_text"] == "follow-up answer"
    contents = client.calls[0]["contents"]
    assert contents[1].role == "model"
    assert contents[1].parts[0].text == 'Tool call requested: search_web {"query": "Google latest AI model release May 2026"}'
    assert contents[2].role == "user"
    assert contents[2].parts[0].text == 'Tool result for search_web: {"items":[{"title":"Google I/O 2026"}]}'


def test_openai_responses_forwards_thinking_control():
    client = FakeTextClient(text="ok")

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={"model": "gemini-3-flash-preview", "input": "hello", "thinking": "off"},
    )

    assert response.status_code == 200
    assert client.calls[0]["enable_thinking"] is False


def test_openai_responses_forwards_enabled_thinking_level():
    client = FakeTextClient(text="ok")

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={"model": "gemini-3-flash-preview", "input": "hello", "thinking": "high"},
    )

    assert response.status_code == 200
    call = client.calls[0]
    assert call["enable_thinking"] is True
    assert call["generation_config_overrides"]["thinking_config"] == [1, None, None, 3]
    assert call["generation_config_overrides"]["request_flag"] == 1


def test_openai_responses_returns_thinking_output():
    client = FakeTextClient(text="answer", thinking="private reasoning")

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={"model": "gemini-3-flash-preview", "input": "hello", "thinking": "high"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["thinking"] == "private reasoning"
    reasoning = body["output"][0]
    assert reasoning["type"] == "reasoning"
    assert reasoning["content"][0] == {"type": "reasoning_text", "text": "private reasoning"}
    assert body["output"][1]["content"][0]["text"] == "answer"


def test_openai_responses_accepts_flat_function_tool_input():
    client = FakeTextClient(text="ok")

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": "use a tool if needed",
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "look things up",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ],
        },
    )

    assert response.status_code == 200
    assert client.calls[0]["tools"] is not None


@pytest.mark.parametrize("tool_type", ["web_search", "web_search_preview", "browser_search"])
def test_chat_completions_accepts_search_tool_shapes(tool_type):
    client = FakeTextClient(text="grounded")

    response = request_with_client(
        client,
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3-flash-preview",
            "messages": [{"role": "user", "content": "search the web"}],
            "tools": [{"type": tool_type}],
        },
    )

    assert response.status_code == 200
    assert client.calls[0]["tools"] == [[None, None, None, [None, [[]]]]]


def test_chat_completions_keeps_output_budget_when_thinking_is_enabled():
    client = FakeTextClient(text="ok")

    response = request_with_client(
        client,
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3-flash-preview",
            "messages": [{"role": "user", "content": "reply briefly"}],
            "max_tokens": 64,
        },
    )

    assert response.status_code == 200
    assert client.calls[0]["max_tokens"] == 1024


def test_chat_completions_preserves_small_max_tokens_when_thinking_is_off():
    client = FakeTextClient(text="ok")

    response = request_with_client(
        client,
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3-flash-preview",
            "messages": [{"role": "user", "content": "reply briefly"}],
            "max_tokens": 64,
            "thinking": "off",
        },
    )

    assert response.status_code == 200
    assert client.calls[0]["max_tokens"] == 64


def test_openai_responses_accepts_web_search_tool_and_outputs_search_call():
    client = FakeTextClient(text="grounded")

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={"model": "gemini-3-flash-preview", "input": "search", "tools": [{"type": "web_search"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["output"][0]["type"] == "web_search_call"
    assert body["output"][1]["content"][0]["text"] == "grounded"
    assert client.calls[0]["tools"] == [[None, None, None, [None, [[]]]]]


def test_openai_responses_accepts_image_generation_tool():
    client = FakeTextAndImageClient(text="", function_calls=[{"name": "image_generation", "args": {"prompt": "draw a small test square"}}])

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": "draw a small test square",
            "tools": [{"type": "image_generation", "model": "gpt-image-2", "size": "1536x864"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    image_item = body["output"][0]
    assert image_item["type"] == "image_generation_call"
    assert base64.b64decode(image_item["result"]) == b"image-bytes"
    image_call = client.calls[1]["image"]
    assert image_call["model"] == "gemini-3.1-flash-image-preview"
    assert image_call["prompt"] == "draw a small test square\n\nUse a horizontal 16:9 composition."


def test_openai_responses_image_generation_tool_does_not_force_plain_chat():
    client = FakeTextAndImageClient(text="plain answer")

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": "say hello, do not draw anything",
            "tools": [{"type": "image_generation", "model": "gpt-image-2", "size": "1536x864"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["output_text"] == "plain answer"
    assert body["output"][0]["type"] == "message"
    assert len(client.calls) == 1
    assert "image" not in client.calls[0]


def test_openai_responses_uses_selected_gemini_image_model():
    client = FakeTextAndImageClient(text="", function_calls=[{"name": "image_generation", "args": {"prompt": "draw a detailed square"}}])

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": "draw a detailed square",
            "tools": [{"type": "image_generation", "provider": "google-ai-studio", "model": "gemini-3-pro-image-preview", "size": "2048x2048"}],
        },
    )

    assert response.status_code == 200
    image_call = client.calls[1]["image"]
    assert image_call["model"] == "gemini-3-pro-image-preview"
    assert image_call["generation_config_overrides"] == {"output_image_size": [None, "2K"]}
    assert response.json()["thinking"] == "Image generation tool selected gemini-3-pro-image-preview at 2048x2048."


def test_openai_responses_maps_image_tool_sizes_to_google_provider_sizes():
    cases = [
        ("1024x1536", "1024x1792", "Use a vertical 9:16 composition."),
        ("1536x1024", "1792x1024", "Use a horizontal 16:9 composition."),
        ("1536x864", "1792x1024", "Use a horizontal 16:9 composition."),
    ]
    for requested_size, expected_size, expected_suffix in cases:
        assert _image_size_for_google_provider(requested_size) == expected_size
        client = FakeTextAndImageClient(text="", function_calls=[{"name": "image_generation", "args": {"prompt": "draw"}}])

        response = request_with_client(
            client,
            "POST",
            "/v1/responses",
            json={
                "model": "gemini-3-flash-preview",
                "input": "draw",
                "tools": [{"type": "image_generation", "model": "gpt-image-2", "size": requested_size}],
            },
        )

        assert response.status_code == 200
        image_call = client.calls[1]["image"]
        assert image_call["generation_config_overrides"] == {"output_image_size": [None, "1K"]}
        assert image_call["prompt"] == f"draw\n\n{expected_suffix}"


def test_openai_responses_combines_web_search_and_image_generation_tool():
    client = FakeTextAndImageClient(text="", function_calls=[{"name": "image_generation", "args": {"prompt": "searched image prompt"}}])

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": "search then draw",
            "tools": [{"type": "web_search_preview"}, {"type": "image_generation", "model": "gpt-image-2"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["type"] for item in body["output"]] == ["web_search_call", "image_generation_call"]
    assert client.calls[0]["tools"]
    assert client.calls[1]["image"]["prompt"] == "searched image prompt\n\nUse a square 1:1 composition."
    assert {key: body["usage"][key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")} == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }


def test_openai_responses_streams_image_generation_tool_events():
    client = FakeTextAndImageClient(text="", function_calls=[{"name": "image_generation", "args": {"prompt": "draw"}}])

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={"model": "gemini-3-flash-preview", "input": "draw", "stream": True, "tools": [{"type": "image_generation"}]},
    )

    assert response.status_code == 200
    events = response_stream_events(response.text)
    assert events[0][0] == "response.created"
    assert any(event_name == "response.reasoning.delta" for event_name, _ in events)
    assert any(event_name == "response.image_generation_call.partial_image" for event_name, _ in events)
    done_items = [data["item"] for event_name, data in events if event_name == "response.output_item.done"]
    image_done_items = [item for item in done_items if item.get("type") == "image_generation_call"]
    assert image_done_items and "result" not in image_done_items[0]
    completed = [data for event_name, data in events if event_name == "response.completed"][0]
    image_output_items = [item for item in completed["response"]["output"] if item.get("type") == "image_generation_call"]
    assert image_output_items and "result" not in image_output_items[0]
    assert "Image generation tool selected" in completed["response"]["thinking"]


def test_openai_responses_streaming_emits_responses_events():
    client = FakeStreamClient(events=[("body", "hel"), ("body", "lo"), ("usage", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})])

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={"model": "gemini-3-flash-preview", "input": "hello", "stream": True},
    )

    assert response.status_code == 200
    assert "event: response.created" in response.text
    assert "event: response.output_text.delta" in response.text
    assert '"delta": "hel"' in response.text
    assert '"delta": "lo"' in response.text
    assert "event: response.completed" in response.text


def test_openai_responses_streaming_emits_reasoning_events():
    client = FakeStreamClient(events=[("thinking", "plan"), ("body", "answer"), ("usage", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})])

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={"model": "gemini-3-flash-preview", "input": "hello", "thinking": "high", "stream": True},
    )

    assert response.status_code == 200
    assert "event: response.reasoning.delta" in response.text
    assert '"delta": "plan"' in response.text
    assert "event: response.reasoning.done" in response.text
    assert '"thinking": "plan"' in response.text


def test_openai_responses_restores_text_tool_request_to_function_call_item():
    client = FakeTextClient(text='Tool call requested: Shell {"command":"python debug_list_page.py"}')

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": "run it",
            "tools": [
                {
                    "type": "function",
                    "name": "Shell",
                    "description": "Run a shell command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["output_text"] == ""
    function_call = body["output"][0]
    assert function_call["type"] == "function_call"
    assert function_call["name"] == "Shell"
    assert json.loads(function_call["arguments"]) == {"command": "python debug_list_page.py"}


def test_openai_responses_streaming_restores_text_tool_request_to_function_call_events():
    client = FakeStreamClient(events=[("body", 'Tool call requested: Shell {"command":"python debug_list_page.py"}')])

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": "run it",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "Shell",
                    "description": "Run a shell command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                }
            ],
        },
    )

    assert response.status_code == 200
    assert "event: response.output_text.delta" not in response.text
    assert "event: response.function_call_arguments.delta" in response.text
    assert "event: response.function_call_arguments.done" in response.text
    assert '"type": "function_call"' in response.text
    assert '"name": "Shell"' in response.text
    assert '"output_text": ""' in response.text
    arguments = reconstructed_function_call_arguments(response_stream_events(response.text))
    assert json.loads(arguments) == {"command": "python debug_list_page.py"}


def test_openai_responses_streaming_tool_call_added_item_does_not_duplicate_arguments():
    client = FakeStreamClient(events=[("tool_calls", [{"name": "Shell", "args": {"command": "dir"}}])])

    response = request_with_client(
        client,
        "POST",
        "/v1/responses",
        json={
            "model": "gemini-3-flash-preview",
            "input": "run it",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "Shell",
                    "description": "Run a shell command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                }
            ],
        },
    )

    assert response.status_code == 200
    arguments = reconstructed_function_call_arguments(response_stream_events(response.text))
    assert json.loads(arguments) == {"command": "dir"}


def test_messages_accepts_anthropic_tools_and_returns_tool_use_blocks():
    client = FakeTextClient(text="", function_calls=[{"name": "lookup", "args": {"query": "weather"}}])

    response = request_with_client(
        client,
        "POST",
        "/v1/messages",
        json={
            "model": "gemini-3-flash-preview",
            "system": "be terse",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "use the tool"}]}],
            "tools": [
                {
                    "name": "lookup",
                    "description": "look things up",
                    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["stop_reason"] == "tool_use"
    assert body["content"][0] == {"type": "tool_use", "id": body["content"][0]["id"], "name": "lookup", "input": {"query": "weather"}}
    assert client.calls[0]["tools"] is not None


def test_messages_accepts_anthropic_web_search_tool():
    client = FakeTextClient(text="grounded")

    response = request_with_client(
        client,
        "POST",
        "/v1/messages",
        json={
            "model": "gemini-3-flash-preview",
            "messages": [{"role": "user", "content": "search"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        },
    )

    assert response.status_code == 200
    assert client.calls[0]["tools"] == [[None, None, None, [None, [[]]]]]


def test_messages_streaming_emits_anthropic_text_events():
    client = FakeStreamClient(events=[("body", "hello"), ("usage", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})])

    response = request_with_client(
        client,
        "POST",
        "/v1/messages",
        json={"model": "gemini-3-flash-preview", "stream": True, "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert "event: message_start" in response.text
    assert "event: content_block_delta" in response.text
    assert '"text": "hello"' in response.text
    assert "event: message_stop" in response.text


def test_messages_streaming_emits_tool_use_events():
    client = FakeStreamClient()

    response = request_with_client(
        client,
        "POST",
        "/v1/messages",
        json={
            "model": "gemini-3-flash-preview",
            "stream": True,
            "messages": [{"role": "user", "content": "use tool"}],
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )

    assert response.status_code == 200
    assert '"type": "tool_use"' in response.text
    assert '"type": "input_json_delta"' in response.text
    assert "event: message_stop" in response.text


def test_messages_count_tokens_returns_anthropic_shape():
    response = request_with_client(
        FakeTextClient(),
        "POST",
        "/v1/messages/count_tokens",
        json={
            "model": "gemini-3-flash-preview",
            "system": "be terse",
            "messages": [{"role": "user", "content": "hello world"}],
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )

    assert response.status_code == 200
    assert response.json()["input_tokens"] >= 3


def test_chat_streaming_tool_calls_include_openai_delta_index_and_string_arguments():
    client = FakeStreamClient()

    response = request_with_client(
        client,
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3-flash-preview",
            "stream": True,
            "messages": [{"role": "user", "content": "call a tool"}],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        },
    )

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    tool_event = next(event for event in events if event.get("choices") and event["choices"][0]["delta"].get("tool_calls"))
    tool_call = tool_event["choices"][0]["delta"]["tool_calls"][0]
    assert tool_call["index"] == 0
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "lookup"
    assert json.loads(tool_call["function"]["arguments"]) == {"query": "weather"}
    assert any(event.get("choices") and event["choices"][0]["finish_reason"] == "tool_calls" for event in events)


def test_openai_routes_return_standard_error_shape():
    response = request_with_client(FakeTextClient(), "POST", "/v1/responses", json={"input": "hello"})

    assert response.status_code == 400
    body = response.json()
    assert "detail" not in body
    assert body["error"] == {
        "message": "model is required",
        "type": "invalid_request_error",
        "param": None,
        "code": None,
    }