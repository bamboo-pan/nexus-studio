"""Account rotation for multi-account load balancing."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aistudio_api.infrastructure.account.account_store import AccountStore, AccountMeta
from aistudio_api.domain.model_capabilities import canonical_model_id, get_model_capabilities

logger = logging.getLogger("aistudio.rotator")

PREMIUM_MODEL_TOKEN_RE = re.compile(r"(?:^|[-_.])pro(?:[-_.]|$)", re.IGNORECASE)
DEFAULT_AFFINITY_TTL_SECONDS = 60 * 60


def _model_name_prefers_premium(model: str | None) -> bool:
    if not model:
        return False
    model_id = canonical_model_id(model).lower()
    return bool(PREMIUM_MODEL_TOKEN_RE.search(model_id)) or "image" in model_id


class RotationMode(str, Enum):
    """轮询模式。"""
    ROUND_ROBIN = "round_robin"          # 均衡模式（兼容旧配置值）
    LEAST_RECENTLY_USED = "lru"         # 最久未用
    LEAST_RATE_LIMITED = "least_rl"     # 最少限流
    EXHAUSTION = "exhaustion"           # 用到额度耗尽再切换


@dataclass
class AccountStats:
    """单账号的运行统计。"""
    account_id: str
    requests: int = 0
    success: int = 0
    rate_limited: int = 0
    errors: int = 0
    last_used: float = 0.0           # timestamp
    last_rate_limited: float = 0.0   # timestamp
    cooldown_until: float = 0.0      # timestamp, 429 后冷却期
    in_flight: int = 0
    image_sizes: dict[str, int] = field(default_factory=dict)

    def is_available(self) -> bool:
        """检查账号是否可用（不在冷却期）。"""
        return time.time() >= self.cooldown_until

    def record_success(self, *, image_size: str | None = None, image_count: int = 1) -> None:
        self.requests += 1
        self.success += 1
        self.last_used = time.time()
        if image_size:
            self.image_sizes[image_size] = self.image_sizes.get(image_size, 0) + max(1, image_count)

    def record_rate_limited(self, cooldown_seconds: int = 60) -> None:
        self.requests += 1
        self.rate_limited += 1
        self.last_rate_limited = time.time()
        self.cooldown_until = time.time() + cooldown_seconds

    def record_error(self) -> None:
        self.requests += 1
        self.errors += 1
        self.last_used = time.time()

    def record_start(self) -> None:
        self.in_flight += 1

    def record_finish(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)


@dataclass
class AffinityBinding:
    """Bounded logical user/session to account mapping."""
    account_id: str
    created_at: float
    expires_at: float


@dataclass
class AccountLease:
    """Request-scoped account lease used for in-flight balancing."""
    account: AccountMeta
    _rotator: AccountRotator = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._rotator.release_account(self.account.id)


class AccountRotator:
    """多账号轮询管理器。

    支持四种模式：
    - round_robin: 均衡分配请求，429 时跳过冷却中的账号
    - lru: 最久未用优先，适合均匀分配负载
    - least_rl: 最少限流优先，适合最大化吞吐
    - exhaustion: 持续使用当前账号，直到限流/隔离/不可用再切换
    """

    def __init__(
        self,
        account_store: AccountStore,
        mode: RotationMode = RotationMode.ROUND_ROBIN,
        cooldown_seconds: int = 60,
        error_isolation_threshold: int = 3,
        affinity_ttl_seconds: int = DEFAULT_AFFINITY_TTL_SECONDS,
    ) -> None:
        self._store = account_store
        self._mode = mode
        self._cooldown_seconds = cooldown_seconds
        self._error_isolation_threshold = error_isolation_threshold
        self._stats: dict[str, AccountStats] = {}
        self._current_index: int = 0
        self._affinity: dict[str, AffinityBinding] = {}
        self._affinity_limit = 1024
        self._affinity_ttl_seconds = affinity_ttl_seconds
        self._lock = asyncio.Lock()
        self.last_selection_reason: str | None = None

        # 初始化已有账号的统计
        for account in self._store.list_accounts():
            if account.id not in self._stats:
                self._stats[account.id] = AccountStats(account_id=account.id)

    @property
    def mode(self) -> RotationMode:
        return self._mode

    @mode.setter
    def mode(self, value: RotationMode) -> None:
        logger.info("轮询模式切换: %s -> %s", self._mode, value)
        self._mode = value

    @property
    def cooldown_seconds(self) -> int:
        return self._cooldown_seconds

    @cooldown_seconds.setter
    def cooldown_seconds(self, value: int) -> None:
        self._cooldown_seconds = value

    def get_all_stats(self) -> dict[str, dict[str, Any]]:
        """获取所有账号的统计信息。"""
        result = {}
        now = time.time()
        self._prune_affinity(now)
        affinity_loads = self._affinity_loads(now)
        for account in self._store.list_accounts():
            stats = self._stats.get(account.id, AccountStats(account_id=account.id))
            affinity_load = affinity_loads.get(account.id, 0)
            result[account.id] = {
                "name": account.name,
                "email": account.email,
                "tier": getattr(account, "tier", "free"),
                "health_status": getattr(account, "health_status", "unknown"),
                "health_reason": getattr(account, "health_reason", None),
                "last_health_check": getattr(account, "last_health_check", None),
                "isolated_until": getattr(account, "isolated_until", None),
                "requests": stats.requests,
                "success": stats.success,
                "rate_limited": stats.rate_limited,
                "errors": stats.errors,
                "in_flight": stats.in_flight,
                "affinity_load": affinity_load,
                "bound_users": affinity_load,
                "affinity_ttl_seconds": self._affinity_ttl_seconds,
                "last_used": datetime.fromtimestamp(stats.last_used, tz=timezone.utc).isoformat() if stats.last_used else None,
                "last_rate_limited": datetime.fromtimestamp(stats.last_rate_limited, tz=timezone.utc).isoformat() if stats.last_rate_limited else None,
                "is_available": stats.is_available() and not account.is_isolated,
                "cooldown_remaining": max(0, int(stats.cooldown_until - time.time())),
                "image_sizes": dict(stats.image_sizes),
                "image_total": sum(stats.image_sizes.values()),
            }
        return result

    def _prune_affinity(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        expired = [key for key, binding in self._affinity.items() if binding.expires_at <= now]
        for key in expired:
            self._affinity.pop(key, None)

    def _affinity_loads(self, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        loads: dict[str, int] = defaultdict(int)
        for binding in self._affinity.values():
            if binding.expires_at > now:
                loads[binding.account_id] += 1
        return dict(loads)

    def _affinity_load(self, account_id: str, now: float | None = None) -> int:
        return self._affinity_loads(now).get(account_id, 0)

    def _bind_affinity(self, affinity_key: str, account_id: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._affinity[affinity_key] = AffinityBinding(
            account_id=account_id,
            created_at=now,
            expires_at=now + self._affinity_ttl_seconds,
        )
        if len(self._affinity) > self._affinity_limit:
            oldest_key = min(self._affinity, key=lambda key: self._affinity[key].created_at)
            self._affinity.pop(oldest_key, None)

    def _get_available_accounts(self) -> list[tuple[AccountMeta, AccountStats]]:
        """获取所有可用的账号（不在冷却期）。"""
        accounts = self._store.list_accounts()
        available = []
        for account in accounts:
            stats = self._stats.get(account.id, AccountStats(account_id=account.id))
            if stats.is_available() and not account.is_isolated:
                available.append((account, stats))
        return available

    def _cooldown_wait_accounts(self, *, exclude_account_id: str | None = None) -> list[AccountMeta]:
        accounts: list[AccountMeta] = []
        for account in self._store.list_accounts():
            if account.id == exclude_account_id:
                continue
            if account.health_status == "rate_limited" or not account.is_isolated:
                accounts.append(account)
        return accounts

    def model_prefers_premium(self, model: str | None) -> bool:
        if not model:
            return False
        try:
            capabilities = get_model_capabilities(model, strict=True)
        except ValueError:
            return _model_name_prefers_premium(model)
        return capabilities.image_output or _model_name_prefers_premium(capabilities.id)

    def has_available_preferred_account(self, model: str | None) -> bool:
        if not self.model_prefers_premium(model):
            return bool(self._get_available_accounts())
        return any(account.is_premium for account, _ in self._get_available_accounts())

    def _filter_for_model(
        self,
        available: list[tuple[AccountMeta, AccountStats]],
        model: str | None,
        *,
        require_preferred: bool = False,
    ) -> list[tuple[AccountMeta, AccountStats]]:
        if not self.model_prefers_premium(model):
            self.last_selection_reason = "text model can use any healthy account"
            return available
        premium = [(account, stats) for account, stats in available if account.is_premium]
        if premium:
            self.last_selection_reason = "premium-preferred model selected a Pro/Ultra account"
            return premium
        if require_preferred:
            self.last_selection_reason = "premium-preferred model requires Pro/Ultra but none are currently available"
            return []
        self.last_selection_reason = "premium-preferred model fell back to a non-premium account because no Pro/Ultra account is available"
        logger.warning("Premium-preferred model account selection fallback: no healthy Pro/Ultra account is available")
        return available

    def _pick_round_robin(self, available: list[tuple[AccountMeta, AccountStats]]) -> tuple[AccountMeta, AccountStats] | None:
        """Round-robin tie-breaker，基于全量账号索引，避免 available 变化导致跳过或重复。"""
        if not available:
            return None
        available_ids = {a.id for a, _ in available}
        all_accounts = self._store.list_accounts()
        if not all_accounts:
            return None
        total = len(all_accounts)
        for i in range(total):
            idx = (self._current_index + i) % total
            if all_accounts[idx].id in available_ids:
                self._current_index = (idx + 1) % total
                return next((a, s) for a, s in available if a.id == all_accounts[idx].id)
        return available[0]

    def _pick_balanced(
        self,
        available: list[tuple[AccountMeta, AccountStats]],
        *,
        affinity_key: str | None = None,
    ) -> tuple[AccountMeta, AccountStats] | None:
        """Pick the least-busy account while keeping light session affinity."""
        if not available:
            return None

        now = time.time()
        self._prune_affinity(now)

        if affinity_key:
            binding = self._affinity.get(affinity_key)
            affinity_pick = next(((account, stats) for account, stats in available if binding is not None and account.id == binding.account_id), None)
            if affinity_pick is not None:
                min_in_flight = min(stats.in_flight for _, stats in available)
                if affinity_pick[1].in_flight <= min_in_flight + 1:
                    if self.last_selection_reason == "text model can use any healthy account":
                        self.last_selection_reason = "balanced mode kept the affinity account"
                    return affinity_pick

        min_score = min((stats.in_flight, stats.requests, stats.rate_limited) for _, stats in available)
        candidates = [(account, stats) for account, stats in available if (stats.in_flight, stats.requests, stats.rate_limited) == min_score]
        pick = self._pick_round_robin(candidates)
        if pick is not None and affinity_key:
            self._bind_affinity(affinity_key, pick[0].id, now)
        if self.last_selection_reason == "text model can use any healthy account":
            self.last_selection_reason = "balanced mode selected the least-busy account"
        return pick

    def _pick_lru(self, available: list[tuple[AccountMeta, AccountStats]]) -> tuple[AccountMeta, AccountStats] | None:
        """最久未用优先。"""
        if not available:
            return None
        return min(available, key=lambda x: x[1].last_used or 0.0)

    def _pick_least_rl(self, available: list[tuple[AccountMeta, AccountStats]]) -> tuple[AccountMeta, AccountStats] | None:
        """最少限流优先。"""
        if not available:
            return None
        return min(available, key=lambda x: x[1].rate_limited)

    def _pick_exhaustion(self, available: list[tuple[AccountMeta, AccountStats]]) -> tuple[AccountMeta, AccountStats] | None:
        """耗尽模式：优先保留当前激活账号，直到它不可用。"""
        if not available:
            return None
        active = self._store.get_active_account()
        if active is not None:
            active_pick = next(((account, stats) for account, stats in available if account.id == active.id), None)
            if active_pick is not None:
                self.last_selection_reason = "exhaustion mode kept the active account"
                return active_pick
        self.last_selection_reason = "exhaustion mode selected the next available account"
        return self._pick_round_robin(available)

    def _pick_for_mode(
        self,
        available: list[tuple[AccountMeta, AccountStats]],
        *,
        affinity_key: str | None = None,
    ) -> tuple[AccountMeta, AccountStats] | None:
        if self._mode == RotationMode.ROUND_ROBIN:
            return self._pick_balanced(available, affinity_key=affinity_key)
        if self._mode == RotationMode.LEAST_RECENTLY_USED:
            return self._pick_lru(available)
        if self._mode == RotationMode.LEAST_RATE_LIMITED:
            return self._pick_least_rl(available)
        if self._mode == RotationMode.EXHAUSTION:
            return self._pick_exhaustion(available)
        return available[0] if available else None

    async def get_next_account(
        self,
        model: str | None = None,
        *,
        require_preferred: bool = False,
        exclude_account_id: str | None = None,
    ) -> AccountMeta | None:
        """获取下一个可用的账号。"""
        async with self._lock:
            available = self._get_available_accounts()
            available = self._filter_for_model(available, model, require_preferred=require_preferred)
            if exclude_account_id:
                available = [(account, stats) for account, stats in available if account.id != exclude_account_id]

            if not available:
                if require_preferred and self.model_prefers_premium(model):
                    return None
                # 所有账号都在冷却期，找一个冷却时间最短的
                all_accounts = self._cooldown_wait_accounts(exclude_account_id=exclude_account_id)
                if not all_accounts:
                    self.last_selection_reason = "no healthy accounts are available"
                    return None
                # 选冷却结束最早的
                earliest = min(
                    [(a, self._stats.get(a.id, AccountStats(account_id=a.id))) for a in all_accounts],
                    key=lambda x: x[1].cooldown_until,
                )
                account, stats = earliest
                wait_time = max(0, stats.cooldown_until - time.time())
                logger.warning("All healthy accounts are cooling down; waiting %.1fs before retry", wait_time)
                await asyncio.sleep(wait_time)
                return account

            # 根据模式选择
            pick = self._pick_for_mode(available)

            if pick is None:
                return None

            account, stats = pick
            logger.info("Account selected for model=%s tier=%s mode=%s reason=%s", model or "<any>", account.tier, self._mode.value, self.last_selection_reason)
            return account

    async def get_next_account_with_stats(
        self,
        model: str | None = None,
        *,
        require_preferred: bool = False,
        exclude_account_id: str | None = None,
    ) -> tuple[AccountMeta, AccountStats] | None:
        """获取下一个可用的账号及其统计。"""
        async with self._lock:
            available = self._get_available_accounts()
            available = self._filter_for_model(available, model, require_preferred=require_preferred)
            if exclude_account_id:
                available = [(account, stats) for account, stats in available if account.id != exclude_account_id]
            if not available:
                if require_preferred and self.model_prefers_premium(model):
                    return None
                all_accounts = self._cooldown_wait_accounts(exclude_account_id=exclude_account_id)
                if not all_accounts:
                    self.last_selection_reason = "no healthy accounts are available"
                    return None
                earliest = min(
                    [(a, self._stats.get(a.id, AccountStats(account_id=a.id))) for a in all_accounts],
                    key=lambda x: x[1].cooldown_until,
                )
                account, stats = earliest
                wait_time = max(0, stats.cooldown_until - time.time())
                await asyncio.sleep(wait_time)
                return account, stats

            return self._pick_for_mode(available)

    async def acquire_account(
        self,
        model: str | None = None,
        *,
        require_preferred: bool = False,
        exclude_account_id: str | None = None,
        affinity_key: str | None = None,
    ) -> AccountLease | None:
        """Acquire a request-scoped account lease for balanced execution."""
        async with self._lock:
            available = self._get_available_accounts()
            available = self._filter_for_model(available, model, require_preferred=require_preferred)
            if exclude_account_id:
                available = [(account, stats) for account, stats in available if account.id != exclude_account_id]
            if not available:
                if require_preferred and self.model_prefers_premium(model):
                    return None
                all_accounts = self._cooldown_wait_accounts(exclude_account_id=exclude_account_id)
                if not all_accounts:
                    self.last_selection_reason = "no healthy accounts are available"
                    return None
                account, stats = min(
                    [(account, self._stats.get(account.id, AccountStats(account_id=account.id))) for account in all_accounts],
                    key=lambda item: item[1].cooldown_until,
                )
                wait_time = max(0, stats.cooldown_until - time.time())
                logger.warning("All healthy accounts are cooling down; waiting %.1fs before leasing", wait_time)
                await asyncio.sleep(wait_time)
                if account.id not in self._stats:
                    self._stats[account.id] = stats
                stats.record_start()
                affinity_load = self._affinity_load(account.id)
                logger.info(
                    "Account leased after cooldown wait for model=%s account=%s tier=%s mode=%s reason=%s in_flight=%d affinity_load=%d affinity_ttl_seconds=%d",
                    model or "<any>",
                    account.id,
                    account.tier,
                    self._mode.value,
                    self.last_selection_reason,
                    stats.in_flight,
                    affinity_load,
                    self._affinity_ttl_seconds,
                )
                return AccountLease(account=account, _rotator=self)

            pick = self._pick_for_mode(available, affinity_key=affinity_key)
            if pick is None:
                return None
            account, stats = pick
            stats.record_start()
            affinity_load = self._affinity_load(account.id)
            logger.info(
                "Account leased for model=%s account=%s tier=%s mode=%s reason=%s in_flight=%d affinity_load=%d affinity_ttl_seconds=%d",
                model or "<any>",
                account.id,
                account.tier,
                self._mode.value,
                self.last_selection_reason,
                stats.in_flight,
                affinity_load,
                self._affinity_ttl_seconds,
            )
            return AccountLease(account=account, _rotator=self)

    async def release_account(self, account_id: str) -> None:
        async with self._lock:
            if account_id not in self._stats:
                self._stats[account_id] = AccountStats(account_id=account_id)
            self._stats[account_id].record_finish()

    def record_success(self, account_id: str, *, image_size: str | None = None, image_count: int = 1) -> None:
        """记录成功请求。"""
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)
        self._stats[account_id].record_success(image_size=image_size, image_count=image_count)
        self._store.set_account_health(account_id, "healthy", "last request succeeded")

    def record_rate_limited(
        self,
        account_id: str,
        *,
        cooldown_seconds: int | None = None,
        reason: str | None = None,
        quota_exhausted: bool = False,
    ) -> None:
        """记录 429 限流。"""
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)
        effective_cooldown_seconds = self._cooldown_seconds if cooldown_seconds is None else cooldown_seconds
        self._stats[account_id].record_rate_limited(effective_cooldown_seconds)
        cooldown_until = datetime.fromtimestamp(self._stats[account_id].cooldown_until, tz=timezone.utc).isoformat()
        status = "quota_exhausted" if quota_exhausted else "rate_limited"
        health_reason = reason or (
            "upstream quota is exhausted; account is paused until quota resets"
            if quota_exhausted
            else "upstream returned rate limit; account is cooling down"
        )
        self._store.set_account_health(
            account_id,
            status,
            health_reason,
            isolated_until=cooldown_until,
        )
        logger.warning("Account was %s; cooling down for %ds", status, effective_cooldown_seconds)

    def record_error(self, account_id: str) -> None:
        """记录错误。"""
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)
        self._stats[account_id].record_error()
        stats = self._stats[account_id]
        if stats.errors >= self._error_isolation_threshold:
            self._store.isolate_account(
                account_id,
                f"isolated after {stats.errors} gateway errors",
                self._cooldown_seconds,
            )
            logger.warning("Account automatically isolated after repeated gateway errors")
        else:
            self._store.set_account_health(account_id, "error", "last request failed")

    def add_account(self, account_id: str) -> None:
        """添加新账号时初始化统计。"""
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)

    def remove_account(self, account_id: str) -> None:
        """删除账号时清理统计。"""
        self._stats.pop(account_id, None)
        for key, binding in list(self._affinity.items()):
            if binding.account_id == account_id:
                self._affinity.pop(key, None)

    def has_accounts(self) -> bool:
        return bool(self._store.list_accounts())


# 全局轮询器实例
_rotator: AccountRotator | None = None


def get_rotator() -> AccountRotator | None:
    """获取全局轮询器。"""
    return _rotator


def init_rotator(account_store: AccountStore, **kwargs) -> AccountRotator:
    """初始化全局轮询器。"""
    global _rotator
    _rotator = AccountRotator(account_store, **kwargs)
    return _rotator
