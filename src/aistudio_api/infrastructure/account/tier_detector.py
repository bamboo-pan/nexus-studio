"""Detect AI Studio account subscription tier by scraping the page header."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger("aistudio.premium_detect")


TIER_DETECT_JS = r"""() => {
    const bodyText = document.body ? (document.body.innerText || '') : '';

    const textOf = (el) => String(el.innerText || el.textContent || '').trim();
    const collectText = (selector, maxLength = 2000) => Array.from(document.querySelectorAll(selector))
        .map(textOf)
        .filter(Boolean)
        .filter(text => text.length < maxLength)
        .join('\n');

    const headerText = collectText('header, [role="banner"], nav');
    const buttonText = collectText('button, [role="button"], [aria-label]');
    const overlayText = collectText('[role="dialog"], [role="menu"], .cdk-overlay-pane');
    const allText = [headerText, buttonText, overlayText, bodyText].filter(Boolean).join('\n');

    const emailMatch = allText.match(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/i);
    const email = emailMatch ? emailMatch[0] : null;

    return {
        email: email,
        header: headerText.substring(0, 1000),
        text: allText.substring(0, 8000),
    };
}"""


ACCOUNT_MENU_CLICK_JS = r"""(email) => {
    if (!email) return false;
    const needle = String(email).toLowerCase();
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], [aria-label]'));
    const target = candidates.find(el => String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').toLowerCase().includes(needle));
    if (!target) return false;
    target.click();
    return true;
}"""

AI_STUDIO_ONBOARDING_JS = r"""() => {
    const body = document.body ? (document.body.innerText || '') : '';
    const lowerBody = body.toLowerCase();
    const needsConsent = lowerBody.includes('i consent to the google apis terms') ||
        lowerBody.includes('gemini api additional terms of service');
    if (!needsConsent) return {needed: false, checked: false, submitted: false, remaining: false};

    const textOf = (el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const requiredConsentText = (text) => {
        const lower = text.toLowerCase();
        if (lower.includes('opt in') || lower.includes('news') || lower.includes('offers') || lower.includes('promotions')) return false;
        return lower.includes('i consent') || lower.includes('terms of service') || lower.includes('gemini api additional terms');
    };

    let checked = false;
    for (const el of Array.from(document.querySelectorAll('input[type="checkbox"], mat-checkbox, .mat-mdc-checkbox, .mdc-checkbox, [role="checkbox"], label'))) {
        const root = el.closest('mat-checkbox') || el.closest('label') || el;
        const input = el.matches && el.matches('input[type="checkbox"]') ? el : root.querySelector && root.querySelector('input[type="checkbox"]');
        const text = `${textOf(root)} ${input ? textOf(input) : ''}`;
        if (!requiredConsentText(text)) continue;
        if (!visible(el)) continue;
        const alreadyChecked = (input && input.checked) || el.getAttribute('aria-checked') === 'true';
        if (!alreadyChecked) {
            const target = input || (root.querySelector && (root.querySelector('.mdc-checkbox') || root.querySelector('[role="checkbox"]'))) || root;
            try { target.scrollIntoView({block: 'center', inline: 'center'}); } catch(e) {}
            target.click();
            if (input && !input.checked) {
                input.checked = true;
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
            }
            checked = true;
        }
        break;
    }

    let submitted = false;
    for (const button of Array.from(document.querySelectorAll('button, [role="button"]'))) {
        const label = textOf(button).toLowerCase();
        if (!label || !visible(button)) continue;
        if (button.disabled || button.getAttribute('aria-disabled') === 'true') continue;
        if (/continue|accept|agree|get started|start using|done|next/.test(label)) {
            button.click();
            submitted = true;
            break;
        }
    }

    const remaining = (document.body ? (document.body.innerText || '') : '').toLowerCase().includes('i consent to the google apis terms');
    return {needed: true, checked, submitted, remaining};
}"""


class AccountTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ULTRA = "ultra"


EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)
TIER_TOKEN_RE = {
    AccountTier.PRO: re.compile(r"(?<![A-Za-z0-9_-])(?:google\s+)?(?:ai\s+)?pro(?![A-Za-z0-9_-])", re.IGNORECASE),
    AccountTier.ULTRA: re.compile(r"(?<![A-Za-z0-9_-])(?:google\s+)?(?:ai\s+)?ultra(?![A-Za-z0-9_-])", re.IGNORECASE),
}
ACCOUNT_CONTEXT_MARKERS = (
    "manage membership",
    "membership",
    "switch account",
    "sign out",
    "google account",
)
UPGRADE_OFFER_MARKERS = (
    "upgrade",
    "try google ai",
    "get google ai",
    "subscribe",
    "start trial",
)


def _text_lines(text: str | None) -> list[str]:
    return [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]


def _has_tier_token(text: str, tier: AccountTier) -> bool:
    return bool(TIER_TOKEN_RE[tier].search(text))


def _is_upgrade_offer(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in UPGRADE_OFFER_MARKERS)


def _is_tier_context(line: str, email: str | None) -> bool:
    lowered = line.lower()
    if email and email.lower() in lowered:
        return True
    if any(marker in lowered for marker in ACCOUNT_CONTEXT_MARKERS):
        return True
    return False


def parse_account_tier_from_text(text: str | None, email: str | None = None) -> AccountTier:
    """Classify an AI Studio account tier from visible page/account-menu text."""
    lines = _text_lines(text)
    if not lines:
        return AccountTier.FREE

    email_lower = email.lower() if email else None
    if email_lower:
        for index, line in enumerate(lines):
            if email_lower not in line.lower():
                continue
            window = "\n".join(lines[max(0, index - 2): index + 3])
            if _is_upgrade_offer(window):
                continue
            for tier in (AccountTier.ULTRA, AccountTier.PRO):
                if _has_tier_token(window, tier):
                    return tier

    for tier in (AccountTier.ULTRA, AccountTier.PRO):
        for index, line in enumerate(lines):
            window = "\n".join(lines[max(0, index - 2): index + 3])
            if _is_upgrade_offer(window):
                continue
            if _has_tier_token(line, tier) and _is_tier_context(window, email):
                return tier

    return AccountTier.FREE


def _tier_result_from_page_data(data: dict) -> TierResult:
    email = data.get("email") if isinstance(data.get("email"), str) else None
    header = data.get("header") if isinstance(data.get("header"), str) else ""
    text = data.get("text") if isinstance(data.get("text"), str) else header
    if email is None:
        match = EMAIL_RE.search(text)
        email = match.group(0) if match else None
    return TierResult(
        tier=parse_account_tier_from_text(text, email=email),
        email=email,
        raw_header=header,
    )


async def _complete_onboarding(page) -> bool:
    completed = False
    for _ in range(4):
        result = await page.evaluate(AI_STUDIO_ONBOARDING_JS)
        if not isinstance(result, dict) or not result.get("needed"):
            return completed
        completed = completed or bool(result.get("checked") or result.get("submitted"))
        await page.wait_for_timeout(1200)
        if not result.get("remaining") and result.get("submitted"):
            return True
    return completed


def _complete_onboarding_sync(page) -> bool:
    completed = False
    for _ in range(4):
        result = page.evaluate(AI_STUDIO_ONBOARDING_JS)
        if not isinstance(result, dict) or not result.get("needed"):
            return completed
        completed = completed or bool(result.get("checked") or result.get("submitted"))
        page.wait_for_timeout(1200)
        if not result.get("remaining") and result.get("submitted"):
            return True
    return completed


@dataclass
class TierResult:
    tier: AccountTier
    email: str | None = None
    raw_header: str | None = None  # for debugging

    @property
    def is_premium(self) -> bool:
        return self.tier in (AccountTier.PRO, AccountTier.ULTRA)


async def detect_tier(
    browser_context,
    timeout_ms: int = 30000,
) -> TierResult:
    """
    Navigate to AI Studio and detect account tier from the page header.

    Premium accounts show a badge (PRO/ULTRA) next to the email.
    Free accounts show an "Upgrade to unlock more" banner.

    Args:
        browser_context: A Playwright BrowserContext with auth cookies loaded.
        timeout_ms: Navigation timeout.

    Returns:
        TierResult with detected tier and email.
    """
    page = await browser_context.new_page()
    try:
        await page.goto(
            "https://aistudio.google.com/",
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        await page.wait_for_function(
            "() => document.body && document.body.innerText && document.body.innerText.length > 0",
            timeout=timeout_ms,
        )
        await page.wait_for_timeout(2500)
        if await _complete_onboarding(page):
            await page.wait_for_timeout(2500)

        result = _tier_result_from_page_data(await page.evaluate(TIER_DETECT_JS))
        if result.tier == AccountTier.FREE and result.email:
            opened = await page.evaluate(ACCOUNT_MENU_CLICK_JS, result.email)
            if opened:
                await page.wait_for_timeout(1000)
                result = _tier_result_from_page_data(await page.evaluate(TIER_DETECT_JS))

        return result
    finally:
        await page.close()


def detect_tier_sync(browser_context, timeout_ms: int = 30000) -> TierResult:
    """Synchronous variant for the sync Camoufox context used by BrowserSession."""
    page = browser_context.new_page()
    try:
        page.goto(
            "https://aistudio.google.com/",
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        page.wait_for_function(
            "() => document.body && document.body.innerText && document.body.innerText.length > 0",
            timeout=timeout_ms,
        )
        page.wait_for_timeout(2500)
        if _complete_onboarding_sync(page):
            page.wait_for_timeout(2500)
        result = _tier_result_from_page_data(page.evaluate(TIER_DETECT_JS))
        if result.tier == AccountTier.FREE and result.email:
            opened = page.evaluate(ACCOUNT_MENU_CLICK_JS, result.email)
            if opened:
                page.wait_for_timeout(1000)
                result = _tier_result_from_page_data(page.evaluate(TIER_DETECT_JS))
        return result
    finally:
        page.close()


async def detect_tier_for_auth_file(
    auth_file: str | Path,
    camoufox_port: int = 9222,
    timeout_ms: int = 30000,
) -> TierResult:
    """
    Convenience function: connect to running Camoufox, load auth, detect tier.

    Args:
        auth_file: Path to the auth JSON file (Playwright storage state).
        camoufox_port: Camoufox debug port.
        timeout_ms: Navigation timeout.

    Returns:
        TierResult with detected tier and email.
    """
    from playwright.async_api import async_playwright

    auth_file = str(auth_file)
    if not Path(auth_file).exists():
        raise FileNotFoundError(f"Auth file not found: {auth_file}")

    pw = await async_playwright().start()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{camoufox_port}/json", timeout=5
        )
        data = json.loads(resp.read())
        ws_url = f"ws://127.0.0.1:{camoufox_port}{data['wsEndpointPath']}"

        browser = await pw.firefox.connect(ws_url)
        ctx = await browser.new_context(storage_state=auth_file)
        try:
            return await detect_tier(ctx, timeout_ms=timeout_ms)
        finally:
            await ctx.close()
    finally:
        await pw.stop()


# --- CLI ---

async def main():
    import sys

    # Walk up to project root (where data/ lives)
    project_root = Path(__file__).resolve().parents[4]  # src/aistudio_api/infrastructure/account/
    accounts_dir = project_root / "data" / "accounts"
    if not accounts_dir.is_dir():
        # Fallback: search upward for data/accounts
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "data" / "accounts"
            if candidate.is_dir():
                accounts_dir = candidate
                break
    if len(sys.argv) > 1:
        # Specific account(s)
        account_ids = sys.argv[1:]
    else:
        # All accounts with auth files
        account_ids = [
            d.name for d in accounts_dir.iterdir()
            if d.is_dir() and (d / "auth.json").exists()
        ]

    print(f"Checking {len(account_ids)} account(s)...\n")

    for aid in sorted(account_ids):
        auth_file = accounts_dir / aid / "auth.json"
        if not auth_file.exists():
            print(f"  {aid}: no auth.json, skipped")
            continue

        try:
            result = await detect_tier_for_auth_file(auth_file)
            badge = "⭐" if result.is_premium else "  "
            print(f"  {badge} {aid}: {result.tier.value.upper():6s}  ({result.email})")
        except Exception as e:
            print(f"  ❌ {aid}: error — {e}")


if __name__ == "__main__":
    asyncio.run(main())
