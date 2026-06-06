from __future__ import annotations

import json
import os
import re
import time
import warnings
from pathlib import Path
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox

from aistudio_api.config import camoufox_proxy_identity_options, settings
from aistudio_api.infrastructure.gateway.session import (
    AI_STUDIO_HOST,
    AI_STUDIO_OPEN_MODEL_PICKER_JS,
    AI_STUDIO_ONBOARDING_JS,
    AI_STUDIO_SEND_BUTTON_JS,
    AI_STUDIO_SELECT_TEXT_MODEL_JS,
    DIALOG_CLEANUP_JS,
    _aistudio_chat_urls,
)


ACCOUNTS_DIR = Path(os.environ.get("AISTUDIO_ACCOUNTS_DIR", "")).expanduser()
PROBE_PROMPT = os.environ.get("AISTUDIO_NATIVE_PROBE_PROMPT", "1")
PROBE_MODEL = os.environ.get("AISTUDIO_NATIVE_PROBE_MODEL", "").strip()
DUMP_MODEL_PICKER = os.environ.get("AISTUDIO_NATIVE_PROBE_DUMP_MODELS", "") in {"1", "true", "yes", "on"}
RESPONSE_TIMEOUT_SECONDS = int(os.environ.get("AISTUDIO_NATIVE_PROBE_TIMEOUT", "75"))
SERVICE_WORKERS = os.environ.get("AISTUDIO_NATIVE_PROBE_SERVICE_WORKERS", "").strip().lower()
REQUESTED_AUTHUSERS = [
    item.strip()
    for item in os.environ.get("AISTUDIO_NATIVE_PROBE_AUTHUSERS", "").split(",")
    if item.strip().isdigit()
]
warnings.filterwarnings("ignore", message="When using a proxy, it is heavily recommended that you pass `geoip=True`.*")


def _browser_options() -> dict[str, object]:
    options: dict[str, object] = {
        "headless": True,
        "main_world_eval": True,
        "firefox_user_prefs": {
            "network.dns.disableIPv6": True,
            "network.http.http3.enable": False,
        },
    }
    if settings.proxy_server:
        options["proxy"] = {"server": settings.proxy_server}
        options.update(camoufox_proxy_identity_options())
    return options


def _account_ids() -> list[str]:
    registry_path = ACCOUNTS_DIR / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    requested = [item.strip() for item in os.environ.get("AISTUDIO_NATIVE_PROBE_ACCOUNTS", "").split(",") if item.strip()]
    if requested:
        return requested
    accounts = registry.get("accounts") if isinstance(registry, dict) else None
    if not isinstance(accounts, dict):
        raise SystemExit("NATIVE_PROBE_FAIL missing_accounts_registry")
    return list(accounts.keys())


