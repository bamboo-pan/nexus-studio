"""账号管理应用服务，协调 account_store 和 login_service。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from aistudio_api.infrastructure.account.account_store import AccountStore, AccountMeta
from aistudio_api.infrastructure.account.login_service import LoginService, LoginSession

logger = logging.getLogger("aistudio.account")


class AccountService:
    """账号管理服务。"""

    def __init__(
        self,
        account_store: AccountStore,
        login_service: LoginService,
    ) -> None:
        self._store = account_store
        self._login = login_service

    def list_accounts(self) -> list[AccountMeta]:
        """列出所有账号。"""
        return self._store.list_accounts()

    def get_account(self, account_id: str) -> AccountMeta | None:
        """获取单个账号。"""
        return self._store.get_account(account_id)

    def get_active_account(self) -> AccountMeta | None:
        """获取当前活跃账号。"""
        return self._store.get_active_account()

    async def start_login(self, name: str | None = None) -> str:
        """启动登录流程，返回 session_id。"""
        return await self._login.start_login(self._store, name)

    def get_login_status(self, session_id: str) -> LoginSession | None:
        """获取登录状态。"""
        return self._login.get_status(session_id)

    async def activate_account(
        self,
        account_id: str,
        browser_session: Any,
        snapshot_cache: Any,
        busy_lock: Any = None,  # None = skip lock (caller already holds it)
        keep_snapshot_cache: bool = False,
    ) -> AccountMeta | None:
        """切换到指定账号。

        Args:
            account_id: 目标账号 ID
            browser_session: 负责切换 auth 的 BrowserSession/AIStudioClient 实例
            snapshot_cache: SnapshotCache 实例
            busy_lock: asyncio.Lock，确保切换时无请求在飞行中。None 则跳过锁
            keep_snapshot_cache: 是否保留 snapshot 缓存（默认 False，避免切号后复用旧 snapshot）

        Returns:
            切换后的账号元数据，或 None（如果账号不存在）
        """
        # 验证账号存在
        account = self._store.get_account(account_id)
        if account is None:
            return None

        async def _do_switch():
            # 获取 auth 路径
            auth_path = self._store.get_auth_path(account_id)
            if auth_path is None:
                logger.error("账号 %s 的 auth.json 不存在", account_id)
                return None

            # 切换浏览器/客户端 auth；AIStudioClient 会同步清理 capture 模板。
            await browser_session.switch_auth(str(auth_path))

            # 切号后默认清理 snapshot，避免旧页面态和新账号 cookies 混用。
            if not keep_snapshot_cache and snapshot_cache is not None:
                snapshot_cache.clear()
                logger.info("已清除 snapshot 缓存")

            # 更新注册表
            self._store.set_active_account(account_id)

            logger.info("已切换到账号: %s (%s)", account_id, account.name)
            return account

        # 获取 busy_lock 确保无请求在飞行中
        if busy_lock is not None:
            async with busy_lock:
                return await _do_switch()
        else:
            return await _do_switch()

    def delete_account(self, account_id: str) -> bool:
        """删除账号。"""
        return self._store.delete_account(account_id)

    def update_account(self, account_id: str, name: str | None = None, tier: str | None = None) -> AccountMeta | None:
        """更新账号名称。"""
        return self._store.update_account(account_id, name, tier)

    def test_account(self, account_id: str) -> dict[str, Any] | None:
        """执行不会发送外部请求的账号健康检查。"""
        return self._store.test_account_health(account_id)

    async def test_account_with_tier(
        self,
        account_id: str,
        *,
        tier_detector: Callable[[Path], Awaitable[Any]] | None = None,
    ) -> dict[str, Any] | None:
        """执行账号健康检查，并在可用时刷新订阅等级。"""
        result = self._store.test_account_health(account_id)
        if result is None or not result.get("ok"):
            return result

        auth_path = self._store.get_auth_path(account_id)
        if auth_path is None or tier_detector is None:
            return result

        try:
            tier_result = await tier_detector(auth_path)
        except Exception as exc:
            logger.warning("账号等级检测失败: %s", exc)
            account = self._store.set_account_health(
                account_id,
                "healthy",
                f"storage state is readable; tier detection unavailable: {exc}",
            )
            if account is None:
                return result
            return {
                "ok": True,
                "account": account.to_dict(),
                "status": account.health_status,
                "reason": account.health_reason,
                "tier": account.tier,
                "last_health_check": account.last_health_check,
                "isolated_until": account.isolated_until,
            }

        account = self._store.get_account(account_id)
        if account is None:
            return result

        detected_tier = tier_result.tier.value
        tier_to_store = detected_tier
        if detected_tier == "free" and account.is_premium:
            tier_to_store = account.tier
            health_reason = f"storage state is readable; tier detection returned free, keeping stored {account.tier} tier"
        else:
            health_reason = f"storage state is readable; detected {detected_tier} tier"

        account = self._store.update_account(account_id, tier=tier_to_store)
        if account is None:
            return result
        account = self._store.set_account_health(
            account_id,
            "healthy",
            health_reason,
        ) or account
        return {
            "ok": True,
            "account": account.to_dict(),
            "status": account.health_status,
            "reason": account.health_reason,
            "tier": account.tier,
            "last_health_check": account.last_health_check,
            "isolated_until": account.isolated_until,
        }

    def isolate_account(self, account_id: str, reason: str, seconds: int | None = None) -> AccountMeta | None:
        """隔离账号，供轮询器在连续失败时调用。"""
        return self._store.isolate_account(account_id, reason, seconds)

    def export_credentials(self, account_id: str | None = None) -> dict[str, Any]:
        """导出账号凭证备份包。"""
        return self._store.export_credentials(account_id)

    def import_credentials(
        self,
        payload: dict[str, Any],
        *,
        name: str | None = None,
        activate: bool = True,
    ) -> list[AccountMeta]:
        """导入账号凭证备份包或单账号 storage state。"""
        return self._store.import_credentials(payload, name=name, activate=activate)
