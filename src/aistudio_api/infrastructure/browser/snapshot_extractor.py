"""Standalone snapshot extraction workflow."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional

from aistudio_api.config import DEFAULT_CAMOUFOX_PORT, settings

logger = logging.getLogger("aistudio.snapshot")
TARGET_HOST = "alkalimakersuite-pa.clients6.google.com"


class SnapshotExtractor:
    def __init__(self, port: int = DEFAULT_CAMOUFOX_PORT):
        self.port = port
        self._snapshot: Optional[str] = None
        self._cookies: Optional[dict[str, str]] = None

    async def extract(self, prompt: str = "test") -> str:
        import urllib.request
        from playwright.async_api import async_playwright

        resp = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=5)
        data = json.loads(resp.read())
        ws_url = f"ws://127.0.0.1:{self.port}{data['wsEndpointPath']}"

        pw = await async_playwright().start()
        browser = await pw.firefox.connect(ws_url)

        ctx_kwargs = {}
        if settings.auth_file and os.path.exists(settings.auth_file):
            ctx_kwargs["storage_state"] = settings.auth_file

        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        snapshots = []

        async def on_route(route, request):
            if TARGET_HOST in request.url and request.method == "POST":
                body = request.post_data
                if body:
                    try:
                        data = json.loads(body)
                        if isinstance(data, list) and len(data) >= 6:
                            snap = data[4]
                            if isinstance(snap, str) and snap.startswith("!"):
                                snapshots.append(snap)
                                logger.info("snapshot: %s 字符", len(snap))
                    except Exception as exc:
                        logger.warning("解析失败: %s", exc)
            await route.continue_()

        await page.route(f"**/{TARGET_HOST}/**", on_route)
        await page.goto(
            "https://aistudio.google.com/app/prompts/new_chat",
            wait_until="networkidle",
            timeout=30000,
        )

        textarea = page.locator("textarea").first
        await textarea.fill(prompt)
        await asyncio.sleep(0.5)
        run_btn = page.locator("button", has_text="Run").first
        await run_btn.click()

        for _ in range(30):
            await asyncio.sleep(1)
            if snapshots:
                break

        result = snapshots[-1] if snapshots else None
        if result:
            self._snapshot = result
            self._snapshot_time = time.time()
            cookies = await ctx.cookies()
            self._cookies = {c["name"]: c["value"] for c in cookies if "google" in c.get("domain", "")}
            logger.info("snapshot: %s 字符, cookies: %s 个", len(result), len(self._cookies))

        await page.close()
        await ctx.close()
        await browser.close()
        await pw.stop()
        return result or ""

    def get_snapshot(self) -> Optional[str]:
        return self._snapshot

    def get_cookies(self) -> Optional[dict[str, str]]:
        return self._cookies


async def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "你好，测试snapshot提取"
    extractor = SnapshotExtractor()
    snap = await extractor.extract(prompt)
    cookies = extractor.get_cookies()

    print(f"\n{'=' * 60}")
    print(f"snapshot: {len(snap)} 字符")
    print(f"cookies: {len(cookies or {})} 个")
    if snap:
        print(f"前50字符: {snap[:50]}...")
    print(f"{'=' * 60}")

