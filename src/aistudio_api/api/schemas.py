"""HTTP request schemas."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from aistudio_api.config import DEFAULT_IMAGE_MODEL, DEFAULT_TEXT_MODEL


class MessageContent(BaseModel):
    type: str
    text: Optional[str] = None
    image_url: Optional[dict] = None
    file: Optional[dict[str, Any]] = None
    file_data: Optional[str] = None
    filename: Optional[str] = None
    mime_type: Optional[str] = None


class Message(BaseModel):
    role: str
    content: str | list[MessageContent]


class OpenAIFunctionDefinition(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[dict[str, Any]] = None


class OpenAITool(BaseModel):
    type: str
    function: Optional[OpenAIFunctionDefinition] = None


class StreamOptions(BaseModel):
    include_usage: bool = True


class ChatRequest(BaseModel):
    model: str = DEFAULT_TEXT_MODEL
    messages: list[Message]
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_tokens: Optional[int] = None
    tools: Optional[list[OpenAITool]] = None
    thinking: Optional[str | bool] = None
    grounding: Optional[bool] = None
    safety_off: Optional[bool] = None
    response_format: Optional[dict[str, Any] | str] = None
    stream_options: StreamOptions | None = None
    user: Optional[str] = None


class ImageUrl(BaseModel):
    url: str


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    model: str = DEFAULT_IMAGE_MODEL
    n: int = 1
    size: str = "1024x1024"
    response_format: str | None = "b64_json"
    images: Optional[list[str | ImageUrl]] = None
    quality: Optional[str] = None
    style: Optional[str] = None
    background: Optional[str] = None
    moderation: Optional[str] = None
    output_compression: Optional[int] = None
    output_format: Optional[str] = None
    partial_images: Optional[int] = None
    timeout: Optional[int] = None
    user: Optional[str] = None


class ImagePromptOptimizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    model: str = DEFAULT_TEXT_MODEL
    style_template: str = "none"
    thinking: Optional[str | bool] = "off"
    images: Optional[list[str | ImageUrl]] = None


class ImageMessage(BaseModel):
    type: str = "image_url"
    image_url: ImageUrl


class GeminiInlineData(BaseModel):
    mimeType: str
    data: str


class GeminiFileData(BaseModel):
    mimeType: Optional[str] = None
    fileUri: str


class GeminiPart(BaseModel):
    text: Optional[str] = None
    inlineData: Optional[GeminiInlineData] = None
    fileData: Optional[GeminiFileData] = None


class GeminiContent(BaseModel):
    role: Optional[str] = None
    parts: list[GeminiPart]


class GeminiTool(BaseModel):
    codeExecution: Optional[dict[str, Any]] = None
    googleSearch: Optional[dict[str, Any]] = None
    googleSearchRetrieval: Optional[dict[str, Any]] = None
    functionDeclarations: Optional[list[dict[str, Any]]] = None


class GeminiGenerationConfig(BaseModel):
    stopSequences: Optional[list[str]] = None
    temperature: Optional[float] = None
    topP: Optional[float] = None
    topK: Optional[int] = None
    maxOutputTokens: Optional[int] = None
    responseMimeType: Optional[str] = None
    responseSchema: Optional[list[Any] | dict[str, Any]] = None
    presencePenalty: Optional[float] = None
    frequencyPenalty: Optional[float] = None
    responseLogprobs: Optional[bool] = None
    logprobs: Optional[int] = None
    mediaResolution: Optional[list[Any] | int | str] = None
    thinkingConfig: Optional[list[Any] | dict[str, Any]] = None


class GeminiGenerateContentRequest(BaseModel):
    contents: list[GeminiContent]
    systemInstruction: Optional[GeminiContent] = None
    tools: Optional[list[GeminiTool]] = None
    generationConfig: Optional[GeminiGenerationConfig] = None
    safetySettings: Optional[list[dict[str, Any]]] = None
    cachedContent: Optional[str] = None
