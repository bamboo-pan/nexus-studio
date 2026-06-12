"""Application service layer for API handlers."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from aistudio_api.config import DEFAULT_IMAGE_MODEL, settings
from aistudio_api.application.chat_service import cleanup_files, encode_schema_to_wire, is_search_tool_type, normalize_chat_request, normalize_gemini_request, normalize_openai_tools_and_search
from aistudio_api.application.chat_service import data_uri_to_file, url_to_file
from aistudio_api.application.validation import validate_number_range
from aistudio_api.domain.errors import AistudioError, AuthError, RequestError, UsageLimitExceeded
from aistudio_api.domain.model_capabilities import (
    DEFAULT_IMAGE_N,
    DEFAULT_IMAGE_RESPONSE_FORMAT,
    DEFAULT_IMAGE_SIZE,
    IMAGE_IGNORED_OPENAI_FIELDS,
    IMAGE_N_MAX,
    IMAGE_N_MIN,
    IMAGE_RESPONSE_FORMATS,
    IMAGE_UNSUPPORTED_OPENAI_FIELDS,
    get_model_capabilities,
    list_model_ids,
    plan_image_generation,
    validate_chat_capabilities,
)
from aistudio_api.infrastructure.generated_images import GeneratedImageStore
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart, AistudioThinkingConfig, ThinkingLevel
from aistudio_api.api.responses import (
    chat_completion_response,
    new_chat_id,
    sse_chunk,
    sse_error,
    sse_usage_chunk,
    to_gemini_parts,
    to_gemini_usage_metadata,
    to_openai_tool_calls,
)
from aistudio_api.api.schemas import ChatRequest, GeminiGenerateContentRequest, ImagePromptOptimizationRequest, ImageRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.infrastructure.account.account_store import AccountMeta
from aistudio_api.application.account_rotator import AccountLease

logger = logging.getLogger("aistudio.server")

MAX_IMAGE_EDIT_INPUTS = 10


@dataclass
class RequestAccountContext:
    client: AIStudioClient
    account: AccountMeta | None = None
    lease: AccountLease | None = None

    @property
    def account_id(self) -> str | None:
        return self.account.id if self.account is not None else None

    async def release(self) -> None:
        if self.lease is not None:
            await self.lease.release()


IMAGE_STYLE_TEMPLATES: dict[str, dict[str, str]] = {
    "none": {
        "label": "无模板",
        "description": "不追加固定风格，尽量保留原始表达。",
        "prompt_hint": "",
    },
    "photorealistic": {
        "label": "写实摄影",
        "description": "对应官方 photography / photorealistic 提示类别。",
        "prompt_hint": "Render as a photorealistic image with natural materials, believable lighting, camera/lens cues, depth of field, and high-detail textures.",
    },
    "comic": {
        "label": "漫画插画",
        "description": "对应 illustration / graphic art 提示类别。",
        "prompt_hint": "Render as a polished comic illustration with clean line art, expressive shapes, controlled colors, readable composition, and strong visual storytelling.",
    },
    "digital-art": {
        "label": "数字艺术",
        "description": "对应 digital art / concept art 提示类别。",
        "prompt_hint": "Render as cinematic digital art with polished art direction, stylized lighting, detailed environment design, and a high-quality finished look.",
    },
    "watercolor": {
        "label": "水彩",
        "description": "对应 painting / traditional media 提示类别。",
        "prompt_hint": "Render as watercolor artwork with translucent washes, soft edges, paper texture, organic color blending, and delicate hand-painted detail.",
    },
    "oil-painting": {
        "label": "油画",
        "description": "对应 painting / historical art references 提示类别。",
        "prompt_hint": "Render as an oil painting with visible brushwork, layered paint texture, rich value contrast, classical composition, and museum-quality finish.",
    },
    "anime": {
        "label": "动漫",
        "description": "对应 stylized illustration / animation 提示类别。",
        "prompt_hint": "Render in anime-inspired cel animation style with expressive characters, clean silhouettes, crisp shading, vivid but controlled colors, and cinematic framing.",
    },
    "3d-render": {
        "label": "3D 渲染",
        "description": "对应 3D / product visualization 提示类别。",
        "prompt_hint": "Render as a high-quality 3D scene with physically based materials, clean geometry, studio lighting, realistic shadows, and precise surface detail.",
    },
    "pixel-art": {
        "label": "像素艺术",
        "description": "对应 stylized illustration / game art 提示类别。",
        "prompt_hint": "Render as pixel art with a crisp low-resolution pixel grid, limited color palette, readable silhouettes, and retro game asset clarity.",
    },
}


IMAGE_PROMPT_OPTIMIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "options": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "special": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["title", "special", "prompt"],
            },
        }
    },
    "required": ["options"],
}


THINKING_LEVELS = {
    "low": ThinkingLevel.LOW,
    "medium": ThinkingLevel.MEDIUM,
    "high": ThinkingLevel.HIGH,
}

MIN_THINKING_CHAT_MAX_TOKENS = 1024


def _bad_request(message: str, error_type: str = "bad_request") -> HTTPException:
    return HTTPException(400, detail={"message": message, "type": error_type})


def parse_image_request(payload: Any) -> ImageRequest:
    try:
        return ImageRequest.model_validate(payload)
    except ValidationError as exc:
        fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors() if error.get("loc")]
        if fields:
            message = f"Invalid image generation request field(s): {', '.join(fields)}. {exc.errors()[0]['msg']}"
        else:
            message = str(exc)
        raise _bad_request(message, "invalid_request_error") from exc


def _upstream_exception(exc: AistudioError) -> HTTPException:
    if isinstance(exc, AuthError):
        return HTTPException(401, detail={"message": str(exc), "type": "authentication_error"})
    if isinstance(exc, RequestError) and exc.status == 501:
        return HTTPException(501, detail={"message": exc.message or str(exc), "type": "unsupported_feature"})
    return HTTPException(502, detail={"message": str(exc), "type": "upstream_error"})


def _unsupported(message: str) -> HTTPException:
    return HTTPException(501, detail={"message": message, "type": "unsupported_feature"})


def _client_is_pure_http(client: AIStudioClient) -> bool:
    return bool(getattr(client, "is_pure_http", False))


def _pure_http_streaming_message() -> str:
    return "Pure HTTP mode is experimental and does not support streaming; disable AISTUDIO_USE_PURE_HTTP or use browser mode"


def _exception_message(exc: BaseException) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail)
        return str(detail)
    if isinstance(exc, RequestError) and exc.message:
        return exc.message
    return str(exc)


def _openai_stream_error_detail(exc: BaseException) -> tuple[str, str, str | None]:
    message = _exception_message(exc)
    if isinstance(exc, RequestError) and exc.status == 501:
        return message, "unsupported_feature", "unsupported_feature"
    if isinstance(exc, AuthError):
        return message, "authentication_error", "authentication_error"
    if isinstance(exc, UsageLimitExceeded):
        return message, "rate_limit_exceeded", "rate_limit_exceeded"
    if isinstance(exc, AistudioError):
        return message, "upstream_error", "upstream_error"
    return message, "server_error", "server_error"


def _gemini_stream_error_payload(exc: BaseException) -> dict[str, Any]:
    message = _exception_message(exc)
    if isinstance(exc, RequestError) and exc.status == 501:
        return {"error": {"code": 501, "message": message, "status": "UNIMPLEMENTED"}}
    if isinstance(exc, AuthError):
        return {"error": {"code": 401, "message": message, "status": "UNAUTHENTICATED"}}
    if isinstance(exc, UsageLimitExceeded):
        return {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED"}}
    if isinstance(exc, AistudioError):
        return {"error": {"code": 502, "message": message, "status": "BAD_GATEWAY"}}
    return {"error": {"code": 500, "message": message, "status": "INTERNAL"}}


def _is_native_worker_unavailable(exc: RequestError) -> bool:
    return exc.status == 503 and "native UI worker unavailable" in (exc.message or "")


def _usage_limit_is_quota_exhausted(exc: UsageLimitExceeded) -> bool:
    message = _exception_message(exc).lower()
    markers = (
        "you exceeded your current quota",
        "current quota",
        "quota for the day",
        "daily quota",
        "quota resets",
        "resource_exhausted",
        "rate-limits",
    )
    return any(marker in message for marker in markers)


def _rate_limit_account_kwargs(exc: UsageLimitExceeded | None) -> dict[str, Any]:
    if exc is None or not _usage_limit_is_quota_exhausted(exc):
        return {}
    cooldown_seconds = max(1, int(getattr(settings, "account_quota_exhausted_cooldown_seconds", 21600)))
    return {
        "cooldown_seconds": cooldown_seconds,
        "quota_exhausted": True,
        "reason": "upstream quota is exhausted; account is paused until quota resets",
    }


async def _request_disconnected(request: Request | None) -> bool:
    if request is None:
        return False
    try:
        return await request.is_disconnected()
    except Exception:
        return False


async def _close_async_iterator(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


def _merge_generation_overrides(*overrides: dict | None) -> dict | None:
    merged: dict[str, Any] = {}
    for override in overrides:
        if override:
            merged.update(override)
    return merged or None


def _schema_from_json_schema_format(response_format: dict[str, Any]) -> dict[str, Any]:
    schema_payload = response_format.get("json_schema")
    if schema_payload is not None:
        if not isinstance(schema_payload, dict):
            raise ValueError("response_format.json_schema must be an object")
        schema = schema_payload.get("schema")
        if isinstance(schema, dict):
            return schema
        if any(key in schema_payload for key in ("type", "properties", "$schema", "items")):
            return schema_payload
        raise ValueError("response_format.json_schema.schema must be an object")

    schema = response_format.get("schema")
    if isinstance(schema, dict):
        return schema
    raise ValueError("response_format json_schema requires a schema object")


def _response_format_overrides(response_format: dict[str, Any] | str | None) -> dict | None:
    if response_format is None:
        return None
    if isinstance(response_format, str):
        response_type = response_format.strip().lower()
        if response_type in ("", "text"):
            return None
        if response_type == "json_object":
            return {"response_mime_type": "application/json"}
        raise ValueError("response_format must be 'text', 'json_object', or an object")
    if not isinstance(response_format, dict):
        raise ValueError("response_format must be a JSON object")
    if "type" not in response_format and isinstance(response_format.get("format"), dict):
        return _response_format_overrides(response_format["format"])

    response_type = str(response_format.get("type") or "text").lower()
    if response_type == "text":
        return None
    if response_type == "json_object":
        return {"response_mime_type": "application/json"}
    if response_type == "json_schema":
        schema = _schema_from_json_schema_format(response_format)
        return {"response_mime_type": "application/json", "response_schema": encode_schema_to_wire(schema)}
    raise ValueError("response_format.type must be one of: text, json_object, json_schema")


def _validate_chat_request(req: ChatRequest) -> dict | None:
    validate_number_range("temperature", req.temperature, minimum=0, maximum=2)
    validate_number_range("top_p", req.top_p, minimum=0, maximum=1)
    validate_number_range("top_k", req.top_k, minimum=1, integer=True)
    validate_number_range("max_tokens", req.max_tokens, minimum=1, integer=True)
    return _response_format_overrides(req.response_format)


def _validate_image_model_chat_options(req: ChatRequest) -> None:
    unsupported = []
    if req.temperature is not None:
        unsupported.append("temperature")
    if req.top_p is not None:
        unsupported.append("top_p")
    if req.top_k is not None:
        unsupported.append("top_k")
    if req.max_tokens is not None:
        unsupported.append("max_tokens")
    if req.tools:
        unsupported.append("tools")
    if req.grounding:
        unsupported.append("grounding")
    if _explicit_thinking_enabled(req.thinking):
        unsupported.append("thinking")
    if req.safety_off:
        unsupported.append("safety_off")
    if req.response_format is not None:
        unsupported.append("response_format")
    if unsupported:
        fields = ", ".join(unsupported)
        raise ValueError(f"Image generation models do not support chat field(s): {fields}")


def _explicit_thinking_enabled(value: str | bool | None) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return value.lower() not in ("off", "false", "0", "none")


def _thinking_enabled(value: str | bool | None) -> bool:
    if value is None:
        return True
    return _explicit_thinking_enabled(value)


def _thinking_overrides(value: str | bool | None) -> dict | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = value.lower()
    if normalized in ("off", "false", "0", "none"):
        return None
    level = THINKING_LEVELS.get(normalized)
    if level is None:
        raise ValueError("thinking must be one of: off, low, medium, high")
    return {"thinking_config": AistudioThinkingConfig(level).to_wire(), "request_flag": 1}


def _chat_max_tokens_for_gateway(req: ChatRequest, *, enable_thinking: bool, capabilities) -> int | None:
    max_tokens = req.max_tokens
    if enable_thinking and capabilities.thinking and max_tokens is not None and max_tokens < MIN_THINKING_CHAT_MAX_TOKENS:
        return MIN_THINKING_CHAT_MAX_TOKENS
    return max_tokens


def _style_template_for(template_id: str) -> dict[str, str]:
    template = IMAGE_STYLE_TEMPLATES.get((template_id or "none").strip())
    if template is None:
        supported = ", ".join(IMAGE_STYLE_TEMPLATES)
        raise ValueError(f"style_template must be one of: {supported}")
    return template


def _optimizer_system_prompt() -> str:
    return (
        "You are an expert prompt optimizer for image generation models. "
        "Return exactly three optimized prompt options as JSON matching the requested schema. "
        "Each option must preserve the user's intent, make the visual scene concrete, and be directly usable as an image prompt. "
        "Write titles and special notes in Chinese. Write optimized prompts in the user's language unless technical visual modifiers are clearer in English. "
        "Do not include markdown, explanations, or extra keys."
    )


def _optimizer_user_prompt(raw_prompt: str, style_template_id: str, style_template: dict[str, str], image_count: int = 0) -> str:
    style_hint = style_template.get("prompt_hint") or "No fixed style template. Preserve the original style direction."
    reference_note = ""
    if image_count:
        reference_note = f"\n参考素材: 用户同时提供了 {image_count} 张图片素材。请结合这些图片的主体、构图、色彩、材质和风格约束优化提示词，不要生成与参考图脱节的内容。\n"
    return (
        f"原始提示词:\n{raw_prompt}\n\n"
        f"风格模板: {style_template['label']} ({style_template_id})\n"
        f"模板说明: {style_template['description']}\n"
        f"模板提示: {style_hint}\n"
        f"{reference_note}\n"
        "请输出 3 个优化版本:\n"
        "1. 一个强调主体与构图的稳定版本。\n"
        "2. 一个强调光线、材质与质感的精修版本。\n"
        "3. 一个强调氛围、镜头感或创意变化的版本。\n"
        "每个版本都必须包含 title、special、prompt。"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("optimizer response was not valid JSON") from None
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("optimizer response JSON must be an object")
    return data


def _normalize_prompt_optimization_options(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_options = payload.get("options")
    if not isinstance(raw_options, list) or len(raw_options) != 3:
        raise ValueError("optimizer response must include exactly 3 options")
    options: list[dict[str, str]] = []
    for index, item in enumerate(raw_options, start=1):
        if not isinstance(item, dict):
            raise ValueError("optimizer response options must be objects")
        title = str(item.get("title") or f"版本 {index}").strip()
        special = str(item.get("special") or item.get("note") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("optimizer response option prompt is required")
        options.append({"title": title, "special": special, "prompt": prompt})
    return options


async def handle_image_prompt_optimization(req: ImagePromptOptimizationRequest, client: AIStudioClient) -> dict[str, Any]:
    raw_prompt = req.prompt.strip()
    if not raw_prompt:
        raise _bad_request("prompt is required", "invalid_request_error")
    try:
        style_template = _style_template_for(req.style_template)
        capabilities = get_model_capabilities(req.model, strict=True)
    except ValueError as exc:
        raise _bad_request(str(exc), "invalid_request_error") from exc
    if not capabilities.text_output or capabilities.image_output:
        raise _bad_request(f"Model '{req.model}' must be a text prompt optimization model", "invalid_request_error")
    images = req.images or []
    if images and not capabilities.image_input:
        raise _bad_request(f"Model '{req.model}' does not support image input for prompt optimization", "invalid_request_error")
    thinking = req.thinking
    if not capabilities.thinking:
        thinking = "off"
    user_content: str | list[dict[str, Any]]
    user_prompt = _optimizer_user_prompt(raw_prompt, req.style_template, style_template, len(images))
    if images:
        user_content = [{"type": "text", "text": user_prompt}]
        for image in images:
            user_content.append({"type": "image_url", "image_url": {"url": _image_reference_url(image)}})
    else:
        user_content = user_prompt

    chat_req = ChatRequest(
        model=capabilities.id,
        messages=[
            {"role": "system", "content": _optimizer_system_prompt()},
            {"role": "user", "content": user_content},
        ],
        thinking=thinking,
        temperature=0.8,
        response_format={"type": "json_schema", "json_schema": {"schema": IMAGE_PROMPT_OPTIMIZATION_SCHEMA}},
    )
    if not capabilities.structured_output:
        chat_req.response_format = None

    chat_response = await handle_chat(chat_req, client)
    content = _chat_text(chat_response)
    try:
        options = _normalize_prompt_optimization_options(_extract_json_object(content))
    except ValueError as exc:
        raise HTTPException(502, detail={"message": str(exc), "type": "upstream_error"}) from exc
    return {
        "object": "image_prompt_optimization",
        "model": capabilities.id,
        "style_template": req.style_template,
        "style_label": style_template["label"],
        "options": options,
        "usage": chat_response.get("usage"),
    }


def _merge_usage(total: dict, usage: dict | None) -> dict:
    if not usage:
        return total
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        total_details = total.setdefault("completion_tokens_details", {})
        for key, value in completion_details.items():
            if isinstance(value, int):
                total_details[key] = total_details.get(key, 0) + value
    return total


def _normalize_image_response_format(response_format: str | None) -> str:
    normalized = (response_format or DEFAULT_IMAGE_RESPONSE_FORMAT).strip().lower()
    if normalized not in IMAGE_RESPONSE_FORMATS:
        supported = ", ".join(IMAGE_RESPONSE_FORMATS)
        raise ValueError(f"response_format must be one of: {supported}")
    return normalized


def _validate_unsupported_image_fields(req: ImageRequest) -> None:
    unsupported = [field for field in IMAGE_UNSUPPORTED_OPENAI_FIELDS if getattr(req, field, None) is not None]
    if not unsupported:
        return
    fields = ", ".join(unsupported)
    supported = "prompt, model, n, size, response_format, images, timeout"
    ignored = ", ".join(IMAGE_IGNORED_OPENAI_FIELDS)
    raise ValueError(
        f"Unsupported image generation field(s): {fields}. Supported fields: {supported}. "
        f"Compatibility-only ignored field(s): {ignored}"
    )


def _image_data_url(mime_type: str | None, b64: str) -> str:
    mime = mime_type or "image/png"
    return f"data:{mime};base64,{b64}"


def _format_image_items(items: list[dict[str, Any]], response_format: str) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    for item in items:
        if response_format == "url":
            data.append(
                {
                    "url": item["url"],
                    "b64_json": item["b64_json"],
                    "revised_prompt": item["revised_prompt"],
                    "id": item.get("id"),
                    "path": item.get("path"),
                    "delete_url": item.get("delete_url"),
                    "mime_type": item.get("mime_type"),
                    "size_bytes": item.get("size_bytes"),
                }
            )
        else:
            data.append(
                {
                    "b64_json": item["b64_json"],
                    "revised_prompt": item["revised_prompt"],
                    "url": item.get("url"),
                    "id": item.get("id"),
                    "path": item.get("path"),
                    "delete_url": item.get("delete_url"),
                    "mime_type": item.get("mime_type"),
                    "size_bytes": item.get("size_bytes"),
                }
            )
    return data


def _image_chat_content(data: list[dict[str, Any]]) -> str:
    lines = []
    for index, item in enumerate(data, start=1):
        image_url = item.get("url")
        if not image_url and item.get("b64_json"):
            image_url = _image_data_url("image/png", item["b64_json"])
        if image_url:
            lines.append(f"![generated image {index}]({image_url})")
    return "\n\n".join(lines)


def _image_reference_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    url = getattr(value, "url", None)
    if isinstance(url, str):
        return url
    raise ValueError("images[] must be a data URI, HTTP URL, or object with url")


def _image_request_images_to_files(images: list[Any] | None) -> list[str]:
    if not images:
        return []
    if len(images) > MAX_IMAGE_EDIT_INPUTS:
        raise ValueError(f"images supports at most {MAX_IMAGE_EDIT_INPUTS} items")

    paths: list[str] = []
    tmp_dir = settings.tmp_dir
    try:
        for index, image in enumerate(images):
            url = _image_reference_url(image).strip()
            if not url:
                raise ValueError(f"images[{index}].url is required")
            if url.startswith("data:"):
                paths.append(data_uri_to_file(url, tmp_dir=tmp_dir))
            elif url.startswith("http://") or url.startswith("https://"):
                paths.append(url_to_file(url, tmp_dir=tmp_dir))
            else:
                raise ValueError(f"images[{index}].url must be a data URI or HTTP URL")
        return paths
    except Exception:
        cleanup_files(paths)
        raise


def _is_image_output_chat_model(model: str) -> bool:
    try:
        return get_model_capabilities(model, strict=True).image_output
    except ValueError:
        return False


def _chat_image_request(req: ChatRequest) -> tuple[ImageRequest, list[str]]:
    normalized = normalize_chat_request(req.messages, req.model)
    cleanup_paths = list(normalized["cleanup_paths"])
    if normalized.get("file_input_mime_types"):
        cleanup_files(cleanup_paths)
        raise ValueError("Image generation through chat completions supports text prompts only")
    if normalized["capture_images"]:
        cleanup_files(cleanup_paths)
        raise ValueError("Image generation through chat completions supports text prompts only")
    return (
        ImageRequest(
            prompt=normalized["capture_prompt"],
            model=normalized["model"],
            n=DEFAULT_IMAGE_N,
            size=DEFAULT_IMAGE_SIZE,
            response_format="url",
        ),
        cleanup_paths,
    )


def _validate_image_request(req: ImageRequest):
    _validate_unsupported_image_fields(req)
    if not req.prompt or not req.prompt.strip():
        raise ValueError("prompt is required")
    if req.n < IMAGE_N_MIN:
        raise ValueError(f"n must be at least {IMAGE_N_MIN}")
    if req.n > IMAGE_N_MAX:
        raise ValueError(f"n must be {IMAGE_N_MAX} or less")
    validate_number_range("timeout", req.timeout, minimum=1, integer=True)
    response_format = _normalize_image_response_format(req.response_format)
    return plan_image_generation(req.model, req.size), response_format


def _record_account_result(
    account_id: str | None,
    event: str,
    *,
    image_size: str | None = None,
    image_count: int = 1,
    rate_limit_error: UsageLimitExceeded | None = None,
) -> None:
    rotator = runtime_state.rotator
    if rotator is None or not account_id:
        return
    if event == "success":
        rotator.record_success(account_id, image_size=image_size, image_count=image_count)
    elif event == "rate_limited":
        rotator.record_rate_limited(account_id, **_rate_limit_account_kwargs(rate_limit_error))
    elif event == "errors":
        rotator.record_error(account_id)


def _record_request_result(
    model: str,
    event: str,
    usage: dict | None = None,
    *,
    account_id: str | None = None,
    image_size: str | None = None,
    image_count: int = 1,
    rate_limit_error: UsageLimitExceeded | None = None,
) -> None:
    runtime_state.record(model, event, usage, image_size=image_size, image_count=image_count)
    _record_account_result(
        account_id,
        event,
        image_size=image_size,
        image_count=image_count,
        rate_limit_error=rate_limit_error,
    )


def _account_id_for_stats(account_context: RequestAccountContext | None = None) -> str | None:
    if account_context is not None:
        return account_context.account_id
    account_service = runtime_state.account_service
    active_account = account_service.get_active_account() if account_service is not None else None
    return active_account.id if active_account is not None else None


def _clear_client_capture_state(client: AIStudioClient) -> None:
    clear_capture_state = getattr(client, "clear_capture_state", None)
    if callable(clear_capture_state):
        clear_capture_state()
        return
    clear_snapshot_cache = getattr(client, "clear_snapshot_cache", None)
    if callable(clear_snapshot_cache):
        clear_snapshot_cache()


async def _request_replacement_account_context(
    *,
    fallback_client: AIStudioClient,
    model: str,
    account_context: RequestAccountContext | None,
    affinity_key: str | None,
) -> RequestAccountContext | None:
    if account_context is None or account_context.account_id is None:
        return None
    if getattr(runtime_state, "account_client_pool", None) is None or runtime_state.rotator is None:
        return None

    failed_account_id = account_context.account_id
    try:
        replacement_context = await _request_account_context(
            fallback_client,
            model,
            exclude_account_id=failed_account_id,
            affinity_key=affinity_key,
        )
    except HTTPException as exc:
        logger.warning("No replacement account available after stream auth error: %s", exc.detail)
        return None
    if replacement_context.account_id is None:
        return None
    await account_context.release()
    return replacement_context


def _is_empty_image_response(exc: RequestError) -> bool:
    return exc.status == 0 and "no image data" in exc.message.lower()


def _is_transient_replay_network_error(exc: RequestError) -> bool:
    if exc.status != 0:
        return False
    message = (exc.message or "").lower()
    transient_markers = (
        "enetunreach",
        "ehostunreach",
        "econnreset",
        "econnrefused",
        "etimedout",
        "socket hang up",
        "socket closed",
        "connection reset",
        "connection refused",
        "connection closed",
        "connect timeout",
        "timed out",
        "timeout",
        "net::err",
    )
    replay_markers = ("apirequestcontext", "connect ", "fetch failed", "network", "socket", "timeout", "timed out")
    return any(marker in message for marker in transient_markers) and any(marker in message for marker in replay_markers)


def _hash_affinity(*parts: Any) -> str | None:
    text = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    if not text or text == "null":
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chat_affinity_key(req: ChatRequest) -> str | None:
    if req.user:
        return _hash_affinity("chat-user", req.model, req.user)
    messages = [message.model_dump(mode="json") for message in req.messages]
    first_user = next((message for message in messages if message.get("role") == "user"), messages[0] if messages else None)
    return _hash_affinity("chat", req.model, first_user)


def _image_affinity_key(req: ImageRequest) -> str | None:
    return _hash_affinity("image", req.model, req.user or req.prompt[:256])


def _gemini_affinity_key(model_path: str, req: GeminiGenerateContentRequest) -> str | None:
    contents = [content.model_dump(mode="json") for content in req.contents]
    first_user = next((content for content in contents if content.get("role") in (None, "user")), contents[0] if contents else None)
    return _hash_affinity("gemini", model_path, first_user)


async def _request_account_context(
    fallback_client: AIStudioClient,
    model: str | None,
    *,
    require_preferred: bool = False,
    exclude_account_id: str | None = None,
    affinity_key: str | None = None,
) -> RequestAccountContext:
    rotator = runtime_state.rotator
    pool = getattr(runtime_state, "account_client_pool", None)
    if rotator is None:
        return RequestAccountContext(client=fallback_client)
    if pool is None:
        await _ensure_account_for_model(model)
        account_service = runtime_state.account_service
        active_account = account_service.get_active_account() if account_service is not None else None
        return RequestAccountContext(client=fallback_client, account=active_account)
    lease = await rotator.acquire_account(
        model,
        require_preferred=require_preferred,
        exclude_account_id=exclude_account_id,
        affinity_key=affinity_key,
    )
    if lease is None:
        if getattr(rotator, "has_accounts", lambda: False)():
            raise HTTPException(503, detail={"message": "No available account", "type": "service_unavailable"})
        return RequestAccountContext(client=fallback_client)
    account_client = await pool.get_client(lease.account.id)
    if account_client is None:
        await lease.release()
        return RequestAccountContext(client=fallback_client)
    return RequestAccountContext(client=account_client, account=lease.account, lease=lease)


async def _try_switch_account(
    model: str | None = None,
    *,
    require_preferred: bool = False,
    exclude_account_id: str | None = None,
) -> bool:
    """尝试切换到下一个可用账号。返回是否成功切换。"""
    rotator = runtime_state.rotator
    if rotator is None:
        return False

    # 获取下一个账号
    next_account = await rotator.get_next_account(
        model,
        require_preferred=require_preferred,
        exclude_account_id=exclude_account_id,
    )
    if next_account is None:
        return False

    account_service = runtime_state.account_service
    client = runtime_state.client

    if not all([account_service, client]):
        return False

    # 切换账号时清掉 snapshot，避免复用旧页面态。
    result = await account_service.activate_account(
        next_account.id,
        client,
        runtime_state.snapshot_cache,
        None,  # skip lock — caller already holds it
        keep_snapshot_cache=False,
    )
    if result is not None:
        logger.info("Account switched for model=%s reason=%s", model or "<any>", getattr(rotator, "last_selection_reason", None))
    return result is not None


async def _ensure_account_for_model(model: str | None) -> None:
    account_service = runtime_state.account_service
    rotator = runtime_state.rotator
    if account_service is None or rotator is None:
        return

    active = account_service.get_active_account()
    if active is None or getattr(active, "is_isolated", False):
        await _try_switch_account(model)
        return

    if rotator.model_prefers_premium(model) and not getattr(active, "is_premium", False):
        if rotator.has_available_preferred_account(model):
            await _try_switch_account(model, require_preferred=True)
            return
        logger.warning("Premium-preferred model is using a non-premium account because no healthy Pro/Ultra account is available")


def health_response() -> dict:
    busy_lock = runtime_state.busy_lock
    return {
        "status": "ok",
        "busy": busy_lock.locked() if busy_lock else False,
        "warmup": {
            "status": runtime_state.warmup_status,
            "target_accounts": list(runtime_state.warmup_target_accounts),
            "completed_accounts": list(runtime_state.warmup_completed_accounts),
            "failed_accounts": list(runtime_state.warmup_failed_accounts),
        },
    }


def stats_response() -> dict:
    stats = dict(runtime_state.model_stats)
    image_sizes: dict[str, int] = {}
    for value in stats.values():
        for size, count in (value.get("image_sizes") or {}).items():
            image_sizes[size] = image_sizes.get(size, 0) + count
    totals = {
        "requests": sum(s.get("requests", 0) for s in stats.values()),
        "success": sum(s.get("success", 0) for s in stats.values()),
        "rate_limited": sum(s.get("rate_limited", 0) for s in stats.values()),
        "errors": sum(s.get("errors", 0) for s in stats.values()),
        "prompt_tokens": sum(s.get("prompt_tokens", 0) for s in stats.values()),
        "completion_tokens": sum(s.get("completion_tokens", 0) for s in stats.values()),
        "total_tokens": sum(s.get("total_tokens", 0) for s in stats.values()),
        "image_sizes": image_sizes,
        "image_total": sum(image_sizes.values()),
    }
    return {"models": stats, "totals": totals}


def _coerce_openai_content_blocks(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("message content must be a string or content block array")
    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            blocks.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            raise ValueError("message content blocks must be objects")
        block_type = str(block.get("type") or "text")
        if block_type in ("text", "input_text", "output_text"):
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("text content blocks require text")
            blocks.append({"type": "text", "text": text})
        elif block_type in ("image_url", "input_image"):
            image_url = block.get("image_url") or block.get("url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if not isinstance(image_url, str) or not image_url:
                raise ValueError("image content blocks require image_url")
            blocks.append({"type": "image_url", "image_url": {"url": image_url}})
        elif block_type == "image":
            source = block.get("source")
            if not isinstance(source, dict):
                raise ValueError("image content blocks require source")
            source_type = source.get("type")
            if source_type == "base64":
                media_type = source.get("media_type") or "image/png"
                data = source.get("data")
                if not isinstance(data, str) or not data:
                    raise ValueError("image source.data is required")
                blocks.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}})
            elif source_type == "url":
                url = source.get("url")
                if not isinstance(url, str) or not url:
                    raise ValueError("image source.url is required")
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            else:
                raise ValueError("image source.type must be base64 or url")
        elif block_type in ("tool_result", "input_tool_result"):
            content_value = block.get("content", "")
            if isinstance(content_value, str):
                blocks.append({"type": "text", "text": content_value})
            elif isinstance(content_value, list):
                blocks.extend(_coerce_openai_content_blocks(content_value))
            else:
                blocks.append({"type": "text", "text": json.dumps(content_value, ensure_ascii=False)})
        elif block_type in ("tool_use", "server_tool_use"):
            name = block.get("name") or "unknown"
            tool_input = block.get("input") if block.get("input") is not None else {}
            blocks.append({"type": "text", "text": f"Tool use requested: {name} {json.dumps(tool_input, ensure_ascii=False)}"})
        elif block_type in ("file", "input_file"):
            file_data = block.get("file_data") or block.get("data")
            if not isinstance(file_data, str) or not file_data:
                raise ValueError("file content blocks require file_data")
            blocks.append(
                {
                    "type": "file",
                    "file": {
                        "file_data": file_data,
                        "filename": block.get("filename") or block.get("name") or "upload",
                        "mime_type": block.get("mime_type") or block.get("media_type") or "application/octet-stream",
                    },
                }
            )
        else:
            raise ValueError(f"unsupported content block type: {block_type}")
    return blocks


def _messages_from_responses_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    function_names_by_call_id: dict[str, str] = {}
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    input_value = payload.get("input")
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
        return messages
    if not isinstance(input_value, list) or not input_value:
        raise ValueError("input must be a non-empty string or message array")

    for item in input_value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            raise ValueError("input array items must be strings or objects")
        item_type = str(item.get("type") or "message")
        if item_type == "message" or "role" in item:
            role = str(item.get("role") or "user")
            messages.append({"role": role, "content": _coerce_openai_content_blocks(item.get("content", ""))})
        elif item_type in ("input_text", "text"):
            text = item.get("text")
            if not isinstance(text, str):
                raise ValueError("input_text items require text")
            messages.append({"role": "user", "content": text})
        elif item_type == "input_image":
            messages.append({"role": "user", "content": _coerce_openai_content_blocks([item])})
        elif item_type in ("input_file", "file"):
            messages.append({"role": "user", "content": _coerce_openai_content_blocks([item])})
        elif item_type == "function_call":
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("function_call items require name")
            arguments = item.get("arguments") if item.get("arguments") is not None else {}
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id:
                function_names_by_call_id[call_id] = name
            messages.append({"role": "assistant", "content": _responses_function_call_text(name, _parse_tool_arguments(arguments))})
        elif item_type in ("function_call_output", "tool_result", "input_tool_result"):
            output = item.get("output") if "output" in item else item.get("content", "")
            call_id = item.get("call_id")
            name = item.get("name") if isinstance(item.get("name"), str) else None
            if name is None and isinstance(call_id, str):
                name = function_names_by_call_id.get(call_id)
            messages.append({"role": "tool", "content": _responses_function_output_text(output, name=name)})
        elif item_type == "reasoning":
            continue
        else:
            raise ValueError(f"unsupported input item type: {item_type}")
    return messages


def _responses_function_call_text(name: str, arguments: Any) -> str:
    return f"Tool call requested: {name} {_json_text(arguments)}"


def _responses_function_output_text(output: Any, *, name: str | None) -> str:
    prefix = f"Tool result for {name}:" if name else "Tool result:"
    return f"{prefix} {_json_text(output)}"


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _chat_text(chat_response: dict[str, Any]) -> str:
    message = _chat_message(chat_response)
    content = message.get("content", "")
    return content if isinstance(content, str) else ""


def _chat_message(chat_response: dict[str, Any]) -> dict[str, Any]:
    choices = chat_response.get("choices") if isinstance(chat_response, dict) else None
    if not choices:
        return {}
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    return message if isinstance(message, dict) else {}


def _chat_tool_calls(chat_response: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = _chat_message(chat_response).get("tool_calls")
    return tool_calls if isinstance(tool_calls, list) else []


def _tool_names_from_payload(tools: Any) -> set[str]:
    if not isinstance(tools, list):
        return set()
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str) and function["name"]:
            names.add(function["name"])
            continue
        name = tool.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _chat_thinking(chat_response: dict[str, Any]) -> str:
    message = _chat_message(chat_response)
    for key in ("thinking", "reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _parse_tool_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments if arguments is not None else {}


def _parse_responses_text_tool_request(text: str, allowed_tool_names: set[str]) -> dict[str, Any] | None:
    if not allowed_tool_names:
        return None
    prefix = "Tool call requested: "
    if text.startswith(prefix):
        payload = text[len(prefix) :].strip()
        if not payload:
            return None
        name, separator, arguments = payload.partition(" ")
        if not separator or name not in allowed_tool_names:
            return None
        arguments = arguments.strip()
        if not arguments:
            return None
        try:
            json.loads(arguments)
        except json.JSONDecodeError:
            return None
        return {"name": name, "arguments": arguments}
    return _parse_responses_dalle_text_tool_request(text, allowed_tool_names)


def _parse_responses_dalle_text_tool_request(text: str, allowed_tool_names: set[str]) -> dict[str, Any] | None:
    if "image_generation" not in allowed_tool_names:
        return None
    payload = _parse_tool_arguments(text.strip())
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    if not isinstance(action, str) or action.strip().lower() != "dalle.text2im":
        return None
    action_input = _parse_tool_arguments(payload.get("action_input"))
    if not isinstance(action_input, dict):
        action_input = {}
    arguments: dict[str, str] = {}
    for key in ("prompt", "size", "model"):
        value = action_input.get(key, payload.get(key))
        if isinstance(value, str) and value.strip():
            arguments[key] = value.strip()
    if "prompt" not in arguments:
        return None
    return {"name": "image_generation", "arguments": json.dumps(arguments, ensure_ascii=False)}


def _may_be_responses_dalle_text_tool_request(text: str, allowed_tool_names: set[str]) -> bool:
    if "image_generation" not in allowed_tool_names:
        return False
    compact = "".join(text.strip().split())
    if not compact:
        return False
    action_prefix = '{"action"'
    if action_prefix.startswith(compact):
        return True
    if not compact.startswith('{"action":'):
        return False
    _prefix, _separator, value_fragment = compact.partition('{"action":')
    if not value_fragment or value_fragment == '"':
        return True
    if not value_fragment.startswith('"'):
        return False
    action_fragment = value_fragment[1:].split('"', 1)[0]
    return "dalle.text2im".startswith(action_fragment) or action_fragment == "dalle.text2im"


def _may_be_responses_text_tool_request(text: str, allowed_tool_names: set[str]) -> bool:
    if not text or not allowed_tool_names:
        return False
    if _may_be_responses_dalle_text_tool_request(text, allowed_tool_names):
        return True
    prefix = "Tool call requested: "
    if prefix.startswith(text):
        return True
    if not text.startswith(prefix):
        return False
    payload = text[len(prefix) :]
    name, separator, _arguments = payload.partition(" ")
    if not separator:
        return any(tool_name.startswith(name) for tool_name in allowed_tool_names)
    return name in allowed_tool_names


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _iter_sse_data_payloads(response: StreamingResponse):
    buffer = ""
    async for chunk in response.body_iterator:
        buffer += chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        while "\n\n" in buffer:
            raw_event, buffer = buffer.split("\n\n", 1)
            data_lines = [line[5:].strip() for line in raw_event.splitlines() if line.startswith("data:")]
            if data_lines:
                yield "\n".join(data_lines)
    data_lines = [line[5:].strip() for line in buffer.splitlines() if line.startswith("data:")]
    if data_lines:
        yield "\n".join(data_lines)


def _chat_request_uses_search(req: ChatRequest) -> bool:
    _tools, tools_use_search = normalize_openai_tools_and_search(req.tools)
    return bool(req.grounding or tools_use_search)


def _responses_web_search_item() -> dict[str, Any]:
    return {
        "id": f"ws_{uuid.uuid4().hex[:24]}",
        "type": "web_search_call",
        "status": "completed",
        "action": {"type": "search"},
    }


def _responses_reasoning_item(thinking: str, *, status: str = "completed") -> dict[str, Any]:
    return {
        "id": f"rs_{uuid.uuid4().hex[:24]}",
        "type": "reasoning",
        "status": status,
        "content": [{"type": "reasoning_text", "text": thinking}],
        "summary": [],
    }


def _responses_function_call_item(name: str, arguments: str, *, call_id: str | None = None) -> dict[str, Any]:
    return {
        "id": f"fc_{uuid.uuid4().hex[:24]}",
        "type": "function_call",
        "status": "completed",
        "call_id": call_id or f"call_{uuid.uuid4().hex[:24]}",
        "name": name,
        "arguments": arguments,
    }


def _responses_function_call_added_item(item: dict[str, Any]) -> dict[str, Any]:
    added_item = dict(item)
    added_item["status"] = "in_progress"
    added_item["arguments"] = ""
    return added_item


def _responses_output_items(
    chat_response: dict[str, Any],
    *,
    uses_search: bool = False,
    allowed_tool_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if uses_search:
        output.append(_responses_web_search_item())
    thinking = _chat_thinking(chat_response)
    if thinking:
        output.append(_responses_reasoning_item(thinking))
    text = _chat_text(chat_response)
    text_tool_request = _parse_responses_text_tool_request(text, allowed_tool_names or set())
    if text_tool_request:
        output.append(_responses_function_call_item(text_tool_request["name"], text_tool_request["arguments"]))
        return output
    if text:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for tool_call in _chat_tool_calls(chat_response):
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(function, dict):
            continue
        output.append(_responses_function_call_item(function.get("name") or "unknown", function.get("arguments") or "{}", call_id=tool_call.get("id")))
    if output:
        return output
    return [
        {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        }
    ]


def _message_content_blocks(chat_response: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    text = _chat_text(chat_response)
    if text:
        blocks.append({"type": "text", "text": text})
    for tool_call in _chat_tool_calls(chat_response):
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(function, dict):
            continue
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id"),
                "name": function.get("name") or "unknown",
                "input": _parse_tool_arguments(function.get("arguments")),
            }
        )
    return blocks or [{"type": "text", "text": ""}]


def _messages_tools_from_payload(tools: Any) -> Any:
    if not tools:
        return None
    if not isinstance(tools, list):
        raise ValueError("tools must be an array")
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("tools items must be objects")
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            converted.append(tool)
            continue
        if is_search_tool_type(tool.get("type")):
            converted.append({"type": tool.get("type")})
            continue
        if tool.get("type") not in (None, "function"):
            converted.append(tool)
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tools[].name is required")
        parameters = tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else None
        if parameters is None and isinstance(tool.get("parameters"), dict):
            parameters = tool.get("parameters")
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description") if isinstance(tool.get("description"), str) else None,
                    "parameters": parameters,
                },
            }
        )
    return converted or None


def _responses_image_generation_tool(tools: Any) -> dict[str, Any] | None:
    if not isinstance(tools, list):
        return None
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "image_generation":
            return tool
    return None


def _responses_image_generation_function_tool(tool: dict[str, Any]) -> dict[str, Any]:
    description = tool.get("description") if isinstance(tool.get("description"), str) else None
    return {
        "type": "function",
        "name": "image_generation",
        "description": description
        or "Generate or edit an image when the user explicitly asks for visual output. Ordinary text replies should not call this tool.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The prompt to send to the image model.",
                },
                "size": {
                    "type": "string",
                    "description": "Optional output size such as 1024x1024 or 1536x864.",
                },
            },
            "required": ["prompt"],
        },
    }


def _responses_tools_for_optional_image_generation(tools: Any) -> Any:
    if not isinstance(tools, list):
        return tools
    converted = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "image_generation":
            converted.append(_responses_image_generation_function_tool(tool))
        else:
            converted.append(tool)
    return converted


def _responses_tools_include_search(tools: Any) -> bool:
    return isinstance(tools, list) and any(isinstance(tool, dict) and is_search_tool_type(tool.get("type")) for tool in tools)


def _responses_search_tools_only(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    return [dict(tool) for tool in tools if isinstance(tool, dict) and is_search_tool_type(tool.get("type"))]


def _responses_image_tool_decision_instruction(tool: dict[str, Any]) -> str:
    default_size = tool.get("size") if isinstance(tool.get("size"), str) and tool.get("size") else "1024x1024"
    return (
        "Image generation tool selection protocol: If the latest user request explicitly asks to create or edit an image, "
        "respond with exactly one line in this format: "
        f"Tool call requested: image_generation {{\"prompt\":\"<complete image prompt>\",\"size\":\"{default_size}\"}}. "
        "Use web search context when it helps make the image prompt accurate. "
        "If the latest user request does not ask for visual output, answer normally and do not mention this protocol."
    )


def _responses_search_only_image_decision_payload(payload: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    tools = _responses_search_tools_only(payload.get("tools"))
    if tools:
        sanitized["tools"] = tools
    else:
        sanitized.pop("tools", None)
    instruction = _responses_image_tool_decision_instruction(tool)
    existing = sanitized.get("instructions")
    sanitized["instructions"] = f"{existing.strip()}\n\n{instruction}" if isinstance(existing, str) and existing.strip() else instruction
    return sanitized


def _responses_needs_search_image_decision_fallback(exc: HTTPException, payload: dict[str, Any]) -> bool:
    message = _exception_message(exc)
    return "include_server_side_tool_invocations" in message and _responses_tools_include_search(payload.get("tools"))


def _responses_optional_image_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    tools = _responses_tools_for_optional_image_generation(payload.get("tools"))
    if tools:
        sanitized["tools"] = tools
    else:
        sanitized.pop("tools", None)
    return sanitized


def _responses_toolless_payload(payload: dict[str, Any], skip_tool_type: str) -> dict[str, Any]:
    sanitized = dict(payload)
    tools = payload.get("tools")
    if isinstance(tools, list):
        remaining = [tool for tool in tools if not (isinstance(tool, dict) and tool.get("type") == skip_tool_type)]
        if remaining:
            sanitized["tools"] = remaining
        else:
            sanitized.pop("tools", None)
    return sanitized


def _image_size_for_google_provider(size: Any) -> str:
    raw_size = str(size or DEFAULT_IMAGE_SIZE).strip().lower()
    return {
        "1024x1024": "1024x1024",
        "1024x1536": "1024x1792",
        "1536x1024": "1792x1024",
        "1536x864": "1792x1024",
    }.get(raw_size, DEFAULT_IMAGE_SIZE)


def _responses_image_model_for_google_provider(tool: dict[str, Any]) -> str:
    model = str(tool.get("model") or "").strip().removeprefix("models/")
    if model:
        try:
            capabilities = get_model_capabilities(model, strict=True)
            if capabilities.image_output:
                return capabilities.id
        except ValueError:
            pass
    return DEFAULT_IMAGE_MODEL


def _responses_image_size_for_google_provider(tool: dict[str, Any], model: str) -> str:
    raw_size = str(tool.get("size") or DEFAULT_IMAGE_SIZE).strip().lower()
    capabilities = get_model_capabilities(model, strict=False)
    if raw_size in capabilities.image_sizes:
        return raw_size
    mapped_size = _image_size_for_google_provider(raw_size)
    if mapped_size in capabilities.image_sizes:
        return mapped_size
    default_size = DEFAULT_IMAGE_SIZE if DEFAULT_IMAGE_SIZE in capabilities.image_sizes else next(iter(capabilities.image_sizes), DEFAULT_IMAGE_SIZE)
    return default_size


def _file_path_to_image_data_url(path: str) -> str:
    mime_type = "image/jpeg"
    if path.endswith(".png"):
        mime_type = "image/png"
    elif path.endswith(".webp"):
        mime_type = "image/webp"
    with open(path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _responses_image_request(payload: dict[str, Any], tool: dict[str, Any], *, prompt_override: str | None = None) -> ImageRequest:
    image_payload = _responses_toolless_payload(payload, "image_generation")
    if prompt_override is not None:
        image_payload = dict(image_payload)
        image_payload["input"] = prompt_override
        image_payload.pop("instructions", None)
        image_payload.pop("tools", None)
    chat_req = ChatRequest(model=str(payload["model"]), messages=_messages_from_responses_payload(image_payload))
    normalized = normalize_chat_request(chat_req.messages, chat_req.model)
    try:
        images = [_file_path_to_image_data_url(path) for path in normalized["capture_images"]]
        image_model = _responses_image_model_for_google_provider(tool)
        return ImageRequest(
            prompt=normalized["capture_prompt"],
            model=image_model,
            n=1,
            size=_responses_image_size_for_google_provider(tool, image_model),
            response_format="b64_json",
            images=images or None,
        )
    finally:
        cleanup_files(normalized["cleanup_paths"])


def _responses_image_generation_output_items(image_response: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for image in image_response.get("data") if isinstance(image_response.get("data"), list) else []:
        if not isinstance(image, dict):
            continue
        item: dict[str, Any] = {
            "id": f"ig_{uuid.uuid4().hex[:24]}",
            "type": "image_generation_call",
            "status": "completed",
        }
        b64_value = image.get("b64_json")
        if b64_value:
            item["result"] = b64_value
        else:
            url_value = image.get("url")
            if url_value:
                item["url"] = url_value
        revised_prompt = image.get("revised_prompt")
        if revised_prompt:
            item["revised_prompt"] = revised_prompt
        item["mime_type"] = image.get("mime_type") or "image/png"
        items.append(item)
    return items


def _responses_stream_item_stub(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in {"result", "b64_json", "url"}}


def _responses_stream_response_stub(response: dict[str, Any]) -> dict[str, Any]:
    stub = dict(response)
    output = response.get("output")
    if isinstance(output, list):
        stub["output"] = [_responses_stream_item_stub(item) if isinstance(item, dict) else item for item in output]
    return stub


def _responses_image_tool_trace(*, model: str, size: str, uses_search: bool, search_thinking: str = "") -> str:
    parts: list[str] = []
    if uses_search:
        parts.append("Web search completed before image generation.")
    if search_thinking:
        parts.append(search_thinking)
    parts.append(f"Image generation tool selected {model} at {size}.")
    return "\n".join(part for part in parts if part).strip()


def _responses_image_tool_request(chat_response: dict[str, Any], allowed_tool_names: set[str]) -> dict[str, Any] | None:
    if "image_generation" not in allowed_tool_names:
        return None
    text_request = _parse_responses_text_tool_request(_chat_text(chat_response), allowed_tool_names)
    if text_request and text_request["name"] == "image_generation":
        return text_request
    for tool_call in _chat_tool_calls(chat_response):
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(function, dict):
            continue
        if function.get("name") != "image_generation":
            continue
        arguments = function.get("arguments")
        if arguments is None:
            arguments = "{}"
        return {"name": "image_generation", "arguments": arguments}
    return None


def _responses_image_tool_prompt_override(tool_request: dict[str, Any] | None) -> str | None:
    if not tool_request:
        return None
    arguments = _parse_tool_arguments(tool_request.get("arguments"))
    if isinstance(arguments, dict):
        prompt = arguments.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return prompt.strip()
    return None


def _responses_image_tool_with_arguments(tool: dict[str, Any], tool_request: dict[str, Any] | None) -> dict[str, Any]:
    if not tool_request:
        return tool
    arguments = _parse_tool_arguments(tool_request.get("arguments"))
    if not isinstance(arguments, dict):
        return tool
    selected = dict(tool)
    for key in ("size", "model"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            selected[key] = value.strip()
    return selected


def _responses_explicit_image_prompt_override(payload: dict[str, Any], tool: dict[str, Any], allowed_tool_names: set[str]) -> str | None:
    if "image_generation" not in allowed_tool_names:
        return None
    latest_text = ""
    for message in reversed(_messages_from_responses_payload(payload)):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            latest_text = content.strip()
            break
        if isinstance(content, list):
            parts = [str(block.get("text") or "").strip() for block in content if isinstance(block, dict) and str(block.get("type") or "") in {"text", "input_text"}]
            latest_text = "\n".join(part for part in parts if part).strip()
            if latest_text:
                break
    if not latest_text:
        return None
    normalized = latest_text.lower()
    negative_visual_intent = any(
        marker in normalized
        for marker in (
            "do not create an image",
            "do not generate an image",
            "don't create an image",
            "don't generate an image",
            "without creating an image",
            "without generating an image",
            "no image",
        )
    ) or any(marker in latest_text for marker in ("不要生成图片", "不要创建图片", "别生成图片", "不生成图片", "无需生成图片"))
    if negative_visual_intent:
        return None
    english_action = any(marker in normalized for marker in ("generate", "create", "draw", "make", "render", "illustrate"))
    english_visual = any(marker in normalized for marker in ("image", "picture", "photo", "icon", "infographic", "illustration"))
    chinese_action = any(marker in latest_text for marker in ("生成", "创建", "绘制", "画"))
    chinese_visual = any(marker in latest_text for marker in ("图片", "图像", "图标", "信息图", "插画", "照片"))
    if not ((english_action and english_visual) or (chinese_action and chinese_visual)):
        return None
    return latest_text


def _responses_google_image_fallback_models(current_model: str) -> list[str]:
    candidates = ["gemini-3-pro-image-preview", DEFAULT_IMAGE_MODEL]
    fallbacks: list[str] = []
    normalized_current = str(current_model or "").strip().removeprefix("models/")
    for candidate in candidates:
        if candidate and candidate != normalized_current and candidate not in fallbacks:
            fallbacks.append(candidate)
    return fallbacks


def _responses_should_retry_google_image_model(exc: HTTPException, tool: dict[str, Any], attempted_models: list[str]) -> bool:
    provider = str(tool.get("provider") or "").strip()
    requested_model = str(tool.get("model") or "").strip().removeprefix("models/")
    if provider != "google-ai-studio" and not requested_model.startswith("gemini-"):
        return False
    message = _exception_message(exc)
    if "Requested entity was not found" not in message and "HTTP 404" not in message:
        return False
    return any(model not in attempted_models for model in _responses_google_image_fallback_models(attempted_models[-1] if attempted_models else requested_model))


async def _responses_generate_image_with_fallback(
    payload: dict[str, Any],
    tool: dict[str, Any],
    client: AIStudioClient,
    *,
    prompt_override: str | None = None,
) -> tuple[ImageRequest, dict[str, Any]]:
    selected_tool = dict(tool)
    attempted_models: list[str] = []
    while True:
        image_request = _responses_image_request(payload, selected_tool, prompt_override=prompt_override)
        attempted_models.append(image_request.model)
        try:
            return image_request, await handle_image_generation(image_request, client)
        except HTTPException as exc:
            if not _responses_should_retry_google_image_model(exc, selected_tool, attempted_models):
                raise
            for fallback_model in _responses_google_image_fallback_models(image_request.model):
                if fallback_model not in attempted_models:
                    selected_tool = {**selected_tool, "model": fallback_model}
                    break
            else:
                raise


def _responses_chat_response_payload(
    payload: dict[str, Any],
    chat_response: dict[str, Any],
    *,
    uses_search: bool,
    allowed_tool_names: set[str],
) -> dict[str, Any]:
    text = _chat_text(chat_response)
    thinking = _chat_thinking(chat_response)
    text_tool_request = _parse_responses_text_tool_request(text, allowed_tool_names)
    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": chat_response.get("model", payload["model"]),
        "output": _responses_output_items(chat_response, uses_search=uses_search, allowed_tool_names=allowed_tool_names),
        "output_text": "" if text_tool_request else text,
        "thinking": thinking,
        "usage": chat_response.get("usage"),
    }


def _responses_decision_response_format(payload: dict[str, Any]) -> Any:
    response_format = payload.get("response_format")
    text_config = payload.get("text")
    if response_format is None and isinstance(text_config, dict):
        response_format = text_config.get("format")
    return response_format


def _responses_thinking_value(payload: dict[str, Any]) -> str | bool | None:
    direct = payload.get("thinking")
    if direct is not None:
        return direct
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    effort = reasoning.get("effort")
    if isinstance(effort, bool):
        return effort
    if isinstance(effort, str):
        normalized = effort.strip()
        return normalized or None
    return None


def _responses_image_decision_payload_and_tools(payload: dict[str, Any], tool: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    decision_payload = _responses_optional_image_payload(payload)
    allowed_tool_names = _tool_names_from_payload(decision_payload.get("tools"))
    if _responses_tools_include_search(payload.get("tools")):
        decision_payload = _responses_search_only_image_decision_payload(payload, tool)
    return decision_payload, allowed_tool_names


def _responses_decision_chat_request(payload: dict[str, Any], current_payload: dict[str, Any], *, stream: bool = False) -> ChatRequest:
    return ChatRequest(
        model=str(payload["model"]),
        messages=_messages_from_responses_payload(current_payload),
        stream=stream,
        temperature=payload.get("temperature"),
        top_p=payload.get("top_p"),
        max_tokens=payload.get("max_output_tokens") or payload.get("max_tokens"),
        tools=_messages_tools_from_payload(current_payload.get("tools")),
        thinking=_responses_thinking_value(payload),
        response_format=_responses_decision_response_format(payload),
    )


def _responses_image_tool_request_from_response(response: dict[str, Any], allowed_tool_names: set[str]) -> dict[str, Any] | None:
    if "image_generation" not in allowed_tool_names:
        return None
    output = response.get("output") if isinstance(response.get("output"), list) else []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call" or item.get("name") != "image_generation":
            continue
        return {"name": "image_generation", "arguments": item.get("arguments") or "{}"}
    return None


def _responses_text_from_parts(parts: Any) -> list[str]:
    values: list[str] = []
    if not isinstance(parts, list):
        return values
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
            values.append(part["text"])
    return values


def _responses_thinking_from_response(response: dict[str, Any]) -> str:
    direct = response.get("thinking")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    output = response.get("output") if isinstance(response.get("output"), list) else []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        parts.extend(_responses_text_from_parts(item.get("summary")))
        parts.extend(_responses_text_from_parts(item.get("content")))
    return "\n".join(part for part in parts if part).strip()


def _responses_image_final_output_items(response: dict[str, Any], image_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = response.get("output") if isinstance(response.get("output"), list) else []
    preserved = [dict(item) for item in output if isinstance(item, dict) and item.get("type") in {"web_search_call", "reasoning"}]
    preserved.extend(image_items)
    return preserved


async def _complete_responses_optional_image_generation(
    payload: dict[str, Any],
    tool: dict[str, Any],
    client: AIStudioClient,
    *,
    response_id: str | None = None,
    created_at: int | None = None,
) -> dict[str, Any]:
    decision_payload, allowed_tool_names = _responses_image_decision_payload_and_tools(payload, tool)
    chat_req = _responses_decision_chat_request(payload, decision_payload)
    uses_search = _chat_request_uses_search(chat_req)
    try:
        chat_response = await handle_chat(chat_req, client)
    except HTTPException as exc:
        if not _responses_needs_search_image_decision_fallback(exc, decision_payload):
            raise
        fallback_payload = _responses_search_only_image_decision_payload(payload, tool)
        chat_req = _responses_decision_chat_request(payload, fallback_payload)
        uses_search = _chat_request_uses_search(chat_req)
        chat_response = await handle_chat(chat_req, client)
    image_tool_request = _responses_image_tool_request(chat_response, allowed_tool_names)
    if image_tool_request is None:
        prompt_override = _responses_explicit_image_prompt_override(payload, tool, allowed_tool_names)
        if prompt_override:
            image_request, image_response = await _responses_generate_image_with_fallback(payload, tool, client, prompt_override=prompt_override)
            image_items = _responses_image_generation_output_items(image_response)
            if not image_items:
                raise HTTPException(502, detail={"message": "Image generation returned no image data", "type": "upstream_error"})
            output_items = []
            if uses_search:
                output_items.append(_responses_web_search_item())
            output_items.extend(image_items)
            return {
                "id": response_id or f"resp_{uuid.uuid4().hex[:24]}",
                "object": "response",
                "created_at": created_at or int(time.time()),
                "status": "completed",
                "model": payload["model"],
                "output": output_items,
                "output_text": "Generated image",
                "thinking": _responses_image_tool_trace(
                    model=image_request.model,
                    size=image_request.size,
                    uses_search=uses_search,
                    search_thinking=_chat_thinking(chat_response).strip(),
                ),
                "usage": _merge_usage(dict(chat_response.get("usage") or {}), image_response.get("usage")) or None,
            }
        response = _responses_chat_response_payload(payload, chat_response, uses_search=uses_search, allowed_tool_names=allowed_tool_names)
        if response_id is not None:
            response["id"] = response_id
        if created_at is not None:
            response["created_at"] = created_at
        return response

    prompt_override = _responses_image_tool_prompt_override(image_tool_request)
    selected_tool = _responses_image_tool_with_arguments(tool, image_tool_request)
    image_request, image_response = await _responses_generate_image_with_fallback(payload, selected_tool, client, prompt_override=prompt_override)
    image_items = _responses_image_generation_output_items(image_response)
    if not image_items:
        raise HTTPException(502, detail={"message": "Image generation returned no image data", "type": "upstream_error"})
    output_items = []
    if uses_search:
        output_items.append(_responses_web_search_item())
    output_items.extend(image_items)
    return {
        "id": response_id or f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": created_at or int(time.time()),
        "status": "completed",
        "model": payload["model"],
        "output": output_items,
        "output_text": "Generated image",
        "thinking": _responses_image_tool_trace(
            model=image_request.model,
            size=image_request.size,
            uses_search=uses_search,
            search_thinking=_chat_thinking(chat_response).strip(),
        ),
        "usage": _merge_usage(dict(chat_response.get("usage") or {}), image_response.get("usage")) or None,
    }


def _build_responses_image_generation_streaming_response(payload: dict[str, Any], tool: dict[str, Any], client: AIStudioClient, request: Request | None = None) -> StreamingResponse:
    async def stream_response():
        try:
            decision_payload, allowed_tool_names = _responses_image_decision_payload_and_tools(payload, tool)
            chat_req = _responses_decision_chat_request(payload, decision_payload, stream=True)
            uses_search = _chat_request_uses_search(chat_req)
            decision_stream = _build_responses_streaming_response(
                chat_req,
                client,
                uses_search=uses_search,
                allowed_tool_names=allowed_tool_names,
                request=request,
            )
            completed_response: dict[str, Any] | None = None

            async for data_payload in _iter_sse_data_payloads(decision_stream):
                if data_payload == "[DONE]":
                    continue
                try:
                    event = json.loads(data_payload)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type") or "") if isinstance(event, dict) else ""
                if event_type == "response.completed" and isinstance(event.get("response"), dict):
                    completed_response = event["response"]
                    continue
                if event_type:
                    yield _sse_event(event_type, event)

            if completed_response is None:
                yield "data: [DONE]\n\n"
                return

            image_tool_request = _responses_image_tool_request_from_response(completed_response, allowed_tool_names)
            if image_tool_request is None:
                yield _sse_event("response.completed", {"type": "response.completed", "response": completed_response})
                yield "data: [DONE]\n\n"
                return

            prompt_override = _responses_image_tool_prompt_override(image_tool_request)
            selected_tool = _responses_image_tool_with_arguments(tool, image_tool_request)
            image_request, image_response = await _responses_generate_image_with_fallback(payload, selected_tool, client, prompt_override=prompt_override)
            image_items = _responses_image_generation_output_items(image_response)
            if not image_items:
                raise HTTPException(502, detail={"message": "Image generation returned no image data", "type": "upstream_error"})

            output = completed_response.get("output") if isinstance(completed_response.get("output"), list) else []
            next_output_index = len(output)
            for index, item in enumerate(image_items, start=next_output_index):
                item_stub = _responses_stream_item_stub(item)
                yield _sse_event("response.output_item.added", {"type": "response.output_item.added", "output_index": index, "item": item_stub})
                if item.get("result"):
                    yield _sse_event(
                        "response.image_generation_call.partial_image",
                        {"type": "response.image_generation_call.partial_image", "item_id": item["id"], "output_index": index, "partial_image": item["result"]},
                    )
                yield _sse_event("response.output_item.done", {"type": "response.output_item.done", "output_index": index, "item": item_stub})

            final_response = dict(completed_response)
            final_response["status"] = "completed"
            final_response["output"] = _responses_image_final_output_items(completed_response, image_items)
            final_response["output_text"] = "Generated image"
            final_response["thinking"] = _responses_image_tool_trace(
                model=image_request.model,
                size=image_request.size,
                uses_search=uses_search,
                search_thinking=_responses_thinking_from_response(completed_response),
            )
            final_response["usage"] = _merge_usage(dict(completed_response.get("usage") or {}), image_response.get("usage")) or None
            yield _sse_event("response.completed", {"type": "response.completed", "response": _responses_stream_response_stub(final_response)})
            yield "data: [DONE]\n\n"
        except Exception as exc:
            message, error_type, code = _openai_stream_error_detail(exc)
            yield _sse_event("error", {"type": "error", "error": {"message": message, "type": error_type, "code": code}})
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _build_responses_streaming_response(
    chat_req: ChatRequest,
    client: AIStudioClient,
    *,
    uses_search: bool,
    allowed_tool_names: set[str],
    request: Request | None = None,
) -> StreamingResponse:
    async def stream_response():
        response_id = f"resp_{uuid.uuid4().hex[:24]}"
        created_at = int(time.time())
        response_model = chat_req.model
        response_base = {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": "in_progress",
            "model": response_model,
            "output": [],
        }
        text_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        thinking_item_id = f"rs_{uuid.uuid4().hex[:24]}"
        text_started = False
        thinking_started = False
        text_output_index: int | None = None
        thinking_output_index: int | None = None
        text_accumulator: list[str] = []
        thinking_accumulator: list[str] = []
        output_items: list[dict[str, Any]] = []
        final_usage = None
        next_output_index = 0

        def function_call_events(tool_request: dict[str, Any], output_index: int):
            item = _responses_function_call_item(tool_request["name"], tool_request["arguments"])
            output_items.append(item)
            yield _sse_event(
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": output_index, "item": _responses_function_call_added_item(item)},
            )
            yield _sse_event(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "delta": item["arguments"],
                },
            )
            yield _sse_event(
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "arguments": item["arguments"],
                },
            )
            yield _sse_event("response.output_item.done", {"type": "response.output_item.done", "output_index": output_index, "item": item})

        try:
            yield _sse_event("response.created", {"type": "response.created", "response": response_base})
            yield _sse_event("response.in_progress", {"type": "response.in_progress", "response": response_base})
            if uses_search:
                search_item = _responses_web_search_item()
                search_index = next_output_index
                next_output_index += 1
                output_items.append(search_item)
                yield _sse_event("response.output_item.added", {"type": "response.output_item.added", "output_index": search_index, "item": search_item})
                yield _sse_event("response.output_item.done", {"type": "response.output_item.done", "output_index": search_index, "item": search_item})

            chat_stream = await handle_chat(chat_req, client, request=request)
            async for payload in _iter_sse_data_payloads(chat_stream):
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if "error" in event:
                    yield _sse_event("error", {"type": "error", "error": event["error"]})
                    continue
                choices = event.get("choices") or []
                if not choices and isinstance(event.get("usage"), dict):
                    final_usage = event["usage"]
                    continue
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                thinking_delta = delta.get("thinking") or delta.get("reasoning_content") or delta.get("reasoning")
                if thinking_delta:
                    thinking_accumulator.append(thinking_delta)
                    if not thinking_started:
                        thinking_started = True
                        thinking_output_index = next_output_index
                        next_output_index += 1
                        item = {
                            "id": thinking_item_id,
                            "type": "reasoning",
                            "status": "in_progress",
                            "content": [],
                            "summary": [],
                        }
                        yield _sse_event(
                            "response.output_item.added",
                            {"type": "response.output_item.added", "output_index": thinking_output_index, "item": item},
                        )
                    yield _sse_event(
                        "response.reasoning.delta",
                        {
                            "type": "response.reasoning.delta",
                            "item_id": thinking_item_id,
                            "output_index": thinking_output_index,
                            "delta": thinking_delta,
                        },
                    )
                text_delta = delta.get("content")
                if text_delta:
                    text_accumulator.append(text_delta)
                    text_value = "".join(text_accumulator)
                    if not text_started and not _may_be_responses_text_tool_request(text_value, allowed_tool_names):
                        text_started = True
                        text_output_index = next_output_index
                        next_output_index += 1
                        item = {
                            "id": text_item_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        }
                        yield _sse_event("response.output_item.added", {"type": "response.output_item.added", "output_index": text_output_index, "item": item})
                        yield _sse_event(
                            "response.content_part.added",
                            {
                                "type": "response.content_part.added",
                                "item_id": text_item_id,
                                "output_index": text_output_index,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": "", "annotations": []},
                            },
                        )
                        yield _sse_event(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "item_id": text_item_id,
                                "output_index": text_output_index,
                                "content_index": 0,
                                "delta": text_value,
                            },
                        )
                    elif text_started:
                        yield _sse_event(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "item_id": text_item_id,
                                "output_index": text_output_index,
                                "content_index": 0,
                                "delta": text_delta,
                            },
                        )
                for tool_call in delta.get("tool_calls") or []:
                    function = tool_call.get("function") if isinstance(tool_call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    item = _responses_function_call_item(function.get("name") or "unknown", function.get("arguments") or "{}", call_id=tool_call.get("id"))
                    tool_output_index = next_output_index
                    next_output_index += 1
                    output_items.append(item)
                    yield _sse_event(
                        "response.output_item.added",
                        {"type": "response.output_item.added", "output_index": tool_output_index, "item": _responses_function_call_added_item(item)},
                    )
                    yield _sse_event(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": item["id"],
                            "output_index": tool_output_index,
                            "delta": item["arguments"],
                        },
                    )
                    yield _sse_event(
                        "response.function_call_arguments.done",
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": item["id"],
                            "output_index": tool_output_index,
                            "arguments": item["arguments"],
                        },
                    )
                    yield _sse_event("response.output_item.done", {"type": "response.output_item.done", "output_index": tool_output_index, "item": item})
                if choice.get("finish_reason") is not None:
                    break

            if thinking_started:
                thinking_value = "".join(thinking_accumulator)
                thinking_item = {
                    "id": thinking_item_id,
                    "type": "reasoning",
                    "status": "completed",
                    "content": [{"type": "reasoning_text", "text": thinking_value}],
                    "summary": [],
                }
                output_items.append(thinking_item)
                yield _sse_event(
                    "response.reasoning.done",
                    {
                        "type": "response.reasoning.done",
                        "item_id": thinking_item_id,
                        "output_index": thinking_output_index,
                        "text": thinking_value,
                    },
                )
                yield _sse_event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": thinking_output_index,
                        "item": thinking_item,
                    },
                )

            text_value = "".join(text_accumulator)
            text_tool_request = _parse_responses_text_tool_request(text_value, allowed_tool_names)
            if text_tool_request:
                tool_output_index = next_output_index
                next_output_index += 1
                for event in function_call_events(text_tool_request, tool_output_index):
                    yield event
            elif text_value:
                if not text_started:
                    text_started = True
                    text_output_index = next_output_index
                    next_output_index += 1
                    item = {
                        "id": text_item_id,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    }
                    yield _sse_event("response.output_item.added", {"type": "response.output_item.added", "output_index": text_output_index, "item": item})
                    yield _sse_event(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "item_id": text_item_id,
                            "output_index": text_output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                    )
                    yield _sse_event(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": text_item_id,
                            "output_index": text_output_index,
                            "content_index": 0,
                            "delta": text_value,
                        },
                    )
                text_item = {
                    "id": text_item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text_value, "annotations": []}],
                }
                output_items.append(text_item)
                yield _sse_event(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": text_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "text": text_value,
                    },
                )
                yield _sse_event(
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": text_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": text_value, "annotations": []},
                    },
                )
                yield _sse_event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": text_output_index,
                        "item": text_item,
                    },
                )
            completed = dict(response_base)
            completed["status"] = "completed"
            completed["usage"] = final_usage
            completed["output"] = output_items
            completed["thinking"] = "".join(thinking_accumulator)
            completed["output_text"] = "" if text_tool_request else text_value
            yield _sse_event("response.completed", {"type": "response.completed", "response": completed})
            yield "data: [DONE]\n\n"
        except Exception as exc:
            message, error_type, code = _openai_stream_error_detail(exc)
            yield _sse_event("error", {"type": "error", "error": {"message": message, "type": error_type, "code": code}})
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def handle_openai_responses(payload: dict[str, Any], client: AIStudioClient, request: Request | None = None) -> dict[str, Any] | StreamingResponse:
    if not payload.get("model"):
        raise _bad_request("model is required", "invalid_request_error")
    image_generation_tool = _responses_image_generation_tool(payload.get("tools"))
    if image_generation_tool is not None:
        if payload.get("stream"):
            return _build_responses_image_generation_streaming_response(payload, image_generation_tool, client, request=request)
        return await _complete_responses_optional_image_generation(payload, image_generation_tool, client)
    response_format = payload.get("response_format")
    text_config = payload.get("text")
    if response_format is None and isinstance(text_config, dict):
        response_format = text_config.get("format")
    try:
        allowed_tool_names = _tool_names_from_payload(payload.get("tools"))
        chat_req = ChatRequest(
            model=str(payload["model"]),
            messages=_messages_from_responses_payload(payload),
            temperature=payload.get("temperature"),
            top_p=payload.get("top_p"),
            max_tokens=payload.get("max_output_tokens") or payload.get("max_tokens"),
            tools=_messages_tools_from_payload(payload.get("tools")),
            thinking=_responses_thinking_value(payload),
            response_format=response_format,
        )
        uses_search = _chat_request_uses_search(chat_req)
    except (ValueError, ValidationError) as exc:
        raise _bad_request(str(exc), "invalid_request_error") from exc
    if payload.get("stream"):
        chat_req.stream = True
        return _build_responses_streaming_response(chat_req, client, uses_search=uses_search, allowed_tool_names=allowed_tool_names, request=request)
    chat_response = await handle_chat(chat_req, client)
    return _responses_chat_response_payload(payload, chat_response, uses_search=uses_search, allowed_tool_names=allowed_tool_names)


def _messages_from_messages_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValueError("messages must be a non-empty array")
    for item in raw_messages:
        if not isinstance(item, dict):
            raise ValueError("messages items must be objects")
        messages.append(
            {
                "role": str(item.get("role") or "user"),
                "content": _coerce_openai_content_blocks(item.get("content", "")),
            }
        )
    return messages


def _anthropic_thinking_value(value: Any) -> str | bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        thinking_type = str(value.get("type") or "").lower()
        if thinking_type in ("disabled", "none", "off"):
            return "off"
        return "high"
    return None


def _anthropic_stream_error(message: str, error_type: str = "api_error") -> str:
    return _sse_event("error", {"type": "error", "error": {"type": error_type, "message": message}})


def _build_messages_streaming_response(chat_req: ChatRequest, client: AIStudioClient, *, request: Request | None = None) -> StreamingResponse:
    async def stream_response():
        message_id = f"msg_{uuid.uuid4().hex[:24]}"
        content_index = 0
        active_block: str | None = None
        final_usage = None
        stop_reason = "end_turn"

        def close_active_block() -> str:
            nonlocal active_block, content_index
            if active_block is None:
                return ""
            payload = _sse_event("content_block_stop", {"type": "content_block_stop", "index": content_index})
            active_block = None
            content_index += 1
            return payload

        try:
            yield _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": chat_req.model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            )

            chat_stream = await handle_chat(chat_req, client, request=request)
            async for payload in _iter_sse_data_payloads(chat_stream):
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if "error" in event:
                    error = event["error"]
                    yield _anthropic_stream_error(str(error.get("message") or error), str(error.get("type") or "api_error"))
                    continue
                choices = event.get("choices") or []
                if not choices and isinstance(event.get("usage"), dict):
                    final_usage = event["usage"]
                    continue
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                thinking_delta = delta.get("thinking")
                if thinking_delta:
                    if active_block != "thinking":
                        yield close_active_block()
                        active_block = "thinking"
                        yield _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": content_index,
                                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                            },
                        )
                    yield _sse_event(
                        "content_block_delta",
                        {"type": "content_block_delta", "index": content_index, "delta": {"type": "thinking_delta", "thinking": thinking_delta}},
                    )
                text_delta = delta.get("content")
                if text_delta:
                    if active_block != "text":
                        yield close_active_block()
                        active_block = "text"
                        yield _sse_event(
                            "content_block_start",
                            {"type": "content_block_start", "index": content_index, "content_block": {"type": "text", "text": ""}},
                        )
                    yield _sse_event(
                        "content_block_delta",
                        {"type": "content_block_delta", "index": content_index, "delta": {"type": "text_delta", "text": text_delta}},
                    )
                for tool_call in delta.get("tool_calls") or []:
                    function = tool_call.get("function") if isinstance(tool_call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    yield close_active_block()
                    active_block = "tool_use"
                    stop_reason = "tool_use"
                    arguments = function.get("arguments") or "{}"
                    yield _sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": content_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                                "name": function.get("name") or "unknown",
                                "input": {},
                            },
                        },
                    )
                    yield _sse_event(
                        "content_block_delta",
                        {"type": "content_block_delta", "index": content_index, "delta": {"type": "input_json_delta", "partial_json": arguments}},
                    )
                    yield close_active_block()
                finish_reason = choice.get("finish_reason")
                if finish_reason == "tool_calls":
                    stop_reason = "tool_use"
                elif finish_reason is not None and stop_reason != "tool_use":
                    stop_reason = "end_turn"
                    break

            yield close_active_block()
            usage = final_usage or {}
            yield _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": usage.get("completion_tokens", 0)},
                },
            )
            yield _sse_event("message_stop", {"type": "message_stop"})
        except Exception as exc:
            message, error_type, _code = _openai_stream_error_detail(exc)
            yield _anthropic_stream_error(message, error_type)

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def handle_messages_count_tokens(payload: dict[str, Any]) -> dict[str, int]:
    if not payload.get("model"):
        raise _bad_request("model is required", "invalid_request_error")
    normalized = None
    try:
        chat_req = ChatRequest(
            model=str(payload["model"]),
            messages=_messages_from_messages_payload(payload),
        )
        normalized = normalize_chat_request(chat_req.messages, chat_req.model)
        total_tokens = _estimate_content_tokens(normalized["contents"])
        if normalized.get("system_instruction"):
            total_tokens += _estimate_text_tokens(normalized["system_instruction"])
        if payload.get("tools"):
            total_tokens += _estimate_text_tokens(json.dumps(payload["tools"], ensure_ascii=False))
        return {"input_tokens": total_tokens}
    except (ValueError, ValidationError) as exc:
        raise _bad_request(str(exc), "invalid_request_error") from exc
    finally:
        if normalized is not None:
            cleanup_files(normalized["cleanup_paths"])


async def handle_messages(payload: dict[str, Any], client: AIStudioClient, request: Request | None = None) -> dict[str, Any] | StreamingResponse:
    if not payload.get("model"):
        raise _bad_request("model is required", "invalid_request_error")
    try:
        chat_req = ChatRequest(
            model=str(payload["model"]),
            messages=_messages_from_messages_payload(payload),
            temperature=payload.get("temperature"),
            top_p=payload.get("top_p"),
            max_tokens=payload.get("max_tokens"),
            tools=_messages_tools_from_payload(payload.get("tools")),
            thinking=_anthropic_thinking_value(payload.get("thinking")),
            response_format=payload.get("response_format"),
        )
        _chat_request_uses_search(chat_req)
    except (ValueError, ValidationError) as exc:
        raise _bad_request(str(exc), "invalid_request_error") from exc
    if payload.get("stream"):
        chat_req.stream = True
        return _build_messages_streaming_response(chat_req, client, request=request)
    chat_response = await handle_chat(chat_req, client)
    usage = chat_response.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": chat_response.get("model", payload["model"]),
        "content": _message_content_blocks(chat_response),
        "stop_reason": "tool_use" if _chat_tool_calls(chat_response) else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def gemini_model_dict(model_id: str) -> dict[str, Any]:
    metadata = get_model_capabilities(model_id, strict=True).to_model_dict()
    capabilities = metadata["capabilities"]
    methods = ["generateContent", "countTokens"]
    if capabilities.get("streaming"):
        methods.append("streamGenerateContent")
    return {
        "name": f"models/{metadata['id']}",
        "version": "001",
        "displayName": metadata["id"],
        "description": "AI Studio replay-backed model metadata",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "supportedGenerationMethods": methods,
    }


def list_gemini_models_response() -> dict[str, Any]:
    return {"models": [gemini_model_dict(model_id) for model_id in list_model_ids()]}


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _estimate_content_tokens(contents: list[AistudioContent]) -> int:
    total = 0
    for content in contents:
        for part in content.parts:
            if part.text is not None:
                total += _estimate_text_tokens(part.text)
            elif part.inline_data is not None:
                total += 258 + max(1, len(part.inline_data[1]) // 1024)
    return total


def gemini_count_tokens_response(model_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request_payload = payload.get("generateContentRequest") if isinstance(payload.get("generateContentRequest"), dict) else payload
    req = GeminiGenerateContentRequest.model_validate(request_payload)
    normalized = None
    try:
        normalized = normalize_gemini_request(req, model_path, stream=False)
        total_tokens = _estimate_content_tokens(normalized["contents"])
        if normalized.get("system_instruction") is not None:
            total_tokens += _estimate_content_tokens([normalized["system_instruction"]])
        return {"totalTokens": total_tokens}
    finally:
        if normalized is not None:
            cleanup_files(normalized["cleanup_paths"])


def _build_chat_image_streaming_response(
    image_req: ImageRequest,
    client: AIStudioClient,
    cleanup_paths: list[str],
    *,
    include_usage: bool,
    request: Request | None = None,
) -> StreamingResponse:
    async def stream_response():
        try:
            if await _request_disconnected(request):
                logger.info("Chat image stream disconnected before downstream call")
                return
            chat_id = new_chat_id()
            image_response = await handle_image_generation(image_req, client)
            if await _request_disconnected(request):
                logger.info("Chat image stream disconnected before response write")
                return
            content = _image_chat_content(image_response["data"])
            if content:
                yield sse_chunk(chat_id, image_req.model, content, include_usage=include_usage)
            yield sse_chunk(chat_id, image_req.model, "", finish="stop", include_usage=include_usage)
            if include_usage:
                yield sse_usage_chunk(chat_id, image_req.model)
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.info("Chat image stream cancelled by client")
            raise
        except HTTPException as exc:
            detail = exc.detail.get("message") if isinstance(exc.detail, dict) else str(exc.detail)
            yield sse_error(detail)
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("Chat image stream error: %s", exc, exc_info=True)
            message, error_type, code = _openai_stream_error_detail(exc)
            yield sse_error(message, error_type=error_type, code=code)
            yield "data: [DONE]\n\n"
        finally:
            cleanup_files(cleanup_paths)

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def handle_chat(req: ChatRequest, client: AIStudioClient, request: Request | None = None):
    try:
        response_format_overrides = _validate_chat_request(req)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc

    busy_lock = runtime_state.busy_lock
    if busy_lock is None:
        raise HTTPException(503, detail={"message": "Server not ready", "type": "service_unavailable"})
    if busy_lock.locked():
        raise HTTPException(429, detail={"message": "Server is busy", "type": "rate_limit_exceeded"})
    if req.stream and _client_is_pure_http(client):
        raise _unsupported(_pure_http_streaming_message())

    if _is_image_output_chat_model(req.model):
        cleanup_paths: list[str] = []
        try:
            _validate_image_model_chat_options(req)
            image_req, cleanup_paths = _chat_image_request(req)
            if req.stream:
                include_usage = True
                if req.stream_options is not None:
                    include_usage = req.stream_options.include_usage
                return _build_chat_image_streaming_response(image_req, client, cleanup_paths, include_usage=include_usage, request=request)

            image_response = await handle_image_generation(image_req, client)
            return chat_completion_response(
                model=image_req.model,
                content=_image_chat_content(image_response["data"]),
            )
        except ValueError as exc:
            raise _bad_request(str(exc)) from exc
        finally:
            if not req.stream:
                cleanup_files(cleanup_paths)

    max_retries = 3  # 最多重试次数
    last_error = None
    affinity_key = _chat_affinity_key(req)
    exclude_account_id = None

    for attempt in range(max_retries):
        async with busy_lock:
            model = req.model
            tmp_files: list[str] = []
            account_context: RequestAccountContext | None = None

            try:
                normalized = normalize_chat_request(req.messages, req.model)
                model = normalized["model"]
                tmp_files = list(normalized["cleanup_paths"])
                tools, tools_use_search = normalize_openai_tools_and_search(req.tools)
                has_image_input = bool(normalized["capture_images"])
                file_input_mime_types = tuple(normalized.get("file_input_mime_types") or ())
                uses_search = bool(req.grounding or tools_use_search)
                validate_chat_capabilities(
                    model,
                    has_image_input=has_image_input,
                    has_file_input=bool(file_input_mime_types),
                    file_input_mime_types=file_input_mime_types,
                    uses_tools=bool(tools),
                    uses_search=uses_search,
                    uses_thinking=_explicit_thinking_enabled(req.thinking),
                    stream=req.stream,
                    uses_structured_output=response_format_overrides is not None,
                )
                capabilities = get_model_capabilities(model)
                account_context = await _request_account_context(
                    client,
                    model,
                    exclude_account_id=exclude_account_id,
                    affinity_key=affinity_key,
                )
                request_client = account_context.client

                logger.info(
                    "Chat: model=%s, contents=%s, capture_prompt=%s..., images=%s, stream=%s, attempt=%d",
                    model,
                    len(normalized["contents"]),
                    normalized["capture_prompt"][:50],
                    len(normalized["capture_images"]),
                    req.stream,
                    attempt + 1,
                )

                if uses_search:
                    from aistudio_api.infrastructure.gateway.request_rewriter import TOOLS_TEMPLATES
                    tools = list(tools or [])
                    tools.append(TOOLS_TEMPLATES["google_search"])

                # Gemma 4 默认开启 Google Search
                if tools is None and any(m in model for m in ("gemma-4-26b-a4b-it", "gemma-4-31b-it")):
                    from aistudio_api.infrastructure.gateway.request_rewriter import TOOLS_TEMPLATES
                    tools = [TOOLS_TEMPLATES["google_search"]]

                generation_config_overrides = _merge_generation_overrides(
                    response_format_overrides,
                    _thinking_overrides(req.thinking),
                )
                enable_thinking = _thinking_enabled(req.thinking)
                gateway_max_tokens = _chat_max_tokens_for_gateway(req, enable_thinking=enable_thinking, capabilities=capabilities)

                if req.stream:
                    include_usage = True
                    if req.stream_options is not None:
                        include_usage = req.stream_options.include_usage
                    return _build_streaming_response(
                        client=request_client,
                        fallback_client=client,
                        capture_prompt=normalized["capture_prompt"],
                        model=model,
                        capture_images=normalized["capture_images"] if normalized["capture_images"] else None,
                        contents=normalized["contents"],
                        system_instruction=normalized["system_instruction"],
                        cleanup_paths=tmp_files,
                        include_usage=include_usage,
                        temperature=req.temperature,
                        top_p=req.top_p,
                        top_k=req.top_k,
                        max_tokens=gateway_max_tokens,
                        tools=tools,
                        generation_config_overrides=generation_config_overrides,
                        safety_off=bool(req.safety_off),
                        enable_thinking=enable_thinking,
                        sanitize_plain_text=response_format_overrides is None,
                        request=request,
                        account_context=account_context,
                        affinity_key=affinity_key,
                    )

                output = await request_client.generate_content(
                    model=model,
                    capture_prompt=normalized["capture_prompt"],
                    capture_images=normalized["capture_images"] if normalized["capture_images"] else None,
                    contents=normalized["contents"],
                    system_instruction_content=(
                        AistudioContent(role="user", parts=[AistudioPart(text=normalized["system_instruction"])])
                        if normalized["system_instruction"]
                        else None
                    ),
                    temperature=req.temperature,
                    top_p=req.top_p,
                    top_k=req.top_k,
                    max_tokens=gateway_max_tokens,
                    tools=tools,
                    generation_config_overrides=generation_config_overrides,
                    safety_off=bool(req.safety_off),
                    enable_thinking=enable_thinking,
                    sanitize_plain_text=response_format_overrides is None,
                )

                _record_request_result(model, "success", output.usage, account_id=account_context.account_id)
                return chat_completion_response(
                    model=model,
                    content=output.text,
                    thinking=output.thinking,
                    usage=output.usage,
                    function_calls=output.function_calls,
                )
            except UsageLimitExceeded as exc:
                failed_account_id = account_context.account_id if account_context is not None else None
                _record_request_result(model, "rate_limited", account_id=failed_account_id, rate_limit_error=exc)
                last_error = exc
                exclude_account_id = failed_account_id

                # 尝试切换账号
                if getattr(runtime_state, "account_client_pool", None) is not None and attempt + 1 < max_retries:
                    logger.info("429 限流，已排除当前账号，重试 %d/%d", attempt + 1, max_retries)
                    continue
                if await _try_switch_account(model):
                    logger.info("429 限流，已切换账号，重试 %d/%d", attempt + 1, max_retries)
                    continue
                else:
                    logger.warning("429 限流，无法切换账号")
                    raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
            except ValueError as exc:
                raise _bad_request(str(exc)) from exc
            except AuthError as exc:
                failed_account_id = account_context.account_id if account_context is not None else None
                exclude_account_id = failed_account_id
                if attempt == 0:
                    logger.warning("Chat auth error; clearing capture state and retrying once: %s", exc)
                    _clear_client_capture_state(account_context.client if account_context is not None else client)
                    last_error = exc
                    continue
                _record_request_result(model, "errors", account_id=failed_account_id)
                raise _upstream_exception(exc) from exc
            except HTTPException as exc:
                if isinstance(last_error, AuthError) and exc.status_code == 503:
                    raise _upstream_exception(last_error) from exc
                raise
            except RequestError as exc:
                failed_account_id = account_context.account_id if account_context is not None else None
                if (
                    _is_native_worker_unavailable(exc)
                    and failed_account_id
                    and getattr(runtime_state, "account_client_pool", None) is not None
                    and attempt + 1 < max_retries
                ):
                    _record_request_result(model, "errors", account_id=failed_account_id)
                    last_error = exc
                    exclude_account_id = failed_account_id
                    logger.warning("Chat native worker unavailable，已排除当前账号并重试 %d/%d: %s", attempt + 1, max_retries, exc)
                    continue
                _record_request_result(model, "errors", account_id=failed_account_id)
                raise _upstream_exception(exc) from exc
            except AistudioError as exc:
                _record_request_result(model, "errors", account_id=account_context.account_id if account_context is not None else None)
                raise _upstream_exception(exc) from exc
            except Exception as exc:
                _record_request_result(model, "errors", account_id=account_context.account_id if account_context is not None else None)
                logger.error("Chat error: %s", exc, exc_info=True)
                raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
            finally:
                if not req.stream:
                    cleanup_files(tmp_files)
                    if account_context is not None:
                        await account_context.release()

    # 所有重试都失败
    raise HTTPException(429, detail={"message": str(last_error), "type": "rate_limit_exceeded"}) from last_error


async def handle_image_generation(req: ImageRequest, client: AIStudioClient):
    busy_lock = runtime_state.busy_lock
    if busy_lock is None:
        raise HTTPException(503, detail={"message": "Server not ready", "type": "service_unavailable"})
    if busy_lock.locked():
        raise HTTPException(429, detail={"message": "Server is busy", "type": "rate_limit_exceeded"})

    try:
        image_plan, response_format = _validate_image_request(req)
        image_paths = _image_request_images_to_files(req.images)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc

    max_retries = 3
    last_error = None
    affinity_key = _image_affinity_key(req)
    exclude_account_id = None
    image_store = GeneratedImageStore()
    try:
        for attempt in range(max_retries):
            async with busy_lock:
                items: list[dict[str, Any]] = []
                account_context: RequestAccountContext | None = None
                try:
                    account_context = await _request_account_context(
                        client,
                        image_plan.model,
                        exclude_account_id=exclude_account_id,
                        affinity_key=affinity_key,
                    )
                    request_client = account_context.client
                    logger.info(
                        "Image: model=%s, size=%s, n=%d, prompt=%s..., images=%d, attempt=%d",
                        image_plan.model,
                        image_plan.size,
                        req.n,
                        req.prompt[:50],
                        len(image_paths),
                        attempt + 1,
                    )
                    usage_total: dict = {}
                    created = int(time.time())
                    for _ in range(req.n):
                        image_kwargs = {
                            "prompt": image_plan.prompt_for(req.prompt),
                            "model": image_plan.model,
                            "generation_config_overrides": image_plan.generation_config_overrides,
                        }
                        if image_paths:
                            image_kwargs["images"] = image_paths
                        if req.timeout is not None:
                            image_kwargs["timeout"] = req.timeout
                        output = await request_client.generate_image(**image_kwargs)
                        if not output.images:
                            raise RequestError(0, "AI Studio returned no image data")
                        _merge_usage(usage_total, output.usage)
                        for img in output.images:
                            b64 = base64.b64encode(img.data).decode("ascii")
                            persisted = image_store.save(img.data, img.mime, created_at=created)
                            items.append(
                                {
                                    "b64_json": b64,
                                    "url": persisted.url,
                                    "revised_prompt": output.text or "",
                                    "id": persisted.id,
                                    "path": persisted.path,
                                    "delete_url": persisted.delete_url,
                                    "mime_type": persisted.mime_type,
                                    "size_bytes": persisted.size,
                                }
                            )

                    _record_request_result(
                        image_plan.model,
                        "success",
                        usage_total,
                        account_id=account_context.account_id,
                        image_size=image_plan.size,
                        image_count=len(items),
                    )
                    return {"created": created, "data": _format_image_items(items, response_format)}
                except UsageLimitExceeded as exc:
                    _cleanup_persisted_image_items(image_store, items)
                    failed_account_id = account_context.account_id if account_context is not None else None
                    _record_request_result(image_plan.model, "rate_limited", account_id=failed_account_id, rate_limit_error=exc)
                    last_error = exc
                    exclude_account_id = failed_account_id

                    # 尝试切换账号
                    if getattr(runtime_state, "account_client_pool", None) is not None and attempt + 1 < max_retries:
                        logger.info("Image 429 限流，已排除当前账号，重试 %d/%d", attempt + 1, max_retries)
                        continue
                    if await _try_switch_account(image_plan.model):
                        logger.info("Image 429 限流，已切换账号，重试 %d/%d", attempt + 1, max_retries)
                        continue
                    else:
                        logger.warning("Image 429 限流，无法切换账号")
                        raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
                except AuthError as exc:
                    _cleanup_persisted_image_items(image_store, items)
                    failed_account_id = account_context.account_id if account_context is not None else None
                    exclude_account_id = failed_account_id
                    if attempt == 0:
                        logger.warning("Image auth error; clearing capture state and retrying once: %s", exc)
                        _clear_client_capture_state(account_context.client if account_context is not None else client)
                        last_error = exc
                        continue
                    _record_request_result(image_plan.model, "errors", account_id=account_context.account_id if account_context is not None else None)
                    raise _upstream_exception(exc) from exc
                except HTTPException as exc:
                    if isinstance(last_error, AuthError) and exc.status_code == 503:
                        raise _upstream_exception(last_error) from exc
                    raise
                except RequestError as exc:
                    _cleanup_persisted_image_items(image_store, items)
                    if attempt == 0 and _is_empty_image_response(exc):
                        logger.warning("Image response contained no image data; clearing capture state and retrying once")
                        _clear_client_capture_state(account_context.client if account_context is not None else client)
                        last_error = exc
                        continue
                    if attempt == 0 and _is_transient_replay_network_error(exc):
                        logger.warning("Image replay network error; clearing capture state and retrying once: %s", exc)
                        _clear_client_capture_state(account_context.client if account_context is not None else client)
                        last_error = exc
                        continue
                    _record_request_result(image_plan.model, "errors", account_id=account_context.account_id if account_context is not None else None)
                    raise _upstream_exception(exc) from exc
                except AistudioError as exc:
                    _cleanup_persisted_image_items(image_store, items)
                    _record_request_result(image_plan.model, "errors", account_id=account_context.account_id if account_context is not None else None)
                    raise _upstream_exception(exc) from exc
                except Exception as exc:
                    _cleanup_persisted_image_items(image_store, items)
                    _record_request_result(image_plan.model, "errors", account_id=account_context.account_id if account_context is not None else None)
                    logger.error("Image error: %s", exc, exc_info=True)
                    raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
                finally:
                    if account_context is not None:
                        await account_context.release()

        # 所有重试都失败
        raise HTTPException(429, detail={"message": str(last_error), "type": "rate_limit_exceeded"}) from last_error
    finally:
        cleanup_files(image_paths)


def _cleanup_persisted_image_items(image_store: GeneratedImageStore, items: list[dict[str, Any]]) -> None:
    for item in items:
        path = item.get("path")
        if not path:
            continue
        try:
            image_store.delete(path)
        except OSError:
            logger.warning("Failed to clean up generated image after request failure")
        except ValueError:
            logger.warning("Skipped invalid generated image cleanup path after request failure")


def _build_streaming_response(
    *,
    client: AIStudioClient,
    capture_prompt: str,
    model: str,
    capture_images: list[str] | None,
    contents: list[AistudioContent],
    system_instruction: str | None,
    cleanup_paths: list[str],
    include_usage: bool = False,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    max_tokens: int | None = None,
    tools: list[list] | None = None,
    generation_config_overrides: dict | None = None,
    safety_off: bool = False,
    enable_thinking: bool = True,
    sanitize_plain_text: bool = True,
    request: Request | None = None,
    account_context: RequestAccountContext | None = None,
    fallback_client: AIStudioClient | None = None,
    affinity_key: str | None = None,
) -> StreamingResponse:
    async def stream_response():
        busy_lock = runtime_state.busy_lock
        if busy_lock is None:
            yield sse_error("Server not ready")
            cleanup_files(cleanup_paths)
            return

        async with busy_lock:
            active_client = client
            active_account_context = account_context
            try:
                chat_id = new_chat_id()
                final_usage = None
                saw_tool_calls = False
                saw_content = False
                max_stream_attempts = 2
                for stream_attempt in range(max_stream_attempts):
                    if await _request_disconnected(request):
                        logger.info("OpenAI stream disconnected before downstream call")
                        return
                    upstream = None
                    try:
                        try:
                            upstream = active_client.stream_generate_content(
                                model=model,
                                capture_prompt=capture_prompt,
                                capture_images=capture_images,
                                contents=contents,
                                system_instruction_content=(
                                    AistudioContent(role="user", parts=[AistudioPart(text=system_instruction)])
                                    if system_instruction
                                    else None
                                ),
                                temperature=temperature,
                                top_p=top_p,
                                top_k=top_k,
                                max_tokens=max_tokens,
                                tools=tools,
                                generation_config_overrides=generation_config_overrides,
                                sanitize_plain_text=sanitize_plain_text,
                                safety_off=safety_off,
                                enable_thinking=enable_thinking,
                                force_refresh_capture=stream_attempt > 0,
                            )
                            async for event_type, text in upstream:
                                if await _request_disconnected(request):
                                    logger.info("OpenAI stream disconnected during downstream replay")
                                    return
                                if event_type == "body" and text:
                                    saw_content = True
                                    yield sse_chunk(chat_id, model, text, include_usage=include_usage)
                                elif event_type == "thinking" and text:
                                    saw_content = True
                                    yield sse_chunk(chat_id, model, "", thinking=text, include_usage=include_usage)
                                elif event_type == "tool_calls" and text:
                                    saw_tool_calls = True
                                    saw_content = True
                                    yield sse_chunk(
                                        chat_id,
                                        model,
                                        "",
                                        tool_calls=to_openai_tool_calls(text if isinstance(text, list) else []),
                                        include_usage=include_usage,
                                    )
                                elif event_type == "usage":
                                    final_usage = text if isinstance(text, dict) else None
                            break
                        finally:
                            if upstream is not None:
                                await _close_async_iterator(upstream)
                    except RequestError as exc:
                        if exc.status == 204 and stream_attempt == 0:
                            logger.warning("Stream 收到 204，清理 snapshot 缓存后重试一次")
                            _clear_client_capture_state(active_client)
                            continue
                        if _is_native_worker_unavailable(exc) and not saw_content and stream_attempt + 1 < max_stream_attempts:
                            replacement_context = await _request_replacement_account_context(
                                fallback_client=fallback_client or active_client,
                                model=model,
                                account_context=active_account_context,
                                affinity_key=affinity_key,
                            )
                            if replacement_context is not None:
                                logger.warning("Stream native worker unavailable，已排除当前账号并重试下一个账号: %s", exc)
                                _record_request_result(model, "errors", account_id=_account_id_for_stats(active_account_context))
                                active_account_context = replacement_context
                                active_client = replacement_context.client
                                continue
                        raise
                    except AuthError as exc:
                        if saw_content:
                            raise
                        _clear_client_capture_state(active_client)
                        if stream_attempt + 1 < max_stream_attempts:
                            replacement_context = await _request_replacement_account_context(
                                fallback_client=fallback_client or active_client,
                                model=model,
                                account_context=active_account_context,
                                affinity_key=affinity_key,
                            )
                            if replacement_context is not None:
                                logger.warning("Stream 鉴权异常，已排除当前账号并重试下一个账号: %s", exc)
                                _record_request_result(model, "errors", account_id=_account_id_for_stats(active_account_context))
                                active_account_context = replacement_context
                                active_client = replacement_context.client
                                continue
                        if stream_attempt == 0:
                            logger.warning("Stream 鉴权异常，清理 snapshot 缓存后重试一次: %s", exc)
                            continue
                        raise

                if not saw_content:
                    raise RequestError(502, "AI Studio returned no response content")

                _record_request_result(model, "success", final_usage, account_id=_account_id_for_stats(active_account_context))
                yield sse_chunk(chat_id, model, "", finish="tool_calls" if saw_tool_calls else "stop", include_usage=include_usage)
                if include_usage:
                    yield sse_usage_chunk(chat_id, model, final_usage)
                yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                logger.info("OpenAI stream cancelled by client")
                raise
            except UsageLimitExceeded as exc:
                _record_request_result(model, "rate_limited", account_id=_account_id_for_stats(active_account_context), rate_limit_error=exc)
                message, error_type, code = _openai_stream_error_detail(exc)
                logger.warning("OpenAI stream rate limited: %s", message)
                yield sse_error(message, error_type=error_type, code=code)
                yield "data: [DONE]\n\n"
            except Exception as exc:
                _record_request_result(model, "errors", account_id=_account_id_for_stats(active_account_context))
                message, error_type, code = _openai_stream_error_detail(exc)
                if error_type == "unsupported_feature":
                    logger.warning("OpenAI stream unsupported: %s", message)
                else:
                    logger.error("Stream error: %s", exc, exc_info=True)
                yield sse_error(message, error_type=error_type, code=code)
                yield "data: [DONE]\n\n"
            finally:
                cleanup_files(cleanup_paths)
                if active_account_context is not None:
                    await active_account_context.release()

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def handle_gemini_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    stream: bool,
    request: Request | None = None,
):
    busy_lock = runtime_state.busy_lock
    if busy_lock is None:
        raise HTTPException(503, detail={"message": "Server not ready", "type": "service_unavailable"})
    if busy_lock.locked():
        raise HTTPException(429, detail={"message": "Server is busy", "type": "rate_limit_exceeded"})
    if stream and _client_is_pure_http(client):
        raise _unsupported(_pure_http_streaming_message())

    max_retries = 3
    last_error = None
    affinity_key = _gemini_affinity_key(model_path, req)
    exclude_account_id = None

    for attempt in range(max_retries):
        async with busy_lock:
            normalized = None
            account_context: RequestAccountContext | None = None
            try:
                normalized = normalize_gemini_request(req, model_path, stream=stream)
                account_context = await _request_account_context(
                    client,
                    normalized["model"],
                    exclude_account_id=exclude_account_id,
                    affinity_key=affinity_key,
                )
                request_client = account_context.client
                logger.info(
                    "Gemini: model=%s, contents=%s, stream=%s, attempt=%d",
                    normalized["model"],
                    len(req.contents),
                    stream,
                    attempt + 1,
                )

                if stream:
                    return _build_gemini_streaming_response(
                        client=request_client,
                        normalized=normalized,
                        request=request,
                        account_context=account_context,
                        fallback_client=client,
                        affinity_key=affinity_key,
                    )

                output = await request_client.generate_content(
                    model=normalized["model"],
                    capture_prompt=normalized["capture_prompt"],
                    capture_images=normalized["capture_images"],
                    contents=normalized["contents"],
                    system_instruction_content=normalized["system_instruction"],
                    tools=normalized["tools"],
                    temperature=normalized["temperature"],
                    top_p=normalized["top_p"],
                    top_k=normalized["top_k"],
                    max_tokens=normalized["max_tokens"],
                    generation_config_overrides=normalized["generation_config_overrides"],
                    sanitize_plain_text=False,
                )

                _record_request_result(normalized["model"], "success", output.usage, account_id=account_context.account_id)
                return {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": to_gemini_parts(
                                    output.text,
                                    function_calls=output.function_calls,
                                    function_responses=output.function_responses,
                                    thinking=output.thinking,
                                ),
                            },
                            "finishReason": "STOP" if not output.function_calls else "FUNCTION_CALL",
                        }
                    ],
                    "usageMetadata": to_gemini_usage_metadata(output.usage),
                }
            except ValueError as exc:
                raise HTTPException(400, detail={"message": str(exc), "type": "bad_request"}) from exc
            except UsageLimitExceeded as exc:
                failed_account_id = account_context.account_id if account_context is not None else None
                _record_request_result(normalized["model"] if normalized else model_path, "rate_limited", account_id=failed_account_id, rate_limit_error=exc)
                last_error = exc
                exclude_account_id = failed_account_id

                # 尝试切换账号
                if getattr(runtime_state, "account_client_pool", None) is not None and attempt + 1 < max_retries:
                    logger.info("Gemini 429 限流，已排除当前账号，重试 %d/%d", attempt + 1, max_retries)
                    continue
                if await _try_switch_account(normalized["model"] if normalized else model_path):
                    logger.info("Gemini 429 限流，已切换账号，重试 %d/%d", attempt + 1, max_retries)
                    continue
                else:
                    logger.warning("Gemini 429 限流，无法切换账号")
                    raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
            except AuthError as exc:
                error_model = normalized["model"] if normalized else model_path
                failed_account_id = account_context.account_id if account_context is not None else None
                exclude_account_id = failed_account_id
                if attempt == 0:
                    logger.warning("Gemini auth error; clearing capture state and retrying once: %s", exc)
                    _clear_client_capture_state(account_context.client if account_context is not None else client)
                    last_error = exc
                    continue
                _record_request_result(error_model, "errors", account_id=account_context.account_id if account_context is not None else None)
                raise _upstream_exception(exc) from exc
            except HTTPException as exc:
                if isinstance(last_error, AuthError) and exc.status_code == 503:
                    raise _upstream_exception(last_error) from exc
                raise
            except RequestError as exc:
                error_model = normalized["model"] if normalized else model_path
                failed_account_id = account_context.account_id if account_context is not None else None
                if (
                    _is_native_worker_unavailable(exc)
                    and failed_account_id
                    and getattr(runtime_state, "account_client_pool", None) is not None
                    and attempt + 1 < max_retries
                ):
                    _record_request_result(error_model, "errors", account_id=failed_account_id)
                    last_error = exc
                    exclude_account_id = failed_account_id
                    logger.warning("Gemini native worker unavailable，已排除当前账号并重试 %d/%d: %s", attempt + 1, max_retries, exc)
                    continue
                _record_request_result(error_model, "errors", account_id=failed_account_id)
                raise _upstream_exception(exc) from exc
            except AistudioError as exc:
                _record_request_result(normalized["model"] if normalized else model_path, "errors", account_id=account_context.account_id if account_context is not None else None)
                raise _upstream_exception(exc) from exc
            except Exception as exc:
                _record_request_result(normalized["model"] if normalized else model_path, "errors", account_id=account_context.account_id if account_context is not None else None)
                logger.error("Gemini error: %s", exc, exc_info=True)
                raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
            finally:
                if normalized is not None and not stream:
                    cleanup_files(normalized["cleanup_paths"])
                    if account_context is not None:
                        await account_context.release()

    raise HTTPException(429, detail={"message": str(last_error), "type": "rate_limit_exceeded"}) from last_error


def _build_gemini_streaming_response(
    *,
    client: AIStudioClient,
    normalized: dict,
    request: Request | None = None,
    account_context: RequestAccountContext | None = None,
    fallback_client: AIStudioClient | None = None,
    affinity_key: str | None = None,
) -> StreamingResponse:
    async def stream_response():
        busy_lock = runtime_state.busy_lock
        if busy_lock is None:
            yield "data: " + json.dumps({"error": {"message": "Server not ready"}}, ensure_ascii=False) + "\n\n"
            cleanup_files(normalized["cleanup_paths"])
            return

        async with busy_lock:
            active_client = client
            active_account_context = account_context
            try:
                final_usage = None
                saw_tool_calls = False
                saw_content = False
                max_stream_attempts = 2
                for stream_attempt in range(max_stream_attempts):
                    if await _request_disconnected(request):
                        logger.info("Gemini stream disconnected before downstream call")
                        return
                    upstream = None
                    try:
                        try:
                            upstream = active_client.stream_generate_content(
                                model=normalized["model"],
                                capture_prompt=normalized["capture_prompt"],
                                capture_images=normalized["capture_images"],
                                contents=normalized["contents"],
                                system_instruction_content=normalized["system_instruction"],
                                tools=normalized["tools"],
                                temperature=normalized["temperature"],
                                top_p=normalized["top_p"],
                                top_k=normalized["top_k"],
                                max_tokens=normalized["max_tokens"],
                                generation_config_overrides=normalized["generation_config_overrides"],
                                sanitize_plain_text=False,
                                force_refresh_capture=stream_attempt > 0,
                            )
                            async for event_type, text in upstream:
                                if await _request_disconnected(request):
                                    logger.info("Gemini stream disconnected during downstream replay")
                                    return
                                if event_type == "body" and text:
                                    saw_content = True
                                    yield "data: " + json.dumps(
                                        {
                                            "candidates": [
                                                {
                                                    "content": {"role": "model", "parts": [{"text": text}]},
                                                    "finishReason": None,
                                                }
                                            ]
                                        },
                                        ensure_ascii=False,
                                    ) + "\n\n"
                                elif event_type == "thinking" and text:
                                    saw_content = True
                                    yield "data: " + json.dumps(
                                        {
                                            "candidates": [
                                                {
                                                    "content": {
                                                        "role": "model",
                                                        "parts": [{"text": text, "thought": True}],
                                                    },
                                                    "finishReason": None,
                                                }
                                            ]
                                        },
                                        ensure_ascii=False,
                                    ) + "\n\n"
                                elif event_type == "tool_calls" and text:
                                    saw_tool_calls = True
                                    saw_content = True
                                    yield "data: " + json.dumps(
                                        {
                                            "candidates": [
                                                {
                                                    "content": {
                                                        "role": "model",
                                                        "parts": to_gemini_parts(
                                                            "",
                                                            function_calls=text if isinstance(text, list) else [],
                                                        ),
                                                    },
                                                    "finishReason": None,
                                                }
                                            ]
                                        },
                                        ensure_ascii=False,
                                    ) + "\n\n"
                                elif event_type == "usage":
                                    final_usage = text if isinstance(text, dict) else None
                            break
                        finally:
                            if upstream is not None:
                                await _close_async_iterator(upstream)
                    except RequestError as exc:
                        if exc.status == 204 and stream_attempt == 0:
                            logger.warning("Gemini stream 收到 204，清理 snapshot 缓存后重试一次")
                            _clear_client_capture_state(active_client)
                            continue
                        if _is_native_worker_unavailable(exc) and not saw_content and stream_attempt + 1 < max_stream_attempts:
                            replacement_context = await _request_replacement_account_context(
                                fallback_client=fallback_client or active_client,
                                model=normalized["model"],
                                account_context=active_account_context,
                                affinity_key=affinity_key,
                            )
                            if replacement_context is not None:
                                logger.warning("Gemini stream native worker unavailable，已排除当前账号并重试下一个账号: %s", exc)
                                _record_request_result(normalized["model"], "errors", account_id=_account_id_for_stats(active_account_context))
                                active_account_context = replacement_context
                                active_client = replacement_context.client
                                continue
                        raise
                    except AuthError as exc:
                        if saw_content:
                            raise
                        _clear_client_capture_state(active_client)
                        if stream_attempt + 1 < max_stream_attempts:
                            replacement_context = await _request_replacement_account_context(
                                fallback_client=fallback_client or active_client,
                                model=normalized["model"],
                                account_context=active_account_context,
                                affinity_key=affinity_key,
                            )
                            if replacement_context is not None:
                                logger.warning("Gemini stream 鉴权异常，已排除当前账号并重试下一个账号: %s", exc)
                                _record_request_result(normalized["model"], "errors", account_id=_account_id_for_stats(active_account_context))
                                active_account_context = replacement_context
                                active_client = replacement_context.client
                                continue
                        if stream_attempt == 0:
                            logger.warning("Gemini stream 鉴权异常，清理 snapshot 缓存后重试一次: %s", exc)
                            continue
                        raise

                if not saw_content:
                    raise RequestError(502, "AI Studio returned no response content")

                _record_request_result(normalized["model"], "success", final_usage, account_id=_account_id_for_stats(active_account_context))
                finish_payload: dict[str, Any] = {
                    "candidates": [{"finishReason": "FUNCTION_CALL" if saw_tool_calls else "STOP"}]
                }
                if final_usage:
                    finish_payload["usageMetadata"] = to_gemini_usage_metadata(final_usage)
                yield "data: " + json.dumps(finish_payload, ensure_ascii=False) + "\n\n"
                yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                logger.info("Gemini stream cancelled by client")
                raise
            except UsageLimitExceeded as exc:
                _record_request_result(normalized["model"], "rate_limited", account_id=_account_id_for_stats(active_account_context), rate_limit_error=exc)
                error_payload = _gemini_stream_error_payload(exc)
                logger.warning("Gemini stream rate limited: %s", error_payload["error"].get("message"))
                yield "data: " + json.dumps(error_payload, ensure_ascii=False) + "\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                _record_request_result(normalized["model"], "errors", account_id=_account_id_for_stats(active_account_context))
                error_payload = _gemini_stream_error_payload(exc)
                if error_payload.get("error", {}).get("status") == "UNIMPLEMENTED":
                    logger.warning("Gemini stream unsupported: %s", error_payload["error"].get("message"))
                else:
                    logger.error("Gemini stream error: %s", exc, exc_info=True)
                yield "data: " + json.dumps(error_payload, ensure_ascii=False) + "\n\n"
                yield "data: [DONE]\n\n"
            finally:
                cleanup_files(normalized["cleanup_paths"])
                if active_account_context is not None:
                    await active_account_context.release()

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
