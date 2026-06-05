"""Hook-first request capture workflow."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from aistudio_api.config import DEFAULT_TEXT_MODEL
from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache
from aistudio_api.infrastructure.gateway.request_rewriter import modify_body, replace_body_model, resolve_aistudio_wire_model
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

logger = logging.getLogger("aistudio")


def _is_transient_aistudio_capture_error(exc: BaseException) -> bool:
    message = str(exc)
    if "Google sign-in" in message or "auth state" in message:
        return False
    return "Page.goto: Timeout" in message or "AI Studio chat runtime not ready" in message or "AI Studio image runtime not ready" in message or "template capture timeout" in message


@dataclass
class CapturedRequest:
    url: str
    headers: dict[str, str]
    body: str
    model: str = ""
    snapshot: str = ""

    def __post_init__(self):
        parsed = json.loads(self.body)
        self.model = parsed[0] if parsed else ""
        self.snapshot = parsed[4] if len(parsed) > 4 and isinstance(parsed[4], str) else ""

    @property
    def replay_headers(self) -> dict[str, str]:
        return {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}


class RequestCaptureService:
    """Single-page hook flow modeled after camoufox-api."""

    def __init__(self, session: BrowserSession, snapshot_cache: SnapshotCache):
        self._session = session
        self._snapshot_cache = snapshot_cache
        self._templates: dict[str, CapturedRequest] = {}

    def clear_templates(self) -> None:
        self._templates.clear()

    async def warmup(
        self,
        prompt: str = "1",
        model: str = DEFAULT_TEXT_MODEL,
        *,
        rewrite_body: bool = True,
        retry_template_capture: bool = True,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
        botguard_timeout_ms: int | None = None,
        template_capture_timeout_ms: int | None = None,
        template_recovery_attempts: int | None = None,
    ) -> CapturedRequest:
        capture_model = resolve_aistudio_wire_model(model)
        template = await self._ensure_template(
            capture_model,
            retry_transient=retry_template_capture,
            navigation_timeout_ms=navigation_timeout_ms,
            chat_ready_timeout_ms=chat_ready_timeout_ms,
            botguard_timeout_ms=botguard_timeout_ms,
            template_capture_timeout_ms=template_capture_timeout_ms,
            template_recovery_attempts=template_recovery_attempts,
        )
        if not rewrite_body:
            body = replace_body_model(template.body, model=capture_model)
            return CapturedRequest(url=template.url, headers=template.headers, body=body)
        snapshot = await self._session.generate_snapshot([self._build_capture_content(prompt=prompt, images=None)])
        body = modify_body(template.body, model=capture_model, prompt=prompt, snapshot=snapshot)
        return CapturedRequest(url=template.url, headers=template.headers, body=body)

    async def capture(
        self,
        prompt: str,
        model: str = DEFAULT_TEXT_MODEL,
        images: list[str] | None = None,
        contents: list[AistudioContent] | None = None,
        force_refresh: bool = False,
    ) -> CapturedRequest | None:
        capture_model = resolve_aistudio_wire_model(model)
        # Image bytes live in rewritten contents, so template capture does not need
        # the original image list. Only cache plain-text prompts.
        if not images and not force_refresh:
            cached = self._snapshot_cache.get(prompt, model=capture_model)
            if cached:
                _snapshot, url, headers, body = cached
                captured = CapturedRequest(url=url, headers=headers, body=body)
                logger.info(
                    "Hook 缓存命中: model=%s, snapshot=%s chars, body=%s chars",
                    captured.model,
                    len(captured.snapshot),
                    len(captured.body),
                )
                return captured

        template = await self._ensure_template(capture_model)
        # 先只走 inlineData 路径，避免 fileData/Drive 上传链路干扰主流程。
        rewritten_contents = contents
        snapshot_contents = rewritten_contents or [self._build_capture_content(prompt=prompt, images=images)]
        snapshot = await self._session.generate_snapshot(snapshot_contents)
        body = modify_body(
            template.body,
            model=capture_model,
            prompt=prompt,
            contents=rewritten_contents,
            snapshot=snapshot,
        )
        captured = CapturedRequest(url=template.url, headers=template.headers, body=body)
        if not images:
            self._snapshot_cache.put(prompt, captured.snapshot, captured.url, captured.headers, captured.body, model=capture_model)
        logger.info(
            "Hook 拦截成功: model=%s, snapshot=%s chars, body=%s chars",
            captured.model,
            len(captured.snapshot),
            len(captured.body),
        )
        return captured

    async def _ensure_template(
        self,
        model: str,
        *,
        retry_transient: bool = True,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
        botguard_timeout_ms: int | None = None,
        template_capture_timeout_ms: int | None = None,
        template_recovery_attempts: int | None = None,
    ) -> CapturedRequest:
        if model in self._templates:
            return self._templates[model]

        capture_kwargs = {}
        if navigation_timeout_ms is not None:
            capture_kwargs["navigation_timeout_ms"] = navigation_timeout_ms
        if chat_ready_timeout_ms is not None:
            capture_kwargs["chat_ready_timeout_ms"] = chat_ready_timeout_ms
        if botguard_timeout_ms is not None:
            capture_kwargs["botguard_timeout_ms"] = botguard_timeout_ms
        if template_capture_timeout_ms is not None:
            capture_kwargs["template_capture_timeout_ms"] = template_capture_timeout_ms
        if template_recovery_attempts is not None:
            capture_kwargs["template_recovery_attempts"] = template_recovery_attempts

        captured = None
        max_attempts = 2 if retry_transient else 1
        for attempt in range(max_attempts):
            try:
                captured = await self._session.capture_template(model, **capture_kwargs)
                break
            except Exception as exc:
                if attempt + 1 >= max_attempts or not _is_transient_aistudio_capture_error(exc):
                    raise
                logger.warning("AI Studio template capture failed during navigation/readiness; retrying once: %s", exc)
        if captured is None:
            raise RuntimeError(f"AI Studio template capture failed for model={model}")
        template = CapturedRequest(**captured)
        self._templates[model] = template
        logger.info("Hook 模板已就绪: requested=%s, captured=%s", model, template.model)
        return template

    def _build_capture_content(self, prompt: str, images: list[str] | None) -> AistudioContent:
        parts = [AistudioPart(text=prompt)]
        return AistudioContent(role="user", parts=parts)
