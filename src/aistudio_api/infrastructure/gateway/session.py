"""Shared Camoufox session management for gateway operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from aistudio_api.config import camoufox_proxy_identity_options, settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

log = logging.getLogger("aistudio.session")

AI_STUDIO_URL = "https://aistudio.google.com/prompts/new_chat"
AI_STUDIO_URL_FALLBACK = "https://aistudio.google.com/app/prompts/new_chat"
AI_STUDIO_IMAGE_URL = "https://aistudio.google.com/prompts/new_image?model=imagen-4.0-generate-001"
AI_STUDIO_IMAGE_URL_FALLBACK = "https://aistudio.google.com/app/prompts/new_image?model=imagen-4.0-generate-001"
AI_STUDIO_HOME_URL = "https://aistudio.google.com/"
AI_STUDIO_HOST = "aistudio.google.com"
AI_DEVELOPERS_HOST = "ai.google.dev"
AI_STUDIO_CHAT_PATH_PREFIXES = ("/prompts/", "/app/prompts/")
AI_STUDIO_CHAT_READY_TIMEOUT_MS = 90_000
AI_STUDIO_CHAT_READY_POLL_MS = 1_000
AI_STUDIO_SEND_BUTTON_SELECTORS = (
    "button:has-text('Run')",
    "button:has-text('Send')",
    "button:has-text('Generate')",
    "button:has-text('运行')",
    "button:has-text('发送')",
    "button[aria-label*='Run' i]",
    "button[aria-label*='Send' i]",
    "button[aria-label*='Generate' i]",
    "button[aria-label*='运行' i]",
    "button[aria-label*='发送' i]",
    "button[title*='Run' i]",
    "button[title*='Send' i]",
    "button[title*='Generate' i]",
    "button[title*='运行' i]",
    "button[title*='发送' i]",
)
AI_STUDIO_SEND_BUTTON_JS = r"""(clickButton) => {
    const keywords = ['run', 'send', 'generate', '运行', '发送'];
    const iconKeywords = ['send', 'play_arrow', 'arrow_upward'];
    const visible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const labelOf = (el) => [
        el.innerText,
        el.textContent,
        el.getAttribute('aria-label'),
        el.getAttribute('title'),
        el.getAttribute('data-tooltip'),
        el.querySelector('mat-icon')?.textContent,
        el.querySelector('[data-mat-icon-name]')?.getAttribute('data-mat-icon-name'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
    const enabled = (el) => !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const candidates = Array.from(document.querySelectorAll('button, [role="button"]'))
        .filter((el) => visible(el) && enabled(el));
    const target = candidates.find((el) => {
        const label = labelOf(el);
        return keywords.some((keyword) => label.includes(keyword)) || iconKeywords.some((keyword) => label.includes(keyword));
    });
    if (!target) return {found: false, clicked: false, label: ''};
    const label = labelOf(target);
    if (clickButton) target.click();
    return {found: true, clicked: !!clickButton, label};
}"""
INSTALL_HOOKS_JS = r"""
mw:((() => {
    // Verify hooks are actually present on XHR prototype, not just a stale flag
    const xhrHookAlive = XMLHttpRequest.prototype.open.__api_hooked === true;
    const fetchHookAlive = window.fetch.__api_hooked === true;
    if (window.__bg_hooked && xhrHookAlive && fetchHookAlive) return 'already_hooked';
    // Reset stale flag if hooks are missing
    if (window.__bg_hooked && (!xhrHookAlive || !fetchHookAlive)) window.__bg_hooked = false;

    const dms = window.default_MakerSuite;
    if (!dms) return 'no_default_MakerSuite';

    // Auto-detect snapshot function via feature matching
    let snapKey = null;
    for (const k of Object.keys(dms)) {
        try {
            if (typeof dms[k] !== 'function') continue;
            const src = dms[k].toString();
            if (src.includes('.snapshot({') && src.includes('content') && src.includes('yield')) {
                snapKey = k;
                break;
            }
        } catch(e) {}
    }
    if (!snapKey) return 'no_snapshot_fn';

    // Hook snapshot function to capture service (only if not already hooked)
    if (!dms[snapKey].__api_hooked) {
        const origSnap = dms[snapKey];
        dms[snapKey] = function(...args) {
            window.__bg_service = args[0];
            const result = origSnap.apply(this, args);
            if (result instanceof Promise) return result.then(s => { window.__bg_snapshot = s; return s; });
            window.__bg_snapshot = result;
            return result;
        };
        dms[snapKey].__api_hooked = true;
    }

    // XHR hook for body replacement (always re-install if missing)
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    const hookedOpen = function(method, url, ...args) {
        this.__url = url;
        this.__is_gen = url.includes('GenerateContent') && !url.includes('CountTokens');
        window.__last_hook_url = url;
        return origOpen.call(this, method, url, ...args);
    };
    hookedOpen.__api_hooked = true;
    XMLHttpRequest.prototype.open = hookedOpen;
    XMLHttpRequest.prototype.send = function(body) {
        if (this.__is_gen && window.__pending_body) {
            const captured = window.__pending_body;
            window.__pending_body = null;
            window.__hooked = true;
            window.__last_hook_url = this.__url || '';
            return origSend.call(this, captured);
        }
        return origSend.call(this, body);
    };

    // fetch hook for body replacement (streaming uses fetch)
    const origFetch = window.fetch;
    const hookedFetch = function(input, init) {
        let url = typeof input === 'string' ? input : (input instanceof Request ? input.url : String(input));
        if (url.includes('GenerateContent') && !url.includes('CountTokens') && window.__pending_body) {
            const captured = window.__pending_body;
            window.__pending_body = null;
            window.__hooked = true;
            window.__last_hook_url = url;
            if (init) {
                init.body = captured;
            } else {
                init = { body: captured };
            }
            return origFetch.call(this, input, init);
        }
        return origFetch.call(this, input, init);
    };
    hookedFetch.__api_hooked = true;
    window.fetch = hookedFetch;

    window.__bg_hooked = true;
    window.__snap_key = snapKey;
    return 'hooked:' + snapKey;
})())
"""

DIALOG_CLEANUP_JS = """(() => {
    document.querySelectorAll('button').forEach((button) => {
        const text = (button.textContent || '').trim().toLowerCase();
        if (['dismiss', 'close', 'accept', 'ok', 'agree', 'got it'].includes(text)) {
            button.click();
        }
    });
    document.querySelectorAll('.cdk-overlay-backdrop').forEach((node) => node.remove());
    document.querySelectorAll('.cdk-overlay-container').forEach((node) => node.remove());
})()"""

AI_STUDIO_ONBOARDING_JS = r"""(() => {
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
        if (!visible(button)) continue;
        if (button.disabled || button.getAttribute('aria-disabled') === 'true') continue;
        const label = textOf(button).toLowerCase();
        if (!label) continue;
        if (/continue|accept|agree|get started|start using|done|next/.test(label)) {
            button.click();
            submitted = true;
            break;
        }
    }

    const remaining = (document.body ? (document.body.innerText || '') : '').toLowerCase().includes('i consent to the google apis terms');
    return {needed: true, checked, submitted, remaining};
})()"""

AI_STUDIO_TRIGGER_IMAGE_ONBOARDING_JS = r"""(() => {
    const body = document.body ? (document.body.innerText || '') : '';
    const lowerBody = body.toLowerCase();
    if (lowerBody.includes('gemini api additional terms of service') || lowerBody.includes('i consent to the google apis terms')) {
        return {triggered: false, reason: 'already_visible'};
    }
    const textOf = (el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    for (const button of Array.from(document.querySelectorAll('button, [role="button"]'))) {
        if (!visible(button)) continue;
        const label = textOf(button).toLowerCase();
        if (label.includes('image generation') || label.includes('nano banana')) {
            button.click();
            return {triggered: true, label: textOf(button).slice(0, 120)};
        }
    }
    return {triggered: false, reason: 'image_entry_not_found'};
})()"""

AI_STUDIO_SELECT_IMAGE_MODEL_JS = r"""(model) => {
    const targetModel = String(model || '').replace(/^models\//, '').toLowerCase();
    const labels = targetModel.includes('pro-image')
        ? ['nano banana pro', 'banana pro', 'gemini 3 pro image', 'pro image']
        : targetModel.includes('flash-image')
            ? ['nano banana 2', 'nano banana', 'gemini 3.1 flash image', 'flash image']
            : [];
    if (!labels.length) return {selected: false, reason: 'not_image_model'};

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
    const normalized = (value) => textOf(value).toLowerCase();
    const modelPickerCandidates = Array.from(document.querySelectorAll('mat-select, [role="combobox"], button, [role="button"]'));
    const sendLike = (label) => /\b(run|send|generate)\b|运行|发送|生成/.test(label);
    const imageRoute = /new_image/i.test(window.location.pathname || window.location.href || '');
    const imageContext = () => imageRoute || /nano banana|imagen|image generation|image model|image_edit_auto|图片|图像|生图/i.test(document.body?.innerText || '');
    const pickerLike = (label) => /nano banana|imagen|image generation|image model|image_edit_auto|gemini 3(?:\.1)? .*image|flash image|pro image|图片|图像|生图/i.test(label);
    const selectedNodes = () => Array.from(document.querySelectorAll('[aria-selected="true"], .selected, .active, .mat-mdc-option-active, [role="combobox"], mat-select'));
    const selectedText = () => selectedNodes().filter(visible).map(normalized).join(' ');
    if (labels.some((label) => selectedText().includes(label))) return {selected: false, reason: 'already_selected', label: selectedText().slice(0, 120)};

    let candidates = Array.from(document.querySelectorAll('button, [role="button"], [role="option"], mat-option, [data-value], [data-model]'));
    const visibleLabels = () => candidates.filter(visible).map(textOf).filter(Boolean).slice(0, 18);
    let openedLabel = '';
    if (!candidates.some((candidate) => visible(candidate) && candidate.matches?.('[role="option"], mat-option, [data-value], [data-model]'))) {
        if (!imageContext()) return {selected: false, reason: 'image_picker_not_open', visible: visibleLabels()};
        for (const candidate of modelPickerCandidates) {
            if (!visible(candidate)) continue;
            if (candidate.disabled || candidate.getAttribute('aria-disabled') === 'true') continue;
            const label = normalized(candidate);
            if (!label || sendLike(label) || !pickerLike(label)) continue;
            try { candidate.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
            candidate.click();
            openedLabel = textOf(candidate).slice(0, 120);
            break;
        }
        candidates = Array.from(document.querySelectorAll('button, [role="button"], [role="option"], mat-option, [data-value], [data-model]'));
    }
    for (const labelNeedle of labels) {
        for (const candidate of candidates) {
            if (!visible(candidate)) continue;
            const label = normalized(candidate);
            if (!label.includes(labelNeedle)) continue;
            if (label.includes('content_copy')) continue;
            if (labelNeedle === 'nano banana' && label.includes('nano banana pro')) continue;
            try { candidate.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
            candidate.click();
            const selectedLabel = textOf(candidate).slice(0, 120);
            return {selected: true, label: selectedLabel, opened: openedLabel, visible: visibleLabels()};
        }
    }
    return {selected: false, reason: 'model_card_not_found', opened: openedLabel, visible: visibleLabels()};
}"""

AI_STUDIO_OPEN_IMAGE_MODEL_PICKER_JS = r"""(() => {
    const textOf = (el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const lowerBody = (document.body?.innerText || '').toLowerCase();
    const imageRoute = /new_image/i.test(window.location.pathname || window.location.href || '');
    const imageContext = imageRoute || /nano banana|imagen|image generation|image model|image_edit_auto|图片|图像|生图/i.test(lowerBody);
    if (!imageContext) return {opened: false, reason: 'not_image_context'};
    const isSendControl = (label) => /\b(run|send|generate)\b|运行|发送|生成/.test(label.toLowerCase());
    const imagePickerLike = (label) => /nano banana|imagen|image generation|image model|image_edit_auto|gemini 3(?:\.1)? .*image|flash image|pro image|图片|图像|生图/i.test(label);
    const candidates = Array.from(document.querySelectorAll('mat-select, [role="combobox"], button, [role="button"]'));
    for (const candidate of candidates) {
        if (!visible(candidate)) continue;
        if (candidate.disabled || candidate.getAttribute('aria-disabled') === 'true') continue;
        const label = textOf(candidate);
        if (!label || isSendControl(label) || !imagePickerLike(label)) continue;
        try { candidate.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
        candidate.click();
        return {opened: true, label: label.slice(0, 160)};
    }
    return {opened: false, reason: 'image_model_picker_not_found'};
})()"""

AI_STUDIO_LIST_MODELS_JS = r"""(() => {
    const values = new Set();
    const add = (value) => {
        const text = String(value || '').trim();
        if (!text) return;
        for (const match of text.matchAll(/\b(?:models\/)?(?:gemini|gemma|deep-research|learnlm)-[a-z0-9][a-z0-9._-]*\b/gi)) {
            const model = match[0].replace(/^models\//i, '').toLowerCase();
            if (model.length <= 80) values.add(model);
        }
    };
    const visit = (value, depth = 0) => {
        if (depth > 4 || value == null) return;
        if (typeof value === 'string' || typeof value === 'number') {
            add(value);
            return;
        }
        if (Array.isArray(value)) {
            value.slice(0, 200).forEach((item) => visit(item, depth + 1));
            return;
        }
        if (typeof value === 'object') {
            for (const key of ['name', 'id', 'model', 'modelId', 'displayName', 'value', 'label']) {
                if (Object.prototype.hasOwnProperty.call(value, key)) visit(value[key], depth + 1);
            }
        }
    };
    const collectFromWindow = (root, depth = 0, seen = new Set()) => {
        if (depth > 2 || root == null || seen.has(root)) return;
        if (typeof root !== 'object' && typeof root !== 'function') return;
        seen.add(root);
        for (const key of Object.keys(root).slice(0, 400)) {
            const lower = key.toLowerCase();
            if (!/(model|gemini|gemma)/.test(lower)) continue;
            try { visit(root[key]); } catch (e) {}
        }
    };
    add(document.body?.innerText || '');
    document.querySelectorAll('[aria-label], [title], [data-value], [data-model], [data-test-id], option').forEach((node) => {
        add(node.getAttribute('aria-label'));
        add(node.getAttribute('title'));
        add(node.getAttribute('data-value'));
        add(node.getAttribute('data-model'));
        add(node.getAttribute('data-test-id'));
        add(node.textContent);
    });
    collectFromWindow(window);
    collectFromWindow(window.default_MakerSuite);
    return Array.from(values).sort((left, right) => left.localeCompare(right));
})()"""

AI_STUDIO_OPEN_MODEL_PICKER_JS = r"""(() => {
    const textOf = (el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const isSendControl = (label) => /\b(run|send|generate)\b|运行|发送|生成/.test(label.toLowerCase());
    const modelish = (label) => /model|模型|gemini|gemma|deep research|nano banana/i.test(label) || /\b(?:models\/)?(?:gemini|gemma|deep-research|learnlm)-[a-z0-9][a-z0-9._-]*\b/i.test(label);
    const candidates = Array.from(document.querySelectorAll('mat-select, [role="combobox"], button, [role="button"]'));
    for (const candidate of candidates) {
        if (!visible(candidate)) continue;
        if (candidate.disabled || candidate.getAttribute('aria-disabled') === 'true') continue;
        const label = textOf(candidate);
        if (!label || isSendControl(label) || !modelish(label)) continue;
        try { candidate.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
        candidate.click();
        return {opened: true, label: label.slice(0, 160)};
    }
    return {opened: false, reason: 'model_picker_not_found'};
})()"""


class BrowserSession:
    def __init__(self, port: int):
        self.port = port
        self._auth_file = settings.auth_file
        self._hook_page = None
        self._ctx = None
        self._browser = None
        self._cf = None
        self._snap_key: str | None = None
        self._templates: dict[str, dict[str, Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aistudio-camoufox")
        self._botguard_lock = asyncio.Lock()
        self._snapshot_lock = asyncio.Lock()

    async def ensure_context(self):
        return await self._run_sync(self._ensure_browser_sync)

    async def switch_auth(self, auth_file: str | None) -> None:
        await self._run_sync(self._switch_auth_sync, auth_file)

    async def close(self) -> None:
        await self._run_sync(self._close_sync)

    def clear_templates(self) -> None:
        self._templates.clear()

    async def detect_tier_for_auth_file(self, auth_file: str, timeout_ms: int = 30000):
        return await self._run_sync(self._detect_tier_for_auth_file_sync, auth_file, timeout_ms)

    async def ensure_hook_page(self):
        await self._run_sync(self._ensure_hook_page_sync)
        return True

    async def ensure_botguard_service(self):
        await self._run_sync(self._ensure_botguard_service_sync)
        return True

    async def capture_template(self, model: str) -> dict[str, Any]:
        return await self._run_sync(self._capture_template_sync, model)

    async def list_available_models(self) -> list[str]:
        return await self._run_sync(self._list_available_models_sync)

    async def upload_images(self, image_paths: list[str]) -> list[str]:
        return await self._run_sync(self._upload_images_sync, image_paths)

    async def generate_snapshot(self, contents: list[AistudioContent]) -> str:
        loop = asyncio.get_running_loop()
        async with self._snapshot_lock:
            return await loop.run_in_executor(self._executor, lambda: self._generate_snapshot_sync(contents))

    async def send_hooked_request(self, *, body: str, url: str, headers: dict[str, str], timeout_ms: int) -> tuple[int, bytes]:
        return await self._run_sync(self._send_hooked_request_sync, body, url, headers, timeout_ms)

    async def send_streaming_request(self, *, body: str, url: str, headers: dict[str, str], timeout_ms: int):
        """Send a streaming request, yielding ("status", int) and ("chunk", bytes) events."""
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancel_event = threading.Event()

        def _stream_worker():
            try:
                log.debug("[stream] worker started")
                self._send_streaming_request_sync(body, url, headers, timeout_ms, queue, loop, cancel_event)
                log.debug("[stream] worker finished")
            except Exception as e:
                log.debug(f"[stream] worker exception: {e}")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", e))
                loop.call_soon_threadsafe(queue.put_nowait, None)

        executor_task = loop.run_in_executor(self._executor, _stream_worker)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                tag, data = item
                if tag == "error":
                    raise data
                yield tag, data
        finally:
            cancel_event.set()
            await executor_task

    async def _run_sync(self, func, *args):
        loop = asyncio.get_running_loop()
        async with self._botguard_lock:
            return await loop.run_in_executor(self._executor, lambda: func(*args))

    def _get_captured_info(self) -> tuple[str, dict[str, str]]:
        """Get captured URL and headers from template."""
        for tpl in self._templates.values():
            if tpl.get("url"):
                url = tpl["url"]
                headers = {k: v for k, v in tpl.get("headers", {}).items() if k.lower() not in ("host", "content-length")}
                return url, headers
        raise RuntimeError("no captured URL available for replay")

    def _send_streaming_request_sync(
        self,
        body: str,
        url: str,
        headers: dict[str, str],
        timeout_ms: int,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        cancel_event: threading.Event,
    ):
        """Sync method: sends XHR request and consumes page-side stream events."""
        import time as _t
        _t0 = _t.time()

        page, captured_url, captured_headers = self._prepare_streaming_sync(url, headers)
        log.debug(f"[stream] prep done in {_t.time()-_t0:.1f}s, url={captured_url}")

        timeout_s = timeout_ms / 1000
        rid = uuid.uuid4().hex[:8]

        # Start XHR in page context. Each request gets an isolated state object
        # keyed by rid, allowing multiple concurrent XHRs on the same page.
        page.evaluate("""(args) => {
            const rid = args.rid;
            if (!window.__streams) window.__streams = {};

            const existing = window.__streams[rid];
            if (existing && existing.xhr && existing.xhr.readyState !== 4) {
                try { existing.xhr.abort(); } catch (e) {}
            }

            const state = {
                xhr: null,
                events: [],
                waiter: null,
                recvPos: 0,
                statusSent: false,
            };
            window.__streams[rid] = state;

            function push(event) {
                if (state.waiter) {
                    const waiter = state.waiter;
                    state.waiter = null;
                    waiter(event);
                    return;
                }
                state.events.push(event);
            }

            function pushStatus(xhr) {
                if (state.statusSent || xhr.readyState < 2) return;
                state.statusSent = true;
                push({type: 'status', status: xhr.status || 0});
            }

            function pushChunk(xhr) {
                if (xhr.readyState < 3) return;
                const chunk = xhr.responseText.substring(state.recvPos);
                if (!chunk) return;
                state.recvPos = xhr.responseText.length;
                push({type: 'chunk', text: chunk});
            }

            if (!window.__stream_next) window.__stream_next = {};
            window.__stream_next[rid] = function(timeoutMs) {
                if (state.events.length) return Promise.resolve(state.events.shift());
                return new Promise((resolve) => {
                    let done = false;
                    const timer = setTimeout(() => {
                        if (done) return;
                        done = true;
                        if (state.waiter === finish) state.waiter = null;
                        resolve({type: 'idle'});
                    }, timeoutMs);
                    const finish = (event) => {
                        if (done) return;
                        done = true;
                        clearTimeout(timer);
                        resolve(event);
                    };
                    state.waiter = finish;
                });
            };

            if (!window.__stream_abort) window.__stream_abort = {};
            window.__stream_abort[rid] = function() {
                if (state.xhr && state.xhr.readyState !== 4) {
                    try { state.xhr.abort(); } catch (e) {}
                }
            };

            var xhr = new XMLHttpRequest();
            xhr.open('POST', args.url);
            var h = args.headers;
            for (var k in h) {
                xhr.setRequestHeader(k, h[k]);
            }
            xhr.withCredentials = true;
            xhr.timeout = args.timeout * 1000;

            xhr.onreadystatechange = function() {
                pushStatus(xhr);
                pushChunk(xhr);
            };
            xhr.onprogress = function() {
                pushStatus(xhr);
                pushChunk(xhr);
            };
            xhr.onload = function() {
                pushStatus(xhr);
                pushChunk(xhr);
                push({type: 'done'});
            };
            xhr.onerror = function() {
                push({type: 'error', message: 'network error'});
            };
            xhr.ontimeout = function() {
                push({type: 'error', message: 'timeout'});
            };
            xhr.onabort = function() {
                push({type: 'aborted'});
            };

            state.xhr = xhr;
            xhr.send(args.body);
        }""", {
            "url": captured_url,
            "headers": captured_headers,
            "body": body,
            "timeout": timeout_s,
            "rid": rid,
        })

        deadline = _t.time() + timeout_s
        status_sent = False
        while _t.time() < deadline:
            if cancel_event.is_set():
                log.debug("[stream] cancellation requested for %s", rid)
                page.evaluate("rid => { if (window.__stream_abort && window.__stream_abort[rid]) window.__stream_abort[rid](); }", rid)
                break

            event = page.evaluate("rid => window.__stream_next[rid](250)", rid)
            event_type = event.get("type")

            if event_type == "idle":
                continue
            if event_type == "status":
                status = event.get("status", 0)
                log.debug(f"[stream] got status={status} after {_t.time()-_t0:.1f}s")
                loop.call_soon_threadsafe(queue.put_nowait, ("status", status))
                status_sent = True
                continue
            if event_type == "chunk":
                text = event.get("text") or ""
                if text:
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", text.encode("utf-8")))
                continue
            if event_type == "error":
                message = event.get("message", "unknown error")
                log.debug(f"[stream] error after {_t.time()-_t0:.1f}s: {message}")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", RuntimeError(f"streaming request failed: {message}")))
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return
            if event_type in ("done", "aborted"):
                break

        if not status_sent:
            log.debug(f"[stream] timeout after {_t.time()-_t0:.1f}s before response status")
            loop.call_soon_threadsafe(queue.put_nowait, ("error", RuntimeError("streaming request timeout: no response status")))
            loop.call_soon_threadsafe(queue.put_nowait, None)
            return

        # Signal completion
        loop.call_soon_threadsafe(queue.put_nowait, None)

    def _prepare_streaming_sync(self, url: str, headers: dict[str, str]):
        """Prepare page for streaming request. Returns (page, url, headers)."""
        page = self._ensure_botguard_service_sync()
        return page, url, headers

    def _switch_auth_sync(self, auth_file: str | None) -> None:
        self._auth_file = auth_file
        self._templates.clear()
        self._last_botguard_template = None
        self._close_sync()

    def _browser_options_sync(self) -> dict[str, Any]:
        browser_options: dict[str, Any] = {
            "headless": settings.camoufox_headless,
            "main_world_eval": True,
            "firefox_user_prefs": {
                "network.dns.disableIPv6": True,
                "network.http.http3.enable": False,
            },
        }
        if settings.proxy_server:
            browser_options["proxy"] = {"server": settings.proxy_server}
            browser_options.update(camoufox_proxy_identity_options())
        return browser_options

    def _ensure_browser_process_sync(self):
        if self._browser is not None:
            return self._browser
        from camoufox.sync_api import Camoufox

        self._cf = Camoufox(**self._browser_options_sync())
        self._browser = self._cf.__enter__()
        return self._browser

    def _ensure_browser_sync(self):
        if self._ctx is not None and self._hook_page is not None and not self._hook_page.is_closed():
            return self._ctx

        import time as _t
        _t0 = _t.time()

        self._close_sync()
        self._ensure_browser_process_sync()
        self._ctx = self._new_context_sync()
        self._hook_page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        log.debug(f"[timing] browser launched in {_t.time()-_t0:.1f}s")
        self._goto_aistudio_sync(self._hook_page)
        log.debug(f"[timing] page loaded in {_t.time()-_t0:.1f}s")
        self._install_hooks_sync(self._hook_page)
        log.debug(f"[timing] hooks installed in {_t.time()-_t0:.1f}s")
        return self._ctx

    def _new_context_sync(self):
        if self._auth_file:
            auth_path = Path(self._auth_file)
            if not auth_path.exists():
                raise FileNotFoundError(
                    f"Browser auth state file is missing: {auth_path}. "
                    "Activate an account or complete login again before browser preheat/capture."
                )
            try:
                return self._browser.new_context(storage_state=self._auth_file)
            except Exception:
                ctx = self._browser.new_context()
                try:
                    self._apply_storage_state_sync(ctx, self._auth_file)
                    return ctx
                except Exception as fallback_exc:
                    raise RuntimeError(
                        f"Browser auth state file is invalid: {auth_path}. "
                        "Activate an account or complete login again before browser preheat/capture."
                    ) from fallback_exc
        return self._browser.new_context()

    def _detect_tier_for_auth_file_sync(self, auth_file: str, timeout_ms: int = 30000):
        from aistudio_api.infrastructure.account.tier_detector import detect_tier_sync

        self._ensure_browser_process_sync()
        try:
            ctx = self._browser.new_context(storage_state=auth_file)
        except Exception:
            ctx = self._browser.new_context()
            self._apply_storage_state_sync(ctx, auth_file)
        try:
            return detect_tier_sync(ctx, timeout_ms=timeout_ms)
        finally:
            ctx.close()

    def _apply_storage_state_sync(self, ctx, auth_file: str) -> None:
        data = json.loads(Path(auth_file).read_text())
        cookies = data.get("cookies") or []
        if cookies:
            ctx.add_cookies(cookies)

    def _ensure_hook_page_sync(self):
        self._ensure_browser_sync()
        if not self._is_chat_runtime_ready_sync(self._hook_page):
            log.debug("hook page not chat-ready before install: %s", self._format_chat_runtime_diagnostics_sync(self._hook_page))
            self._goto_aistudio_sync(self._hook_page)
        if self._complete_aistudio_onboarding_sync(self._hook_page):
            self._wait_for_chat_runtime_sync(self._hook_page)
        self._install_hooks_sync(self._hook_page)
        return self._hook_page

    def _ensure_botguard_service_sync(self):
        import time as _t
        _t0 = _t.time()
        page = self._ensure_hook_page_sync()
        if page.evaluate("mw:!!window.__bg_service"):
            log.debug(f"[timing] botguard cached, took {_t.time()-_t0:.1f}s")
            return page

        page.evaluate(DIALOG_CLEANUP_JS)
        textarea = page.query_selector("textarea")
        if textarea is None:
            # Debug: show page state
            try:
                dbg_url = page.url
                dbg_title = page.title()
                dbg_body = page.evaluate("() => document.body?.innerText?.substring(0, 300) || ''")
            except Exception:
                dbg_url = dbg_title = dbg_body = '<error>'
            raise RuntimeError(f"textarea not found while capturing BotGuardService; url={dbg_url}, title={dbg_title}, body={dbg_body[:200]}")
        self._fill_prompt_text_sync(page, textarea, "1")
        page.wait_for_timeout(800)
        page.evaluate(DIALOG_CLEANUP_JS)
        captured: dict[str, Any] = {}

        def on_route(route):
            request = route.request
            body = request.post_data
            if not captured and self._is_template_capture_request(url=request.url, body=body, model_marker="", allow_text_markers=True):
                captured["url"] = request.url
                captured["headers"] = dict(request.headers)
                captured["body"] = body
            route.continue_()

        route_pattern = "**/*"
        page.route(route_pattern, on_route)
        try:
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger send while capturing BotGuardService")

            for i in range(45):
                page.wait_for_timeout(1000)
                if page.evaluate("mw:!!window.__bg_service"):
                    self._wait_until_idle_sync(page)
                    log.debug(f"[timing] botguard captured after {i+1}s, total {_t.time()-_t0:.1f}s")
                    return page
        finally:
            page.unroute(route_pattern, on_route)

        raise RuntimeError("BotGuardService capture timeout")

    def _capture_template_sync(self, model: str) -> dict[str, Any]:
        import time as _t
        _t0 = _t.time()
        if model in self._templates:
            log.debug(f"[timing] template cached for {model}")
            return self._templates[model]

        if self._is_image_model(model):
            page = self._ensure_hook_page_sync()
            self._prepare_model_onboarding_sync(page, model)
            self._install_hooks_sync(page)
            captured = self._capture_template_request_with_recovery_sync(page, model)
            if not page.evaluate("mw:!!window.__bg_service"):
                self._goto_aistudio_sync(page)
                self._install_hooks_sync(page)
                self._ensure_botguard_service_sync()
            self._templates[model] = captured
            log.debug(f"[timing] image template captured for {model} in {_t.time()-_t0:.1f}s")
            return captured

        page = self._ensure_botguard_service_sync()
        log.debug(f"[timing] botguard done in {_t.time()-_t0:.1f}s, starting template capture")
        captured = self._capture_template_request_with_recovery_sync(page, model)
        self._templates[model] = captured
        log.debug(f"[timing] template captured for {model} in {_t.time()-_t0:.1f}s")
        return captured

    def _is_image_model(self, model: str) -> bool:
        return "image" in (model or "").lower()

    def _capture_template_request_sync(self, page, model: str) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        route_pattern = "**/*"
        observed: list[str] = []
        model_marker = (model or "").lower()

        def on_route(route):
            request = route.request
            if captured:
                route.continue_()
                return
            body = request.post_data
            url = request.url
            if self._is_template_capture_request(url=url, body=body, model_marker=model_marker, allow_text_markers=not self._is_image_model(model)):
                captured["url"] = url
                captured["headers"] = dict(request.headers)
                captured["body"] = body
                route.abort()
                return
            if body and len(body) > 100 and AI_STUDIO_HOST in url and len(observed) < 5:
                observed.append(f"{url} body={len(body)}")
                route.continue_()
                return
            route.continue_()

        page.route(route_pattern, on_route)
        try:
            textarea = page.query_selector("textarea")
            if textarea is None:
                raise RuntimeError("textarea not found during template capture")
            self._fill_prompt_text_sync(page, textarea, "template")
            page.wait_for_timeout(500)
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger send during template capture")

            for _ in range(300):
                if captured:
                    break
                page.wait_for_timeout(100)
            if not captured:
                suffix = f"; observed={observed}" if observed else ""
                raise RuntimeError(f"template capture timeout for model={model}{suffix}")
        finally:
            page.unroute(route_pattern, on_route)

        self._wait_until_idle_sync(page)
        return captured

    def _is_template_capture_request(self, *, url: str, body: str | None, model_marker: str, allow_text_markers: bool = False) -> bool:
        if not body or len(body) <= 100:
            return False
        try:
            parsed_body = json.loads(body)
        except json.JSONDecodeError:
            return False
        if not isinstance(parsed_body, list):
            return False
        lower_url = (url or "").lower()
        if "counttokens" in lower_url or "count" in lower_url:
            return False
        if "generatecontent" in lower_url:
            return True
        if AI_STUDIO_HOST not in lower_url:
            return False
        if "/data/batchexecute" not in lower_url:
            return False
        lower_body = body.lower()
        if "template" in lower_body or bool(model_marker and model_marker in lower_body):
            return True
        if allow_text_markers:
            return "models/" in lower_body or "snapshot" in lower_body or "generatecontent" in lower_body
        return False

    def _capture_template_request_with_recovery_sync(self, page, model: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            if not self._is_chat_runtime_ready_sync(page):
                self._goto_aistudio_sync(page)
                self._install_hooks_sync(page)
            try:
                return self._capture_template_request_sync(page, model)
            except Exception as exc:
                last_error = exc
                diagnostics = self._format_chat_runtime_diagnostics_sync(page)
                if attempt == 0 and not self._is_chat_runtime_ready_sync(page):
                    log.info("AI Studio page left chat runtime during template capture; reopening before retry: %s", diagnostics)
                    self._goto_aistudio_sync(page)
                    self._install_hooks_sync(page)
                    continue
                if attempt == 0 and self._is_image_model(model):
                    log.info("AI Studio image template capture failed; re-opening image model before retry: %s; %s", exc, diagnostics)
                    self._prepare_model_onboarding_sync(page, model)
                    self._install_hooks_sync(page)
                    continue
                if attempt == 0 and "template capture timeout" in str(exc):
                    log.info("AI Studio template send produced no capture request; reopening before retry: %s; %s", exc, diagnostics)
                    self._goto_aistudio_sync(page)
                    self._install_hooks_sync(page)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"template capture failed for model={model}")

    def _generate_snapshot_sync(self, contents: list[AistudioContent]) -> str:
        snapshot = ""
        for attempt in range(3):
            snapshot = self._generate_snapshot_once_sync(contents)
            if len(snapshot) >= 1500:
                return snapshot
            if attempt < 2:
                log.info("AI Studio snapshot looks immature (%d chars); retrying", len(snapshot))
                page = self._ensure_botguard_service_sync()
                page.wait_for_timeout(1500)
        return snapshot

    def _generate_snapshot_once_sync(self, contents: list[AistudioContent]) -> str:
        page = self._ensure_botguard_service_sync()
        if not self._snap_key:
            raise RuntimeError("Snapshot function not detected")

        # 计算 content hash（包含图片数据，与 camoufox-api 一致）
        hash_parts: list[str] = []
        for content in contents:
            for part in content.parts:
                if part.inline_data:
                    hash_parts.append(part.inline_data[1])  # base64 data
                if part.text:
                    hash_parts.append(str(part.text))
        content_hash = sha256(" ".join(hash_parts).encode("utf-8")).hexdigest()

        page.evaluate(
            """
mw:((hash) => {
    const dms = window.default_MakerSuite;
    const service = window.__bg_service;
    const snapKey = window.__snap_key;
    if (!dms || !service || !snapKey || typeof dms[snapKey] !== 'function') {
        window.__sr = '';
        window.__sl = 0;
        window.__snap_error = 'service_unavailable';
        return;
    }
    window.__sr = '';
    window.__sl = 0;
    window.__snap_error = '';
    const result = dms[snapKey](service, hash);
    if (result instanceof Promise) {
        result.then((snapshot) => {
            window.__sr = snapshot || '';
            window.__sl = snapshot ? snapshot.length : 0;
        }).catch((error) => {
            window.__snap_error = String(error);
        });
        return;
    }
    window.__sr = result || '';
    window.__sl = result ? result.length : 0;
})(%s)
"""
            % json.dumps(content_hash)
        )
        for _ in range(20):
            if page.evaluate("mw:(window.__sl || 0)") > 0:
                break
            page.wait_for_timeout(500)

        snapshot = page.evaluate("mw:window.__sr")
        if snapshot:
            return snapshot
        error = page.evaluate("mw:window.__snap_error || ''")
        raise RuntimeError(f"Snapshot generation failed: {error or 'unknown'}")

    def _upload_images_sync(self, image_paths: list[str]) -> list[str]:
        if not image_paths:
            return []

        # 尝试非 UI 方式上传（更快、更可靠）
        # 需要在主线程中获取 cookies，因为 Playwright 的同步 API 有 greenlet 限制
        try:
            if self._ctx is not None:
                cookies = self._ctx.cookies()
                return self._upload_images_via_api_sync(image_paths, cookies)
        except Exception as e:
            # 如果非 UI 方式失败，回退到 UI 方式
            import logging
            logging.getLogger("aistudio").debug("Non-UI upload failed, falling back to UI: %s", e)
            pass

        # UI 方式上传（原有逻辑）
        page = self._ensure_botguard_service_sync()
        self._wait_until_idle_sync(page)
        uploaded_ids: list[str] = []

        def on_response(response):
            if "content.googleapis.com/upload/drive/v3/files" not in response.url:
                return
            try:
                payload = json.loads(response.text())
            except Exception:
                return
            file_id = payload.get("id")
            if file_id:
                uploaded_ids.append(file_id)

        page.on("response", on_response)
        try:
            for image_path in image_paths:
                target_count = len(uploaded_ids) + 1
                page.evaluate(DIALOG_CLEANUP_JS)
                upload_btn = page.locator('[aria-label="Insert images, videos, audio, or files"]').first
                if not upload_btn.is_visible(timeout=3000):
                    raise RuntimeError("upload button not visible")
                upload_btn.click()
                page.wait_for_timeout(1500)
                page.evaluate(DIALOG_CLEANUP_JS)
                upload_files_btn = page.locator("text=Upload files").first
                if not upload_files_btn.is_visible(timeout=3000):
                    upload_btn.click()
                    page.wait_for_timeout(1000)
                    upload_files_btn = page.locator("text=Upload files").first
                if not upload_files_btn.is_visible(timeout=3000):
                    raise RuntimeError("upload files button not visible")
                with page.expect_file_chooser(timeout=10000) as chooser_info:
                    upload_files_btn.click()
                chooser_info.value.set_files(image_path)

                deadline = time.time() + 30
                while time.time() < deadline:
                    if len(uploaded_ids) >= target_count:
                        break
                    page.wait_for_timeout(500)
                page.wait_for_timeout(1500)
        finally:
            page.remove_listener("response", on_response)

        if len(uploaded_ids) != len(image_paths):
            raise RuntimeError(f"image upload incomplete: expected={len(image_paths)} uploaded={len(uploaded_ids)}")
        return uploaded_ids

    def _upload_images_via_api_sync(self, image_paths: list[str], cookies: list[dict]) -> list[str]:
        """通过 Playwright 的 setInputFiles 方法上传图片（非 UI 点击方式）"""
        page = self._hook_page
        if page is None:
            raise RuntimeError("Hook page not initialized")

        uploaded_ids: list[str] = []

        def on_response(response):
            if "content.googleapis.com/upload/drive/v3/files" not in response.url:
                return
            try:
                payload = json.loads(response.text())
            except Exception:
                return
            file_id = payload.get("id")
            if file_id:
                uploaded_ids.append(file_id)

        page.on("response", on_response)
        try:
            # 找到文件输入元素（如果有的话）
            file_input = page.query_selector('input[type="file"]')

            if file_input:
                # 直接使用 setInputFiles 方法上传
                for image_path in image_paths:
                    target_count = len(uploaded_ids) + 1
                    file_input.set_input_files(image_path)

                    # 等待上传完成
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if len(uploaded_ids) >= target_count:
                            break
                        page.wait_for_timeout(500)
                    page.wait_for_timeout(1000)
            else:
                # 如果没有 file input，尝试创建一个
                page.evaluate("""
                    () => {
                        const input = document.createElement('input');
                        input.type = 'file';
                        input.id = '__api_file_input__';
                        input.style.display = 'none';
                        input.accept = 'image/*';
                        document.body.appendChild(input);

                        // 监听文件选择事件
                        input.addEventListener('change', (e) => {
                            const file = e.target.files[0];
                            if (file) {
                                // 触发上传逻辑
                                window.__api_upload_file = file;
                            }
                        });
                    }
                """)

                file_input = page.query_selector('#__api_file_input__')
                if not file_input:
                    raise RuntimeError("Failed to create file input")

                for image_path in image_paths:
                    target_count = len(uploaded_ids) + 1
                    file_input.set_input_files(image_path)
                    page.wait_for_timeout(1000)

                    # 触发上传
                    page.evaluate("""
                        () => {
                            if (window.__api_upload_file) {
                                // 模拟拖放或触发上传按钮
                                const event = new Event('change', { bubbles: true });
                                const input = document.querySelector('#__api_file_input__');
                                if (input) input.dispatchEvent(event);
                            }
                        }
                    """)

                    # 等待上传完成
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if len(uploaded_ids) >= target_count:
                            break
                        page.wait_for_timeout(500)
                    page.wait_for_timeout(1000)

        finally:
            page.remove_listener("response", on_response)

        if len(uploaded_ids) != len(image_paths):
            raise RuntimeError(f"image upload incomplete: expected={len(image_paths)} uploaded={len(uploaded_ids)}")
        return uploaded_ids

    def _send_hooked_request_sync(self, body: str, url: str, headers: dict[str, str], timeout_ms: int) -> tuple[int, bytes]:
        import time as _t
        _t0 = _t.time()
        page = self._ensure_botguard_service_sync()
        log.debug(f"[timing] botguard ready in {_t.time()-_t0:.1f}s")
        captured_url = url
        captured_headers = headers

        # Replay via XHR in browser context (same approach as non-streaming replay_v2)
        timeout_s = timeout_ms / 1000
        result = page.evaluate("""(args) => {
            return new Promise((resolve) => {
                var xhr = new XMLHttpRequest();
                xhr.open('POST', args.url);
                var h = args.headers;
                for (var k in h) {
                    xhr.setRequestHeader(k, h[k]);
                }
                xhr.withCredentials = true;
                xhr.timeout = args.timeout * 1000;
                xhr.onload = function() {
                    resolve({status: xhr.status, body: xhr.responseText});
                };
                xhr.onerror = function() {
                    resolve({status: 0, body: 'network error'});
                };
                xhr.ontimeout = function() {
                    resolve({status: 0, body: 'timeout'});
                };
                xhr.send(args.body);
            });
        }""", {
            "url": captured_url,
            "headers": captured_headers,
            "body": body,
            "timeout": timeout_s,
        })

        status = result.get("status", 0)
        raw_text = result.get("body", "")
        log.debug(f"[timing] replay done in {_t.time()-_t0:.1f}s, status={status}")
        if status == 0:
            raise RuntimeError(f"replay failed: {raw_text}")
        return status, raw_text.encode("utf-8")

    def _goto_aistudio_sync(self, page) -> None:
        import time as _t
        last_error = None
        for url in (AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK, AI_STUDIO_HOME_URL):
            route_started_at = _t.time()
            goto_error = None
            try:
                page.goto(url, wait_until="commit", timeout=60000)
                log.debug(f"[timing] goto {url} took {_t.time()-route_started_at:.1f}s")
            except Exception as exc:
                goto_error = exc
                last_error = exc
                log.debug(f"[timing] goto {url} failed after {_t.time()-route_started_at:.1f}s: {exc}")

            current_url = getattr(page, "url", "")
            if self._is_google_signin_url(current_url):
                diagnostics = self._format_chat_runtime_diagnostics_sync(page)
                auth_state = self._format_auth_state_diagnostics()
                raise RuntimeError(
                    f"AI Studio redirected to Google sign-in after navigating to {url}; "
                    f"browser auth state is missing or invalid ({auth_state}). "
                    f"Activate an account or complete login again. {diagnostics}"
                )

            if self._is_ai_developers_url(current_url):
                log.debug("AI Studio redirected to docs after %s: %s", url, current_url)
                if url != AI_STUDIO_URL_FALLBACK:
                    last_error = RuntimeError(f"AI Studio redirected to docs after navigating to {url}: {current_url}")
                    continue
                try:
                    page.goto(AI_STUDIO_URL_FALLBACK, wait_until="commit", timeout=60000)
                    page.wait_for_timeout(2500)
                    current_url = getattr(page, "url", "")
                except Exception as exc:
                    last_error = exc
                    continue

            if goto_error is not None and not self._is_aistudio_url(current_url):
                continue

            if self._wait_for_chat_runtime_sync(page):
                if self._complete_aistudio_onboarding_sync(page):
                    self._wait_for_chat_runtime_sync(page)
                log.debug(f"[timing] UI ready (dms+textarea) after {_t.time()-route_started_at:.1f}s")
                return

            diagnostics = self._format_chat_runtime_diagnostics_sync(page)
            last_error = RuntimeError(f"AI Studio chat runtime not ready after navigating to {url}: {diagnostics}")
            log.debug("[timing] UI not ready after %.1fs: %s", _t.time() - route_started_at, diagnostics)
        if last_error is not None:
            raise last_error

    def _is_aistudio_url(self, url: str | None) -> bool:
        try:
            parsed = urlparse(url or "")
        except Exception:
            return False
        return parsed.hostname == AI_STUDIO_HOST

    def _is_google_signin_url(self, url: str | None) -> bool:
        try:
            parsed = urlparse(url or "")
        except Exception:
            return False
        return parsed.hostname == "accounts.google.com" and "/signin" in (parsed.path or "")

    def _is_ai_developers_url(self, url: str | None) -> bool:
        try:
            parsed = urlparse(url or "")
        except Exception:
            return False
        return parsed.hostname == AI_DEVELOPERS_HOST

    def _format_auth_state_diagnostics(self) -> str:
        if not self._auth_file:
            return "auth_file=<none>"
        auth_path = Path(self._auth_file)
        return f"auth_file={auth_path}, exists={auth_path.exists()}"

    def _is_aistudio_chat_url(self, url: str | None) -> bool:
        try:
            parsed = urlparse(url or "")
        except Exception:
            return False
        if parsed.hostname != AI_STUDIO_HOST:
            return False
        return any((parsed.path or "").startswith(prefix) for prefix in AI_STUDIO_CHAT_PATH_PREFIXES)

    def _chat_runtime_state_sync(self, page, *, include_details: bool = False) -> dict[str, Any]:
        errors: list[str] = []
        try:
            url = page.url or ""
        except Exception as exc:
            url = ""
            errors.append(f"url={exc}")

        state: dict[str, Any] = {
            "url": url,
            "is_chat_route": self._is_aistudio_chat_url(url),
            "has_default_MakerSuite": False,
            "has_textarea": False,
            "title": "",
            "body": "",
            "errors": errors,
        }

        try:
            state["has_default_MakerSuite"] = bool(page.evaluate("mw:!!window.default_MakerSuite"))
        except Exception as exc:
            errors.append(f"default_MakerSuite={exc}")

        try:
            state["has_textarea"] = page.query_selector("textarea") is not None
        except Exception as exc:
            errors.append(f"textarea={exc}")

        if include_details:
            try:
                state["title"] = page.title()
            except Exception as exc:
                errors.append(f"title={exc}")
            try:
                state["body"] = page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")
            except Exception as exc:
                errors.append(f"body={exc}")

        return state

    def _is_chat_runtime_ready_sync(self, page) -> bool:
        state = self._chat_runtime_state_sync(page)
        return bool(state["is_chat_route"] and state["has_default_MakerSuite"] and state["has_textarea"])

    def _wait_for_chat_runtime_sync(self, page) -> bool:
        attempts = max(1, AI_STUDIO_CHAT_READY_TIMEOUT_MS // AI_STUDIO_CHAT_READY_POLL_MS)
        for attempt_index in range(attempts):
            state = self._chat_runtime_state_sync(page)
            if state["is_chat_route"] and state["has_default_MakerSuite"] and state["has_textarea"]:
                return True
            if state["has_default_MakerSuite"] and attempt_index >= 20:
                try:
                    page.evaluate(DIALOG_CLEANUP_JS)
                except Exception:
                    pass
            page.wait_for_timeout(AI_STUDIO_CHAT_READY_POLL_MS)

        return self._is_chat_runtime_ready_sync(page)

    def _format_chat_runtime_diagnostics_sync(self, page) -> str:
        state = self._chat_runtime_state_sync(page, include_details=True)
        body = str(state.get("body") or "").replace("\n", " ")[:300]
        parts = [
            f"url={state.get('url') or '<unknown>'}",
            f"title={state.get('title') or '<unknown>'}",
            f"chat_route={state.get('is_chat_route')}",
            f"default_MakerSuite={state.get('has_default_MakerSuite')}",
            f"textarea={state.get('has_textarea')}",
            f"body={body or '<empty>'}",
        ]
        errors = state.get("errors") or []
        if errors:
            parts.append(f"errors={'; '.join(errors)}")
        return ", ".join(parts)

    def _complete_aistudio_onboarding_sync(self, page) -> bool:
        completed = False
        for _ in range(4):
            try:
                result = page.evaluate(AI_STUDIO_ONBOARDING_JS)
            except Exception as exc:
                log.debug("AI Studio onboarding check failed: %s", exc)
                return completed
            if not isinstance(result, dict) or not result.get("needed"):
                return completed
            completed = completed or bool(result.get("checked") or result.get("submitted"))
            if result.get("submitted"):
                log.info("AI Studio onboarding confirmation submitted")
            page.wait_for_timeout(1200)
            if not result.get("remaining") and result.get("submitted"):
                return True
        return completed

    def _prepare_model_onboarding_sync(self, page, model: str) -> bool:
        if "image" not in (model or "").lower():
            return False
        prepared = False

        if self._complete_aistudio_onboarding_sync(page):
            prepared = True
            page.wait_for_timeout(1200)

        trigger_result: dict[str, Any] | None = None
        for _ in range(3):
            try:
                result = page.evaluate(AI_STUDIO_TRIGGER_IMAGE_ONBOARDING_JS)
            except Exception as exc:
                log.debug("AI Studio image onboarding trigger failed: %s", exc)
                return prepared
            trigger_result = result if isinstance(result, dict) else None
            if trigger_result and trigger_result.get("triggered"):
                prepared = True
                log.info("AI Studio image onboarding entry opened")
                page.wait_for_timeout(1200)
                try:
                    page.evaluate(DIALOG_CLEANUP_JS)
                except Exception:
                    pass
                break
            if trigger_result and trigger_result.get("reason") == "already_visible":
                if self._complete_aistudio_onboarding_sync(page):
                    prepared = True
                    page.wait_for_timeout(1200)
                    continue
            break

        if trigger_result and not trigger_result.get("triggered") and trigger_result.get("reason") not in {"already_visible"}:
            log.info("AI Studio image onboarding entry not opened: %s", trigger_result.get("reason"))
            if trigger_result.get("reason") == "image_entry_not_found":
                self._goto_aistudio_image_sync(page, model)
                prepared = True

        try:
            page.evaluate(DIALOG_CLEANUP_JS)
        except Exception:
            pass

        if self._complete_aistudio_onboarding_sync(page):
            prepared = True
            page.wait_for_timeout(1200)
        try:
            opened = page.evaluate(AI_STUDIO_OPEN_IMAGE_MODEL_PICKER_JS)
            if isinstance(opened, dict) and opened.get("opened"):
                page.wait_for_timeout(800)
        except Exception as exc:
            log.debug("AI Studio image model picker open failed: %s", exc)
        try:
            selected = page.evaluate(AI_STUDIO_SELECT_IMAGE_MODEL_JS, model)
        except Exception as exc:
            log.debug("AI Studio image model selection failed: %s", exc)
            return prepared
        if isinstance(selected, dict) and selected.get("selected"):
            prepared = True
            log.info("AI Studio image model selected: %s", selected.get("label", "<unknown>"))
            page.wait_for_timeout(1200)
            if self._complete_aistudio_onboarding_sync(page):
                page.wait_for_timeout(1200)
        elif isinstance(selected, dict) and selected.get("reason") not in {"already_selected", "not_image_model"}:
            if not self._is_aistudio_image_context_sync(page, selected):
                self._goto_aistudio_image_sync(page, model)
                prepared = True
            if self._is_aistudio_image_context_sync(page, selected):
                log.info(
                    "AI Studio image model picker did not expose %s (%s); continuing on image route for body rewrite",
                    model,
                    selected.get("reason"),
                )
                return True
            raise RuntimeError(
                "AI Studio image model not selected: "
                f"model={model}, reason={selected.get('reason')}, opened={selected.get('opened')}, visible={selected.get('visible')}"
            )
        return prepared

    def _aistudio_image_urls_for_model(self, model: str) -> tuple[str, str]:
        model_id = str(model or "").strip().removeprefix("models/") or "imagen-4.0-generate-001"
        encoded = quote(model_id, safe="")
        return (
            f"https://aistudio.google.com/prompts/new_image?model={encoded}",
            f"https://aistudio.google.com/app/prompts/new_image?model={encoded}",
        )

    def _is_aistudio_image_url(self, url: str | None) -> bool:
        try:
            parsed = urlparse(url or "")
        except Exception:
            return False
        if parsed.hostname != AI_STUDIO_HOST:
            return False
        path = parsed.path or ""
        return path.startswith("/prompts/new_image") or path.startswith("/app/prompts/new_image")

    def _is_aistudio_image_context_sync(self, page, selection: dict[str, Any] | None = None) -> bool:
        if self._is_aistudio_image_url(getattr(page, "url", "")):
            return True
        selection = selection if isinstance(selection, dict) else {}
        visible_values = selection.get("visible") if isinstance(selection.get("visible"), list) else []
        hints = [selection.get("opened"), *visible_values]
        if any("image_edit_auto" in str(hint).lower() or "nano banana" in str(hint).lower() or "imagen" in str(hint).lower() for hint in hints):
            return True
        try:
            body = page.evaluate("() => document.body?.innerText?.toLowerCase() || ''")
        except Exception:
            return False
        return any(hint in body for hint in ("image_edit_auto", "nano banana", "imagen", "image generation"))

    def _goto_aistudio_image_sync(self, page, model: str = "") -> None:
        last_error = None
        for url in self._aistudio_image_urls_for_model(model):
            try:
                page.goto(url, wait_until="commit", timeout=60000)
            except Exception as exc:
                last_error = exc
                continue
            current_url = getattr(page, "url", "")
            if self._is_google_signin_url(current_url):
                diagnostics = self._format_chat_runtime_diagnostics_sync(page)
                auth_state = self._format_auth_state_diagnostics()
                raise RuntimeError(
                    f"AI Studio redirected to Google sign-in after navigating to {url}; "
                    f"browser auth state is missing or invalid ({auth_state}). "
                    f"Activate an account or complete login again. {diagnostics}"
                )
            if self._wait_for_chat_runtime_sync(page):
                if self._complete_aistudio_onboarding_sync(page):
                    self._wait_for_chat_runtime_sync(page)
                return
            last_error = RuntimeError(f"AI Studio image runtime not ready after navigating to {url}: {self._format_chat_runtime_diagnostics_sync(page)}")
        if last_error is not None:
            raise last_error

    def _list_available_models_sync(self) -> list[str]:
        page = self._ensure_hook_page_sync()
        try:
            opened = page.evaluate(AI_STUDIO_OPEN_MODEL_PICKER_JS)
            if isinstance(opened, dict) and opened.get("opened"):
                page.wait_for_timeout(800)
        except Exception as exc:
            log.debug("AI Studio model picker open failed before extraction: %s", exc)
        try:
            models = page.evaluate(AI_STUDIO_LIST_MODELS_JS)
        except Exception as exc:
            diagnostics = self._format_chat_runtime_diagnostics_sync(page)
            raise RuntimeError(f"AI Studio model list extraction failed: {exc}; {diagnostics}") from exc
        if not isinstance(models, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for model in models:
            model_id = str(model or "").strip().removeprefix("models/").lower()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            result.append(model_id)
        return result

    def _install_hooks_sync(self, page) -> None:
        try:
            result = page.evaluate(INSTALL_HOOKS_JS)
        except Exception as exc:
            diagnostics = self._format_chat_runtime_diagnostics_sync(page)
            raise RuntimeError(f"Hook install failed: {exc}; {diagnostics}") from exc
        if result == "already_hooked":
            return
        if isinstance(result, str) and result.startswith("hooked:"):
            self._snap_key = result.split(":", 1)[1]
            return
        for attempt_index in range(3):
            page.wait_for_timeout(2000)
            try:
                result = page.evaluate(INSTALL_HOOKS_JS)
            except Exception as exc:
                diagnostics = self._format_chat_runtime_diagnostics_sync(page)
                raise RuntimeError(f"Hook install failed after retry {attempt_index + 1}: {exc}; {diagnostics}") from exc
            if result == "already_hooked":
                return
            if isinstance(result, str) and result.startswith("hooked:"):
                self._snap_key = result.split(":", 1)[1]
                return
        diagnostics = self._format_chat_runtime_diagnostics_sync(page)
        raise RuntimeError(f"Hook install failed: {result}; {diagnostics}")

    def _click_run_button_sync(self, page) -> bool:
        for selector in AI_STUDIO_SEND_BUTTON_SELECTORS:
            try:
                button = page.query_selector(selector)
            except Exception:
                continue
            if button is None:
                continue
            try:
                if button.evaluate("el => el.disabled || el.getAttribute('aria-disabled') === 'true'"):
                    continue
            except Exception:
                pass
            try:
                button.click()
                return True
            except Exception:
                continue

        try:
            result = page.evaluate(AI_STUDIO_SEND_BUTTON_JS, True)
            if isinstance(result, dict) and result.get("clicked"):
                return True
        except Exception:
            pass

        try:
            textarea = page.query_selector("textarea")
            if textarea is None:
                return False
            try:
                textarea.click()
            except Exception:
                try:
                    textarea.focus()
                except Exception:
                    pass
            page.keyboard.press("Control+Enter")
            page.wait_for_timeout(500)
            return False
        except Exception:
            return False

    def _fill_prompt_text_sync(self, page, textarea, text: str) -> None:
        try:
            textarea.click()
        except Exception:
            try:
                textarea.focus()
            except Exception:
                pass
        textarea.fill(text)
        try:
            page.evaluate(
                """(value) => {
                    const textarea = document.querySelector('textarea');
                    if (!textarea) return false;
                    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
                    if (setter) setter.call(textarea, value);
                    else textarea.value = value;
                    textarea.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
                    textarea.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }""",
                text,
            )
        except Exception:
            pass

    def _has_run_button_sync(self, page) -> bool:
        for selector in AI_STUDIO_SEND_BUTTON_SELECTORS:
            try:
                button = page.query_selector(selector)
                if button is not None:
                    try:
                        if button.evaluate("el => el.disabled || el.getAttribute('aria-disabled') === 'true'"):
                            continue
                    except Exception:
                        pass
                    return True
            except Exception:
                continue
        try:
            result = page.evaluate(AI_STUDIO_SEND_BUTTON_JS, False)
            return isinstance(result, dict) and bool(result.get("found"))
        except Exception:
            return False

    def _wait_until_idle_sync(self, page) -> None:
        for _ in range(60):
            if self._has_run_button_sync(page):
                return
            page.wait_for_timeout(1000)
        raise RuntimeError("page never became idle")

    def _close_sync(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._cf is not None:
            try:
                self._cf.__exit__(None, None, None)
            except Exception:
                pass
        self._hook_page = None
        self._ctx = None
        self._browser = None
        self._cf = None
        self._snap_key = None
        self.clear_templates()
