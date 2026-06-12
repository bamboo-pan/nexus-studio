"""Browser-backed AI Studio client facade."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from aistudio_api.config import DEFAULT_CAMOUFOX_PORT, DEFAULT_IMAGE_MODEL, DEFAULT_TEXT_MODEL, DEFAULT_WARMUP_TEXT_MODEL, DEFAULT_WARMUP_TEXT_MODEL_CANDIDATES, settings
from aistudio_api.domain.errors import AuthError, ModelNotFoundError, RequestError, UsageLimitExceeded, classify_error
from aistudio_api.domain.models import ModelOutput, parse_image_output, parse_text_output
from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache
from aistudio_api.infrastructure.gateway.capture import CapturedRequest, RequestCaptureService
from aistudio_api.infrastructure.gateway.request_rewriter import TOOLS_TEMPLATES, modify_body
from aistudio_api.infrastructure.gateway.replay import RequestReplayService
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_codec import resolve_aistudio_wire_model
from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioGenerationConfig, AistudioPart, AistudioRequest
from aistudio_api.infrastructure.request_logs import RequestLogStore

logger = logging.getLogger("aistudio")

_snapshot_cache = SnapshotCache()

PURE_HTTP_GENERATE_CONTENT_UNSUPPORTED = (
    "Pure HTTP mode is experimental and currently supports only single-turn "
    "non-streaming plain-text prompts; it does not support images, tools, "
    "thinking, system instructions, multi-turn conversations, safety overrides, "
    "or structured generation config. Disable AISTUDIO_USE_PURE_HTTP or use browser mode "
    "for full compatibility"
)

_IMAGE_REPLAY_MODEL_ALIASES = {
    "gemini-3.1-flash-image-preview": "gemini-3.1-flash-image",
    "gemini-3-pro-image-preview": "gemini-3-pro-image",
}

_STARTUP_WARMUP_NAVIGATION_TIMEOUT_MS = 30_000
_STARTUP_WARMUP_CHAT_READY_TIMEOUT_MS = 30_000
_STARTUP_WARMUP_BOTGUARD_TIMEOUT_MS = 15_000
_STARTUP_WARMUP_TEMPLATE_CAPTURE_TIMEOUT_MS = 30_000
_WARMUP_PROBE_TIMEOUT_SECONDS = 30
_DEFAULT_CAPTURE_TIMEOUT_SECONDS = 30
_DEFAULT_ACCOUNT_NATIVE_REQUEST_TIMEOUT_SECONDS = 120

AI_STUDIO_GENERATE_CONTENT_AUTH_GUIDANCE = (
    "AI Studio GenerateContent permission check failed. The browser can open AI Studio, "
    "but the stored auth state cannot generate content. Re-login or import the browser "
    "session for the Google account that can generate in AI Studio; Playwright storage state "
    "must be captured after AI Studio fully loads."
)


def _configured_startup_capture_timeout_ms(default_ms: int) -> int:
    try:
        configured_ms = int(settings.timeout_capture) * 1000
    except (TypeError, ValueError):
        return default_ms
    if configured_ms <= _DEFAULT_CAPTURE_TIMEOUT_SECONDS * 1000:
        return default_ms
    return max(default_ms, configured_ms)


def _configured_warmup_probe_timeout_seconds() -> int:
    if "AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS" in os.environ:
        try:
            configured_seconds = int(settings.warmup_probe_timeout_seconds)
        except (TypeError, ValueError):
            return _WARMUP_PROBE_TIMEOUT_SECONDS
        return max(_WARMUP_PROBE_TIMEOUT_SECONDS, configured_seconds)
    if "AISTUDIO_TIMEOUT_REPLAY" not in os.environ:
        return _WARMUP_PROBE_TIMEOUT_SECONDS
    try:
        configured_seconds = int(settings.timeout_replay)
    except (TypeError, ValueError):
        return _WARMUP_PROBE_TIMEOUT_SECONDS
    return max(_WARMUP_PROBE_TIMEOUT_SECONDS, configured_seconds)


def _expected_wire_model(model: str) -> str:
    wire_model = resolve_aistudio_wire_model(model)
    if not wire_model.startswith("models/"):
        wire_model = f"models/{wire_model}"
    return wire_model


def _split_model_candidates(raw: str | None) -> list[str]:
    if not raw:
        return []
    normalized = raw.replace(";", ",").replace("\n", ",")
    return [candidate.strip() for candidate in normalized.split(",") if candidate.strip()]


def _warmup_text_model_candidates() -> list[str]:
    candidates: list[str] = [DEFAULT_WARMUP_TEXT_MODEL]
    candidates.extend(_split_model_candidates(os.getenv("AISTUDIO_WARMUP_TEXT_MODEL_CANDIDATES", DEFAULT_WARMUP_TEXT_MODEL_CANDIDATES)))
    candidates.extend(_split_model_candidates(os.getenv("SYSTEM_TEST_MODEL_CANDIDATES", "")))
    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped or [DEFAULT_WARMUP_TEXT_MODEL]


def _warmup_model_unavailable_error(exc: BaseException) -> bool:
    if isinstance(exc, ModelNotFoundError):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "ai studio text model not selected",
            "text model not selected",
            "text_model_not_found",
            "current_text_model_not_found",
            "not_text_model",
        )
    )


def _warmup_model_candidate_fallback_error(exc: BaseException) -> bool:
    return _warmup_model_unavailable_error(exc) or isinstance(exc, UsageLimitExceeded)


def _raise_warmup_model_candidates_exhausted(last_model_error: BaseException | None) -> None:
    if isinstance(last_model_error, UsageLimitExceeded):
        raise last_model_error
    raise ModelNotFoundError("AI Studio warmup could not select any configured text model") from last_model_error


def image_replay_model_id(model: str) -> str:
    raw = str(model or "").strip()
    prefix = "models/" if raw.startswith("models/") else ""
    model_id = raw.removeprefix("models/")
    return f"{prefix}{_IMAGE_REPLAY_MODEL_ALIASES.get(model_id, model_id)}"


class AIStudioClient:
    def __init__(
        self,
        port: int = DEFAULT_CAMOUFOX_PORT,
        use_pure_http: bool = False,
        snapshot_cache: SnapshotCache | None = None,
        request_log_store: RequestLogStore | None = None,
    ):
        self.port = port
        self._use_pure_http = use_pure_http
        self._snapshot_cache = snapshot_cache or _snapshot_cache
        self._request_log_store = request_log_store
        self._captured: Optional[CapturedRequest] = None
        
        if use_pure_http:
            # Pure HTTP mode: no browser needed for capture
            from aistudio_api.infrastructure.gateway.pure_capture import PureHttpCaptureService
            self._capture_service = PureHttpCaptureService(self._snapshot_cache)
            self._session = None
            self._replay_service = RequestReplayService(session=None, request_log_store=request_log_store)
        else:
            # Browser mode: uses browser for capture and replay
            self._session = BrowserSession(port=port)
            self._capture_service = RequestCaptureService(self._session, self._snapshot_cache)
            self._replay_service = RequestReplayService(session=self._session, request_log_store=request_log_store)
        
        self._streaming_gateway = StreamingGateway(session=self._session, request_log_store=request_log_store)

    @property
    def is_pure_http(self) -> bool:
        return self._use_pure_http

    async def warmup(
        self,
        *,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
        botguard_timeout_ms: int | None = None,
        template_capture_timeout_ms: int | None = None,
    ) -> None:
        """预热浏览器，启动 Camoufox 并准备首个文本请求所需的捕获模板。"""
        navigation_timeout_ms = (
            _configured_startup_capture_timeout_ms(_STARTUP_WARMUP_NAVIGATION_TIMEOUT_MS)
            if navigation_timeout_ms is None
            else navigation_timeout_ms
        )
        chat_ready_timeout_ms = (
            _configured_startup_capture_timeout_ms(_STARTUP_WARMUP_CHAT_READY_TIMEOUT_MS)
            if chat_ready_timeout_ms is None
            else chat_ready_timeout_ms
        )
        botguard_timeout_ms = (
            _configured_startup_capture_timeout_ms(_STARTUP_WARMUP_BOTGUARD_TIMEOUT_MS)
            if botguard_timeout_ms is None
            else botguard_timeout_ms
        )
        template_capture_timeout_ms = (
            _configured_startup_capture_timeout_ms(_STARTUP_WARMUP_TEMPLATE_CAPTURE_TIMEOUT_MS)
            if template_capture_timeout_ms is None
            else template_capture_timeout_ms
        )
        if self._session is not None:
            try:
                await self._warmup_with_authuser_failover(
                    navigation_timeout_ms=navigation_timeout_ms,
                    chat_ready_timeout_ms=chat_ready_timeout_ms,
                    botguard_timeout_ms=botguard_timeout_ms,
                    template_capture_timeout_ms=template_capture_timeout_ms,
                )
                logger.info("浏览器预热完成，文本请求模板已就绪")
            except Exception as exc:
                logger.warning("浏览器文本请求模板预热失败，仅完成页面预热: %s", exc)
                raise

    async def warmup_account_text_native(self) -> None:
        """Warm up account-backed text GenerateContent through the native worker pool."""
        if self._session is None:
            return
        probe_worker = getattr(self._session, "probe_native_worker_generate_content", None)
        if not callable(probe_worker):
            await self.warmup()
            return
        timeout_seconds = _configured_warmup_probe_timeout_seconds()
        last_model_error: BaseException | None = None
        for warmup_model in _warmup_text_model_candidates():
            logger.info(
                "AI Studio account warmup: probing native UI worker pool for model=%s",
                warmup_model,
            )
            try:
                status, raw, wire_model = await probe_worker(model=warmup_model, timeout_ms=timeout_seconds * 1000)
                self._validate_generate_content_probe_result(
                    model=warmup_model,
                    status=status,
                    raw=raw,
                    wire_model=wire_model,
                    source="Native UI worker warmup",
                )
                logger.info("AI Studio account warmup: native UI worker pool is ready for model=%s", warmup_model)
                return
            except Exception as exc:
                if not _warmup_model_candidate_fallback_error(exc):
                    raise
                last_model_error = exc
                self.clear_capture_state()
                logger.warning("AI Studio account warmup model unavailable or quota-limited; trying next candidate: model=%s error=%s", warmup_model, exc)
        _raise_warmup_model_candidates_exhausted(last_model_error)

    async def _warmup_with_authuser_failover(
        self,
        *,
        navigation_timeout_ms: int,
        chat_ready_timeout_ms: int,
        botguard_timeout_ms: int,
        template_capture_timeout_ms: int,
    ) -> None:
        if self._session is None:
            return
        last_auth_error: AuthError | None = None
        last_model_error: BaseException | None = None
        while True:
            probe_native = getattr(self._session, "probe_native_generate_content", None)
            if callable(probe_native):
                selected_warmup_model = ""
                route_advanced = False
                for warmup_model in _warmup_text_model_candidates():
                    logger.info(
                        "AI Studio warmup: probing native GenerateContent permission for model=%s before template capture",
                        warmup_model,
                    )
                    try:
                        await self._probe_generate_content_permission(None, model=warmup_model)
                        logger.info("AI Studio warmup: GenerateContent permission probe passed for model=%s", warmup_model)
                        selected_warmup_model = warmup_model
                        break
                    except (ModelNotFoundError, UsageLimitExceeded) as exc:
                        last_model_error = exc
                        self.clear_capture_state()
                        logger.info("AI Studio warmup model unavailable or quota-limited for current authuser route; trying next model: %s", exc)
                        continue
                    except AuthError as exc:
                        last_auth_error = exc
                        self.clear_capture_state()
                        advance = getattr(self._session, "advance_chat_route_after_auth_failure", None)
                        if not callable(advance) or not await advance():
                            raise last_auth_error
                        logger.info("AI Studio GenerateContent probe failed for current authuser route; trying next authuser candidate: %s", exc)
                        route_advanced = True
                        break
                if route_advanced:
                    continue
                if not selected_warmup_model:
                    advance = getattr(self._session, "advance_chat_route_after_auth_failure", None)
                    if callable(advance) and await advance():
                        continue
                    _raise_warmup_model_candidates_exhausted(last_model_error)

                logger.info(
                    "AI Studio warmup: ensuring browser context for model=%s",
                    selected_warmup_model,
                )
                await self._session.ensure_context(
                    navigation_timeout_ms=navigation_timeout_ms,
                    chat_ready_timeout_ms=chat_ready_timeout_ms,
                )
                logger.info(
                    "AI Studio warmup: capturing native template for model=%s",
                    selected_warmup_model,
                )
                await self._capture_service.warmup(
                    prompt="1",
                    model=selected_warmup_model,
                    rewrite_body=False,
                    retry_template_capture=False,
                    navigation_timeout_ms=navigation_timeout_ms,
                    chat_ready_timeout_ms=chat_ready_timeout_ms,
                    botguard_timeout_ms=botguard_timeout_ms,
                    template_capture_timeout_ms=template_capture_timeout_ms,
                    template_recovery_attempts=1,
                )
                return

            route_advanced = False
            for warmup_model in _warmup_text_model_candidates():
                logger.info(
                    "AI Studio warmup: ensuring browser context for model=%s",
                    warmup_model,
                )
                await self._session.ensure_context(
                    navigation_timeout_ms=navigation_timeout_ms,
                    chat_ready_timeout_ms=chat_ready_timeout_ms,
                )
                logger.info(
                    "AI Studio warmup: capturing native template for model=%s",
                    warmup_model,
                )
                try:
                    captured = await self._capture_service.warmup(
                        prompt="1",
                        model=warmup_model,
                        rewrite_body=False,
                        retry_template_capture=False,
                        navigation_timeout_ms=navigation_timeout_ms,
                        chat_ready_timeout_ms=chat_ready_timeout_ms,
                        botguard_timeout_ms=botguard_timeout_ms,
                        template_capture_timeout_ms=template_capture_timeout_ms,
                        template_recovery_attempts=1,
                    )
                    logger.info(
                        "AI Studio warmup: probing GenerateContent permission for model=%s",
                        warmup_model,
                    )
                    await self._probe_generate_content_permission(captured, model=warmup_model)
                    logger.info("AI Studio warmup: GenerateContent permission probe passed for model=%s", warmup_model)
                    return
                except (ModelNotFoundError, UsageLimitExceeded) as exc:
                    last_model_error = exc
                    self.clear_capture_state()
                    logger.info("AI Studio warmup model unavailable or quota-limited for current authuser route; trying next model: %s", exc)
                    continue
                except AuthError as exc:
                    last_auth_error = exc
                    self.clear_capture_state()
                    advance = getattr(self._session, "advance_chat_route_after_auth_failure", None)
                    if not callable(advance) or not await advance():
                        raise last_auth_error
                    logger.info("AI Studio GenerateContent probe failed for current authuser route; trying next candidate")
                    route_advanced = True
                    break
            if route_advanced:
                continue
            advance = getattr(self._session, "advance_chat_route_after_auth_failure", None)
            if callable(advance) and await advance():
                continue
            _raise_warmup_model_candidates_exhausted(last_model_error)

    async def _probe_generate_content_permission(self, captured: CapturedRequest | None, *, model: str) -> None:
        probe_native = getattr(self._session, "probe_native_generate_content", None)
        if callable(probe_native):
            timeout_seconds = _configured_warmup_probe_timeout_seconds()
            status, raw, wire_model = await probe_native(model=model, timeout_ms=timeout_seconds * 1000)
            self._validate_generate_content_probe_result(
                model=model,
                status=status,
                raw=raw,
                wire_model=wire_model,
                source="Browser native warmup",
            )
            return

        if captured is None:
            raise RuntimeError("warmup replay probe requires a captured request")

        status, raw = await self._replay_request(
            captured,
            body=captured.body,
            kind="warmup_probe",
            model=model,
            timeout=_configured_warmup_probe_timeout_seconds(),
        )
        if status == 200:
            return
        raw_text = raw.decode("utf-8", errors="replace")
        classified = classify_error(status, raw_text)
        if status in (401, 403):
            raise type(classified)(f"{AI_STUDIO_GENERATE_CONTENT_AUTH_GUIDANCE} Upstream returned HTTP {status}: {raw_text[:200]}") from classified
        raise classified

    def _validate_generate_content_probe_result(
        self,
        *,
        model: str,
        status: int,
        raw: bytes,
        wire_model: str,
        source: str,
    ) -> None:
        expected_model = _expected_wire_model(model)
        raw_text = raw.decode("utf-8", errors="replace")
        logger.info(
            "AI Studio warmup native probe result: requested=%s, wire_model=%s, status=%s, response_head=%s",
            expected_model,
            wire_model or "<unknown>",
            status,
            raw_text[:120],
        )
        if wire_model != expected_model:
            raise AuthError(
                f"{AI_STUDIO_GENERATE_CONTENT_AUTH_GUIDANCE} {source} sent "
                f"{wire_model or '<unknown>'} instead of {expected_model}."
            )
        if status == 200:
            return
        classified = classify_error(status, raw_text)
        if status in (401, 403):
            raise type(classified)(f"{AI_STUDIO_GENERATE_CONTENT_AUTH_GUIDANCE} Upstream returned HTTP {status}: {raw_text[:200]}") from classified
        raise classified

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def switch_auth(self, auth_file: str | None) -> None:
        """切换账号的 auth 文件。"""
        if self._session is not None:
            await self._session.switch_auth(auth_file)
        self.clear_capture_state()

    async def list_available_models(self) -> list[str]:
        if self._session is None:
            return []
        return await self._session.list_available_models()

    def clear_snapshot_cache(self) -> None:
        """清除 snapshot 缓存及其依赖的 capture 模板。"""
        self.clear_capture_state()

    def clear_capture_state(self) -> None:
        """清除依赖当前浏览器认证态的 capture 缓存。"""
        self._captured = None
        clear_templates = getattr(self._capture_service, "clear_templates", None)
        if callable(clear_templates):
            clear_templates()
        clear_session_templates = getattr(self._session, "clear_templates", None)
        if callable(clear_session_templates):
            clear_session_templates()
        self._snapshot_cache.clear()

    def _dump_raw_exchange(
        self,
        *,
        kind: str,
        model: str,
        capture_prompt: str,
        modified_body: str,
        raw_response: str,
    ) -> None:
        if not settings.dump_raw_response:
            return

        out_dir = Path(settings.dump_raw_response_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_model = model.replace("/", "_")
        timestamp = __import__("time").strftime("%Y%m%d_%H%M%S")
        payload = {
            "kind": kind,
            "model": model,
            "capture_prompt": capture_prompt,
            "modified_body": json.loads(modified_body),
            "raw_response": raw_response,
        }
        path = out_dir / f"aistudio_{kind}_{safe_model}_{timestamp}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        logger.info("已落盘原始请求/响应: %s", path)

    async def capture_request(
        self,
        prompt: str,
        model: str = DEFAULT_TEXT_MODEL,
        images: Optional[list[str]] = None,
        contents: Optional[list[AistudioContent]] = None,
        force_refresh: bool = False,
    ) -> Optional[CapturedRequest]:
        return await self._capture_service.capture(
            prompt=prompt,
            model=model,
            images=images,
            contents=contents,
            force_refresh=force_refresh,
        )

    async def replay(self, body: str, timeout: int | None = None) -> tuple[int, bytes]:
        return await self._replay_service.replay(self._captured, body=body, timeout=timeout)

    async def stream_chat(
        self,
        *,
        prompt: str,
        model: str = DEFAULT_TEXT_MODEL,
        images: Optional[list[str]] = None,
        system_instruction: str | None = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        max_tokens: Optional[int] = None,
        tools: list[list] | None = None,
    ):
        merged_tools = list(tools or [])
        async for event in self.stream_generate_content(
            model=model,
            capture_prompt=prompt,
            capture_images=images,
            contents=[self._build_user_content(prompt=prompt, images=images)],
            system_instruction_content=(
                AistudioContent(role="user", parts=[AistudioPart(text=system_instruction)])
                if system_instruction
                else None
            ),
            tools=merged_tools or None,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
        ):
            yield event

    async def stream_generate_content(
        self,
        *,
        model: str = DEFAULT_TEXT_MODEL,
        capture_prompt: str,
        capture_images: Optional[list[str]] = None,
        contents: Optional[list[AistudioContent]] = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        max_tokens: Optional[int] = None,
        generation_config_overrides: dict | None = None,
        sanitize_plain_text: bool = True,
        force_refresh_capture: bool = False,
        safety_off: bool = False,
        enable_thinking: bool = True,
    ):
        if self._use_pure_http:
            raise RequestError(501, "Pure HTTP mode is experimental and does not support streaming; disable AISTUDIO_USE_PURE_HTTP or use browser mode")
        native_body = self._account_native_text_body(
            model=model,
            capture_prompt=capture_prompt,
            capture_images=capture_images,
            contents=contents,
            system_instruction_content=system_instruction_content,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            safety_off=safety_off,
        )
        if native_body is not None:
            async for event in self._stream_account_native_text_body(body=native_body, model=model):
                yield event
            return
        captured = await self.capture_request(
            prompt=capture_prompt,
            model=model,
            images=capture_images,
            contents=contents,
            force_refresh=force_refresh_capture,
        )
        async for event in self._streaming_gateway.stream_chat(
            captured=captured,
            model=model,
            system_instruction=None,
            contents=contents,
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
        ):
            yield event

    async def chat(
        self,
        prompt: str,
        model: str = DEFAULT_TEXT_MODEL,
        system_instruction: Optional[str] = None,
        code_execution: bool = False,
        google_search: bool = False,
        images: Optional[list[str]] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        max_tokens: Optional[int] = None,
        tools: list[list] | None = None,
    ) -> ModelOutput:
        merged_tools = list(tools or [])
        if code_execution or google_search:
            if code_execution:
                merged_tools.append(TOOLS_TEMPLATES["code_execution"])
            if google_search:
                merged_tools.append(TOOLS_TEMPLATES["google_search"])

        return await self.generate_content(
            model=model,
            capture_prompt=prompt,
            capture_images=images,
            contents=[self._build_user_content(prompt=prompt, images=images)],
            system_instruction_content=(
                AistudioContent(role="user", parts=[AistudioPart(text=system_instruction)])
                if system_instruction
                else None
            ),
            tools=merged_tools or None,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
        )

    async def generate_content(
        self,
        *,
        model: str = DEFAULT_TEXT_MODEL,
        capture_prompt: str,
        capture_images: Optional[list[str]] = None,
        contents: Optional[list[AistudioContent]] = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        max_tokens: Optional[int] = None,
        generation_config_overrides: dict | None = None,
        sanitize_plain_text: bool = True,
        safety_off: bool = False,
        enable_thinking: bool = True,
    ) -> ModelOutput:
        if self._use_pure_http and not self._pure_http_generate_content_supported(
            capture_images=capture_images,
            contents=contents,
            system_instruction_content=system_instruction_content,
            tools=tools,
            generation_config_overrides=generation_config_overrides,
            safety_off=safety_off,
        ):
            raise RequestError(
                501,
                PURE_HTTP_GENERATE_CONTENT_UNSUPPORTED,
            )
        native_body = self._account_native_text_body(
            model=model,
            capture_prompt=capture_prompt,
            capture_images=capture_images,
            contents=contents,
            system_instruction_content=system_instruction_content,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            safety_off=safety_off,
        )
        if native_body is not None:
            return await self._generate_account_native_text_body(body=native_body, model=model)
        logger.info("拦截请求: %r", f"{capture_prompt[:20]}...")
        captured = await self.capture_request(capture_prompt, model=model, images=capture_images, contents=contents)
        if not captured:
            raise RequestError(0, "无法拦截请求")

        modified_body = modify_body(
            captured.body,
            model=model,
            contents=contents,
            system_instruction_content=system_instruction_content,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            sanitize_plain_text=sanitize_plain_text,
            safety_off=safety_off,
            enable_thinking=False if self._use_pure_http else enable_thinking,
        )

        status, raw = await self._replay_request(captured, body=modified_body, kind="generate_content", model=model)
        raw_text = raw.decode("utf-8", errors="replace")
        self._dump_raw_exchange(
            kind="generate_content",
            model=model,
            capture_prompt=capture_prompt,
            modified_body=modified_body,
            raw_response=raw_text,
        )
        if status != 200:
            raise classify_error(status, raw_text)
        output = parse_text_output(raw_text)
        output.model = model
        return output

    async def generate_image(
        self,
        prompt: str,
        model: str = DEFAULT_IMAGE_MODEL,
        save_path: Optional[str] = None,
        generation_config_overrides: dict | None = None,
        images: Optional[list[str]] = None,
        timeout: int | None = None,
    ) -> ModelOutput:
        if self._use_pure_http:
            raise RequestError(501, "Pure HTTP mode is experimental and does not support image generation; use browser mode")
        logger.info("生图请求: %r", f"{prompt[:20]}...")
        contents = [self._build_user_content(prompt=prompt, images=images)] if images else None
        captured = await self.capture_request(prompt, model=model, images=images, contents=contents)
        if not captured:
            raise RequestError(0, "无法拦截请求")

        replay_model = image_replay_model_id(model)
        modified_body = modify_body(
            captured.body,
            model=replay_model,
            prompt=None if contents else prompt,
            contents=contents,
            generation_config_overrides=generation_config_overrides,
            sanitize_plain_text=False,
            enable_thinking=False,
        )
        status, raw = await self._replay_request(captured, body=modified_body, timeout=timeout, kind="generate_image", model=replay_model)
        raw_text = raw.decode("utf-8", errors="replace")
        self._dump_raw_exchange(
            kind="generate_image",
            model=model,
            capture_prompt=prompt,
            modified_body=modified_body,
            raw_response=raw_text,
        )
        if status != 200:
            raise classify_error(status, raw_text)
        output = parse_image_output(raw_text)
        output.model = model

        if output.images:
            img = output.images[0]
            ext = "jpg" if "jpeg" in img.mime else "png"
            path = save_path if save_path and save_path.endswith(f".{ext}") else (
                f"{save_path}.{ext}" if save_path else f"/tmp/aistudio_generated.{ext}"
            )
            with open(path, "wb") as file:
                file.write(img.data)
            logger.info("图片已保存: %s (%s bytes)", path, img.size)

        return output

    def _account_native_text_body(
        self,
        *,
        model: str,
        capture_prompt: str,
        capture_images: Optional[list[str]],
        contents: Optional[list[AistudioContent]],
        system_instruction_content: AistudioContent | None,
        tools: list[list] | None,
        temperature: Optional[float],
        top_p: Optional[float],
        top_k: Optional[int],
        max_tokens: Optional[int],
        generation_config_overrides: dict | None,
        safety_off: bool,
    ) -> str | None:
        session = self._session
        if (
            self._use_pure_http
            or session is None
            or not bool(getattr(session, "has_account_auth", False))
            or not callable(getattr(session, "send_account_native_generate_content_body", None))
        ):
            return None
        if capture_images or tools or generation_config_overrides or safety_off:
            return None
        if any(value is not None for value in (temperature, top_p, top_k, max_tokens)):
            return None

        native_contents = contents or [self._build_user_content(prompt=capture_prompt, images=None)]
        if not self._text_only_contents(native_contents):
            return None
        if system_instruction_content is not None and not self._text_only_contents([system_instruction_content]):
            return None
        if not self._contents_have_text(native_contents, system_instruction_content):
            return None

        wire_model = resolve_aistudio_wire_model(model)
        request = AistudioRequest(
            model=wire_model,
            contents=native_contents,
            safety_settings=None,
            generation_config=AistudioGenerationConfig([]),
            snapshot=None,
            system_instruction=system_instruction_content,
            tools=None,
            raw_body=[],
        )
        return AistudioWireCodec().encode(request)

    def _text_only_contents(self, contents: list[AistudioContent]) -> bool:
        for content in contents:
            for part in content.parts:
                if part.inline_data is not None or part.file_id is not None:
                    return False
        return True

    def _contents_have_text(self, contents: list[AistudioContent], system_instruction_content: AistudioContent | None) -> bool:
        for content in [*(contents or []), *([system_instruction_content] if system_instruction_content is not None else [])]:
            if any(part.text and str(part.text).strip() for part in content.parts):
                return True
        return False

    async def _generate_account_native_text_body(self, *, body: str, model: str) -> ModelOutput:
        session = self._session
        if session is None:
            raise RequestError(503, "native UI worker unavailable: browser session is not initialized")
        logger.info("AI Studio account native worker direct GenerateContent: model=%s", model)
        status, raw = await session.send_account_native_generate_content_body(
            body=body,
            timeout_ms=self._account_native_request_timeout_ms(settings.timeout_replay),
            retry_statuses=(401, 403),
        )
        raw_text = raw.decode("utf-8", errors="replace")
        if status != 200:
            raise classify_error(status, raw_text)
        output = parse_text_output(raw_text)
        output.model = model
        return output

    async def _stream_account_native_text_body(self, *, body: str, model: str):
        session = self._session
        if session is None:
            raise RequestError(503, "native UI worker unavailable: browser session is not initialized")
        logger.info("AI Studio account native worker direct stream GenerateContent: model=%s", model)
        status, raw = await session.send_account_native_generate_content_body(
            body=body,
            timeout_ms=self._account_native_request_timeout_ms(settings.timeout_stream),
            retry_statuses=(401, 403),
        )
        raw_text = raw.decode("utf-8", errors="replace")
        if status != 200:
            raise classify_error(status, raw_text)
        output = parse_text_output(raw_text)
        if output.thinking:
            yield ("thinking", output.thinking)
        if output.text:
            yield ("body", output.text)
        if output.function_calls:
            yield ("tool_calls", output.function_calls)
        yield ("usage", output.usage)
        yield ("done", None)

    def _account_native_request_timeout_ms(self, configured_seconds: int) -> int:
        try:
            seconds = int(configured_seconds or 0)
        except (TypeError, ValueError):
            seconds = 0
        return max(_DEFAULT_ACCOUNT_NATIVE_REQUEST_TIMEOUT_SECONDS, seconds) * 1000

    def _pure_http_generate_content_supported(
        self,
        *,
        capture_images: Optional[list[str]],
        contents: Optional[list[AistudioContent]],
        system_instruction_content: AistudioContent | None,
        tools: list[list] | None,
        generation_config_overrides: dict | None,
        safety_off: bool,
    ) -> bool:
        if capture_images or system_instruction_content or tools or generation_config_overrides or safety_off:
            return False
        if contents is None:
            return True
        if len(contents) != 1 or contents[0].role != "user":
            return False
        return all(part.text is not None and part.inline_data is None and part.file_id is None for part in contents[0].parts)

    async def _replay_request(
        self,
        captured: CapturedRequest,
        *,
        body: str,
        kind: str,
        model: str,
        timeout: int | None = None,
    ) -> tuple[int, bytes]:
        import inspect

        replay = self._replay_service.replay
        try:
            parameters = inspect.signature(replay).parameters
            accepts_metadata = "kind" in parameters or "model" in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            accepts_metadata = True
        if accepts_metadata:
            return await replay(captured, body=body, timeout=timeout, kind=kind, model=model)
        return await replay(captured, body=body, timeout=timeout)

    def _build_user_content(self, prompt: str, images: Optional[list[str]] = None) -> AistudioContent:
        import base64
        import mimetypes

        parts = []
        for image_path in images or []:
            mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            with open(image_path, "rb") as file:
                parts.append(AistudioPart(inline_data=(mime, base64.b64encode(file.read()).decode("ascii"))))
        parts.append(AistudioPart(text=prompt))
        return AistudioContent(role="user", parts=parts)


from aistudio_api.infrastructure.gateway.cli import cli_main

__all__ = ["AIStudioClient", "CapturedRequest", "cli_main"]
