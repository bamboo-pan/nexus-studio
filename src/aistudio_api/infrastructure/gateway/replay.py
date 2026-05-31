"""Captured request replay workflow."""

from __future__ import annotations

import logging
import time

from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.request_logs import RequestLogStore

logger = logging.getLogger("aistudio")


class RequestReplayService:
    def __init__(self, session: BrowserSession | None, request_log_store: RequestLogStore | None = None):
        self._session = session
        self._request_log_store = request_log_store

    async def replay(
        self,
        captured: CapturedRequest | None,
        body: str,
        timeout: int | None = None,
        *,
        kind: str = "replay",
        model: str | None = None,
    ) -> tuple[int, bytes]:
        if not captured:
            return 0, b""

        if timeout is None:
            timeout = settings.timeout_replay

        headers = captured.replay_headers
        transport = "browser" if self._session is not None else "http"
        entry = self._record_request(captured=captured, body=body, headers=headers, kind=kind, model=model, transport=transport)
        started = time.perf_counter()

        try:
            if self._session is not None:
                status, raw = await self._session.send_hooked_request(
                    body=body,
                    url=captured.url,
                    headers=headers,
                    timeout_ms=timeout * 1000,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                self._record_response(captured=captured, entry=entry, kind=kind, model=model, status=status, raw=raw, transport=transport, elapsed_ms=elapsed_ms)
                return status, raw

            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    captured.url,
                    data=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    raw = await resp.read()
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    self._record_response(
                        captured=captured,
                        entry=entry,
                        kind=kind,
                        model=model,
                        status=resp.status,
                        raw=raw,
                        headers=resp.headers,
                        transport=transport,
                        elapsed_ms=elapsed_ms,
                    )
                    return resp.status, raw
        except Exception as exc:
            logger.error("Replay error: %s", exc)
            raw = str(exc).encode()
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._record_response(captured=captured, entry=entry, kind=kind, model=model, status=0, raw=raw, transport=transport, elapsed_ms=elapsed_ms)
            return 0, raw

    def _record_request(
        self,
        *,
        captured: CapturedRequest,
        body: str,
        headers: dict[str, str],
        kind: str,
        model: str | None,
        transport: str,
    ) -> dict | None:
        if self._request_log_store is None:
            return None
        try:
            return self._request_log_store.save(
                kind=kind,
                model=model or captured.model,
                method="POST",
                url=captured.url,
                headers=headers,
                captured_headers=captured.headers,
                body=body,
                transport=transport,
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
        kind: str,
        model: str | None,
        status: int,
        raw: bytes,
        transport: str,
        elapsed_ms: float,
        headers: dict | None = None,
    ) -> None:
        if self._request_log_store is None:
            return
        chain_id = entry.get("chain_id") if isinstance(entry, dict) else None
        try:
            if isinstance(entry, dict) and entry.get("id"):
                self._request_log_store.attach_response(
                    str(entry["id"]),
                    status_code=status,
                    response_headers=headers,
                    response_body=raw,
                    elapsed_ms=elapsed_ms,
                )
            self._request_log_store.save(
                kind=kind,
                model=model or captured.model,
                method="POST",
                url=captured.url,
                headers=headers or {},
                body="",
                transport=transport,
                chain_id=chain_id,
                direction="inbound",
                phase="upstream_response",
                status_code=status,
                response_headers=headers,
                response_body=raw,
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            logger.warning("Request log response write failed: %s", exc)