def _safe_text(value: object, limit: int = 240) -> str:
    text = str(value or "")
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _url_path(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.hostname != AI_STUDIO_HOST:
        return f"{parsed.hostname or '<unknown>'}{parsed.path or ''}"
    return parsed.path or "/"


def _page_state(page) -> dict[str, object]:
    try:
        body = page.evaluate("() => document.body?.innerText || ''")
    except Exception:
        body = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        has_textarea = page.query_selector("textarea") is not None
    except Exception:
        has_textarea = False
    return {
        "url_path": _url_path(getattr(page, "url", "")),
        "title": _safe_text(title, 120),
        "has_textarea": has_textarea,
        "body_head": _safe_text(body, 300),
    }


def _wait_for_chat_ready(page) -> bool:
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            if page.evaluate("mw:!!window.default_MakerSuite") and page.query_selector("textarea") is not None:
                return True
        except Exception:
            pass
        try:
            page.evaluate(DIALOG_CLEANUP_JS)
            result = page.evaluate(AI_STUDIO_ONBOARDING_JS)
            if isinstance(result, dict) and result.get("submitted"):
                page.wait_for_timeout(1500)
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False


def _chat_urls(route_authuser: str | None) -> list[str]:
    if route_authuser is None:
        return list(_aistudio_chat_urls())
    return [f"https://aistudio.google.com/u/{route_authuser}/prompts/new_chat"]


def _open_chat(page, route_authuser: str | None = None) -> bool:
    for url in _chat_urls(route_authuser):
        try:
            page.goto(url, wait_until="commit", timeout=90000)
        except Exception as exc:
            print(json.dumps({"phase": "goto_error", "url_path": _url_path(url), "error": _safe_text(exc, 180)}), flush=True)
            continue
        if "accounts.google.com" in str(getattr(page, "url", "")):
            return False
        if _wait_for_chat_ready(page):
            return True
    return False


def _fill_prompt(page) -> None:
    textarea = page.query_selector("textarea")
    if textarea is None:
        raise RuntimeError("textarea not found")
    textarea.fill(PROBE_PROMPT)
    page.wait_for_timeout(500)


def _click_run(page) -> bool:
    try:
        page.evaluate(DIALOG_CLEANUP_JS)
    except Exception:
        pass
    try:
        result = page.evaluate(AI_STUDIO_SEND_BUTTON_JS, True)
        if isinstance(result, dict) and result.get("clicked"):
            return True
    except Exception:
        pass
    for selector in ("button:has-text('Run')", "button[aria-label*='Run' i]", "button[title*='Run' i]", "button:has-text('Send')"):
        button = page.query_selector(selector)
        if button is None:
            continue
        button.click()
        return True
    return False


def _model_picker_candidates(page) -> list[dict[str, object]]:
    try:
        raw = page.evaluate(
            r"""(() => {
                const textOf = (el) => String(
                    el?.innerText ||
                    el?.textContent ||
                    el?.getAttribute?.('aria-label') ||
                    el?.getAttribute?.('title') ||
                    el?.getAttribute?.('data-value') ||
                    el?.getAttribute?.('data-model') ||
                    ''
                ).replace(/\s+/g, ' ').trim();
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                return Array.from(document.querySelectorAll('button, [role="button"], [role="option"], mat-option, [data-value], [data-model], mat-select, [role="combobox"]'))
                    .filter(visible)
                    .slice(0, 80)
                    .map((node) => ({
                        tag: String(node.tagName || '').toLowerCase(),
                        role: node.getAttribute?.('role') || '',
                        text: textOf(node).slice(0, 220),
                        ariaLabel: String(node.getAttribute?.('aria-label') || '').slice(0, 220),
                        title: String(node.getAttribute?.('title') || '').slice(0, 220),
                        dataValue: String(node.getAttribute?.('data-value') || '').slice(0, 220),
                        dataModel: String(node.getAttribute?.('data-model') || '').slice(0, 220),
                        ariaSelected: String(node.getAttribute?.('aria-selected') || ''),
                        classes: String(node.className || '').slice(0, 160),
                    }));
            })()"""
        )
    except Exception as exc:
        return [{"error": _safe_text(exc, 220)}]
    if not isinstance(raw, list):
        return []
    safe: list[dict[str, object]] = []
    for item in raw[:80]:
        if not isinstance(item, dict):
            continue
        safe.append({key: _safe_text(value, 220) for key, value in item.items() if value})
    return safe


def _select_probe_model(page, model: str) -> dict[str, object]:
    result: dict[str, object] = {"requested": model}
    try:
        opened = page.evaluate(AI_STUDIO_OPEN_MODEL_PICKER_JS)
        if isinstance(opened, dict):
            result["opened"] = {key: _safe_text(value, 220) for key, value in opened.items()}
        page.wait_for_timeout(1000)
    except Exception as exc:
        result["open_error"] = _safe_text(exc, 220)
    if DUMP_MODEL_PICKER:
        result["before"] = _model_picker_candidates(page)
    try:
        selected = page.evaluate(AI_STUDIO_SELECT_TEXT_MODEL_JS, model)
        if isinstance(selected, dict):
            result["selected"] = {key: _safe_text(value, 220) for key, value in selected.items()}
        else:
            result["selected"] = _safe_text(selected, 220)
        page.wait_for_timeout(2500)
    except Exception as exc:
        result["select_error"] = _safe_text(exc, 220)
    if DUMP_MODEL_PICKER:
        result["after"] = _model_picker_candidates(page)
    return result


def _probe_account(browser, account_id: str, route_authuser: str | None = None) -> dict[str, object]:
    auth_path = ACCOUNTS_DIR / account_id / "auth.json"
    context_options: dict[str, object] = {"storage_state": str(auth_path)}
    if SERVICE_WORKERS in {"allow", "block"}:
        context_options["service_workers"] = SERVICE_WORKERS
    context = browser.new_context(**context_options)
    page = context.new_page()
    events: list[dict[str, object]] = []
    model_selection: dict[str, object] | None = None

    def on_response(response) -> None:
        url = response.url or ""
        if "GenerateContent" not in url or "CountTokens" in url:
            return
        request = response.request
        body = request.post_data or ""
        model = ""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, list) and parsed:
                model = str(parsed[0])
        except Exception:
            pass
        response_head = ""
        try:
            response_head = response.text()[:240]
        except Exception as exc:
            response_head = f"response_text_error={type(exc).__name__}"
        events.append(
            {
                "status": response.status,
                "model": model,
                "body_size": len(body.encode("utf-8")),
                "response_head": _safe_text(response_head),
            }
        )

    page.on("response", on_response)
    try:
        ready = _open_chat(page, route_authuser=route_authuser)
        if not ready:
            return {
                "account": account_id,
                "authuser": route_authuser,
                "ok": False,
                "reason": "chat_not_ready",
                **_page_state(page),
            }
        if PROBE_MODEL:
            model_selection = _select_probe_model(page, PROBE_MODEL)
        _fill_prompt(page)
        clicked = _click_run(page)
        if not clicked:
            return {
                "account": account_id,
                "authuser": route_authuser,
                "ok": False,
                "reason": "run_button_not_found",
                **_page_state(page),
            }

        deadline = time.time() + RESPONSE_TIMEOUT_SECONDS
        while time.time() < deadline and not events:
            page.wait_for_timeout(500)
        return {
            "account": account_id,
            "authuser": route_authuser,
            "ok": bool(events and events[-1].get("status") == 200),
            "model_selection": model_selection,
            "events": events,
            **_page_state(page),
        }
    except Exception as exc:
        return {
            "account": account_id,
            "authuser": route_authuser,
            "ok": False,
            "reason": f"{type(exc).__name__}: {_safe_text(exc, 220)}",
            **_page_state(page),
        }
    finally:
        context.close()


def main() -> None:
    if not ACCOUNTS_DIR.is_dir():
        raise SystemExit("NATIVE_PROBE_FAIL missing_accounts_dir")
    with Camoufox(**_browser_options()) as browser:
        for account_id in _account_ids():
            authusers = REQUESTED_AUTHUSERS or [None]
            for route_authuser in authusers:
                print(json.dumps({"phase": "native_probe_start", "account": account_id, "authuser": route_authuser}), flush=True)
                result = _probe_account(browser, account_id, route_authuser=route_authuser)
                print(json.dumps({"phase": "native_probe_result", **result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()