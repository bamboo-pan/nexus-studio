"""Streaming replay workflow for chat completions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError, classify_error
from aistudio_api.domain.models import parse_chunk_usage
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.request_rewriter import modify_body
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.stream_parser import IncrementalJSONStreamParser, classify_chunk
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.request_logs import RequestLogStore

logger = logging.getLogger("aistudio")


def _dump_stream_exchange(
    *,
    model: str,
    url: str,
    modified_body: str,
    status_code: int,
    raw_response: str,
) -> None:
    if not settings.dump_raw_response:
        return

    out_dir = Path(settings.dump_raw_response_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_model = model.replace("/", "_")
    timestamp = __import__("time").strftime("%Y%m%d_%H%M%S")
    payload = {
        "kind": "stream_generate_content",
        "model": model,
        "url": url,
        "status_code": status_code,
        "modified_body": json.loads(modified_body),
        "raw_response": raw_response,
    }
    path = out_dir / f"aistudio_stream_generate_content_{safe_model}_{timestamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info("已落盘流式原始请求/响应: %s", path)


def _summarize_error_body(raw_response: str, limit: int = 500) -> str:
    text = raw_response.strip()
    if not text:
        return ""

    try:
        payload = json.loads(text)
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        compact = " ".join(text.split())

    if len(compact) > limit:
        return compact[:limit] + "..."
    return compact


class StreamingGateway:
    def __init__(self, session: BrowserSession | None = None, request_log_store: RequestLogStore | None = None):
        self._session = session
        self._request_log_store = request_log_store

    async def stream_chat(
        self,
        *,
        captured: CapturedRequest | None,
        model: str,
        system_instruction: str | None,
        contents: list[AistudioContent] | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        generation_config_overrides: dict | None = None,
        sanitize_plain_text: bool = True,
        safety_off: bool = False,
        enable_thinking: bool = True,
    ) -> AsyncGenerator[tuple[str, object | None], None]:
        if not captured:
            raise ValueError("captured request is required")
        if self._session is None:
            raise RuntimeError("browser session is required for streaming xhr replay")

        modified_body = modify_body(
            captured.body,
            model=model,
            contents=contents,
            system_instruction=system_instruction,
            system_instruction_content=system_instruction_content,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            sanitize_plain_text=sanitize_plain_text,
            safety_off=safety_off,
            enable_thinking=enable_thinking,
        )
        entry = self._record_request(captured=captured, body=modified_body, model=model)
        started = time.perf_counter()

        parser = IncrementalJSONStreamParser()
        latest_usage: dict | None = None
        raw_parts: list[str] = []
        status_code = 0

        async for event_type, payload in self._session.send_streaming_request(
            body=modified_body,
            url=captured.url,
            headers=captured.replay_headers,
            timeout_ms=settings.timeout_stream * 1000,
        ):
            if event_type == "status" and payload and not status_code:
                status_code = int(payload)
            elif event_type == "chunk" and payload:
                text_payload = payload.decode("utf-8", errors="replace")
                raw_parts.append(text_payload)
                for parsed_chunk in parser.feed(text_payload):
                    usage = parse_chunk_usage(parsed_chunk)
                    if usage:
                        latest_usage = usage
                    ctype, text = classify_chunk(parsed_chunk)
                    if ctype in ("body", "thinking", "tool_calls") and text:
                        yield (ctype, text)

        raw_response = "".join(raw_parts)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._record_response(
            captured=captured,
            entry=entry,
            model=model,
            status_code=status_code,
            raw_response=raw_response,
            elapsed_ms=elapsed_ms,
        )
        _dump_stream_exchange(
            model=model,
            url=captured.url,
            modified_body=modified_body,
            status_code=status_code,
            raw_response=raw_response,
        )
        if status_code != 200:
            detail = _summarize_error_body(raw_response)
            if status_code in (401, 403, 429):
                raise classify_error(status_code, raw_response)
            if detail:
                raise RequestError(status_code, detail)
            raise RequestError(status_code, "")

        yield ("usage", latest_usage)
        yield ("done", None)

    def _record_request(self, *, captured: CapturedRequest, body: str, model: str) -> dict | None:
        if self._request_log_store is None:
            return None
        try:
            return self._request_log_store.save(
                kind="stream_generate_content",
                model=model or captured.model,
                method="POST",
                url=captured.url,
                headers=captured.replay_headers,
                captured_headers=captured.headers,
                body=body,
                transport="browser_stream",
                direction="outbound",
                phase="upstream_request",
            )
        except Exception as exc:
            logger.warning("Request log write failed: %s", exc)
            return None

    def _record_response(
        self,
        *,
        captured: CapturedRequest,
        entry: dict | None,
        model: str,
        status_code: int,
        raw_response: str,
        elapsed_ms: float,
    ) -> None:
        if self._request_log_store is None:
            return
        chain_id = entry.get("chain_id") if isinstance(entry, dict) else None
        try:
            if isinstance(entry, dict) and entry.get("id"):
                self._request_log_store.attach_response(
                    str(entry["id"]),
                    status_code=status_code,
                    response_body=raw_response,
                    elapsed_ms=elapsed_ms,
                )
            self._request_log_store.save(
                kind="stream_generate_content",
                model=model or captured.model,
                method="POST",
                url=captured.url,
                headers={},
                body="",
                transport="browser_stream",
                chain_id=chain_id,
                direction="inbound",
                phase="upstream_response",
                status_code=status_code,
                response_body=raw_response,
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            logger.warning("Request log response write failed: %s", exc)
