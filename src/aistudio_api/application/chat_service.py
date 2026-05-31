"""Application services for chat/image orchestration."""

from __future__ import annotations

import base64
import binascii
import os
import re
import uuid
from typing import Optional

import httpx

from aistudio_api.config import DEFAULT_IMAGE_MODEL, settings
from aistudio_api.application.validation import validate_number_range
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.model_capabilities import mime_type_supported, require_model_capabilities
from aistudio_api.infrastructure.gateway.request_rewriter import TOOLS_TEMPLATES
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


SCHEMA_TYPE_CODES = {
    "string": 1,
    "number": 2,
    "integer": 3,
    "boolean": 4,
    "array": 5,
    "object": 6,
}


MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024
OPENAI_CHAT_ROLES = {"system", "developer", "user", "assistant", "tool"}
GEMINI_CONTENT_ROLES = {"user", "model"}
SEARCH_TOOL_TYPES = {
    "browser_search",
    "google_search",
    "search",
    "web_search",
    "web_search_preview",
}


GENERATION_CONFIG_FIELD_NAMES = {
    "stop_sequences": "stopSequences",
    "max_tokens": "maxOutputTokens",
    "temperature": "temperature",
    "top_p": "topP",
    "top_k": "topK",
    "response_mime_type": "responseMimeType",
    "response_schema": "responseSchema",
    "presence_penalty": "presencePenalty",
    "frequency_penalty": "frequencyPenalty",
    "response_logprobs": "responseLogprobs",
    "logprobs": "logprobs",
    "media_resolution": "mediaResolution",
    "thinking_config": "thinkingConfig",
}


def _decode_base64_data(data: str, label: str) -> bytes:
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} must contain valid base64 data") from exc
    if len(decoded) > MAX_INLINE_IMAGE_BYTES:
        raise ValueError(f"{label} is too large; maximum size is {MAX_INLINE_IMAGE_BYTES // (1024 * 1024)} MB")
    return decoded


def _decode_image_base64(data: str, label: str) -> bytes:
    return _decode_base64_data(data, label)


def _is_image_mime(mime_type: str) -> bool:
    return (mime_type or "").lower().startswith("image/")


def _parse_data_uri(uri: str, label: str) -> tuple[str, str]:
    match = re.match(r"data:([^,;]+)(?:;[^,]*)*;base64,(.+)", uri, re.DOTALL)
    if not match:
        raise ValueError(f"{label} must be a base64 data URI")
    mime, b64 = match.group(1).strip(), match.group(2)
    _decode_base64_data(b64, label)
    return mime or "application/octet-stream", b64


def data_uri_to_inline_data(uri: str, label: str = "file data URI") -> tuple[str, str]:
    return _parse_data_uri(uri, label)


def _temporary_directory(tmp_dir: str | None = None) -> str:
    path = tmp_dir or settings.tmp_dir
    os.makedirs(path, exist_ok=True)
    return path


def data_uri_to_file(uri: str, tmp_dir: str | None = None) -> str:
    mime, b64 = _parse_data_uri(uri, "image data URI")
    if not _is_image_mime(mime):
        raise ValueError("image data URI must contain an image MIME type")
    ext = mime.split("/")[-1].replace("jpeg", "jpg")
    tmp_dir = _temporary_directory(tmp_dir)
    path = os.path.join(tmp_dir, f"aistudio_img_{uuid.uuid4().hex[:8]}.{ext}")
    decoded = _decode_base64_data(b64, "image data URI")
    with open(path, "wb") as file:
        file.write(decoded)
    return path


def url_to_file(url: str, tmp_dir: str | None = None) -> str:
    tmp_dir = _temporary_directory(tmp_dir)
    path = os.path.join(tmp_dir, f"aistudio_img_{uuid.uuid4().hex[:8]}.jpg")
    with httpx.Client(timeout=30) as http:
        resp = http.get(url)
        resp.raise_for_status()
        if len(resp.content) > MAX_INLINE_IMAGE_BYTES:
            raise ValueError(f"image URL is too large; maximum size is {MAX_INLINE_IMAGE_BYTES // (1024 * 1024)} MB")
        with open(path, "wb") as file:
            file.write(resp.content)
    return path


