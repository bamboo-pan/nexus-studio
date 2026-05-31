"""Shared API runtime state."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.application.account_client_pool import AccountClientPool
from aistudio_api.infrastructure.request_logs import RequestLogStore


@dataclass
class RuntimeState:
    client: AIStudioClient | None = None
    busy_lock: asyncio.Semaphore | None = None
    camoufox_port: int = 9222
    snapshot_cache: object | None = None  # SnapshotCache 实例
    account_service: object | None = None  # AccountService 实例
    rotator: object | None = None  # AccountRotator 实例
    account_client_pool: AccountClientPool | None = None
    request_log_store: RequestLogStore | None = None
    warmup_status: str = "idle"
    warmup_target_accounts: list[str] = field(default_factory=list)
    warmup_completed_accounts: list[str] = field(default_factory=list)
    warmup_failed_accounts: list[str] = field(default_factory=list)
    model_stats: dict[str, dict] = field(
        default_factory=lambda: defaultdict(
            lambda: {
                "requests": 0,
                "success": 0,
                "rate_limited": 0,
                "errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "image_sizes": {},
                "last_used": None,
            }
        )
    )

    def record(
        self,
        model: str,
        event: str,
        usage: dict | None = None,
        *,
        image_size: str | None = None,
        image_count: int = 1,
    ):
        stats = self.model_stats[model]
        stats["requests"] += 1
        stats[event] += 1
        stats["last_used"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        if event == "success" and image_size:
            image_sizes = stats.setdefault("image_sizes", {})
            image_sizes[image_size] = image_sizes.get(image_size, 0) + max(1, image_count)
        if usage and event == "success":
            pt = usage.get("prompt_tokens", 0)
            ct = usage.get("completion_tokens", 0)
            tt = usage.get("total_tokens", 0)
            stats["prompt_tokens"] += pt if isinstance(pt, int) else 0
            stats["completion_tokens"] += ct if isinstance(ct, int) else 0
            stats["total_tokens"] += tt if isinstance(tt, int) else 0


runtime_state = RuntimeState()

