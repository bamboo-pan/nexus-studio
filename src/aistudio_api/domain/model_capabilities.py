"""Model capability registry used by API validation and UI metadata."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


MODEL_CREATED = 1700000000
IMAGE_RESPONSE_FORMATS = ("b64_json", "url")
DEFAULT_FILE_INPUT_MIME_TYPES = (
    "image/*",
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "audio/*",
    "video/*",
)
DEFAULT_IMAGE_SIZE = "1024x1024"
DEFAULT_IMAGE_N = 1
IMAGE_N_MIN = 1
IMAGE_N_MAX = 10
DEFAULT_IMAGE_RESPONSE_FORMAT = "b64_json"
SQUARE_IMAGE_PROMPT_SUFFIX = "Use a square 1:1 composition."
IMAGE_IGNORED_OPENAI_FIELDS = ("user",)
IMAGE_UNSUPPORTED_OPENAI_FIELDS = (
    "quality",
    "style",
    "background",
    "moderation",
    "output_compression",
    "output_format",
    "partial_images",
)


@dataclass(frozen=True)
class ImageSizeCapability:
    """Mapping from OpenAI-compatible image size to AI Studio request hints."""

    size: str
    aspect_ratio: str
    output_image_size: str | None = None
    prompt_suffix: str | None = None

    def generation_config_overrides(self) -> dict[str, Any]:
        if self.output_image_size is None:
            return {}
        return {"output_image_size": [None, self.output_image_size]}

    def to_public_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"size": self.size, "aspect_ratio": self.aspect_ratio}
        if self.output_image_size is not None:
            data["output_image_size"] = self.output_image_size
        return data


@dataclass(frozen=True)
class ModelCapabilities:
    id: str
    text_output: bool = True
    image_input: bool = False
    file_input: bool = False
    file_input_mime_types: tuple[str, ...] = ()
    image_output: bool = False
    search: bool = False
    tools: bool = False
    thinking: bool = False
    streaming: bool = True
    structured_output: bool = False
    safety_settings: bool = True
    owned_by: str = "google"
    unsupported_generation_fields: tuple[str, ...] = ()
    image_sizes: dict[str, ImageSizeCapability] = field(default_factory=dict)

    def to_model_dict(self) -> dict[str, Any]:
        capabilities = {
            "text_output": self.text_output,
            "image_input": self.image_input,
            "file_input": self.file_input,
            "file_input_mime_types": list(self.file_input_mime_types),
            "image_output": self.image_output,
            "search": self.search,
            "tools": self.tools,
            "tool_calls": self.tools,
            "thinking": self.thinking,
            "streaming": self.streaming,
            "structured_output": self.structured_output,
            "safety_settings": self.safety_settings,
            "unsupported_generation_fields": list(self.unsupported_generation_fields),
        }
        data: dict[str, Any] = {
            "id": self.id,
            "object": "model",
            "created": MODEL_CREATED,
            "owned_by": self.owned_by,
            "capabilities": capabilities,
        }
        if self.image_sizes:
            sizes = [size.to_public_dict() for size in self.image_sizes.values()]
            size_values = [size["size"] for size in sizes]
            data["image_generation"] = {
                "sizes": sizes,
                "response_formats": list(IMAGE_RESPONSE_FORMATS),
                "defaults": {
                    "size": DEFAULT_IMAGE_SIZE,
                    "n": DEFAULT_IMAGE_N,
                    "response_format": DEFAULT_IMAGE_RESPONSE_FORMAT,
                },
                "parameters": {
                    "size": {"type": "string", "enum": size_values, "default": DEFAULT_IMAGE_SIZE},
                    "n": {"type": "integer", "minimum": IMAGE_N_MIN, "maximum": IMAGE_N_MAX, "default": DEFAULT_IMAGE_N},
                    "response_format": {
                        "type": "string",
                        "enum": list(IMAGE_RESPONSE_FORMATS),
                        "default": DEFAULT_IMAGE_RESPONSE_FORMAT,
                    },
                },
                "unsupported_fields": list(IMAGE_UNSUPPORTED_OPENAI_FIELDS),
                "ignored_fields": list(IMAGE_IGNORED_OPENAI_FIELDS),
            }
        if self.unsupported_generation_fields:
            data["unsupported_generation_fields"] = list(self.unsupported_generation_fields)
        return data


IMAGE_MODEL_UNSUPPORTED_FIELDS = (
    "stop_sequences",
    "response_mime_type",
    "response_schema",
    "presence_penalty",
    "frequency_penalty",
    "response_logprobs",
    "logprobs",
    "media_resolution",
    "thinking_config",
    "request_flag",
)


FLASH_IMAGE_SIZES = {
    "512x512": ImageSizeCapability(
        "512x512",
        aspect_ratio="1:1",
        output_image_size="512",
        prompt_suffix=SQUARE_IMAGE_PROMPT_SUFFIX,
    ),
    "1024x1024": ImageSizeCapability(
        "1024x1024",
        aspect_ratio="1:1",
        output_image_size="1K",
        prompt_suffix=SQUARE_IMAGE_PROMPT_SUFFIX,
    ),
    "1024x1792": ImageSizeCapability(
        "1024x1792",
        aspect_ratio="9:16",
        output_image_size="1K",
        prompt_suffix="Use a vertical 9:16 composition.",
    ),
    "1792x1024": ImageSizeCapability(
        "1792x1024",
        aspect_ratio="16:9",
        output_image_size="1K",
        prompt_suffix="Use a horizontal 16:9 composition.",
    ),
}

PRO_IMAGE_SIZES = {
    **FLASH_IMAGE_SIZES,
    "2048x2048": ImageSizeCapability(
        "2048x2048",
        aspect_ratio="1:1",
        output_image_size="2K",
        prompt_suffix=SQUARE_IMAGE_PROMPT_SUFFIX,
    ),
    "1536x2816": ImageSizeCapability(
        "1536x2816",
        aspect_ratio="9:16",
        output_image_size="2K",
        prompt_suffix="Use a vertical 9:16 composition.",
    ),
    "2816x1536": ImageSizeCapability(
        "2816x1536",
        aspect_ratio="16:9",
        output_image_size="2K",
        prompt_suffix="Use a horizontal 16:9 composition.",
    ),
    "4096x4096": ImageSizeCapability(
        "4096x4096",
        aspect_ratio="1:1",
        output_image_size="4K",
        prompt_suffix=SQUARE_IMAGE_PROMPT_SUFFIX,
    ),
    "2304x4096": ImageSizeCapability(
        "2304x4096",
        aspect_ratio="9:16",
        output_image_size="4K",
        prompt_suffix="Use a vertical 9:16 composition.",
    ),
    "4096x2304": ImageSizeCapability(
        "4096x2304",
        aspect_ratio="16:9",
        output_image_size="4K",
        prompt_suffix="Use a horizontal 16:9 composition.",
    ),
}

DEFAULT_IMAGE_SIZES = FLASH_IMAGE_SIZES


def _text_model(
    model_id: str,
    *,
    image_input: bool = True,
    file_input: bool = True,
    file_input_mime_types: tuple[str, ...] = DEFAULT_FILE_INPUT_MIME_TYPES,
    search: bool = True,
    tools: bool = True,
    thinking: bool = True,
    streaming: bool = True,
    structured_output: bool = True,
) -> ModelCapabilities:
    return ModelCapabilities(
        id=model_id,
        text_output=True,
        image_input=image_input,
        file_input=file_input,
        file_input_mime_types=file_input_mime_types if file_input else (),
        image_output=False,
        search=search,
        tools=tools,
        thinking=thinking,
        streaming=streaming,
        structured_output=structured_output,
    )


def _image_model(
    model_id: str,
    *,
    image_sizes: dict[str, ImageSizeCapability] = DEFAULT_IMAGE_SIZES,
) -> ModelCapabilities:
    return ModelCapabilities(
        id=model_id,
        text_output=True,
        image_input=True,
        file_input=False,
        file_input_mime_types=(),
        image_output=True,
        search=False,
        tools=False,
        thinking=False,
        streaming=False,
        structured_output=False,
        safety_settings=False,
        unsupported_generation_fields=IMAGE_MODEL_UNSUPPORTED_FIELDS,
        image_sizes=image_sizes,
    )


PREFERRED_TEXT_MODEL_IDS = ("gemini-3-flash-preview", "gemini-3.5-flash", "gemma-4-31b-it")


MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    # Gemma 4 series
    "gemma-4-31b-it": _text_model("gemma-4-31b-it", image_input=False, file_input=False),
    "gemma-4-26b-a4b-it": _text_model("gemma-4-26b-a4b-it", image_input=False, file_input=False),
    # Gemini 3 series
    "gemini-3.5-flash": _text_model("gemini-3.5-flash"),
    "gemini-3-flash-preview": _text_model("gemini-3-flash-preview"),
    "gemini-3.1-pro-preview": _text_model("gemini-3.1-pro-preview"),
    "gemini-3.1-flash-lite": _text_model("gemini-3.1-flash-lite"),
    "gemini-3.1-flash-image-preview": _image_model("gemini-3.1-flash-image-preview"),
    "gemini-3-pro-image-preview": _image_model("gemini-3-pro-image-preview", image_sizes=PRO_IMAGE_SIZES),
    "gemini-3.1-flash-live-preview": _text_model("gemini-3.1-flash-live-preview", tools=False, streaming=True),
    "gemini-3.1-flash-tts-preview": _text_model("gemini-3.1-flash-tts-preview", image_input=False, file_input=False, search=False, tools=False, thinking=False, structured_output=False),
    # Latest aliases
    "gemini-pro-latest": _text_model("gemini-pro-latest"),
    "gemini-flash-latest": _text_model("gemini-flash-latest"),
    "gemini-flash-lite-latest": _text_model("gemini-flash-lite-latest"),
}


DYNAMIC_MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {}


GENERIC_TEXT_CAPABILITIES = ModelCapabilities(
    id="unknown",
    text_output=True,
    image_input=True,
    file_input=True,
    file_input_mime_types=DEFAULT_FILE_INPUT_MIME_TYPES,
    image_output=False,
    search=True,
    tools=True,
    thinking=True,
    streaming=True,
    structured_output=True,
)

GENERIC_IMAGE_CAPABILITIES = ModelCapabilities(
    id="unknown-image",
    text_output=True,
    image_input=True,
    file_input=False,
    file_input_mime_types=(),
    image_output=True,
    search=False,
    tools=False,
    thinking=False,
    streaming=False,
    structured_output=False,
    safety_settings=False,
    unsupported_generation_fields=IMAGE_MODEL_UNSUPPORTED_FIELDS,
    image_sizes=DEFAULT_IMAGE_SIZES,
)


def canonical_model_id(model: str) -> str:
    return model.strip().removeprefix("models/")


def _inferred_model_capabilities(model_id: str) -> ModelCapabilities:
    generic = GENERIC_IMAGE_CAPABILITIES if "image" in model_id.lower() else GENERIC_TEXT_CAPABILITIES
    return ModelCapabilities(
        id=model_id,
        text_output=generic.text_output,
        image_input=generic.image_input,
        file_input=generic.file_input,
        file_input_mime_types=generic.file_input_mime_types,
        image_output=generic.image_output,
        search=generic.search,
        tools=generic.tools,
        thinking=generic.thinking,
        streaming=generic.streaming,
        structured_output=generic.structured_output,
        safety_settings=generic.safety_settings,
        unsupported_generation_fields=generic.unsupported_generation_fields,
        image_sizes=generic.image_sizes,
    )


def register_dynamic_model(model: str) -> ModelCapabilities | None:
    model_id = canonical_model_id(model)
    if not model_id:
        return None
    capabilities = MODEL_CAPABILITIES.get(model_id)
    if capabilities is not None:
        return capabilities
    capabilities = _inferred_model_capabilities(model_id)
    DYNAMIC_MODEL_CAPABILITIES[model_id] = capabilities
    return capabilities


def register_dynamic_models(models: Iterable[str]) -> list[ModelCapabilities]:
    registered: list[ModelCapabilities] = []
    seen: set[str] = set()
    for model in models:
        capabilities = register_dynamic_model(model)
        if capabilities is None or capabilities.id in seen:
            continue
        seen.add(capabilities.id)
        registered.append(capabilities)
    return registered


def clear_dynamic_model_capabilities() -> None:
    DYNAMIC_MODEL_CAPABILITIES.clear()


def _all_model_capabilities() -> list[ModelCapabilities]:
    return [*MODEL_CAPABILITIES.values(), *DYNAMIC_MODEL_CAPABILITIES.values()]


def list_model_ids() -> list[str]:
    return [capabilities.id for capabilities in _all_model_capabilities()]


def get_model_capabilities(model: str, *, strict: bool = False) -> ModelCapabilities:
    model_id = canonical_model_id(model)
    capabilities = MODEL_CAPABILITIES.get(model_id)
    if capabilities is None:
        capabilities = DYNAMIC_MODEL_CAPABILITIES.get(model_id)
    if capabilities is not None:
        return capabilities
    if strict:
        raise ValueError(f"Model '{model}' is not registered")
    return _inferred_model_capabilities(model_id)


def list_model_metadata() -> list[dict[str, Any]]:
    preferred_rank = {model_id: index for index, model_id in enumerate(PREFERRED_TEXT_MODEL_IDS)}

    def sort_key(capabilities: ModelCapabilities) -> tuple[int, int, str]:
        if capabilities.id in preferred_rank:
            return (0, preferred_rank[capabilities.id], capabilities.id)
        if capabilities.image_output:
            return (2, 0, capabilities.id)
        return (1, 0, capabilities.id)

    return [capabilities.to_model_dict() for capabilities in sorted(_all_model_capabilities(), key=sort_key)]


def get_model_metadata(model: str) -> dict[str, Any]:
    return get_model_capabilities(model, strict=True).to_model_dict()


def require_model_capabilities(model: str) -> ModelCapabilities:
    return get_model_capabilities(model, strict=True)


@dataclass(frozen=True)
class ImageGenerationPlan:
    model: str
    size: str
    prompt_suffix: str | None
    generation_config_overrides: dict[str, Any]

    def prompt_for(self, prompt: str) -> str:
        if not self.prompt_suffix:
            return prompt
        return f"{prompt}\n\n{self.prompt_suffix}"


def plan_image_generation(model: str, size: str) -> ImageGenerationPlan:
    capabilities = require_model_capabilities(model)
    if not capabilities.image_output:
        raise ValueError(f"Model '{model}' does not support image generation")
    size_capability = capabilities.image_sizes.get(size)
    if size_capability is None:
        supported = ", ".join(capabilities.image_sizes) or "none"
        raise ValueError(f"Model '{model}' does not support image size '{size}'. Supported sizes: {supported}")
    overrides = size_capability.generation_config_overrides()
    return ImageGenerationPlan(
        model=capabilities.id,
        size=size,
        prompt_suffix=size_capability.prompt_suffix,
        generation_config_overrides=overrides,
    )


def validate_chat_capabilities(
    model: str,
    *,
    has_image_input: bool,
    uses_tools: bool,
    uses_search: bool,
    uses_thinking: bool,
    stream: bool,
    uses_structured_output: bool = False,
    has_file_input: bool = False,
    file_input_mime_types: tuple[str, ...] = (),
) -> ModelCapabilities:
    capabilities = require_model_capabilities(model)
    if not capabilities.text_output:
        raise ValueError(f"Model '{model}' does not support text generation")
    if has_image_input and not capabilities.image_input:
        raise ValueError(f"Model '{model}' does not support image input")
    if has_file_input and not capabilities.file_input:
        raise ValueError(f"Model '{model}' does not support file input")
    unsupported_mime_types = [
        mime_type
        for mime_type in file_input_mime_types
        if not mime_type_supported(mime_type, capabilities.file_input_mime_types)
    ]
    if unsupported_mime_types:
        supported = ", ".join(capabilities.file_input_mime_types) or "none"
        rejected = ", ".join(unsupported_mime_types)
        raise ValueError(f"Model '{model}' does not support file MIME type(s): {rejected}. Supported: {supported}")
    if uses_tools and not capabilities.tools:
        raise ValueError(f"Model '{model}' does not support tool calls")
    if uses_search and not capabilities.search:
        raise ValueError(f"Model '{model}' does not support Google Search grounding")
    if uses_thinking and not capabilities.thinking:
        raise ValueError(f"Model '{model}' does not support thinking configuration")
    if uses_structured_output and not capabilities.structured_output:
        raise ValueError(f"Model '{model}' does not support structured output")
    if stream and not capabilities.streaming:
        raise ValueError(f"Model '{model}' does not support streaming responses")
    return capabilities


def unsupported_generation_fields_for(model: str) -> tuple[str, ...]:
    return get_model_capabilities(model).unsupported_generation_fields


def mime_type_supported(mime_type: str, accepted: tuple[str, ...] | list[str]) -> bool:
    normalized = (mime_type or "application/octet-stream").strip().lower()
    for pattern in accepted:
        allowed = (pattern or "").strip().lower()
        if not allowed:
            continue
        if allowed == "*/*" or allowed == normalized:
            return True
        if allowed.endswith("/*") and normalized.startswith(allowed[:-1]):
            return True
    return False