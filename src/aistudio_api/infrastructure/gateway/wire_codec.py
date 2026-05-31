"""Codec between typed request objects and reverse-engineered AI Studio wire arrays."""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import replace

from aistudio_api.config import DEFAULT_TEXT_MODEL
from aistudio_api.domain.model_capabilities import get_model_capabilities, unsupported_generation_fields_for

from .wire_types import AistudioContent, AistudioGenerationConfig, AistudioPart, AistudioRequest

TOOLS_TEMPLATES = {
    "code_execution": [[]],
    "google_search": [None, None, None, [None, [[]]]],
}

AI_STUDIO_WIRE_MODEL_ALIASES = {
    "gemini-3.5-flash": "models/gemini-3-flash-preview",
    "models/gemini-3.5-flash": "models/gemini-3-flash-preview",
}


def resolve_aistudio_wire_model(model: str) -> str:
    normalized = (model or "").strip()
    return AI_STUDIO_WIRE_MODEL_ALIASES.get(normalized.lower(), normalized)


def _encode_image(path: str) -> tuple[str, str]:
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as file:
        data = file.read()
    return mime, base64.b64encode(data).decode("ascii")


class AistudioWireCodec:
    MODEL_INDEX = 0
    CONTENTS_INDEX = 1
    SAFETY_INDEX = 2
    GENERATION_CONFIG_INDEX = 3
    SNAPSHOT_INDEX = 4
    SYSTEM_INSTRUCTION_INDEX = 5
    TOOLS_INDEX = 6
    REQUEST_FLAG_INDEX = 10
    CACHED_CONTENT_INDEX = 11
    TIMEZONE_INDEX = 13

    def decode(self, raw_body: str) -> AistudioRequest:
        body = json.loads(raw_body)
        raw_generation_config = body[self.GENERATION_CONFIG_INDEX] if len(body) > self.GENERATION_CONFIG_INDEX else []
        if not isinstance(raw_generation_config, list):
            raw_generation_config = []
        return AistudioRequest(
            model=body[self.MODEL_INDEX],
            contents=self._decode_contents(body[self.CONTENTS_INDEX]),
            safety_settings=body[self.SAFETY_INDEX],
            generation_config=AistudioGenerationConfig(list(raw_generation_config)),
            snapshot=body[self.SNAPSHOT_INDEX] if len(body) > self.SNAPSHOT_INDEX else None,
            system_instruction=self._decode_system_instruction(body[self.SYSTEM_INSTRUCTION_INDEX] if len(body) > self.SYSTEM_INSTRUCTION_INDEX else None),
            tools=body[self.TOOLS_INDEX] if len(body) > self.TOOLS_INDEX else None,
            request_flag=body[self.REQUEST_FLAG_INDEX] if len(body) > self.REQUEST_FLAG_INDEX else None,
            cached_content=body[self.CACHED_CONTENT_INDEX] if len(body) > self.CACHED_CONTENT_INDEX else None,
            location=body[self.TIMEZONE_INDEX] if len(body) > self.TIMEZONE_INDEX else None,
            raw_body=body,
        )

    def encode(self, request: AistudioRequest) -> str:
        body = list(request.raw_body)
        self._ensure_len(body, self.TIMEZONE_INDEX + 1)

        model = request.model
        if not model.startswith("models/"):
            model = f"models/{model}"
        body[self.MODEL_INDEX] = model
        body[self.CONTENTS_INDEX] = [content.to_wire() for content in request.contents]
        body[self.SAFETY_INDEX] = request.safety_settings
        body[self.GENERATION_CONFIG_INDEX] = request.generation_config.values
        body[self.SNAPSHOT_INDEX] = request.snapshot
        body[self.SYSTEM_INSTRUCTION_INDEX] = request.system_instruction.to_wire() if request.system_instruction else None
        body[self.TOOLS_INDEX] = request.tools
        body[self.REQUEST_FLAG_INDEX] = request.request_flag
        body[self.CACHED_CONTENT_INDEX] = request.cached_content
        body[self.TIMEZONE_INDEX] = request.location

        is_image_model = "image" in request.model.lower()
        if not is_image_model:
            if request.tools:
                self._ensure_len(body, self.TIMEZONE_INDEX + 1)
                body[self.TIMEZONE_INDEX] = request.location or [[None, None, "Asia/Shanghai"]]
            else:
                body = body[:11]

        return json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    def rewrite(
        self,
        original_body: str,
        model: str = DEFAULT_TEXT_MODEL,
        snapshot: str | None = None,
        prompt: str | None = None,
        contents: list[AistudioContent] | None = None,
        system_instruction: str | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        images: list[str] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        generation_config_overrides: dict | None = None,
        sanitize_plain_text: bool = True,
        safety_off: bool = False,
        enable_thinking: bool = True,
    ) -> str:
        request = self.decode(original_body)
        wire_model = resolve_aistudio_wire_model(model)
        request.model = wire_model
        capabilities = get_model_capabilities(model)
        if snapshot is not None:
            request.snapshot = snapshot

        if contents is not None:
            request.contents = contents
        elif prompt is not None:
            request.contents = [self._build_user_content(prompt=prompt, images=images)]

        if system_instruction_content is not None:
            request.system_instruction = system_instruction_content
        else:
            request.system_instruction = (
                AistudioContent(role="user", parts=[AistudioPart(text=system_instruction)])
                if system_instruction
                else None
            )

        if max_tokens is not None:
            request.generation_config.max_tokens = max_tokens
        if temperature is not None:
            request.generation_config.temperature = temperature
        if top_p is not None:
            request.generation_config.top_p = top_p
        if top_k is not None:
            request.generation_config.top_k = top_k
        for attr, value in (generation_config_overrides or {}).items():
            if value is None or not hasattr(request.generation_config, attr):
                continue
            setattr(request.generation_config, attr, value)

        # OpenAI chat compatibility should not inherit browser-side structured output
        # or explicit reasoning settings from a previously captured AI Studio request.
        if sanitize_plain_text and not capabilities.image_output:
            request.generation_config.sanitize_for_plain_text()

        if enable_thinking and capabilities.thinking:
            request.generation_config.enable_default_thinking()
        else:
            request.generation_config.thinking_config = None
            request.generation_config.request_flag = None

        self._sanitize_request_for_model(request, model)

        if safety_off:
            request.safety_settings = [[None, None, cat, 4] for cat in [7, 8, 9, 10]]

        if not capabilities.image_output:
            request.tools = tools if tools else None
        else:
            request.tools = None

        return self.encode(request)

    def _sanitize_request_for_model(self, request: AistudioRequest, model: str) -> None:
        config = request.generation_config
        for attr in unsupported_generation_fields_for(model):
            if hasattr(config, attr):
                setattr(config, attr, None)
            if attr == "request_flag":
                request.request_flag = None

    def _build_user_content(self, prompt: str, images: list[str] | None) -> AistudioContent:
        parts = []
        for img_path in images or []:
            parts.append(AistudioPart(inline_data=_encode_image(img_path)))
        parts.append(AistudioPart(text=prompt))
        return AistudioContent(role="user", parts=parts)

    def _decode_contents(self, raw_contents) -> list[AistudioContent]:
        contents = []
        if not isinstance(raw_contents, list):
            return contents
        for item in raw_contents:
            if not isinstance(item, list) or len(item) < 2:
                continue
            raw_parts, role = item[0], item[1]
            parts = []
            if isinstance(raw_parts, list):
                for raw_part in raw_parts:
                    parts.append(self._decode_part(raw_part))
            contents.append(AistudioContent(role=role, parts=parts))
        return contents

    def _decode_part(self, raw_part) -> AistudioPart:
        if (
            isinstance(raw_part, list)
            and len(raw_part) > 5
            and isinstance(raw_part[5], list)
            and len(raw_part[5]) >= 1
            and isinstance(raw_part[5][0], str)
        ):
            return AistudioPart(file_id=raw_part[5][0])
        if (
            isinstance(raw_part, list)
            and len(raw_part) > 2
            and isinstance(raw_part[2], list)
            and len(raw_part[2]) >= 2
        ):
            return AistudioPart(inline_data=(raw_part[2][0], raw_part[2][1]))
        if isinstance(raw_part, list) and len(raw_part) > 1:
            return AistudioPart(text=raw_part[1])
        return AistudioPart()

    def _decode_system_instruction(self, raw_instruction) -> AistudioContent | None:
        if not raw_instruction or not isinstance(raw_instruction, list):
            return None
        if len(raw_instruction) >= 2 and isinstance(raw_instruction[1], str):
            decoded = self._decode_contents([raw_instruction])
            return decoded[0] if decoded else None
        parts = [self._decode_part(raw_part) for raw_part in raw_instruction if isinstance(raw_part, list)]
        if not parts:
            return None
        return AistudioContent(role="user", parts=parts)

    def _ensure_len(self, body: list, size: int):
        while len(body) < size:
            body.append(None)


_codec = AistudioWireCodec()


def modify_body(
    original_body: str,
    model: str = DEFAULT_TEXT_MODEL,
    snapshot: str | None = None,
    prompt: str | None = None,
    contents: list[AistudioContent] | None = None,
    system_instruction: str | None = None,
    system_instruction_content: AistudioContent | None = None,
    tools: list[list] | None = None,
    images: list[str] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    max_tokens: int | None = None,
    generation_config_overrides: dict | None = None,
    sanitize_plain_text: bool = True,
    safety_off: bool = False,
    enable_thinking: bool = True,
) -> str:
    return _codec.rewrite(
        original_body=original_body,
        model=model,
        snapshot=snapshot,
        prompt=prompt,
        contents=contents,
        system_instruction=system_instruction,
        system_instruction_content=system_instruction_content,
        tools=tools,
        images=images,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        max_tokens=max_tokens,
        generation_config_overrides=generation_config_overrides,
        sanitize_plain_text=sanitize_plain_text,
        safety_off=safety_off,
        enable_thinking=enable_thinking,
    )