def normalize_chat_request(messages, requested_model: str, tmp_dir: str | None = None) -> dict:
    if not messages:
        raise ValueError("messages must contain at least one message")

    system_texts: list[str] = []
    contents: list[AistudioContent] = []
    capture_texts: list[str] = []
    capture_images: list[str] = []
    file_input_mime_types: list[str] = []
    cleanup_paths: list[str] = []
    saw_images = False
    success = False

    try:
        for msg in messages:
            role = (msg.role or "").strip().lower()
            if role not in OPENAI_CHAT_ROLES:
                raise ValueError(f"unsupported message role: {msg.role}")
            if role in ("system", "developer"):
                text = _message_text_content(msg.content)
                if text:
                    system_texts.append(text)
                    capture_texts.append(text)
                continue

            parts: list[AistudioPart] = []
            text_parts: list[str] = []
            image_paths: list[str] = []
            inline_files: list[tuple[str, str]] = []

            if isinstance(msg.content, str):
                if msg.content:
                    parts.append(AistudioPart(text=msg.content))
                    text_parts.append(msg.content)
            elif isinstance(msg.content, list):
                for part in msg.content:
                    if part.type == "text" and part.text:
                        parts.append(AistudioPart(text=part.text))
                        text_parts.append(part.text)
                    elif part.type == "image_url":
                        if not part.image_url:
                            raise ValueError("image_url.url is required")
                        url = part.image_url.get("url") if isinstance(part.image_url, dict) else part.image_url.url
                        if not isinstance(url, str) or not url:
                            raise ValueError("image_url.url is required")
                        if url.startswith("data:"):
                            mime, _b64 = data_uri_to_inline_data(url, "image_url.url")
                            if not _is_image_mime(mime):
                                raise ValueError("image_url.url data URI must contain an image MIME type")
                            path = data_uri_to_file(url, tmp_dir=tmp_dir)
                            image_paths.append(path)
                            cleanup_paths.append(path)
                        elif url.startswith("http"):
                            path = url_to_file(url, tmp_dir=tmp_dir)
                            image_paths.append(path)
                            cleanup_paths.append(path)
                        else:
                            raise ValueError("image_url.url must be a data URI or HTTP URL")
                    elif part.type in ("file", "input_file"):
                        mime, b64 = _message_file_inline_data(part)
                        inline_files.append((mime, b64))
                        file_input_mime_types.append(mime)
                    elif part.type not in ("text", "image_url", "file", "input_file"):
                        raise ValueError(f"unsupported message content type: {part.type}")

            for image_path in image_paths:
                parts.append(_file_path_to_part(image_path))
            for inline_file in inline_files:
                parts.append(AistudioPart(inline_data=inline_file))

            if not parts:
                continue

            mapped_role = "model" if role == "assistant" else "user"
            contents.append(AistudioContent(role=mapped_role, parts=parts))
            capture_texts.extend(text_parts)
            if image_paths:
                saw_images = True
                capture_images.extend(image_paths)

        if not contents:
            raise ValueError("messages must contain at least one non-empty content part")

        capture_prompt = "\n".join(capture_texts) if capture_texts else "你好"
        model = requested_model
        if model.startswith("gpt-") or model.startswith("openai/"):
            model = DEFAULT_IMAGE_MODEL if saw_images else requested_model

        success = True
        return {
            "model": model,
            "system_instruction": "\n".join(system_texts) if system_texts else None,
            "contents": contents or [AistudioContent(role="user", parts=[AistudioPart(text="你好")])],
            "capture_prompt": capture_prompt,
            "capture_images": capture_images,
            "file_input_mime_types": file_input_mime_types,
            "cleanup_paths": cleanup_paths,
        }
    finally:
        if not success:
            cleanup_files(cleanup_paths)


def _message_text_content(content) -> str | None:
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        texts = [part.text for part in content if part.type == "text" and part.text]
        return "\n".join(texts) if texts else None
    return None


def _message_file_inline_data(part) -> tuple[str, str]:
    payload = part.file if isinstance(part.file, dict) else {}
    data = payload.get("file_data") or payload.get("data") or part.file_data
    if not isinstance(data, str) or not data:
        raise ValueError("file content blocks require file_data")
    if data.startswith("data:"):
        return data_uri_to_inline_data(data, "file.file_data")
    mime_type = payload.get("mime_type") or part.mime_type or "application/octet-stream"
    _decode_base64_data(data, "file.file_data")
    return mime_type, data


