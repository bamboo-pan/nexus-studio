"""Google 账号登录服务，通过有头浏览器完成登录并保存 cookie。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aistudio_api.infrastructure.browser.camoufox_manager import CamoufoxManager

logger = logging.getLogger("aistudio.login")

LOGIN_IDENTITY_ERROR = "未能确认 Google 账号身份，请确认登录成功后重试"
EMAIL_DETECTION_SCRIPT = r"""
    () => {
        const values = [];
        const add = (value) => {
            if (typeof value === 'string' && value.trim()) {
                values.push(value.trim());
            }
        };
        for (const selector of ['[data-email]', '[data-profile-identifier]', '[aria-label*="@"]', '[title*="@"]', 'a[href^="mailto:"]']) {
            for (const el of document.querySelectorAll(selector)) {
                add(el.getAttribute('data-email'));
                add(el.getAttribute('data-profile-identifier'));
                add(el.getAttribute('aria-label'));
                add(el.getAttribute('title'));
                add(el.getAttribute('href'));
                add(el.textContent);
            }
        }
        add(document.body ? document.body.innerText : '');
        const match = values.join('\n').match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}/);
        return match ? match[0] : null;
    }
"""


class LoginStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class LoginSession:
    """登录会话状态。"""
    session_id: str
    status: LoginStatus = LoginStatus.PENDING
    account_id: str | None = None
    email: str | None = None
    error: str | None = None
    auth_activated: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class LoginService:
    """Google 账号登录服务。"""

    def __init__(self, port: int = 9223) -> None:
        self._port = port
        self._sessions: dict[str, LoginSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _generate_session_id(self) -> str:
        return f"login_{secrets.token_hex(8)}"

    async def start_login(
        self,
        account_store: Any,  # AccountStore
        name: str | None = None,
    ) -> str:
        """启动登录流程，返回 session_id。"""
        session_id = self._generate_session_id()
        session = LoginSession(session_id=session_id)
        self._sessions[session_id] = session
        # 启动后台任务
        task = asyncio.create_task(
            self._login_worker(session_id, account_store, name)
        )
        self._tasks[session_id] = task
        return session_id

    def get_status(self, session_id: str) -> LoginSession | None:
        """获取登录状态。"""
        return self._sessions.get(session_id)

    async def _extract_email_from_page(self, page: Any) -> str | None:
        try:
            email = await page.evaluate(EMAIL_DETECTION_SCRIPT)
        except Exception:
            return None
        return email if isinstance(email, str) and "@" in email else None

    async def _extract_email_from_account_page(self, page: Any) -> str | None:
        try:
            logger.info("尝试从 Google 账号页面获取邮箱")
            await page.goto("https://myaccount.google.com", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("从 Google 账号页面获取邮箱失败: %s", e)
        return await self._extract_email_from_page(page)

    async def _verify_login_identity(
        self,
        account_store: Any,
        page: Any,
        storage_state: dict[str, Any],
        detected_email: str | None,
    ) -> str | None:
        storage_email = account_store.validate_storage_state(storage_state)
        email = detected_email or storage_email
        if email is None:
            email = await self._extract_email_from_account_page(page)
        return email

    async def _login_worker(
        self,
        session_id: str,
        account_store: Any,
        name: str | None,
    ) -> None:
        """登录工作协程。"""
        session = self._sessions[session_id]
        manager = CamoufoxManager(
            port=self._port,
            headless=False,  # 有头模式，用户需要看到浏览器
        )
        playwright = None
        browser = None
        try:
            # 启动浏览器
            logger.info("启动登录浏览器，端口 %d", self._port)
            ws_endpoint = await manager.start()
            logger.info("浏览器已启动: %s", ws_endpoint)

            # 连接 Playwright
            from playwright.async_api import async_playwright
            playwright = await async_playwright().start()
            browser = await playwright.firefox.connect(ws_endpoint)
            context = await browser.new_context()
            page = await context.new_page()

            # 设置登录完成检测
            login_done = asyncio.Event()
            detected_email: str | None = None

            async def on_navigation(frame):
                nonlocal detected_email
                url = frame.url
                logger.debug("导航到: %s", url)
                # 检测登录完成：跳转到非登录页面
                if "accounts.google.com" not in url and "google.com" in url:
                    # 尝试提取邮箱
                    detected_email = await self._extract_email_from_page(page)
                    login_done.set()

            page.on("framenavigated", on_navigation)

            # 导航到 Google 登录页面
            logger.info("打开 Google 登录页面")
            await page.goto(
                "https://accounts.google.com/ServiceLogin?continue=https://aistudio.google.com",
                wait_until="networkidle",
            )

            # 等待用户完成登录（最多 5 分钟）
            logger.info("等待用户登录...")
            try:
                await asyncio.wait_for(login_done.wait(), timeout=300)
            except asyncio.TimeoutError:
                session.status = LoginStatus.FAILED
                session.error = "登录超时（5 分钟）"
                logger.warning("登录超时")
                return

            # 登录完成，保存 storage state
            logger.info("登录完成，保存 cookie")
            storage_state = await context.storage_state()

            try:
                detected_email = await self._verify_login_identity(
                    account_store,
                    page,
                    storage_state,
                    detected_email,
                )
            except ValueError as e:
                session.status = LoginStatus.FAILED
                session.error = f"登录状态无效: {e}"
                logger.warning("登录状态无效，不保存账号: %s", e)
                return

            if detected_email is None:
                session.status = LoginStatus.FAILED
                session.error = LOGIN_IDENTITY_ERROR
                logger.warning("登录未确认 Google 账号身份，不保存账号")
                return

            # 保存账号
            account_name = name or detected_email or "Google 账号"
            if detected_email and not name:
                account_name = detected_email
            meta = account_store.save_account(
                name=account_name,
                email=detected_email,
                storage_state=storage_state,
                activate=False,
            )

            session.status = LoginStatus.COMPLETED
            session.account_id = meta.id
            session.email = detected_email
            logger.info("账号已保存: %s (%s)", meta.id, detected_email)

        except Exception as e:
            session.status = LoginStatus.FAILED
            session.error = str(e)
            logger.exception("登录失败")
        finally:
            # 清理浏览器和 Playwright
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            try:
                if playwright:
                    await playwright.stop()
            except Exception:
                pass
            try:
                await manager.stop()
            except Exception:
                pass
            # 清理任务引用
            self._tasks.pop(session_id, None)
