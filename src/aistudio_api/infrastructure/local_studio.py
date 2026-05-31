"""Local OpenAI-compatible studio persistence and payload helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from aistudio_api.config import settings
from aistudio_api.domain.model_capabilities import get_model_capabilities


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_DATA_URI_RE = re.compile(r"^data:([^;,]+)?(;base64)?,(.*)$", re.DOTALL)
_HEAVY_FIELDS = {"data", "data_url", "file_data", "b64", "b64_json"}
_IMAGE_MODEL_PREFIXES = ("gpt-image-",)
_NON_CHAT_MODEL_MARKERS = ("audio", "realtime", "tts", "transcribe", "embedding")
LOCAL_STUDIO_INTERFACE_MODES = ("openai", "responses", "gemini", "claude")
LOCAL_STUDIO_PROVIDER_GOOGLE = "google-ai-studio"
LOCAL_STUDIO_PROVIDER_OPENAI = "openai"
LOCAL_STUDIO_PROVIDER_TYPES = (LOCAL_STUDIO_PROVIDER_GOOGLE, LOCAL_STUDIO_PROVIDER_OPENAI)
GPT_IMAGE_2_SIZE_OPTIONS = [
    {"label": "Square", "size": "1024x1024", "note": "general-purpose default"},
    {"label": "HD portrait", "size": "1024x1536", "note": "standard portrait"},
    {"label": "HD landscape", "size": "1536x1024", "note": "standard landscape"},
    {"label": "Deck landscape", "size": "1536x864", "note": "16:9 slide or UI mock"},
    {"label": "2K / QHD", "size": "2560x1440", "note": "upper reliability boundary"},
    {"label": "Near 4K / UHD", "size": "3824x2144", "note": "experimental, below 3840px max edge"},
]
_GPT_IMAGE_2_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_GPT_IMAGE_2_MAX_EDGE_EXCLUSIVE = 3840
_GPT_IMAGE_2_MAX_PIXELS = 8_294_400
_GPT_IMAGE_2_MIN_PIXELS = 655_360
_GPT_IMAGE_2_MAX_RATIO = 3

_OPENAI_IMAGE_PARAMETER_METADATA = {
    "size": {"type": "string", "enum": [item["size"] for item in GPT_IMAGE_2_SIZE_OPTIONS], "default": "1024x1024"},
    "quality": {"type": "string", "enum": ["auto", "low", "medium", "high"], "default": "auto"},
    "background": {"type": "string", "enum": ["auto", "transparent", "opaque"], "default": "auto"},
    "output_format": {"type": "string", "enum": ["png", "jpeg", "webp"], "default": "png"},
    "output_compression": {"type": "integer", "minimum": 0, "maximum": 100, "default": 100},
}

_MIME_EXTENSIONS = {
    "application/json": ".json",
    "application/pdf": ".pdf",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


def normalize_openai_base_url(value: str) -> str:
    base_url = str(value or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("OpenAI-compatible base URL is required")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("OpenAI-compatible base URL must start with http:// or https://")
    return base_url


def upstream_url(base_url: str, path: str) -> str:
    return f"{normalize_openai_base_url(base_url)}/{path.lstrip('/')}"


def normalize_interface_mode(value: str | None, *, default: str = "responses") -> str:
    mode = str(value or default).strip().lower()
    if mode not in LOCAL_STUDIO_INTERFACE_MODES:
        raise ValueError(f"interface_mode must be one of: {', '.join(LOCAL_STUDIO_INTERFACE_MODES)}")
    return mode


def normalize_provider_kind(value: str | None, *, default: str = LOCAL_STUDIO_PROVIDER_GOOGLE) -> str:
    kind = str(value or default).strip().lower().replace("_", "-")
    if kind in {"google", "google-ai", "google-aistudio", "google-ai-studio"}:
        return LOCAL_STUDIO_PROVIDER_GOOGLE
    if kind in {"openai", "open-ai"}:
        return LOCAL_STUDIO_PROVIDER_OPENAI
    if kind not in LOCAL_STUDIO_PROVIDER_TYPES:
        raise ValueError(f"provider_type must be one of: {', '.join(LOCAL_STUDIO_PROVIDER_TYPES)}")
    return kind


def default_local_studio_base_url(mode: str) -> str:
    version = "v1beta" if normalize_interface_mode(mode) == "gemini" else "v1"
    return f"http://127.0.0.1:{settings.port}/{version}"


def infer_provider_kind(
    value: str | None = None,
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> str:
    if value:
        return normalize_provider_kind(value)
    if str(base_url or "").strip() or str(token or "").strip():
        return LOCAL_STUDIO_PROVIDER_OPENAI
    return LOCAL_STUDIO_PROVIDER_GOOGLE


def resolve_local_studio_provider_settings(
    *,
    provider_type: str | None = None,
    base_url: str | None = None,
    token: str | None = None,
    mode: str = "responses",
    internal_base_url: str | None = None,
) -> tuple[str, str, str]:
    provider_kind = infer_provider_kind(provider_type, base_url=base_url, token=token)
    if provider_kind == LOCAL_STUDIO_PROVIDER_GOOGLE:
        if internal_base_url:
            return provider_kind, normalize_openai_base_url(internal_base_url), ""
        return provider_kind, default_local_studio_base_url(mode), ""
    resolved_base_url = normalize_openai_base_url(str(base_url or ""))
    resolved_token = str(token or "").strip()
    if not resolved_token:
        raise ValueError("API token is required for OpenAI providers")
    if "\n" in resolved_token or "\r" in resolved_token:
        raise ValueError("API token must be a single line")
    return provider_kind, resolved_base_url, resolved_token


def filter_chat_models(models: Iterable[Mapping[str, Any]], mode: str = "responses") -> list[dict[str, Any]]:
    normalized_mode = normalize_interface_mode(mode)
    filtered: list[dict[str, Any]] = []
    for model in models:
        model_id = _model_id(model, normalized_mode)
        if not model_id:
            continue
        lowered = model_id.lower()
        if any(lowered.startswith(prefix) for prefix in _IMAGE_MODEL_PREFIXES):
            continue
        if any(marker in lowered for marker in _NON_CHAT_MODEL_MARKERS):
            continue
        capabilities = model.get("capabilities") if isinstance(model.get("capabilities"), Mapping) else None
        registry_capabilities = _registered_model_capabilities(model_id)
        if (isinstance(capabilities, Mapping) and capabilities.get("image_output") is True) or (registry_capabilities is not None and registry_capabilities.image_output):
            continue
        if normalized_mode == "gemini" and "image" in lowered:
            continue
        item = dict(model, id=model_id)
        item.setdefault("object", "model")
        item.setdefault("created", 0)
        item.setdefault("owned_by", "local-studio")
        if not isinstance(item.get("capabilities"), Mapping):
            item["capabilities"] = default_capabilities_for_model(model_id, mode=normalized_mode, source=model)
        filtered.append(item)
    return filtered


def _registered_model_capabilities(model_id: str):
    try:
        return get_model_capabilities(model_id, strict=True)
    except ValueError:
        return None


def _openai_image_model_metadata() -> dict[str, Any]:
    return {
        "sizes": [{"size": item["size"], "aspect_ratio": item["note"]} for item in GPT_IMAGE_2_SIZE_OPTIONS],
        "response_formats": ["png", "jpeg", "webp"],
        "defaults": {"size": "1024x1024", "n": 1, "response_format": "png"},
        "parameters": dict(_OPENAI_IMAGE_PARAMETER_METADATA),
        "unsupported_fields": [],
        "ignored_fields": ["user"],
    }


def filter_image_models(models: Iterable[Mapping[str, Any]], mode: str = "responses", provider_type: str = LOCAL_STUDIO_PROVIDER_OPENAI) -> list[dict[str, Any]]:
    normalized_mode = normalize_interface_mode(mode)
    provider = normalize_provider_kind(provider_type, default=LOCAL_STUDIO_PROVIDER_OPENAI)
    filtered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in models:
        model_id = _model_id(model, normalized_mode)
        if not model_id or model_id in seen:
            continue
        lowered = model_id.lower()
        capabilities = model.get("capabilities") if isinstance(model.get("capabilities"), Mapping) else None
        registry_capabilities = _registered_model_capabilities(model_id)
        is_openai_image_model = any(lowered.startswith(prefix) for prefix in _IMAGE_MODEL_PREFIXES)
        is_image_model = is_openai_image_model or (isinstance(capabilities, Mapping) and capabilities.get("image_output") is True) or (registry_capabilities is not None and registry_capabilities.image_output)
        if not is_image_model:
            continue
        if provider == LOCAL_STUDIO_PROVIDER_GOOGLE and (is_openai_image_model or registry_capabilities is None or not registry_capabilities.image_output):
            continue
        if provider == LOCAL_STUDIO_PROVIDER_OPENAI and registry_capabilities is not None and registry_capabilities.image_output and not is_openai_image_model:
            continue
        item = dict(model, id=model_id)
        item.setdefault("object", "model")
        item.setdefault("created", 0)
        item.setdefault("owned_by", "local-studio")
        if not isinstance(item.get("capabilities"), Mapping):
            if registry_capabilities is not None:
                item["capabilities"] = registry_capabilities.to_model_dict()["capabilities"]
            else:
                item["capabilities"] = {"image_output": True}
        if registry_capabilities is not None and registry_capabilities.image_sizes and not isinstance(item.get("image_generation"), Mapping):
            item["image_generation"] = registry_capabilities.to_model_dict()["image_generation"]
        elif is_openai_image_model and not isinstance(item.get("image_generation"), Mapping):
            item["image_generation"] = _openai_image_model_metadata()
        filtered.append(item)
        seen.add(model_id)
    return filtered


def default_capabilities_for_model(model_id: str, *, mode: str = "responses", source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    methods = source.get("supportedGenerationMethods") if isinstance(source, Mapping) else None
    method_list = [str(item) for item in methods] if isinstance(methods, list) else []
    supports_stream = "streamGenerateContent" in method_list if mode == "gemini" and method_list else True
    lowered = str(model_id or "").lower()
    file_input = not lowered.startswith("gemma-")
    capabilities = {
        "text_output": True,
        "image_input": file_input,
        "file_input": file_input,
        "file_input_mime_types": ["image/*", "application/pdf", "text/plain", "text/markdown", "text/csv", "application/json"],
        "image_output": False,
        "search": mode in {"openai", "responses", "gemini", "claude"},
        "tools": mode in {"openai", "responses", "claude"},
        "tool_calls": mode in {"openai", "responses", "claude"},
        "thinking": mode in {"openai", "responses", "gemini", "claude"},
        "streaming": supports_stream,
        "structured_output": mode in {"openai", "responses", "gemini"},
        "safety_settings": mode == "gemini",
        "unsupported_generation_fields": [],
    }
    if not file_input:
        capabilities["image_input"] = False
        capabilities["file_input_mime_types"] = []
    return capabilities


def local_studio_models_path(mode: str) -> str:
    normalize_interface_mode(mode)
    return "/models"


def local_studio_chat_path(mode: str, model: str, *, stream: bool = False) -> str:
    normalized_mode = normalize_interface_mode(mode)
    if normalized_mode == "openai":
        return "/chat/completions"
    if normalized_mode == "responses":
        return "/responses"
    if normalized_mode == "gemini":
        action = "streamGenerateContent" if stream else "generateContent"
        return f"/models/{quote(str(model or '').replace('models/', ''), safe='') }:{action}"
    return "/messages"


def build_local_studio_chat_payload(
    *,
    mode: str,
    model: str,
    messages: list[Mapping[str, Any]],
    options: Mapping[str, Any] | None = None,
    asset_resolver: Callable[[Mapping[str, Any]], str] | None = None,
    provider_type: str | None = None,
) -> dict[str, Any]:
    normalized_mode = normalize_interface_mode(mode)
    if normalized_mode == "openai":
        return build_chat_completions_payload(model=model, messages=messages, options=options, asset_resolver=asset_resolver)
    if normalized_mode == "responses":
        return build_responses_payload(model=model, messages=messages, options=options, asset_resolver=asset_resolver, provider_type=provider_type)
    if normalized_mode == "gemini":
        return build_gemini_payload(model=model, messages=messages, options=options, asset_resolver=asset_resolver)
    return build_claude_payload(model=model, messages=messages, options=options, asset_resolver=asset_resolver)


def parse_local_studio_output(mode: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized_mode = normalize_interface_mode(mode)
    if normalized_mode == "openai":
        return parse_chat_completions_output(payload)
    if normalized_mode == "responses":
        return parse_responses_output(payload)
    if normalized_mode == "gemini":
        return parse_gemini_output(payload)
    return parse_claude_output(payload)


def parse_local_studio_stream_event(mode: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized_mode = normalize_interface_mode(mode)
    if normalized_mode == "openai":
        return parse_chat_completions_stream_event(payload)
    if normalized_mode == "responses":
        return parse_responses_stream_event(payload)
    if normalized_mode == "gemini":
        return parse_gemini_output(payload)
    return parse_claude_stream_event(payload)


def build_chat_completions_payload(
    *,
    model: str,
    messages: list[Mapping[str, Any]],
    options: Mapping[str, Any] | None = None,
    asset_resolver: Callable[[Mapping[str, Any]], str] | None = None,
) -> dict[str, Any]:
    if not model:
        raise ValueError("model is required")
    options = options or {}
    payload: dict[str, Any] = {
        "model": model,
        "messages": [_message_to_openai_chat_message(message, asset_resolver) for message in messages if message.get("role") in {"user", "assistant", "system", "developer"}],
    }
    _apply_common_sampling(payload, options, max_tokens_key="max_tokens")
    if options.get("stream"):
        payload["stream"] = True
    thinking = _thinking_option(options)
    if thinking:
        payload["thinking"] = thinking
    if options.get("search"):
        payload["grounding"] = True
    return payload


def build_gemini_payload(
    *,
    model: str,
    messages: list[Mapping[str, Any]],
    options: Mapping[str, Any] | None = None,
    asset_resolver: Callable[[Mapping[str, Any]], str] | None = None,
) -> dict[str, Any]:
    if not model:
        raise ValueError("model is required")
    options = options or {}
    contents: list[dict[str, Any]] = []
    system_parts: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user").lower()
        parts = _message_to_gemini_parts(message, asset_resolver)
        if not parts:
            continue
        if role in {"system", "developer"}:
            system_parts.extend(parts)
        else:
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
    if not contents:
        contents.append({"role": "user", "parts": [{"text": ""}]})
    payload: dict[str, Any] = {"contents": contents}
    generation_config: dict[str, Any] = {}
    _apply_common_sampling(generation_config, options, max_tokens_key="maxOutputTokens", top_p_key="topP")
    thinking = _gemini_thinking_config(_thinking_option(options))
    if thinking:
        generation_config["thinkingConfig"] = thinking
    if generation_config:
        payload["generationConfig"] = generation_config
    if system_parts:
        payload["systemInstruction"] = {"parts": system_parts}
    if options.get("search"):
        payload["tools"] = [{"googleSearch": {}}]
    return payload


def build_claude_payload(
    *,
    model: str,
    messages: list[Mapping[str, Any]],
    options: Mapping[str, Any] | None = None,
    asset_resolver: Callable[[Mapping[str, Any]], str] | None = None,
) -> dict[str, Any]:
    if not model:
        raise ValueError("model is required")
    options = options or {}
    payload: dict[str, Any] = {
        "model": model,
        "messages": [_message_to_claude_message(message, asset_resolver) for message in messages if message.get("role") in {"user", "assistant"}],
    }
    max_tokens = _positive_int(options.get("max_tokens") or options.get("max_output_tokens")) or 8192
    payload["max_tokens"] = max_tokens
    _apply_common_sampling(payload, options, max_tokens_key="")
    if options.get("stream"):
        payload["stream"] = True
    thinking = _thinking_option(options)
    if thinking:
        payload["thinking"] = thinking
    if options.get("search"):
        payload["tools"] = [{"type": "web_search"}]
    return payload


def validate_gpt_image_2_size(value: str) -> str:
    size = str(value or "").strip().lower()
    if not size:
        raise ValueError("gpt-image-2 size is required")
    if size == "auto":
        return size
    match = _GPT_IMAGE_2_SIZE_RE.fullmatch(size)
    if not match:
        raise ValueError("gpt-image-2 size must use WIDTHxHEIGHT format")
    width = int(match.group(1))
    height = int(match.group(2))
    if width % 16 or height % 16:
        raise ValueError("gpt-image-2 size edges must be multiples of 16")
    if max(width, height) >= _GPT_IMAGE_2_MAX_EDGE_EXCLUSIVE:
        raise ValueError("gpt-image-2 maximum edge must be less than 3840px")
    if max(width, height) / min(width, height) > _GPT_IMAGE_2_MAX_RATIO:
        raise ValueError("gpt-image-2 size ratio must not exceed 3:1")
    pixels = width * height
    if pixels > _GPT_IMAGE_2_MAX_PIXELS:
        raise ValueError("gpt-image-2 size total pixels must not exceed 8,294,400")
    if pixels < _GPT_IMAGE_2_MIN_PIXELS:
        raise ValueError("gpt-image-2 size total pixels must be at least 655,360")
    return f"{width}x{height}"


def _normalized_image_provider(options: Mapping[str, Any]) -> str:
    raw = options.get("image_tool_provider") or options.get("image_provider") or options.get("provider_type") or options.get("providerType")
    if raw in (None, ""):
        return LOCAL_STUDIO_PROVIDER_OPENAI
    return normalize_provider_kind(str(raw), default=LOCAL_STUDIO_PROVIDER_OPENAI)


def build_image_generation_tool(options: Mapping[str, Any] | None) -> dict[str, Any] | None:
    options = options or {}
    if not options.get("image_tool_enabled"):
        return None
    provider = _normalized_image_provider(options)
    requested_model = str(options.get("image_model") or options.get("imageToolModel") or "").strip()
    if provider == LOCAL_STUDIO_PROVIDER_GOOGLE:
        tool = {
            "type": "image_generation",
            "provider": LOCAL_STUDIO_PROVIDER_GOOGLE,
            "model": requested_model or "gemini-3.1-flash-image-preview",
        }
        size = str(options.get("size") or "").strip()
        if size:
            tool["size"] = size
        return tool

    tool: dict[str, Any] = {"type": "image_generation", "model": requested_model or "gpt-image-2"}
    for key in ("size", "quality", "background", "output_format", "output_compression"):
        value = options.get(key)
        if value not in (None, ""):
            if key == "size":
                value = validate_gpt_image_2_size(str(value))
            tool[key] = value
    return tool


def build_responses_payload(
    *,
    model: str,
    messages: list[Mapping[str, Any]],
    options: Mapping[str, Any] | None = None,
    asset_resolver: Callable[[Mapping[str, Any]], str] | None = None,
    provider_type: str | None = None,
) -> dict[str, Any]:
    if not model:
        raise ValueError("model is required")
    options = options or {}
    search_provider = normalize_provider_kind(
        provider_type or str(options.get("provider_type") or options.get("providerType") or ""),
        default=LOCAL_STUDIO_PROVIDER_GOOGLE,
    )
    payload: dict[str, Any] = {
        "model": model,
        "input": [_message_to_response_input(message, asset_resolver) for message in messages if message.get("role") in {"user", "assistant"}],
    }
    reasoning: dict[str, Any] = {}
    effort = str(options.get("reasoning_effort") or "off").strip()
    summary = str(options.get("reasoning_summary") or "").strip()
    if effort and effort != "off":
        reasoning["effort"] = effort
    if summary and summary != "none":
        reasoning["summary"] = summary
    if reasoning:
        payload["reasoning"] = reasoning
    tools: list[dict[str, Any]] = []
    if options.get("search"):
        tools.append({"type": "web_search_preview" if search_provider == LOCAL_STUDIO_PROVIDER_GOOGLE else "web_search"})
    tool_options = options
    if provider_type and not any(key in options for key in ("image_tool_provider", "image_provider", "provider_type", "providerType")):
        tool_options = {**options, "provider_type": provider_type}
    image_tool = build_image_generation_tool(tool_options)
    if image_tool:
        tools.append(image_tool)
    if tools:
        payload["tools"] = tools
    if options.get("stream"):
        payload["stream"] = True
    return payload


def parse_responses_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    image_candidates: list[dict[str, Any]] = []

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        text_parts.append(output_text)

    explicit_thinking = payload.get("thinking")
    if isinstance(explicit_thinking, str) and explicit_thinking:
        thinking_parts.append(explicit_thinking)

    for item in payload.get("output") if isinstance(payload.get("output"), list) else []:
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "reasoning":
            thinking_parts.extend(_text_from_parts(item.get("summary")))
            thinking_parts.extend(_text_from_parts(item.get("content")))
        elif item_type in {"message", "output_text"}:
            text_parts.extend(_text_from_parts(item.get("content")))
            if isinstance(item.get("text"), str):
                text_parts.append(str(item["text"]))
        elif item_type == "image_generation_call":
            image_candidates.extend(_image_candidates_from_mapping(item))
        else:
            text_parts.extend(_text_from_parts(item.get("content")))
            image_candidates.extend(_image_candidates_from_mapping(item))

    image_candidates.extend(_image_candidates_from_mapping(payload))
    return {
        "content": "".join(text_parts).strip(),
        "thinking": "\n".join(part for part in thinking_parts if part).strip(),
        "usage": payload.get("usage") if isinstance(payload.get("usage"), Mapping) else None,
        "image_candidates": image_candidates,
    }


class LocalStudioStore:
    """Persist local OpenAI studio conversations and uploaded/generated assets."""

    def __init__(self, storage_dir: str | Path | None = None, *, max_conversations: int = 300, max_cache_entries: int = 500) -> None:
        self.root = Path(storage_dir or settings.local_studio_dir).expanduser().resolve()
        self.conversations_dir = self.root / "conversations"
        self.files_dir = self.root / "files"
        self.request_cache_dir = self.root / "cache" / "requests"
        self.max_conversations = max_conversations
        self.max_cache_entries = max_cache_entries

    def ensure_directory(self) -> None:
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.request_cache_dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict[str, Any]]:
        self.ensure_directory()
        conversations: list[dict[str, Any]] = []
        for path in self.conversations_dir.glob("*.json"):
            try:
                conversations.append(self._summary(self._read_json(path)))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        conversations.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or 0, reverse=True)
        return conversations[: self.max_conversations]

    def create(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        now = int(time.time())
        conversation = {
            "id": uuid.uuid4().hex,
            "title": str(payload.get("title") or "New conversation")[:80],
            "created_at": now,
            "updated_at": now,
            "model": str(payload.get("model") or ""),
            "interface_mode": normalize_interface_mode(str(payload.get("interface_mode") or payload.get("mode") or "responses")),
            "settings": dict(payload.get("settings") or {}),
            "messages": [],
        }
        return self.save(conversation)

    def get(self, conversation_id: str) -> dict[str, Any]:
        path = self._path_for(conversation_id)
        if not path.is_file():
            raise FileNotFoundError(conversation_id)
        return self._read_json(path)

    def save(self, conversation: Mapping[str, Any]) -> dict[str, Any]:
        data = self._strip_heavy_fields(dict(conversation))
        conversation_id = self._validate_id(str(data.get("id") or uuid.uuid4().hex))
        now = int(time.time())
        existing = self._read_json(self._path_for(conversation_id)) if self._path_for(conversation_id).is_file() else {}
        data["id"] = conversation_id
        data["created_at"] = int(data.get("created_at") or existing.get("created_at") or now)
        data["updated_at"] = now
        data["interface_mode"] = normalize_interface_mode(str(data.get("interface_mode") or data.get("mode") or existing.get("interface_mode") or "responses"))
        data["messages"] = [self._normalize_message(message) for message in data.get("messages") if isinstance(message, Mapping)] if isinstance(data.get("messages"), list) else []
        data["title"] = self._title_for(data)
        self.ensure_directory()
        self._write_json(self._path_for(conversation_id), data)
        self._prune_old_conversations()
        return data

    def patch(self, conversation_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        conversation = self.get(conversation_id)
        if "title" in payload:
            conversation["title"] = str(payload.get("title") or "").strip()[:80] or self._title_for(conversation)
        if "model" in payload:
            conversation["model"] = str(payload.get("model") or "")
        if isinstance(payload.get("settings"), Mapping):
            conversation["settings"] = dict(payload["settings"])
        if "interface_mode" in payload or "mode" in payload:
            conversation["interface_mode"] = normalize_interface_mode(str(payload.get("interface_mode") or payload.get("mode") or conversation.get("interface_mode") or "responses"))
        return self.save(conversation)

    def delete(self, conversation_id: str) -> bool:
        path = self._path_for(conversation_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def bulk_delete(self, conversation_ids: Iterable[str]) -> dict[str, Any]:
        deleted: list[str] = []
        missing: list[str] = []
        seen: set[str] = set()
        for raw_id in conversation_ids:
            conversation_id = str(raw_id or "").strip()
            if not conversation_id or conversation_id in seen:
                continue
            seen.add(conversation_id)
            if self.delete(conversation_id):
                deleted.append(conversation_id)
            else:
                missing.append(conversation_id)
        return {"deleted": deleted, "missing": missing, "deleted_count": len(deleted)}

    def add_user_message(self, conversation: dict[str, Any], content: str, files: list[Mapping[str, Any]]) -> dict[str, Any]:
        now = int(time.time())
        attachments = [self.save_data_url_asset(file) for file in files]
        message = {
            "id": uuid.uuid4().hex,
            "role": "user",
            "content": str(content or ""),
            "attachments": attachments,
            "created_at": now,
        }
        conversation.setdefault("messages", []).append(message)
        return message

    def add_assistant_message(
        self,
        conversation: dict[str, Any],
        *,
        content: str = "",
        thinking: str = "",
        usage: Mapping[str, Any] | None = None,
        images: list[Mapping[str, Any]] | None = None,
        error: str = "",
        cache: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": content,
            "thinking": thinking,
            "usage": dict(usage or {}),
            "images": [dict(image) for image in images or []],
            "error": error,
            "created_at": int(time.time()),
        }
        if isinstance(cache, Mapping):
            message["cache"] = dict(cache)
        conversation.setdefault("messages", []).append(message)
        return message

    def request_cache_key(
        self,
        *,
        base_url: str,
        token: str,
        mode: str,
        model: str,
        request_body: Mapping[str, Any],
        provider_type: str = "",
        provider_id: str = "",
        provider_name: str = "",
        namespace: str = "",
    ) -> str:
        provider_signature = {
            "base_url": normalize_openai_base_url(base_url),
            "token_hash": hashlib.sha256(str(token or "").encode("utf-8")).hexdigest() if token else "",
            "mode": normalize_interface_mode(mode),
            "model": str(model or ""),
            "provider_type": normalize_provider_kind(provider_type) if provider_type else "",
            "provider_id": str(provider_id or "").strip(),
            "provider_name": str(provider_name or "").strip(),
            "namespace": str(namespace or "").strip(),
            "request": self._request_cache_shape(request_body),
        }
        payload = json.dumps(provider_signature, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get_request_cache(self, cache_key: str) -> dict[str, Any] | None:
        path = self._request_cache_path(cache_key)
        if not path.is_file():
            return None
        try:
            data = self._read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def save_request_cache(self, cache_key: str, data: Mapping[str, Any]) -> dict[str, Any]:
        self.ensure_directory()
        payload = dict(data)
        payload["cache_key"] = cache_key
        payload.setdefault("created_at", int(time.time()))
        payload["updated_at"] = int(time.time())
        path = self._request_cache_path(cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
        self._prune_old_request_cache()
        return payload

    def truncate_for_rerun(self, conversation: dict[str, Any], message_index: int) -> dict[str, Any]:
        messages = conversation.get("messages") if isinstance(conversation.get("messages"), list) else []
        user_index = -1
        for index in range(min(message_index, len(messages) - 1), -1, -1):
            if isinstance(messages[index], Mapping) and messages[index].get("role") == "user":
                user_index = index
                break
        if user_index < 0:
            raise ValueError("No user message is available to rerun")
        conversation["messages"] = messages[: user_index + 1]
        return conversation["messages"][user_index]

    def save_data_url_asset(self, file: Mapping[str, Any]) -> dict[str, Any]:
        data_url = str(file.get("data_url") or file.get("url") or file.get("file_data") or "")
        data, mime_type = decode_data_uri(data_url, fallback_mime=str(file.get("mime") or file.get("mime_type") or "application/octet-stream"))
        return self.save_binary_asset(
            data,
            mime_type,
            filename=str(file.get("name") or file.get("filename") or "upload"),
            source=str(file.get("source") or "upload"),
        )

    def save_binary_asset(self, data: bytes, mime_type: str, *, filename: str = "asset", source: str = "generated") -> dict[str, Any]:
        self.ensure_directory()
        created = int(time.time())
        day = datetime.fromtimestamp(created, UTC).strftime("%Y%m%d")
        asset_id = uuid.uuid4().hex
        extension = _extension_for_mime(mime_type, filename)
        relative_path = Path(day) / f"{asset_id}{extension}"
        target = self.files_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        path = relative_path.as_posix()
        return {
            "id": asset_id,
            "name": filename[:160] or f"asset{extension}",
            "mime": mime_type or "application/octet-stream",
            "size": len(data),
            "path": path,
            "url": self.public_url(path),
            "source": source,
            "created_at": created,
        }

    def save_response_images(self, candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            try:
                if candidate.get("data_url"):
                    data, mime_type = decode_data_uri(str(candidate["data_url"]), fallback_mime=str(candidate.get("mime") or "image/png"))
                    signature = ("bytes", hashlib.sha256(data).hexdigest())
                    if signature in seen:
                        continue
                    seen.add(signature)
                    saved = self.save_binary_asset(data, mime_type, filename=f"generated-{len(images) + 1}", source="generated")
                elif candidate.get("b64_json") or candidate.get("b64") or candidate.get("result") or candidate.get("partial_image"):
                    encoded = str(candidate.get("b64_json") or candidate.get("b64") or candidate.get("result") or candidate.get("partial_image") or "")
                    data = base64.b64decode(encoded)
                    signature = ("bytes", hashlib.sha256(data).hexdigest())
                    if signature in seen:
                        continue
                    seen.add(signature)
                    saved = self.save_binary_asset(data, str(candidate.get("mime") or "image/png"), filename=f"generated-{len(images) + 1}", source="generated")
                elif candidate.get("url"):
                    signature = ("url", str(candidate["url"]))
                    if signature in seen:
                        continue
                    seen.add(signature)
                    saved = {"id": uuid.uuid4().hex, "url": str(candidate["url"]), "name": f"generated-{len(images) + 1}", "mime": str(candidate.get("mime") or "image/png"), "source": "generated"}
                else:
                    continue
            except (ValueError, OSError):
                continue
            images.append(saved)
        return images

    def asset_to_data_url(self, asset: Mapping[str, Any]) -> str:
        path = self.resolve_asset_path(str(asset.get("path") or ""))
        data = path.read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{asset.get('mime') or 'application/octet-stream'};base64,{encoded}"

    def public_url(self, relative_path: str) -> str:
        encoded = "/".join(quote(part) for part in relative_path.replace("\\", "/").split("/") if part)
        return f"/api/local-studio/assets/{encoded}"

    def resolve_asset_path(self, asset_path_or_url: str) -> Path:
        raw = str(asset_path_or_url or "").strip()
        if not raw or "\x00" in raw:
            raise ValueError("asset path is required")
        parsed = urlparse(raw)
        path = parsed.path if parsed.scheme or parsed.netloc else raw
        path = unquote(path).replace("\\", "/")
        prefix = "/api/local-studio/assets/"
        if path.startswith(prefix):
            path = path[len(prefix) :]
        elif path.startswith("/"):
            raise ValueError("asset path is outside local studio storage")
        candidate = (self.files_dir / path).resolve()
        try:
            candidate.relative_to(self.files_dir)
        except ValueError as exc:
            raise ValueError("asset path is outside local studio storage") from exc
        if candidate == self.files_dir or not candidate.is_file():
            raise FileNotFoundError(path)
        return candidate

    def _path_for(self, conversation_id: str) -> Path:
        return self.conversations_dir / f"{self._validate_id(conversation_id)}.json"

    def _request_cache_path(self, cache_key: str) -> Path:
        key = str(cache_key or "").strip()
        if not key:
            raise ValueError("cache key is required")
        return self.request_cache_dir / key[:2] / f"{key}.json"

    def _validate_id(self, value: str) -> str:
        conversation_id = str(value or "").strip()
        if not _ID_RE.fullmatch(conversation_id):
            raise ValueError("conversation id is invalid")
        return conversation_id

    def _read_json(self, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("conversation file must contain an object")
        return data

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def _request_cache_shape(self, request_body: Mapping[str, Any]) -> dict[str, Any]:
        shape: dict[str, Any] = {}
        for key, value in request_body.items():
            if str(key) == "stream":
                continue
            shape[str(key)] = value
        return shape

    def _normalize_message(self, message: Mapping[str, Any]) -> dict[str, Any]:
        role = "assistant" if message.get("role") == "assistant" else "user"
        normalized = {
            "id": str(message.get("id") or uuid.uuid4().hex),
            "role": role,
            "content": str(message.get("content") or ""),
            "created_at": int(message.get("created_at") or time.time()),
        }
        for key in ("thinking", "error"):
            if message.get(key):
                normalized[key] = str(message[key])
        if isinstance(message.get("usage"), Mapping):
            normalized["usage"] = dict(message["usage"])
        if isinstance(message.get("cache"), Mapping):
            normalized["cache"] = dict(message["cache"])
        for key in ("attachments", "images"):
            if isinstance(message.get(key), list):
                normalized[key] = [self._strip_heavy_fields(dict(item)) for item in message[key] if isinstance(item, Mapping)]
        return normalized

    def _summary(self, conversation: Mapping[str, Any]) -> dict[str, Any]:
        messages = conversation.get("messages") if isinstance(conversation.get("messages"), list) else []
        last_message = next((message for message in reversed(messages) if isinstance(message, Mapping)), {})
        return {
            "id": conversation.get("id", ""),
            "title": self._title_for(conversation),
            "created_at": conversation.get("created_at"),
            "updated_at": conversation.get("updated_at"),
            "model": conversation.get("model", ""),
            "interface_mode": conversation.get("interface_mode", "responses"),
            "message_count": len(messages),
            "preview": str(last_message.get("error") or last_message.get("content") or "")[:120],
            "last_error": str(last_message.get("error") or ""),
        }

    def _title_for(self, conversation: Mapping[str, Any]) -> str:
        title = str(conversation.get("title") or "").strip()
        if title and title != "New conversation":
            return title[:80]
        messages = conversation.get("messages") if isinstance(conversation.get("messages"), list) else []
        for message in messages:
            if isinstance(message, Mapping) and message.get("role") == "user":
                content = str(message.get("content") or "").strip()
                if content:
                    return content[:80]
                attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
                if attachments:
                    return "Attachment conversation"
        return title[:80] or "New conversation"

    def _strip_heavy_fields(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._strip_heavy_fields(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._strip_heavy_fields(item) for key, item in value.items() if str(key) not in _HEAVY_FIELDS}
        return value

    def _prune_old_conversations(self) -> None:
        files = []
        for path in self.conversations_dir.glob("*.json"):
            try:
                data = self._read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            files.append((data.get("updated_at") or data.get("created_at") or 0, path.name, path))
        files.sort(reverse=True)
        for _, _, path in files[self.max_conversations :]:
            try:
                path.unlink()
            except FileNotFoundError:
                continue

    def _prune_old_request_cache(self) -> None:
        files: list[tuple[int, str, Path]] = []
        for path in self.request_cache_dir.glob("**/*.json"):
            try:
                data = self._read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            files.append((int(data.get("updated_at") or data.get("created_at") or 0), path.name, path))
        files.sort(reverse=True)
        for _, _, path in files[self.max_cache_entries :]:
            try:
                path.unlink()
            except FileNotFoundError:
                continue


def decode_data_uri(value: str, *, fallback_mime: str = "application/octet-stream") -> tuple[bytes, str]:
    match = _DATA_URI_RE.match(value or "")
    if not match:
        raise ValueError("file data must be a data URL")
    mime_type = match.group(1) or fallback_mime
    payload = match.group(3) or ""
    if match.group(2):
        return base64.b64decode(payload), mime_type
    return unquote(payload).encode("utf-8"), mime_type


def _extension_for_mime(mime_type: str | None, filename: str = "") -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    if normalized in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[normalized]
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix and len(suffix) <= 8 else ".bin"


def _message_to_response_input(message: Mapping[str, Any], asset_resolver: Callable[[Mapping[str, Any]], str] | None) -> dict[str, Any]:
    role = "assistant" if message.get("role") == "assistant" else "user"
    content = str(message.get("content") or "")
    if role == "assistant":
        return {"role": "assistant", "content": content}
    blocks: list[dict[str, Any]] = []
    if content:
        blocks.append({"type": "input_text", "text": content})
    attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue
        data_url = str(attachment.get("data_url") or attachment.get("file_data") or "")
        if not data_url and asset_resolver is not None:
            data_url = asset_resolver(attachment)
        if not data_url:
            continue
        mime_type = str(attachment.get("mime") or attachment.get("mime_type") or "application/octet-stream")
        if mime_type.startswith("image/"):
            blocks.append({"type": "input_image", "image_url": data_url})
        else:
            blocks.append({"type": "input_file", "filename": str(attachment.get("name") or "upload"), "file_data": data_url})
    return {"role": "user", "content": blocks or content}


def _message_to_openai_chat_message(message: Mapping[str, Any], asset_resolver: Callable[[Mapping[str, Any]], str] | None) -> dict[str, Any]:
    role = str(message.get("role") or "user").lower()
    if role == "developer":
        role = "system"
    if role not in {"system", "user", "assistant"}:
        role = "user"
    content = str(message.get("content") or "")
    if role != "user":
        return {"role": role, "content": content}
    blocks = _message_to_openai_content_blocks(message, asset_resolver)
    return {"role": role, "content": blocks if blocks else content}


def _message_to_claude_message(message: Mapping[str, Any], asset_resolver: Callable[[Mapping[str, Any]], str] | None) -> dict[str, Any]:
    role = "assistant" if message.get("role") == "assistant" else "user"
    content = str(message.get("content") or "")
    if role == "assistant":
        return {"role": role, "content": content}
    blocks: list[dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for attachment, data_url in _attachment_data_urls(message, asset_resolver):
        mime_type = str(attachment.get("mime") or attachment.get("mime_type") or "application/octet-stream")
        if mime_type.startswith("image/"):
            media_type, encoded = _split_base64_data_url(data_url, fallback_mime=mime_type)
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}})
        else:
            blocks.append({"type": "text", "text": f"[Attached file: {attachment.get('name') or 'upload'}]"})
    return {"role": role, "content": blocks or content}


def _message_to_gemini_parts(message: Mapping[str, Any], asset_resolver: Callable[[Mapping[str, Any]], str] | None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    content = str(message.get("content") or "")
    if content:
        parts.append({"text": content})
    for attachment, data_url in _attachment_data_urls(message, asset_resolver):
        mime_type, encoded = _split_base64_data_url(data_url, fallback_mime=str(attachment.get("mime") or attachment.get("mime_type") or "application/octet-stream"))
        parts.append({"inlineData": {"mimeType": mime_type, "data": encoded}})
    return parts


def _message_to_openai_content_blocks(message: Mapping[str, Any], asset_resolver: Callable[[Mapping[str, Any]], str] | None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    content = str(message.get("content") or "")
    if content:
        blocks.append({"type": "text", "text": content})
    for attachment, data_url in _attachment_data_urls(message, asset_resolver):
        mime_type = str(attachment.get("mime") or attachment.get("mime_type") or "application/octet-stream")
        if mime_type.startswith("image/"):
            blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        else:
            blocks.append({"type": "file", "file": {"file_data": data_url, "filename": str(attachment.get("name") or "upload"), "mime_type": mime_type}})
    return blocks


def _attachment_data_urls(message: Mapping[str, Any], asset_resolver: Callable[[Mapping[str, Any]], str] | None) -> list[tuple[Mapping[str, Any], str]]:
    attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
    items: list[tuple[Mapping[str, Any], str]] = []
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue
        data_url = str(attachment.get("data_url") or attachment.get("file_data") or "")
        if not data_url and asset_resolver is not None:
            data_url = asset_resolver(attachment)
        if data_url:
            items.append((attachment, data_url))
    return items


def _split_base64_data_url(data_url: str, *, fallback_mime: str = "application/octet-stream") -> tuple[str, str]:
    match = _DATA_URI_RE.match(data_url or "")
    if not match or not match.group(2):
        raise ValueError("file data must be a base64 data URL")
    return match.group(1) or fallback_mime, match.group(3) or ""


def _model_id(model: Mapping[str, Any], mode: str) -> str:
    if mode == "gemini":
        value = model.get("name") or model.get("id") or model.get("displayName")
    else:
        value = model.get("id") or model.get("name")
    return str(value or "").strip().replace("models/", "", 1)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _thinking_option(options: Mapping[str, Any]) -> str:
    value = str(options.get("thinking") or options.get("reasoning_effort") or "off").strip()
    return "" if value in {"", "off"} else value


def _apply_common_sampling(
    payload: dict[str, Any],
    options: Mapping[str, Any],
    *,
    max_tokens_key: str = "max_tokens",
    top_p_key: str = "top_p",
) -> None:
    temperature = _positive_float(options.get("temperature"))
    if temperature is not None:
        payload["temperature"] = temperature
    top_p = _positive_float(options.get("top_p") or options.get("topP"))
    if top_p is not None and top_p_key:
        payload[top_p_key] = top_p
    max_tokens = _positive_int(options.get("max_tokens") or options.get("max_output_tokens") or options.get("maxOutputTokens"))
    if max_tokens is not None and max_tokens_key:
        payload[max_tokens_key] = max_tokens


def _gemini_thinking_config(value: Any) -> list[Any] | None:
    levels = {"low": 1, "medium": 2, "high": 3}
    level = levels.get(str(value or "off"))
    return [1, None, None, level] if level else None


def _text_from_parts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                for key in ("text", "output_text", "reasoning_text", "summary_text"):
                    text = item.get(key)
                    if isinstance(text, str) and text:
                        parts.append(text)
        return parts
    if isinstance(value, Mapping):
        return _text_from_parts([value])
    return []


def parse_chat_completions_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    message = {}
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        raw_message = choices[0].get("message")
        if isinstance(raw_message, Mapping):
            message = raw_message
    return {
        "content": str(message.get("content") or "").strip(),
        "thinking": str(message.get("reasoning_content") or message.get("thinking") or message.get("reasoning") or "").strip(),
        "usage": payload.get("usage") if isinstance(payload.get("usage"), Mapping) else None,
        "image_candidates": _image_candidates_from_mapping(payload),
    }


def parse_chat_completions_stream_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("error"), Mapping):
        return {"error": str(payload["error"].get("message") or payload["error"])}
    choices = payload.get("choices")
    delta = {}
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        raw_delta = choices[0].get("delta")
        if isinstance(raw_delta, Mapping):
            delta = raw_delta
    return {
        "content": str(delta.get("content") or ""),
        "thinking": str(delta.get("reasoning_content") or delta.get("thinking") or delta.get("reasoning") or ""),
        "usage": payload.get("usage") if isinstance(payload.get("usage"), Mapping) else None,
        "image_candidates": _image_candidates_from_mapping(payload),
    }


def parse_responses_stream_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("type") == "error" or isinstance(payload.get("error"), Mapping):
        error = payload.get("error") if isinstance(payload.get("error"), Mapping) else payload
        return {"error": str(error.get("message") or error)}
    event_type = str(payload.get("type") or "")
    content = ""
    thinking = ""
    usage = None
    image_candidates: list[dict[str, Any]] = []
    if event_type == "response.output_text.delta":
        content = str(payload.get("delta") or "")
    elif event_type in {"response.reasoning.delta", "response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
        thinking = str(payload.get("delta") or "")
    elif event_type in {"response.reasoning.done", "response.reasoning_text.done", "response.reasoning_summary_text.done"}:
        thinking = str(payload.get("text") or "")
    elif event_type in {"response.output_item.done", "response.output_item.added"} and isinstance(payload.get("item"), Mapping):
        item = payload["item"]
        if str(item.get("type") or "") == "reasoning":
            thinking = parse_responses_output({"output": [item]})["thinking"]
        elif event_type == "response.output_item.done" and str(item.get("type") or "") == "function_call":
            name = str(item.get("name") or "").strip()
            if name:
                arguments = str(item.get("arguments") or "").strip()
                thinking = f"Tool call requested: {name} {arguments}".strip()
    elif event_type in {"response.reasoning_summary_part.done", "response.reasoning_summary_part.added"}:
        thinking = "\n".join(_text_from_parts(payload.get("part"))).strip()
    elif event_type == "response.completed" and isinstance(payload.get("response"), Mapping):
        response = payload["response"]
        parsed = parse_responses_output(response)
        content = parsed["content"]
        thinking = parsed["thinking"]
        usage = parsed["usage"]
        image_candidates.extend(parsed["image_candidates"])
    if event_type == "response.image_generation_call.partial_image":
        image_candidates.extend(_image_candidates_from_mapping(payload))
    for key in ("item", "output_item", "partial_image"):
        item = payload.get(key)
        if isinstance(item, Mapping):
            image_candidates.extend(_image_candidates_from_mapping(item))
    image_candidates.extend(_image_candidates_from_mapping(payload))
    return {"content": content, "thinking": thinking, "usage": usage, "image_candidates": _dedupe_image_candidates(image_candidates)}


def _dedupe_image_candidates(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for candidate in candidates:
        item = dict(candidate)
        signature = tuple(sorted((str(key), str(value)) for key, value in item.items()))
        if signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


def parse_gemini_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        content = candidate.get("content") if isinstance(candidate.get("content"), Mapping) else {}
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            if isinstance(part.get("text"), str) and part.get("thought"):
                thinking_parts.append(part["text"])
            elif isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            elif isinstance(part.get("functionCall"), Mapping):
                text_parts.append(json.dumps(part["functionCall"], ensure_ascii=False))
    usage = payload.get("usageMetadata") if isinstance(payload.get("usageMetadata"), Mapping) else payload.get("usage")
    return {
        "content": "".join(text_parts).strip(),
        "thinking": "\n".join(part for part in thinking_parts if part).strip(),
        "usage": _normalize_gemini_usage(usage if isinstance(usage, Mapping) else None),
        "image_candidates": _image_candidates_from_mapping(payload),
    }


def parse_claude_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in payload.get("content") if isinstance(payload.get("content"), list) else []:
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("type") or "")
        if block_type == "thinking":
            thinking_parts.append(str(block.get("thinking") or block.get("text") or ""))
        elif block_type == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block_type == "tool_use":
            text_parts.append(json.dumps(block, ensure_ascii=False))
    return {
        "content": "".join(text_parts).strip(),
        "thinking": "\n".join(part for part in thinking_parts if part).strip(),
        "usage": payload.get("usage") if isinstance(payload.get("usage"), Mapping) else None,
        "image_candidates": _image_candidates_from_mapping(payload),
    }


def parse_claude_stream_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("type") == "error" or isinstance(payload.get("error"), Mapping):
        error = payload.get("error") if isinstance(payload.get("error"), Mapping) else payload
        return {"error": str(error.get("message") or error)}
    delta = payload.get("delta") if isinstance(payload.get("delta"), Mapping) else {}
    usage = None
    if isinstance(payload.get("message"), Mapping) and isinstance(payload["message"].get("usage"), Mapping):
        usage = payload["message"]["usage"]
    elif isinstance(payload.get("usage"), Mapping):
        usage = payload["usage"]
    content = ""
    thinking = ""
    if payload.get("type") == "content_block_delta":
        if delta.get("type") == "text_delta":
            content = str(delta.get("text") or "")
        elif delta.get("type") == "thinking_delta":
            thinking = str(delta.get("thinking") or "")
        elif delta.get("type") == "input_json_delta":
            content = "\n" + str(delta.get("partial_json") or "")
    return {"content": content, "thinking": thinking, "usage": usage, "image_candidates": _image_candidates_from_mapping(payload)}


def _response_output_text(payload: Mapping[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    output = payload.get("output") if isinstance(payload.get("output"), list) else []
    return "".join(part for item in output if isinstance(item, Mapping) for part in _text_from_parts(item.get("content")))


def _response_output_thinking(payload: Mapping[str, Any]) -> str:
    if isinstance(payload.get("thinking"), str):
        return payload["thinking"]
    output = payload.get("output") if isinstance(payload.get("output"), list) else []
    parts: list[str] = []
    for item in output:
        if isinstance(item, Mapping) and item.get("type") == "reasoning":
            parts.extend(_text_from_parts(item.get("content")))
            parts.extend(_text_from_parts(item.get("summary")))
    return "".join(parts)


def _normalize_gemini_usage(usage: Mapping[str, Any] | None) -> dict[str, int] | None:
    if not usage:
        return None
    prompt = int(usage.get("promptTokenCount") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("candidatesTokenCount") or usage.get("completion_tokens") or 0)
    total = int(usage.get("totalTokenCount") or usage.get("total_tokens") or prompt + completion)
    cached = int(usage.get("cachedContentTokenCount") or usage.get("cached_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total, "cached_tokens": cached}


def _image_candidates_from_mapping(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in ("b64_json", "b64", "result", "partial_image"):
        item = value.get(key)
        if isinstance(item, str) and item:
            candidates.append({key: item, "mime": value.get("mime") or value.get("mime_type") or "image/png"})
    url = value.get("url")
    if isinstance(url, str) and url:
        if url.startswith("data:image/"):
            candidates.append({"data_url": url, "mime": value.get("mime") or value.get("mime_type") or "image/png"})
        else:
            candidates.append({"url": url, "mime": value.get("mime") or value.get("mime_type") or "image/png"})
    for key in ("data", "content"):
        item = value.get(key)
        if isinstance(item, list):
            for part in item:
                if isinstance(part, Mapping):
                    candidates.extend(_image_candidates_from_mapping(part))
    return candidates