def _file_path_to_part(path: str) -> AistudioPart:
    mime = "image/jpeg"
    if path.endswith(".png"):
        mime = "image/png"
    elif path.endswith(".webp"):
        mime = "image/webp"
    with open(path, "rb") as file:
        return AistudioPart(inline_data=(mime, base64.b64encode(file.read()).decode("ascii")))


def cleanup_files(paths: list[str]):
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def inline_data_to_file(mime_type: str, data: str, tmp_dir: str | None = None) -> str:
    ext = mime_type.split("/")[-1].replace("jpeg", "jpg")
    tmp_dir = _temporary_directory(tmp_dir)
    path = os.path.join(tmp_dir, f"aistudio_img_{uuid.uuid4().hex[:8]}.{ext}")
    decoded = _decode_base64_data(data, "inlineData")
    with open(path, "wb") as file:
        file.write(decoded)
    return path


def _validate_gemini_generation_config(generation_config) -> None:
    if generation_config is None:
        return
    validate_number_range("generationConfig.temperature", generation_config.temperature, minimum=0, maximum=2)
    validate_number_range("generationConfig.topP", generation_config.topP, minimum=0, maximum=1)
    validate_number_range("generationConfig.topK", generation_config.topK, minimum=1, integer=True)
    validate_number_range("generationConfig.maxOutputTokens", generation_config.maxOutputTokens, minimum=1, integer=True)
    validate_number_range("generationConfig.presencePenalty", generation_config.presencePenalty, minimum=-2, maximum=2)
    validate_number_range("generationConfig.frequencyPenalty", generation_config.frequencyPenalty, minimum=-2, maximum=2)
    validate_number_range("generationConfig.logprobs", generation_config.logprobs, minimum=0, integer=True)


def encode_schema_to_wire(schema: dict) -> list:
    schema_type = schema.get("type")
    type_code = SCHEMA_TYPE_CODES.get(schema_type, 0)
    wire = [type_code]

    if schema_type == "array" and isinstance(schema.get("items"), dict):
        while len(wire) <= 5:
            wire.append(None)
        wire[5] = encode_schema_to_wire(schema["items"])

    properties = schema.get("properties")
    if isinstance(properties, dict):
        while len(wire) <= 6:
            wire.append(None)
        wire[6] = [[name, encode_schema_to_wire(prop)] for name, prop in properties.items() if isinstance(prop, dict)]

    required = schema.get("required")
    if isinstance(required, list):
        while len(wire) <= 7:
            wire.append(None)
        wire[7] = list(required)

    property_ordering = schema.get("propertyOrdering")
    if isinstance(property_ordering, list):
        while len(wire) <= 22:
            wire.append(None)
        wire[22] = list(property_ordering)

    return wire


def encode_function_declaration_to_wire(declaration: dict) -> list:
    if not declaration.get("name"):
        raise ValueError("functionDeclarations[].name is required")

    wire = [declaration["name"]]
    if declaration.get("description") is not None:
        while len(wire) <= 1:
            wire.append(None)
        wire[1] = declaration["description"]

    parameters = declaration.get("parameters")
    if isinstance(parameters, dict):
        while len(wire) <= 2:
            wire.append(None)
        wire[2] = encode_schema_to_wire(parameters)

    return wire


def is_search_tool_type(tool_type: str | None) -> bool:
    normalized = (tool_type or "").strip().lower()
    return normalized in SEARCH_TOOL_TYPES or normalized.startswith("web_search_preview_") or normalized.startswith("web_search_")


def normalize_openai_tools(tools) -> list[list] | None:
    normalized, _uses_search = normalize_openai_tools_and_search(tools)
    return normalized


def normalize_openai_tools_and_search(tools) -> tuple[list[list] | None, bool]:
    if not tools:
        return None, False

    function_declarations: list[dict] = []
    uses_search = False
    for tool in tools:
        tool_type = (tool.type or "").strip().lower()
        if is_search_tool_type(tool_type):
            uses_search = True
            continue
        if tool_type != "function":
            raise ValueError(f"unsupported tool type: {tool.type}")
        if tool.function is None:
            raise ValueError("tools[].function is required when type=function")

        function_declarations.append(
            {
                "name": tool.function.name,
                "description": tool.function.description,
                "parameters": tool.function.parameters,
            }
        )

    if not function_declarations:
        return None, uses_search

    return [[None, [encode_function_declaration_to_wire(decl) for decl in function_declarations]]], uses_search


