import pytest

from aistudio_api.api.schemas import GeminiContent, GeminiGenerateContentRequest, GeminiGenerationConfig, GeminiPart
import aistudio_api.application.chat_service as chat_service
from aistudio_api.application.chat_service import normalize_chat_request, normalize_gemini_request, normalize_openai_tools
from aistudio_api.api.schemas import ChatRequest


def test_normalize_gemini_request_exposes_generation_config_overrides():
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
        generationConfig=GeminiGenerationConfig(
            stopSequences=["6"],
            temperature=1,
            topP=0.95,
            topK=64,
            maxOutputTokens=65536,
            responseMimeType="text/plain",
            responseSchema={
                "type": "object",
                "properties": {"test_response": {"type": "string"}},
                "propertyOrdering": ["test_response"],
            },
            presencePenalty=0.1,
            frequencyPenalty=0.2,
            responseLogprobs=True,
            logprobs=5,
            mediaResolution=[2, 1],
            thinkingConfig=[1, None, None, 3],
        ),
    )

    normalized = normalize_gemini_request(req, "models/gemini-3-flash-preview")

    assert normalized["generation_config_overrides"] == {
        "stop_sequences": ["6"],
        "max_tokens": 65536,
        "temperature": 1,
        "top_p": 0.95,
        "top_k": 64,
        "response_mime_type": "text/plain",
        "response_schema": [6, None, None, None, None, None, [["test_response", [1]]], None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, ["test_response"]],
        "presence_penalty": 0.1,
        "frequency_penalty": 0.2,
        "response_logprobs": True,
        "logprobs": 5,
        "media_resolution": [2, 1],
        "thinking_config": [1, None, None, 3],
    }


def test_normalize_gemini_request_rejects_media_resolution_for_image_model():
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
        generationConfig=GeminiGenerationConfig(mediaResolution=[2, 1]),
    )

    with pytest.raises(ValueError, match="mediaResolution"):
        normalize_gemini_request(req, "models/gemini-3.1-flash-image-preview")


def test_normalize_gemini_request_rejects_invalid_numeric_generation_config():
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
        generationConfig=GeminiGenerationConfig(topP=1.5),
    )

    with pytest.raises(ValueError, match="topP"):
        normalize_gemini_request(req, "models/gemini-3-flash-preview")


def test_normalize_gemini_request_uses_structured_output_capability():
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
        generationConfig=GeminiGenerationConfig(responseMimeType="application/json"),
    )

    with pytest.raises(ValueError, match="structured output"):
        normalize_gemini_request(req, "models/gemini-3.1-flash-tts-preview")


def test_normalize_gemini_request_rejects_oversized_inline_image(monkeypatch):
    monkeypatch.setattr(chat_service, "MAX_INLINE_IMAGE_BYTES", 4)
    req = GeminiGenerateContentRequest(
        contents=[
            GeminiContent(
                role="user",
                parts=[GeminiPart(inlineData={"mimeType": "image/png", "data": "aGVsbG8="})],
            )
        ],
    )

    with pytest.raises(ValueError, match="too large"):
        normalize_gemini_request(req, "models/gemini-3-flash-preview")


def test_normalize_gemini_request_accepts_inline_pdf_file():
    req = GeminiGenerateContentRequest(
        contents=[
            GeminiContent(
                role="user",
                parts=[
                    GeminiPart(text="summarize"),
                    GeminiPart(inlineData={"mimeType": "application/pdf", "data": "aGVsbG8="}),
                ],
            )
        ],
    )

    normalized = normalize_gemini_request(req, "models/gemini-3-flash-preview")

    parts = normalized["contents"][0].parts
    assert parts[1].inline_data == ("application/pdf", "aGVsbG8=")
    assert normalized["capture_images"] is None
    assert normalized["file_input_mime_types"] == ["application/pdf"]


def test_normalize_gemini_request_rejects_inline_pdf_for_text_only_model():
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(inlineData={"mimeType": "application/pdf", "data": "aGVsbG8="})])],
    )

    with pytest.raises(ValueError, match="file input"):
        normalize_gemini_request(req, "models/gemma-4-31b-it")


