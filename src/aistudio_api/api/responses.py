"""OpenAI-compatible HTTP/SSE response helpers."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any


def new_chat_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


def normalize_usage(usage: dict | None = None) -> dict:
    prompt_details = (usage or {}).get("prompt_tokens_details") or {}
    completion_details = (usage or {}).get("completion_tokens_details") or {}
    cached_tokens = (usage or {}).get("cached_tokens", prompt_details.get("cached_tokens", 0) if isinstance(prompt_details, dict) else 0) or 0
    return {
        "prompt_tokens": (usage or {}).get("prompt_tokens", 0) or 0,
        "completion_tokens": (usage or {}).get("completion_tokens", 0) or 0,
        "total_tokens": (usage or {}).get("total_tokens", 0) or 0,
        "cached_tokens": cached_tokens,
        "prompt_tokens_details": {
            "cached_tokens": cached_tokens,
        },
        "completion_tokens_details": {
            "reasoning_tokens": completion_details.get("reasoning_tokens", 0) or 0,
        },
    }


def to_gemini_usage_metadata(usage: dict | None = None) -> dict:
    prompt_details = (usage or {}).get("prompt_tokens_details") or {}
    completion_details = (usage or {}).get("completion_tokens_details") or {}
    reasoning_tokens = completion_details.get("reasoning_tokens", 0) or 0
    visible_tokens = completion_details.get("visible_tokens")
    candidates_tokens = visible_tokens if visible_tokens is not None else (usage or {}).get("completion_tokens", 0)
    cached_tokens = (usage or {}).get("cached_tokens", prompt_details.get("cached_tokens", 0) if isinstance(prompt_details, dict) else 0) or 0
    return {
        "promptTokenCount": (usage or {}).get("prompt_tokens", 0) or 0,
        "candidatesTokenCount": candidates_tokens or 0,
        "thoughtsTokenCount": reasoning_tokens,
        "cachedContentTokenCount": cached_tokens,
        "totalTokenCount": (usage or {}).get("total_tokens", 0) or 0,
    }


def _normalize_tool_call_delta(tool_call: dict[str, Any], index: int) -> dict[str, Any]:
    delta = dict(tool_call)
    delta.setdefault("index", index)
    function = delta.get("function")
    if isinstance(function, dict):
        function = dict(function)
        arguments = function.get("arguments")
        if arguments is None:
            function.setdefault("arguments", "")
        elif not isinstance(arguments, str):
            function["arguments"] = json.dumps(arguments, ensure_ascii=False)
        delta["function"] = function
    return delta


def sse_chunk(
    chat_id: str,
    model: str,
    content: str,
    finish: str | None = None,
    thinking: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    include_usage: bool = True,
) -> str:
    delta = {"role": "assistant"}
    if content:
        delta["content"] = content
    if thinking:
        delta["thinking"] = thinking
    if tool_calls:
        delta["tool_calls"] = [_normalize_tool_call_delta(tool_call, index) for index, tool_call in enumerate(tool_calls)]
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    data = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }
    if include_usage:
        data["usage"] = None
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_usage_chunk(chat_id: str, model: str, usage: dict | None = None) -> str:
    data = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": normalize_usage(usage),
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_error(
    message: str,
    *,
    error_type: str = "server_error",
    param: str | None = None,
    code: str | None = None,
) -> str:
    data = {"error": {"message": message, "type": error_type, "param": param, "code": code}}
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _function_call_arguments(function_call: dict[str, Any]) -> str:
    if "args" in function_call:
        return json.dumps(function_call["args"], ensure_ascii=False)
    if "arguments" in function_call:
        arguments = function_call["arguments"]
        return arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    raw = function_call.get("raw")
    if isinstance(raw, list) and len(raw) > 1:
        second = raw[1]
        if isinstance(second, str):
            return second
        return json.dumps(second, ensure_ascii=False)
    return "{}"


def to_openai_tool_calls(function_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tool_calls = []
    for idx, function_call in enumerate(function_calls):
        tool_calls.append(
            {
                "index": idx,
                "id": f"call_{uuid.uuid4().hex[:12]}_{idx}",
                "type": "function",
                "function": {
                    "name": function_call.get("name", "unknown"),
                    "arguments": _function_call_arguments(function_call),
                },
            }
        )
    return tool_calls


def to_gemini_parts(
    content: str,
    function_calls: list[dict[str, Any]] | None = None,
    function_responses: list[dict[str, Any]] | None = None,
    thinking: str = "",
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if thinking:
        parts.append({"text": thinking, "thought": True})
    if content:
        parts.append({"text": content})
    for function_call in function_calls or []:
        part = {"functionCall": {"name": function_call.get("name", "unknown")}}
        if "args" in function_call:
            part["functionCall"]["args"] = function_call["args"]
        elif "arguments" in function_call:
            part["functionCall"]["args"] = function_call["arguments"]
        elif isinstance(function_call.get("raw"), list) and len(function_call["raw"]) > 1:
            part["functionCall"]["args"] = function_call["raw"][1]
        parts.append(part)
    for function_response in function_responses or []:
        part = {"functionResponse": {"name": function_response.get("name", "unknown")}}
        if "args" in function_response:
            part["functionResponse"]["response"] = function_response["args"]
        elif "arguments" in function_response:
            part["functionResponse"]["response"] = function_response["arguments"]
        elif isinstance(function_response.get("raw"), list) and len(function_response["raw"]) > 1:
            part["functionResponse"]["response"] = function_response["raw"][1]
        parts.append(part)
    if not parts:
        parts.append({"text": ""})
    return parts


def chat_completion_response(
    model: str,
    content: str,
    thinking: str = "",
    usage: dict | None = None,
    function_calls: list[dict[str, Any]] | None = None,
) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if thinking:
        message["thinking"] = thinking
    if function_calls:
        message["tool_calls"] = to_openai_tool_calls(function_calls)

    finish_reason = "tool_calls" if function_calls else "stop"
    return {
        "id": new_chat_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": normalize_usage(usage),
    }