def normalize_gemini_request(req, requested_model: str, tmp_dir: str | None = None, *, stream: bool = False) -> dict:
    if req.cachedContent:
        raise ValueError("cachedContent is not supported by AI Studio browser replay mode yet")
    if req.safetySettings:
        raise ValueError("safetySettings are not supported by AI Studio browser replay mode yet")
    if not req.contents:
        raise ValueError("contents is required")

    model = requested_model if requested_model.startswith("models/") else f"models/{requested_model}"
    capabilities = require_model_capabilities(model)
    contents: list[AistudioContent] = []
    cleanup_paths: list[str] = []
    capture_prompt = "你好"
    capture_images: list[str] = []
    file_input_mime_types: list[str] = []
    success = False

    try:
        for content in req.contents:
            role = (content.role or "user").strip().lower()
            if role not in GEMINI_CONTENT_ROLES:
                raise ValueError(f"unsupported content role: {content.role}")
            if not content.parts:
                raise ValueError("contents[].parts must contain at least one part")
            parts: list[AistudioPart] = []
            text_parts: list[str] = []
            content_images: list[str] = []

            for part in content.parts:
                if part.text is not None:
                    parts.append(AistudioPart(text=part.text))
                    text_parts.append(part.text)
                    continue
                if part.inlineData is not None:
                    _decode_base64_data(part.inlineData.data, "inlineData")
                    parts.append(AistudioPart(inline_data=(part.inlineData.mimeType, part.inlineData.data)))
                    if _is_image_mime(part.inlineData.mimeType):
                        image_path = inline_data_to_file(part.inlineData.mimeType, part.inlineData.data, tmp_dir=tmp_dir)
                        content_images.append(image_path)
                        cleanup_paths.append(image_path)
                    else:
                        file_input_mime_types.append(part.inlineData.mimeType)
                    continue
                if part.fileData is not None:
                    raise ValueError("fileData is not supported by AI Studio browser replay mode yet; use inlineData")

            if not parts:
                raise ValueError("contents[].parts must contain text or inlineData")

            contents.append(AistudioContent(role=role, parts=parts))

            if role == "user":
                if text_parts:
                    capture_prompt = "\n".join(text_parts)
                if content_images:
                    capture_images = content_images

        system_instruction = None
        if req.systemInstruction is not None:
            system_role = (req.systemInstruction.role or "user").strip().lower()
            if system_role not in GEMINI_CONTENT_ROLES:
                raise ValueError(f"unsupported systemInstruction role: {req.systemInstruction.role}")
            if not req.systemInstruction.parts:
                raise ValueError("systemInstruction.parts must contain at least one part")
            system_parts: list[AistudioPart] = []
            for part in req.systemInstruction.parts:
                if part.text is not None:
                    system_parts.append(AistudioPart(text=part.text))
                    continue
                if part.inlineData is not None:
                    _decode_base64_data(part.inlineData.data, "systemInstruction.inlineData")
                    system_parts.append(AistudioPart(inline_data=(part.inlineData.mimeType, part.inlineData.data)))
                    if not _is_image_mime(part.inlineData.mimeType):
                        file_input_mime_types.append(part.inlineData.mimeType)
                    continue
                if part.fileData is not None:
                    raise ValueError("systemInstruction.fileData is not supported by AI Studio browser replay mode yet; use inlineData")
            if not system_parts:
                raise ValueError("systemInstruction.parts must contain text or inlineData")
            system_instruction = AistudioContent(
                role=system_role,
                parts=system_parts,
            )

        tools = None
        uses_function_tools = False
        uses_search = False
        if req.tools:
            tools = []
            for tool in req.tools:
                if tool.codeExecution is not None:
                    uses_function_tools = True
                    tools.append(TOOLS_TEMPLATES["code_execution"])
                if tool.functionDeclarations:
                    uses_function_tools = True
                    tools.append([None, [encode_function_declaration_to_wire(decl) for decl in tool.functionDeclarations]])
                if tool.googleSearch is not None or tool.googleSearchRetrieval is not None:
                    uses_search = True
                    tools.append(TOOLS_TEMPLATES["google_search"])

        # Gemma 4 小模型默认开启 Google Search
        if tools is None and any(m in model for m in ("gemma-4-26b-a4b-it", "gemma-4-31b-it")):
            uses_search = True
            tools = [TOOLS_TEMPLATES["google_search"]]

        generation_config = req.generationConfig
        _validate_gemini_generation_config(generation_config)
        generation_config_overrides = None
        if generation_config is not None:
            generation_config_overrides = {}
            if generation_config.stopSequences is not None:
                generation_config_overrides["stop_sequences"] = generation_config.stopSequences
            if generation_config.maxOutputTokens is not None:
                generation_config_overrides["max_tokens"] = generation_config.maxOutputTokens
            if generation_config.temperature is not None:
                generation_config_overrides["temperature"] = generation_config.temperature
            if generation_config.topP is not None:
                generation_config_overrides["top_p"] = generation_config.topP
            if generation_config.topK is not None:
                generation_config_overrides["top_k"] = generation_config.topK
            if generation_config.responseMimeType is not None:
                generation_config_overrides["response_mime_type"] = generation_config.responseMimeType
            if generation_config.responseSchema is not None:
                generation_config_overrides["response_schema"] = (
                    encode_schema_to_wire(generation_config.responseSchema)
                    if isinstance(generation_config.responseSchema, dict)
                    else generation_config.responseSchema
                )
            if generation_config.presencePenalty is not None:
                generation_config_overrides["presence_penalty"] = generation_config.presencePenalty
            if generation_config.frequencyPenalty is not None:
                generation_config_overrides["frequency_penalty"] = generation_config.frequencyPenalty
            if generation_config.responseLogprobs is not None:
                generation_config_overrides["response_logprobs"] = generation_config.responseLogprobs
            if generation_config.logprobs is not None:
                generation_config_overrides["logprobs"] = generation_config.logprobs
            if generation_config.mediaResolution is not None:
                generation_config_overrides["media_resolution"] = generation_config.mediaResolution
            if generation_config.thinkingConfig is not None:
                generation_config_overrides["thinking_config"] = generation_config.thinkingConfig

        if capture_images and not capabilities.image_input:
            raise ValueError(f"Model '{model}' does not support image input")
        if file_input_mime_types and not capabilities.file_input:
            raise ValueError(f"Model '{model}' does not support file input")
        unsupported_file_types = [
            mime_type
            for mime_type in file_input_mime_types
            if not mime_type_supported(mime_type, capabilities.file_input_mime_types)
        ]
        if unsupported_file_types:
            supported = ", ".join(capabilities.file_input_mime_types) or "none"
            rejected = ", ".join(unsupported_file_types)
            raise ValueError(f"Model '{model}' does not support file MIME type(s): {rejected}. Supported: {supported}")
        if stream and not capabilities.streaming:
            raise ValueError(f"Model '{model}' does not support streaming responses")
        if uses_function_tools and not capabilities.tools:
            raise ValueError(f"Model '{model}' does not support tool calls")
        if uses_search and not capabilities.search:
            raise ValueError(f"Model '{model}' does not support Google Search grounding")
        if generation_config is not None and (generation_config.responseMimeType is not None or generation_config.responseSchema is not None) and not capabilities.structured_output:
            raise ValueError(f"Model '{model}' does not support structured output")
        if generation_config_overrides:
            unsupported = set(capabilities.unsupported_generation_fields)
            if "thinking_config" in generation_config_overrides and not capabilities.thinking:
                unsupported.add("thinking_config")
            blocked = [field for field in generation_config_overrides if field in unsupported]
            if blocked:
                field_names = ", ".join(GENERATION_CONFIG_FIELD_NAMES.get(field, field) for field in blocked)
                raise ValueError(f"Model '{model}' does not support generationConfig field(s): {field_names}")

        success = True
        return {
            "model": model,
            "contents": contents,
            "system_instruction": system_instruction,
            "tools": tools or None,
            "capture_prompt": capture_prompt,
            "capture_images": capture_images or None,
            "file_input_mime_types": file_input_mime_types,
            "cleanup_paths": cleanup_paths,
            "temperature": generation_config.temperature if generation_config else None,
            "top_p": generation_config.topP if generation_config else None,
            "top_k": generation_config.topK if generation_config else None,
            "max_tokens": generation_config.maxOutputTokens if generation_config else None,
            "generation_config_overrides": generation_config_overrides or None,
        }
    finally:
        if not success:
            cleanup_files(cleanup_paths)