def test_normalize_gemini_request_rejects_oversized_system_instruction_inline_image(monkeypatch):
    monkeypatch.setattr(chat_service, "MAX_INLINE_IMAGE_BYTES", 4)
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
        systemInstruction=GeminiContent(
            role="user",
            parts=[GeminiPart(inlineData={"mimeType": "image/png", "data": "aGVsbG8="})],
        ),
    )

    with pytest.raises(ValueError, match="systemInstruction.inlineData is too large"):
        normalize_gemini_request(req, "models/gemini-3-flash-preview")


def test_normalize_gemini_request_cleans_inline_image_file_after_late_validation_error(tmp_path):
    req = GeminiGenerateContentRequest(
        contents=[
            GeminiContent(
                role="user",
                parts=[GeminiPart(inlineData={"mimeType": "image/png", "data": "aGVsbG8="})],
            )
        ],
    )

    with pytest.raises(ValueError, match="image input"):
        normalize_gemini_request(req, "models/gemma-4-31b-it", tmp_dir=str(tmp_path))

    assert list(tmp_path.iterdir()) == []


def test_normalize_chat_request_cleans_image_file_after_late_validation_error(tmp_path):
    req = ChatRequest(
        model="gemini-3-flash-preview",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    {"type": "unsupported", "text": "later failure"},
                ],
            }
        ],
    )

    with pytest.raises(ValueError, match="unsupported message content type"):
        normalize_chat_request(req.messages, req.model, tmp_dir=str(tmp_path))

    assert list(tmp_path.iterdir()) == []


def test_normalize_chat_request_default_temp_dir_accepts_inline_image():
    req = ChatRequest(
        model="gemini-3-flash-preview",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                ],
            }
        ],
    )

    normalized = normalize_chat_request(req.messages, req.model)

    assert len(normalized["capture_images"]) == 1
    chat_service.cleanup_files(normalized["cleanup_paths"])


def test_normalize_chat_request_uses_configured_temp_dir_by_default(tmp_path, monkeypatch):
    configured_tmp = tmp_path / "configured-temp"
    monkeypatch.setattr(chat_service.settings, "tmp_dir", str(configured_tmp))
    req = ChatRequest(
        model="gemini-3-flash-preview",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                ],
            }
        ],
    )

    normalized = normalize_chat_request(req.messages, req.model)

    assert configured_tmp.is_dir()
    assert len(normalized["capture_images"]) == 1
    assert normalized["capture_images"][0].startswith(str(configured_tmp))
    chat_service.cleanup_files(normalized["cleanup_paths"])


def test_normalize_gemini_request_rejects_streaming_for_image_model():
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
    )

    with pytest.raises(ValueError, match="streaming"):
        normalize_gemini_request(req, "models/gemini-3.1-flash-image-preview", stream=True)


def test_normalize_gemini_request_encodes_function_declarations_to_wire_tools():
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
        tools=[
            {
                "functionDeclarations": [
                    {
                        "name": "getWeather",
                        "description": "gets the weather for a requested city",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "propertyOrdering": ["city"],
                        },
                    }
                ]
            }
        ],
    )

    normalized = normalize_gemini_request(req, "models/gemma-4-31b-it")

    assert normalized["tools"] == [
        [
            None,
            [
                [
                    "getWeather",
                    "gets the weather for a requested city",
                    [6, None, None, None, None, None, [["city", [1]]], None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, ["city"]],
                ]
            ],
        ]
    ]


def test_normalize_openai_tools_encodes_function_tools_to_wire():
    req = ChatRequest(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "getWeather",
                    "description": "gets the weather for a requested city",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "propertyOrdering": ["city"],
                    },
                },
            }
        ],
    )

    assert normalize_openai_tools(req.tools) == [
        [
            None,
            [
                [
                    "getWeather",
                    "gets the weather for a requested city",
                    [6, None, None, None, None, None, [["city", [1]]], None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, ["city"]],
                ]
            ],
        ]
    ]


def test_normalize_openai_tools_encodes_required_to_schema_index_7():
    req = ChatRequest(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "browser_click",
                    "description": "Click by ref",
                    "parameters": {
                        "type": "object",
                        "properties": {"ref": {"type": "string"}},
                        "required": ["ref"],
                    },
                },
            }
        ],
    )

    schema = normalize_openai_tools(req.tools)[0][1][0][2]
    assert schema[7] == ["ref"]
