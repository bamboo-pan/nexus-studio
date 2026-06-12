"""Shared Camoufox session management for gateway operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from aistudio_api.config import camoufox_proxy_identity_options, settings
from aistudio_api.domain.errors import ModelNotFoundError, RequestError
from aistudio_api.infrastructure.gateway.native_ui_worker_pool import NativeUiWorkerError, NativeUiWorkerPool, NativeUiWorkerRequestError
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

log = logging.getLogger("aistudio.session")


class _NativeUiReplayUnsupported(RuntimeError):
    pass


def _is_per_user_quota_ambiguous_response(status: int, raw: bytes | str | None) -> bool:
    if int(status or 0) != 404:
        return False
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    lower_text = text.lower()
    return "ambiguous request" in lower_text and "streamgeneratecontentperuserquota" in lower_text


def _native_worker_unavailable_error(exc: BaseException) -> RequestError:
    return RequestError(503, f"native UI worker unavailable: {exc}")


def _native_worker_warmup_error_is_recoverable(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "ai studio text model not selected in native ui sender",
            "current_text_model_not_found",
            "text_category",
            "elementhandle.click: timeout",
            "element is not enabled",
            "waiting for element to be visible, enabled and stable",
        )
    )

AI_STUDIO_URL = "https://aistudio.google.com/u/2/prompts/new_chat"
AI_STUDIO_URL_FALLBACK = "https://aistudio.google.com/u/0/prompts/new_chat"
AI_STUDIO_URL_UNSCOPED_FALLBACK = "https://aistudio.google.com/prompts/new_chat"
AI_STUDIO_URL_LEGACY_FALLBACK = "https://aistudio.google.com/app/prompts/new_chat"
AI_STUDIO_IMAGE_URL = "https://aistudio.google.com/u/2/prompts/new_image?model=imagen-4.0-generate-001"
AI_STUDIO_IMAGE_URL_FALLBACK = "https://aistudio.google.com/u/0/prompts/new_image?model=imagen-4.0-generate-001"
AI_STUDIO_IMAGE_URL_UNSCOPED_FALLBACK = "https://aistudio.google.com/prompts/new_image?model=imagen-4.0-generate-001"
AI_STUDIO_IMAGE_URL_LEGACY_FALLBACK = "https://aistudio.google.com/app/prompts/new_image?model=imagen-4.0-generate-001"
AI_STUDIO_HOME_URL = "https://aistudio.google.com/"
AI_STUDIO_HOST = "aistudio.google.com"
AI_DEVELOPERS_HOST = "ai.google.dev"
AI_STUDIO_CHAT_PATH_PREFIXES = ("/prompts/", "/app/prompts/")
AI_STUDIO_NAVIGATION_TIMEOUT_MS = 60_000
AI_STUDIO_CHAT_READY_TIMEOUT_MS = 90_000
AI_STUDIO_CHAT_READY_POLL_MS = 1_000
AI_STUDIO_BOTGUARD_CAPTURE_TIMEOUT_MS = 45_000
AI_STUDIO_TEMPLATE_CAPTURE_TIMEOUT_MS = 30_000
AI_STUDIO_TEMPLATE_CAPTURE_POLL_MS = 100
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


def _configured_authuser_candidates() -> tuple[str, ...]:
    raw = str(getattr(settings, "ai_studio_authuser_candidates", "") or "")
    values: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if not value or not value.isdigit():
            continue
        if value not in values:
            values.append(value)
    for fallback in ("2", "0"):
        if fallback not in values:
            values.append(fallback)
    return tuple(values)


def _aistudio_chat_url_with_model(url: str, model: str | None = None) -> str:
    model_id = str(model or "").strip().removeprefix("models/")
    if not model_id or "new_chat" not in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}model={quote(model_id, safe='')}"


def _aistudio_chat_urls(model: str | None = None) -> tuple[str, ...]:
    urls = [_aistudio_chat_url_with_model(f"https://aistudio.google.com/u/{authuser}/prompts/new_chat", model) for authuser in _configured_authuser_candidates()]
    urls.extend([
        _aistudio_chat_url_with_model(AI_STUDIO_URL_UNSCOPED_FALLBACK, model),
        _aistudio_chat_url_with_model(AI_STUDIO_URL_LEGACY_FALLBACK, model),
        AI_STUDIO_HOME_URL,
    ])
    return tuple(dict.fromkeys(urls))


def _aistudio_chat_url_for_authuser(authuser: str, model: str | None = None) -> str:
    return _aistudio_chat_url_with_model(f"https://aistudio.google.com/u/{authuser}/prompts/new_chat", model)


def _aistudio_image_urls(model: str) -> tuple[str, ...]:
    model_id = str(model or "").strip().removeprefix("models/") or "imagen-4.0-generate-001"
    encoded = quote(model_id, safe="")
    urls = [f"https://aistudio.google.com/u/{authuser}/prompts/new_image?model={encoded}" for authuser in _configured_authuser_candidates()]
    urls.extend([
        f"https://aistudio.google.com/prompts/new_image?model={encoded}",
        f"https://aistudio.google.com/app/prompts/new_image?model={encoded}",
    ])
    return tuple(dict.fromkeys(urls))
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
    const textareas = Array.from(document.querySelectorAll('textarea')).filter(visible);
    const textarea = document.activeElement && document.activeElement.matches && document.activeElement.matches('textarea')
        ? document.activeElement
        : textareas[textareas.length - 1];
    if (!textarea) return {found: false, clicked: false, label: '', reason: 'no_textarea'};

    const matchesSendIntent = (el) => {
        const label = labelOf(el);
        return keywords.some((keyword) => label.includes(keyword)) || iconKeywords.some((keyword) => label.includes(keyword));
    };
    const buttonCandidates = (root) => Array.from(root.querySelectorAll('button, [role="button"]'))
        .filter((el) => visible(el) && enabled(el) && matchesSendIntent(el));

    let ancestor = textarea;
    let target = null;
    for (let depth = 0; depth < 8 && ancestor; depth += 1) {
        ancestor = ancestor.parentElement;
        if (!ancestor) break;
        const candidates = buttonCandidates(ancestor);
        if (!candidates.length) continue;
        const textareaRect = textarea.getBoundingClientRect();
        const nearby = candidates
            .map((el) => {
                const rect = el.getBoundingClientRect();
                const dx = Math.abs((rect.left + rect.width / 2) - (textareaRect.left + textareaRect.width / 2));
                const dy = Math.abs((rect.top + rect.height / 2) - (textareaRect.top + textareaRect.height / 2));
                return {el, score: dy * 3 + dx};
            })
            .filter(({el}) => {
                const rect = el.getBoundingClientRect();
                const textareaRect = textarea.getBoundingClientRect();
                return rect.bottom >= textareaRect.top - 160 && rect.top <= textareaRect.bottom + 160;
            })
            .sort((a, b) => a.score - b.score);
        if (nearby.length) {
            target = nearby[0].el;
            break;
        }
    }

    if (!target) return {found: false, clicked: false, label: ''};
    const label = labelOf(target);
    if (clickButton) target.click();
    return {found: true, clicked: !!clickButton, label};
}"""
TRANSPORT_HOOKS_JS = r"""(() => {
    const xhrOpenHookAlive = XMLHttpRequest.prototype.open.__api_transport_hooked === true;
    const xhrSendHookAlive = XMLHttpRequest.prototype.send.__api_transport_hooked === true;
    const fetchHookAlive = window.fetch.__api_transport_hooked === true;
    if (window.__api_transport_hooked && xhrOpenHookAlive && xhrSendHookAlive && fetchHookAlive) {
        return 'transport_already_hooked';
    }

    const isGenerateContentUrl = (url) => String(url || '').includes('GenerateContent') && !String(url || '').includes('CountTokens');

    const origOpen = XMLHttpRequest.prototype.open.__api_transport_original || XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send.__api_transport_original || XMLHttpRequest.prototype.send;
    const hookedOpen = function(method, url, ...args) {
        this.__url = String(url || '');
        this.__is_gen = isGenerateContentUrl(url);
        window.__last_hook_url = this.__url;
        return origOpen.call(this, method, url, ...args);
    };
    hookedOpen.__api_transport_hooked = true;
    hookedOpen.__api_transport_original = origOpen;
    hookedOpen.__api_hooked = true;
    XMLHttpRequest.prototype.open = hookedOpen;

    const hookedSend = function(body) {
        if (this.__is_gen && window.__pending_body) {
            const captured = window.__pending_body;
            window.__pending_body = null;
            window.__hooked = true;
            window.__last_hook_url = this.__url || '';
            window.__last_hook_transport = 'xhr';
            return origSend.call(this, captured);
        }
        return origSend.call(this, body);
    };
    hookedSend.__api_transport_hooked = true;
    hookedSend.__api_transport_original = origSend;
    XMLHttpRequest.prototype.send = hookedSend;

    const origFetch = window.fetch.__api_transport_original || window.fetch;
    const hookedFetch = function(input, init) {
        let url = typeof input === 'string' ? input : (input instanceof Request ? input.url : String(input));
        if (isGenerateContentUrl(url) && window.__pending_body) {
            const captured = window.__pending_body;
            window.__pending_body = null;
            window.__hooked = true;
            window.__last_hook_url = url;
            window.__last_hook_transport = 'fetch';
            if (init) {
                init = Object.assign({}, init, {body: captured});
            } else if (input instanceof Request) {
                input = new Request(input, {body: captured});
            } else {
                init = {body: captured};
            }
            return origFetch.call(this, input, init);
        }
        return origFetch.call(this, input, init);
    };
    hookedFetch.__api_transport_hooked = true;
    hookedFetch.__api_transport_original = origFetch;
    hookedFetch.__api_hooked = true;
    window.fetch = hookedFetch;

    window.__api_transport_hooked = true;
    return 'transport_hooked';
})()"""
INSTALL_TRANSPORT_HOOKS_JS = "mw:" + TRANSPORT_HOOKS_JS

INSTALL_HOOKS_JS = r"""
mw:((() => {
    if (window.__bg_hooked && window.__snap_key) return 'already_hooked';
    if (window.__bg_hooked && !window.__snap_key) window.__bg_hooked = false;

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

    window.__bg_hooked = true;
    window.__snap_key = snapKey;
    return 'hooked:' + snapKey;
})())
"""

BROWSER_FETCH_REPLAY_JS = r"""
mw:(async ({url, headers, body, timeoutMs}) => {
    window.__api_browser_fetch_replay = {url, headers, body};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.max(1000, Number(timeoutMs) || 30000));
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: headers || {},
            body,
            credentials: 'include',
            mode: 'cors',
            signal: controller.signal,
        });
        const text = await response.text();
        const responseHeaders = {};
        try { response.headers.forEach((value, key) => { responseHeaders[key] = value; }); } catch(e) {}
        return {status: response.status, body: text, url: response.url, headers: responseHeaders};
    } catch (error) {
        return {error: String((error && (error.stack || error.message)) || error)};
    } finally {
        clearTimeout(timer);
    }
})
"""

FORBIDDEN_BROWSER_FETCH_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "cookie",
    "date",
    "dnt",
    "expect",
    "host",
    "keep-alive",
    "origin",
    "permissions-policy",
    "proxy-authorization",
    "referer",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "user-agent",
    "via",
}

FORBIDDEN_CONTEXT_REQUEST_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

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

AI_STUDIO_OPEN_MODEL_PICKER_JS = r"""(model) => {
    const targetModel = String(model || '').replace(/^models\//, '').toLowerCase();
    const textOf = (el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const normalize = (value) => String(value || '').toLowerCase().replace(/^models\//, '').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
    const targetLabel = normalize(targetModel);
    const targetTokens = targetLabel.split(' ').filter(Boolean);
    const modelValueOf = (el) => normalize([
        el.getAttribute?.('data-value'),
        el.getAttribute?.('data-model'),
        el.getAttribute?.('aria-label'),
        el.getAttribute?.('title'),
        textOf(el),
    ].filter(Boolean).join(' '));
    const matchesTarget = (el) => {
        if (!targetLabel) return false;
        const value = modelValueOf(el);
        if (!value) return false;
        if (value.includes(targetModel) || value.includes(targetLabel)) return true;
        if (targetModel === 'gemini-3.5-flash') return value.includes('gemini') && value.includes('3.5') && value.includes('flash');
        return targetTokens.length > 0 && targetTokens.every((token) => value.includes(token));
    };
    const isSendControl = (label) => /\b(run|send|generate)\b|运行|发送|生成/.test(label.toLowerCase());
    const isInstructionControl = (label) => /system instructions|optional tone|style instructions|create new instruction|instruction\.*/i.test(label);
    const classOf = (el) => String(el.className || '');
    const isGenericNavigation = (el, label) => {
        const lower = label.toLowerCase();
        const classes = classOf(el);
        if (/\b(category-card|toggle-button)\b/.test(classes)) return true;
        if (el.getAttribute?.('role') === 'radio') return true;
        if (/^(models|agents)$/.test(lower)) return true;
        return /featured test out|code and chat build|image generation create|video generation generate|speech and music|real-time/.test(lower);
    };
    const isTextModelCategory = (label) => /code and chat|build chatbots|chatbots, agents|text generation|chat models|文本|聊天/i.test(label);
    const hasRunSettingsAncestor = (el) => {
        let node = el;
        for (let depth = 0; node && node !== document.body && depth < 8; depth += 1) {
            const label = textOf(node).toLowerCase();
            if (label.includes('run settings') || label.includes('thinking level') || label.includes('system instructions')) return true;
            node = node.parentElement;
        }
        return false;
    };
    const rightSide = (el) => {
        const rect = el.getBoundingClientRect();
        return rect.left >= Math.max(0, window.innerWidth * 0.50);
    };
    const modelish = (label) => /\b(?:models\/)?(?:gemini|gemma|deep-research|learnlm)-[a-z0-9][a-z0-9._-]*\b/i.test(label)
        || /\b(gemini|gemma|deep research|learnlm|nano banana|chat spark playground|spark playground)\b/i.test(label);
    const pickerControlSelector = [
        'mat-select',
        '[role="combobox"]',
        'button',
        '[role="button"]',
        '[aria-haspopup]',
        '[aria-expanded]',
        '[tabindex]',
        '.selected',
        '.active',
        '.model-selector',
        '.model-selector-trigger',
        '.model-selector-button',
        '.model-picker',
        '.model-select',
        '.model-display',
        '.mat-mdc-select',
        '.mat-mdc-menu-trigger',
    ].join(', ');
    const isModelSelectorCard = (candidate) => candidate.matches?.('.model-selector-card') || candidate.closest?.('.model-selector-card');
    const nearestPickerControl = (candidate) => {
        let fallback = null;
        let node = candidate;
        for (let depth = 0; node && node !== document.body && depth < 10; depth += 1) {
            if (node.matches?.(pickerControlSelector)) return node;
            const label = textOf(node);
            const pointer = (() => {
                try { return window.getComputedStyle(node).cursor === 'pointer'; } catch (e) { return false; }
            })();
            const clickable = pointer || node.matches?.('button, [role="button"], [role="option"], mat-option, mat-card, [tabindex], [data-value], [data-model]');
            if (!fallback && clickable && label && label.length < 320 && !isSendControl(label) && !isInstructionControl(label) && !isGenericNavigation(node, label)) {
                fallback = node;
            }
            node = node.parentElement;
        }
        if (fallback) return fallback;
        return (hasRunSettingsAncestor(candidate) || rightSide(candidate)) ? candidate : null;
    };
    const clickCandidate = (candidate, type) => {
        const label = textOf(candidate);
        if (!visible(candidate) || candidate.disabled || candidate.getAttribute('aria-disabled') === 'true') return null;
        if (!label || isSendControl(label) || isInstructionControl(label) || isGenericNavigation(candidate, label)) return null;
        try { candidate.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
        candidate.click();
        return {opened: true, type, label: label.slice(0, 160)};
    };
    const clickTextCategory = (candidate) => {
        const label = textOf(candidate);
        if (!visible(candidate) || candidate.disabled || candidate.getAttribute('aria-disabled') === 'true') return null;
        if (!label || !isTextModelCategory(label)) return null;
        try { candidate.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
        candidate.click();
        return {opened: true, type: 'text_category', label: label.slice(0, 160)};
    };
    const dedupe = (values) => Array.from(new Set(values.filter(Boolean)));
    const pickerControls = dedupe(Array.from(document.querySelectorAll(pickerControlSelector)));
    const currentModelControls = dedupe(
        Array.from(document.querySelectorAll('body *'))
            .filter((node) => {
                const label = textOf(node);
                return label.length > 0 && label.length < 240 && modelish(label) && !isInstructionControl(label)
                    && !isGenericNavigation(node, label) && (hasRunSettingsAncestor(node) || rightSide(node));
            })
            .map(nearestPickerControl)
    );
    for (const candidate of currentModelControls) {
        if (isModelSelectorCard(candidate)) continue;
        const label = textOf(candidate);
        if (!modelish(label)) continue;
        const result = clickCandidate(candidate, 'picker_control');
        if (result) return result;
    }
    for (const candidate of pickerControls) {
        if (isModelSelectorCard(candidate)) continue;
        const label = textOf(candidate);
        if (!modelish(label)) continue;
        const result = clickCandidate(candidate, 'picker_control');
        if (result) return result;
    }
    const modelTextControls = dedupe(
        Array.from(document.querySelectorAll('body *'))
            .filter((node) => {
                const label = textOf(node);
                return label.length > 0 && label.length < 240 && modelish(label) && !isInstructionControl(label);
            })
            .map(nearestPickerControl)
    );
    for (const candidate of modelTextControls) {
        if (isModelSelectorCard(candidate)) continue;
        const label = textOf(candidate);
        if (!modelish(label)) continue;
        const result = clickCandidate(candidate, 'picker_control');
        if (result) return result;
    }
    if (targetLabel) {
        for (const candidate of Array.from(document.querySelectorAll('button.model-selector-card, .model-selector-card, [data-model], [data-value]'))) {
            if (!matchesTarget(candidate)) continue;
            const result = clickCandidate(candidate, 'target_card');
            if (result) return result;
        }
    }
    const textCategorySelectors = 'button, [role="button"], .category-card, mat-card, [tabindex]';
    for (const candidate of Array.from(document.querySelectorAll(textCategorySelectors))) {
        const result = clickTextCategory(candidate);
        if (result) return result;
    }
    for (const candidate of pickerControls) {
        if (isModelSelectorCard(candidate)) continue;
        const label = textOf(candidate);
        if (!modelish(label)) continue;
        const result = clickCandidate(candidate, 'picker_control');
        if (result) return result;
    }
    return {opened: false, reason: 'model_picker_not_found'};
}"""

AI_STUDIO_CURRENT_TEXT_MODEL_JS = r"""(model) => {
    const targetModel = String(model || '').replace(/^models\//, '').toLowerCase();
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
    const normalize = (value) => String(value || '').toLowerCase().replace(/^models\//, '').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
    const targetLabel = normalize(targetModel);
    const targetTokens = targetLabel.split(' ').filter(Boolean);
    const modelValueOf = (el) => normalize([
        el.getAttribute?.('data-value'),
        el.getAttribute?.('data-model'),
        el.getAttribute?.('aria-label'),
        el.getAttribute?.('title'),
        textOf(el),
    ].filter(Boolean).join(' '));
    const matchesTargetValue = (value) => {
        if (!targetLabel || !value) return false;
        if (value.includes(targetModel) || value.includes(targetLabel)) return true;
        if (targetModel === 'gemini-3.5-flash') return value.includes('gemini') && value.includes('3.5') && value.includes('flash');
        if (targetModel === 'gemini-3-flash-preview') return value.includes('gemini') && value.includes('3') && value.includes('flash') && value.includes('preview');
        return targetTokens.length > 0 && targetTokens.every((token) => value.includes(token));
    };
    const modelish = (label) => /\b(?:models\/)?(?:gemini|gemma|deep-research|learnlm)-[a-z0-9][a-z0-9._-]*\b/i.test(label)
        || /\b(gemini|gemma|deep research|learnlm|chat spark playground|spark playground)\b/i.test(label);
    const badPickerText = (label) => /featured spark|code and chat build|image generation create|video generation generate|speech and music|model selector|models agents/i.test(label);
    const hasRunSettingsAncestor = (el) => {
        let node = el;
        for (let depth = 0; node && node !== document.body && depth < 6; depth += 1) {
            const label = textOf(node).toLowerCase();
            if (label.includes('run settings') || label.includes('thinking level') || label.includes('system instructions')) return true;
            node = node.parentElement;
        }
        return false;
    };
    const rightSide = (el) => {
        const rect = el.getBoundingClientRect();
        return rect.left >= Math.max(0, window.innerWidth * 0.55);
    };
    const isPickerOverlayCard = (el, label) => {
        if (badPickerText(label)) return true;
        const card = el.closest?.('.model-selector-card, .category-card');
        if (!card) return false;
        return !hasRunSettingsAncestor(card) && !rightSide(card);
    };
    const candidates = [];
    const seen = new Set();
    for (const node of Array.from(document.querySelectorAll('mat-select, [role="combobox"], button, [role="button"], [aria-haspopup], [aria-expanded], .model-selector-card, .model-display, .model-selector, .mat-mdc-select, body *'))) {
        if (!visible(node)) continue;
        const label = textOf(node);
        if (!label || label.length > 280 || !modelish(label)) continue;
        if (isPickerOverlayCard(node, label)) continue;
        const runSettings = hasRunSettingsAncestor(node);
        const onRight = rightSide(node);
        const controlLike = Boolean(node.matches?.('mat-select, [role="combobox"], button, [role="button"], [aria-haspopup], [aria-expanded], .model-display, .model-selector, .mat-mdc-select'));
        if (!runSettings && !onRight && !controlLike) continue;
        const normalized = modelValueOf(node);
        if (!normalized || seen.has(normalized)) continue;
        seen.add(normalized);
        const priority = (runSettings ? 0 : 10) + (onRight ? 0 : 5) + (controlLike ? 0 : 2) + Math.min(20, Math.floor(label.length / 60));
        candidates.push({label: label.slice(0, 180), normalized: normalized.slice(0, 180), matches: matchesTargetValue(normalized), priority});
    }
    candidates.sort((left, right) => left.priority - right.priority || left.label.length - right.label.length);
    const best = candidates[0];
    if (!best) {
        const visibleModelLabels = Array.from(document.querySelectorAll('body *'))
            .filter((node) => visible(node))
            .map(textOf)
            .filter((label) => label && label.length <= 180 && modelish(label) && !badPickerText(label))
            .slice(0, 20);
        return {matches: false, reason: 'current_text_model_not_found', target_model: targetModel, visible: visibleModelLabels};
    }
    return {matches: Boolean(best.matches), target_model: targetModel, label: best.label, normalized: best.normalized, candidates: candidates.slice(0, 10)};
}"""

AI_STUDIO_SELECT_TEXT_MODEL_JS = r"""(model) => {
    const targetModel = String(model || '').replace(/^models\//, '').toLowerCase();
    if (!targetModel || targetModel.includes('image')) return {selected: false, reason: 'not_text_model'};

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
    const normalize = (value) => String(value || '').toLowerCase().replace(/^models\//, '').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
    const targetLabel = normalize(targetModel);
    const targetTokens = targetLabel.split(' ').filter(Boolean);
    const selectedNodes = () => Array.from(document.querySelectorAll('[aria-selected="true"], .mat-mdc-option-active, [role="combobox"], mat-select'))
        .filter((node) => !node.closest?.('.model-selector-card, .category-card'));
    const selectedText = () => selectedNodes().filter(visible).map((node) => normalize(textOf(node))).join(' ');
    const modelValueOf = (el) => normalize([
        el.getAttribute?.('data-value'),
        el.getAttribute?.('data-model'),
        el.getAttribute?.('aria-label'),
        el.getAttribute?.('title'),
        textOf(el),
    ].filter(Boolean).join(' '));
    const matchesTarget = (el) => {
        const value = modelValueOf(el);
        if (!value) return false;
        if (value.includes(targetModel) || value.includes(targetLabel)) return true;
        if (targetModel === 'gemini-3.5-flash') return value.includes('gemini') && value.includes('3.5') && value.includes('flash');
        return targetTokens.length > 0 && targetTokens.every((token) => value.includes(token));
    };

    const selected = selectedText();
    if (selected.includes(targetModel) || selected.includes(targetLabel)) {
        return {selected: false, reason: 'already_selected', label: selected.slice(0, 160)};
    }

    const choiceSelector = 'button, [role="button"], [role="option"], mat-option, mat-card, [tabindex], [data-value], [data-model], .model-selector-card, .model-card, [class*="model"]';
    const isGenericModelNavigation = (label) => /featured spark|code and chat build|build chatbots, agents|image generation create|video generation generate|speech and music|models agents|model selector/i.test(label);
    const nearestModelChoice = (candidate) => {
        let node = candidate;
        for (let depth = 0; node && node !== document.body && depth < 8; depth += 1) {
            if (node.matches?.(choiceSelector)) return node;
            node = node.parentElement;
        }
        return candidate;
    };
    let candidates = Array.from(document.querySelectorAll(choiceSelector));
    const targetTextChoices = Array.from(document.querySelectorAll('body *'))
        .filter((node) => {
            const label = textOf(node);
            return label.length > 0 && label.length < 360 && matchesTarget(node) && !isGenericModelNavigation(label);
        })
        .map(nearestModelChoice);
    candidates = Array.from(new Set(candidates.concat(targetTextChoices).filter(Boolean)));
    const visibleLabels = () => candidates.filter(visible).map(textOf).filter(Boolean).slice(0, 24);
    for (const candidate of candidates) {
        if (!visible(candidate) || !matchesTarget(candidate)) continue;
        const label = textOf(candidate);
        if (!label || label.length > 520 || isGenericModelNavigation(label)) continue;
        try { candidate.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
        candidate.click();
        return {selected: true, label: label.slice(0, 160), visible: visibleLabels()};
    }
    return {selected: false, reason: 'text_model_not_found', current: selected.slice(0, 160), visible: visibleLabels()};
}"""


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
        self._native_worker_executor = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(settings, "native_ui_workers_per_account", 3) or 1)),
            thread_name_prefix="aistudio-native-ui",
        )
        self._botguard_lock = asyncio.Lock()
        self._snapshot_lock = asyncio.Lock()
        self._native_worker_pool_lock = threading.Lock()
        self._native_worker_pool: NativeUiWorkerPool | None = None
        self._preferred_chat_url: str | None = None
        self._last_requested_chat_url: str | None = None
        self._failed_chat_urls: set[str] = set()

    @property
    def has_account_auth(self) -> bool:
        return bool(self._auth_file)

    async def ensure_context(
        self,
        *,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
    ):
        if navigation_timeout_ms is None and chat_ready_timeout_ms is None:
            return await self._run_sync(self._ensure_browser_sync)
        return await self._run_sync(self._ensure_browser_sync, navigation_timeout_ms, chat_ready_timeout_ms)

    async def switch_auth(self, auth_file: str | None) -> None:
        await self._run_sync(self._switch_auth_sync, auth_file)

    async def close(self) -> None:
        await self._run_sync(self._close_all_sync)
        self._native_worker_executor.shutdown(wait=False, cancel_futures=True)

    def clear_templates(self) -> None:
        self._templates.clear()

    async def advance_chat_route_after_auth_failure(self) -> bool:
        return await self._run_sync(self._advance_chat_route_after_auth_failure_sync)

    async def detect_tier_for_auth_file(self, auth_file: str, timeout_ms: int = 30000):
        return await self._run_sync(self._detect_tier_for_auth_file_sync, auth_file, timeout_ms)

    async def ensure_hook_page(self):
        await self._run_sync(self._ensure_hook_page_sync)
        return True

    async def ensure_botguard_service(self):
        await self._run_sync(self._ensure_botguard_service_sync)
        return True

    async def capture_template(
        self,
        model: str,
        *,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
        botguard_timeout_ms: int | None = None,
        template_capture_timeout_ms: int | None = None,
        template_recovery_attempts: int | None = None,
    ) -> dict[str, Any]:
        if (
            navigation_timeout_ms is None
            and chat_ready_timeout_ms is None
            and botguard_timeout_ms is None
            and template_capture_timeout_ms is None
            and template_recovery_attempts is None
        ):
            return await self._run_sync(self._capture_template_sync, model)
        return await self._run_sync(
            self._capture_template_sync,
            model,
            navigation_timeout_ms,
            chat_ready_timeout_ms,
            botguard_timeout_ms,
            template_capture_timeout_ms,
            template_recovery_attempts,
        )

    async def list_available_models(self) -> list[str]:
        return await self._run_sync(self._list_available_models_sync)

    async def upload_images(self, image_paths: list[str]) -> list[str]:
        return await self._run_sync(self._upload_images_sync, image_paths)

    async def generate_snapshot(self, contents: list[AistudioContent]) -> str:
        loop = asyncio.get_running_loop()
        async with self._snapshot_lock:
            return await loop.run_in_executor(self._executor, lambda: self._generate_snapshot_sync(contents))

    async def send_hooked_request(self, *, body: str, url: str, headers: dict[str, str], timeout_ms: int) -> tuple[int, bytes]:
        if self._auth_file:
            try:
                return await self._send_account_native_generate_content_body_async(body=body, timeout_ms=timeout_ms, retry_statuses=(401, 403))
            except _NativeUiReplayUnsupported as exc:
                log.info("AI Studio native UI worker replay unsupported before browser replay: %s", exc)
            except RequestError:
                raise
            except NativeUiWorkerError as exc:
                raise _native_worker_unavailable_error(exc) from exc
            except Exception as exc:
                raise _native_worker_unavailable_error(exc) from exc
            return await self._run_sync(self._send_hooked_request_sync, body, url, headers, timeout_ms, False)
        return await self._run_sync(self._send_hooked_request_sync, body, url, headers, timeout_ms)

    async def send_account_native_generate_content_body(
        self,
        *,
        body: str,
        timeout_ms: int,
        max_attempts: int | None = None,
        retry_statuses: tuple[int, ...] | None = None,
    ) -> tuple[int, bytes]:
        try:
            return await self._send_account_native_generate_content_body_async(
                body=body,
                timeout_ms=timeout_ms,
                max_attempts=max_attempts,
                retry_statuses=retry_statuses,
            )
        except RequestError:
            raise
        except NativeUiWorkerError as exc:
            raise _native_worker_unavailable_error(exc) from exc
        except Exception as exc:
            raise _native_worker_unavailable_error(exc) from exc

    async def probe_native_generate_content(self, *, model: str, timeout_ms: int) -> tuple[int, bytes, str]:
        return await self._run_sync(self._probe_native_generate_content_sync, model, timeout_ms)

    async def probe_native_worker_generate_content(self, *, model: str, timeout_ms: int) -> tuple[int, bytes, str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._native_worker_executor,
            lambda: self._probe_native_worker_generate_content_sync(model=model, timeout_ms=timeout_ms),
        )

    async def send_streaming_request(self, *, body: str, url: str, headers: dict[str, str], timeout_ms: int):
        """Send a streaming request, yielding ("status", int) and ("chunk", bytes) events."""
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancel_event = threading.Event()

        if self._auth_file:
            try:
                status, raw = await self._send_account_native_generate_content_body_async(body=body, timeout_ms=timeout_ms, retry_statuses=(401, 403))
            except _NativeUiReplayUnsupported as exc:
                log.info("AI Studio native UI worker replay unsupported before streaming browser replay: %s", exc)
            except RequestError:
                raise
            except NativeUiWorkerError as exc:
                raise _native_worker_unavailable_error(exc) from exc
            except Exception as exc:
                raise _native_worker_unavailable_error(exc) from exc
            else:
                yield "status", status
                if raw:
                    yield "chunk", raw
                return

        def _stream_worker():
            try:
                log.debug("[stream] worker started")
                self._send_streaming_request_sync(
                    body,
                    url,
                    headers,
                    timeout_ms,
                    queue,
                    loop,
                    cancel_event,
                    try_native=not bool(self._auth_file),
                )
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

    async def _send_account_native_generate_content_body_async(
        self,
        *,
        body: str,
        timeout_ms: int,
        max_attempts: int | None = None,
        retry_statuses: tuple[int, ...] | None = None,
    ) -> tuple[int, bytes]:
        if not self._auth_file:
            raise RuntimeError("native UI worker replay requires account auth file")
        try:
            model, prompt = self._native_text_replay_payload_from_body(body)
        except Exception as exc:
            raise _NativeUiReplayUnsupported(str(exc)) from exc
        loop = asyncio.get_running_loop()
        if max_attempts is None:
            return await loop.run_in_executor(
                self._native_worker_executor,
                lambda: self._send_native_generate_content_worker_pool_sync(
                    model=model,
                    prompt=prompt,
                    timeout_ms=timeout_ms,
                    retry_statuses=retry_statuses,
                ),
            )
        return await loop.run_in_executor(
            self._native_worker_executor,
            lambda: self._send_native_generate_content_worker_pool_sync(
                model=model,
                prompt=prompt,
                timeout_ms=timeout_ms,
                max_attempts=max_attempts,
                retry_statuses=retry_statuses,
            ),
        )

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
        try_native: bool = True,
    ):
        """Sync method: triggers AI Studio's native request and forwards the response."""
        import time as _t
        _t0 = _t.time()

        try:
            if cancel_event.is_set():
                return
            status, raw = self._send_generate_content_with_fallback_sync(
                body=body,
                url=url,
                headers=headers,
                timeout_ms=timeout_ms,
                try_native=try_native,
            )
            if cancel_event.is_set():
                return
            log.debug("[stream] browser fetch replay done in %.1fs, status=%s", _t.time() - _t0, status)
            loop.call_soon_threadsafe(queue.put_nowait, ("status", status))
            if raw:
                loop.call_soon_threadsafe(queue.put_nowait, ("chunk", raw))
        except Exception as exc:
            log.debug("[stream] native replay error after %.1fs: %s", _t.time() - _t0, exc)
            loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    def _trigger_native_generate_content_sync(self, *, body: str, timeout_ms: int) -> tuple[int, bytes]:
        import time as _t

        page = self._ensure_hook_page_sync()
        self._install_hooks_sync(page)
        page.evaluate("mw:(body => { window.__pending_body = body; window.__hooked = false; window.__last_hook_url = ''; })", body)

        response_holder: dict[str, Any] = {}
        observed_responses: list[str] = []

        def hook_state() -> dict[str, Any]:
            try:
                state = page.evaluate(
                    "mw:(() => ({hooked: !!window.__hooked, transport: !!window.__api_transport_hooked, lastUrl: window.__last_hook_url || '', lastTransport: window.__last_hook_transport || ''}))()"
                )
            except Exception:
                return {"hooked": False, "transport": False, "lastUrl": "", "lastTransport": ""}
            return state if isinstance(state, dict) else {"hooked": False, "transport": False, "lastUrl": "", "lastTransport": ""}

        def hook_state_suffix() -> str:
            state = hook_state()
            return (
                f"hooked={bool(state.get('hooked'))}, "
                f"transport={bool(state.get('transport'))}, "
                f"last_url={state.get('lastUrl') or ''}, "
                f"last_transport={state.get('lastTransport') or ''}"
            )

        def observed_suffix() -> str:
            parts: list[str] = []
            if observed_responses:
                parts.append(f"responses={observed_responses}")
            return f"; {'; '.join(parts)}" if parts else ""

        def on_response(response):
            if response_holder:
                return
            response_url = getattr(response, "url", "") or ""
            if "CountTokens" in response_url or "PerUserQuota" in response_url:
                return
            response_request = getattr(response, "request", None)
            response_request_body = getattr(response_request, "post_data", None)
            if "GenerateContent" not in response_url and response_request_body != body:
                return
            try:
                status = int(response.status)
                raw = response.body()
            except Exception as exc:
                response_holder["error"] = exc
                return
            raw_len = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw or b"")
            if (status == 204 or raw_len == 0) and len(observed_responses) < 5:
                observed_responses.append(f"{response_url} status={status} body={raw_len}")
                return
            if _is_per_user_quota_ambiguous_response(status, raw):
                if len(observed_responses) < 5:
                    observed_responses.append(f"{response_url} status={status} per_user_quota_ambiguous=true")
                return
            response_holder["status"] = status
            response_holder["body"] = raw

        page.on("response", on_response)
        try:
            page.evaluate(DIALOG_CLEANUP_JS)
            textarea = page.query_selector("textarea")
            if textarea is None:
                raise RuntimeError("textarea not found during native replay")
            self._fill_prompt_text_sync(page, textarea, "1")
            page.wait_for_timeout(300)
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger native GenerateContent request")

            deadline = _t.time() + max(1.0, timeout_ms / 1000)
            while _t.time() < deadline:
                if response_holder:
                    break
                page.wait_for_timeout(100)
            if not response_holder:
                raise RuntimeError(f"native GenerateContent replay timeout; {hook_state_suffix()}{observed_suffix()}")
            if "error" in response_holder:
                raise RuntimeError(f"native GenerateContent replay failed: {response_holder['error']}")
            state = hook_state()
            if not bool(state.get("hooked")):
                raise RuntimeError(f"native GenerateContent replay completed without body hook; {hook_state_suffix()}{observed_suffix()}")
            status = int(response_holder.get("status") or 0)
            raw = response_holder.get("body") or b""
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            return status, raw
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
            try:
                page.evaluate("mw:(() => { window.__pending_body = null; })()")
            except Exception:
                pass

    def _send_generate_content_with_fallback_sync(
        self,
        *,
        body: str,
        url: str,
        headers: dict[str, str],
        timeout_ms: int,
        try_native: bool = True,
    ) -> tuple[int, bytes]:
        if try_native:
            try:
                native_status, native_raw = self._send_native_generate_content_body_sync(body=body, timeout_ms=timeout_ms)
            except _NativeUiReplayUnsupported as exc:
                log.info("AI Studio native UI replay unsupported before browser replay: %s", exc)
            except RequestError:
                raise
            except NativeUiWorkerError as exc:
                if self._auth_file:
                    raise _native_worker_unavailable_error(exc) from exc
                log.info("AI Studio native UI replay unavailable before browser replay: %s", exc)
            except Exception as exc:
                if self._auth_file:
                    raise _native_worker_unavailable_error(exc) from exc
                log.info("AI Studio native UI replay unavailable before browser replay: %s", exc)
            else:
                log.info("AI Studio native UI replay completed before browser replay: native_status=%s", native_status)
                return native_status, native_raw

        status, raw = self._browser_fetch_generate_content_sync(
            body=body,
            url=url,
            headers=headers,
            timeout_ms=timeout_ms,
        )
        return status, raw

    def _browser_fetch_generate_content_sync(self, *, body: str, url: str, headers: dict[str, str], timeout_ms: int) -> tuple[int, bytes]:
        page = self._ensure_hook_page_sync()
        self._install_hooks_sync(page)

        context_request = getattr(self._ctx, "request", None) if self._ctx is not None else None
        if context_request is not None:
            replay_headers = self._context_request_headers(headers)
            response = context_request.post(url, data=body, headers=replay_headers, timeout=timeout_ms)
            status = int(getattr(response, "status", 0) or 0)
            raw = response.body()
            return status, raw

        replay_headers = self._browser_fetch_headers(headers)
        result = page.evaluate(
            BROWSER_FETCH_REPLAY_JS,
            {"url": url, "headers": replay_headers, "body": body, "timeoutMs": timeout_ms},
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"browser fetch replay returned unexpected result: {result!r}")
        error = result.get("error")
        if error:
            raise RuntimeError(f"browser fetch replay failed: {error}")
        status = int(result.get("status") or 0)
        raw = result.get("body") or b""
        if isinstance(raw, bytes):
            return status, raw
        return status, str(raw).encode("utf-8")

    def _browser_fetch_headers(self, headers: dict[str, str]) -> dict[str, str]:
        replay_headers: dict[str, str] = {}
        for name, value in (headers or {}).items():
            header_name = str(name)
            lower_name = header_name.lower()
            if lower_name.startswith("proxy-") or lower_name.startswith("sec-"):
                continue
            if lower_name in FORBIDDEN_BROWSER_FETCH_HEADERS:
                continue
            replay_headers[header_name] = str(value)
        return replay_headers

    def _context_request_headers(self, headers: dict[str, str]) -> dict[str, str]:
        replay_headers: dict[str, str] = {}
        for name, value in (headers or {}).items():
            header_name = str(name)
            lower_name = header_name.lower()
            if lower_name in FORBIDDEN_CONTEXT_REQUEST_HEADERS:
                continue
            replay_headers[header_name] = str(value)
        return replay_headers

    def _send_native_generate_content_body_sync(self, *, body: str, timeout_ms: int) -> tuple[int, bytes]:
        try:
            model, prompt = self._native_text_replay_payload_from_body(body)
        except Exception as exc:
            raise _NativeUiReplayUnsupported(str(exc)) from exc
        if self._auth_file:
            return self._send_native_generate_content_worker_pool_sync(model=model, prompt=prompt, timeout_ms=timeout_ms)
        return self._send_native_generate_content_prompt_sync(model=model, prompt=prompt, timeout_ms=timeout_ms)

    def _send_native_generate_content_worker_pool_sync(
        self,
        *,
        model: str,
        prompt: str,
        timeout_ms: int,
        max_attempts: int | None = None,
        retry_statuses: tuple[int, ...] | None = None,
    ) -> tuple[int, bytes]:
        pool = self._native_worker_pool_sync()
        if max_attempts is None:
            return pool.send(model=model, prompt=prompt, timeout_ms=timeout_ms, retry_statuses=retry_statuses)
        return pool.send(model=model, prompt=prompt, timeout_ms=timeout_ms, max_attempts=max_attempts, retry_statuses=retry_statuses)

    def _probe_native_worker_generate_content_sync(self, *, model: str, timeout_ms: int) -> tuple[int, bytes, str]:
        pool = self._native_worker_pool_sync()
        last_status = 0
        last_raw = b""
        last_wire_model = ""
        required_successes = max(1, pool.worker_count)
        max_probe_attempts = required_successes * 2
        successful_worker_indexes: set[object] = set()
        last_recoverable_error: NativeUiWorkerRequestError | None = None
        for _ in range(max_probe_attempts):
            try:
                status, raw, metadata = pool.send_with_metadata(
                    model=model,
                    prompt="1",
                    timeout_ms=timeout_ms,
                    max_attempts=1,
                    retry_statuses=(401, 403),
                    prefer_recent_worker=False,
                )
            except NativeUiWorkerRequestError as exc:
                if _native_worker_warmup_error_is_recoverable(exc):
                    last_recoverable_error = exc
                    log.warning("Native UI worker warmup probe recovered by trying another worker: %s", exc)
                    continue
                raise
            last_status = status
            last_raw = raw
            last_wire_model = str(metadata.get("wire_model") or "")
            if status != 200:
                return status, raw, last_wire_model
            worker_index = metadata.get("worker_index")
            successful_worker_indexes.add(worker_index if worker_index is not None else len(successful_worker_indexes))
            if len(successful_worker_indexes) >= required_successes:
                return last_status, last_raw, last_wire_model
        if last_recoverable_error is not None:
            raise last_recoverable_error
        return last_status, last_raw, last_wire_model

    def _native_worker_pool_sync(self) -> NativeUiWorkerPool:
        auth_file = str(self._auth_file or "")
        if not auth_file:
            raise RuntimeError("native UI worker pool requires account auth file")
        worker_count = max(1, int(getattr(settings, "native_ui_workers_per_account", 3) or 1))
        with self._native_worker_pool_lock:
            pool = self._native_worker_pool
            if pool is not None and pool.auth_file == auth_file and pool.worker_count == worker_count:
                return pool
            if pool is not None:
                pool.close()
            self._native_worker_pool = NativeUiWorkerPool(auth_file=auth_file, worker_count=worker_count)
            return self._native_worker_pool

    def _close_native_worker_pool_sync(self) -> None:
        with self._native_worker_pool_lock:
            pool = self._native_worker_pool
            self._native_worker_pool = None
        if pool is not None:
            pool.close()

    def _native_text_replay_payload_from_body(self, body: str) -> tuple[str, str]:
        from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec

        request = AistudioWireCodec().decode(body)
        model = request.model or ""

        def content_text(content: AistudioContent) -> str:
            parts: list[str] = []
            for part in content.parts:
                if part.inline_data or part.file_id:
                    raise RuntimeError("native UI replay fallback only supports text-only requests")
                if part.text:
                    parts.append(str(part.text))
            return "\n".join(parts).strip()

        sections: list[str] = []
        if request.system_instruction is not None:
            system_text = content_text(request.system_instruction)
            if system_text:
                sections.append(f"System:\n{system_text}")

        for content in request.contents:
            text = content_text(content)
            if not text:
                continue
            role = str(content.role or "user").strip() or "user"
            if not sections and len(request.contents) == 1 and role == "user":
                sections.append(text)
            else:
                sections.append(f"{role.capitalize()}:\n{text}")

        prompt = "\n\n".join(sections).strip()
        if not prompt:
            raise RuntimeError("native UI replay fallback requires text prompt")
        return model, prompt

    def _send_native_generate_content_prompt_sync(self, *, model: str, prompt: str, timeout_ms: int) -> tuple[int, bytes]:
        import time as _t

        page, probe_context, close_probe_page = self._native_generate_content_probe_page_sync(fresh_chat_routes=True, model=model)
        response_holder: dict[str, Any] = {}
        observed_responses: list[str] = []
        target_model = str(model or "").strip().removeprefix("models/")
        prompt_marker = prompt.strip()[:80]

        def observed_suffix() -> str:
            parts: list[str] = []
            if observed_responses:
                parts.append(f"responses={observed_responses[:5]}")
            return f"; {'; '.join(parts)}" if parts else ""

        def wire_model_from_body(body: str | None) -> str:
            if not body:
                return ""
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return ""
            if not isinstance(parsed, list) or not parsed:
                return ""
            wire_model = parsed[0]
            return str(wire_model or "") if isinstance(wire_model, str) else ""

        def body_contains_prompt(body: str | None) -> bool:
            if not body or not prompt_marker:
                return False
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return prompt_marker in body

            def walk(value: Any) -> bool:
                if isinstance(value, str):
                    return prompt_marker in value
                if isinstance(value, list):
                    return any(walk(item) for item in value)
                if isinstance(value, dict):
                    return any(walk(item) for item in value.values())
                return False

            return walk(parsed)

        def response_head(raw: bytes | str) -> str:
            if isinstance(raw, bytes):
                return raw[:220].decode("utf-8", errors="replace")
            return str(raw)[:220]

        def on_response(response):
            if response_holder:
                return
            response_url = getattr(response, "url", "") or ""
            if "CountTokens" in response_url or "PerUserQuota" in response_url:
                return
            try:
                request_body = response.request.post_data
            except Exception:
                request_body = None
            is_generate = "GenerateContent" in response_url or self._is_template_capture_request(
                url=response_url,
                body=request_body,
                model_marker="",
                allow_text_markers=True,
            )
            if not is_generate:
                return
            wire_model = wire_model_from_body(request_body)
            response_model = wire_model.removeprefix("models/") if wire_model else ""
            model_matches = bool(response_model and response_model == target_model)
            prompt_matches = body_contains_prompt(request_body)
            if not model_matches or not prompt_matches:
                if len(observed_responses) < 5:
                    observed_responses.append(
                        f"{response_url} model={wire_model or '<unknown>'} model_match={model_matches} prompt_match={prompt_matches}"
                    )
                return
            try:
                status = int(response.status)
                raw = response.body()
            except Exception as exc:
                response_holder["error"] = exc
                return
            raw_len = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw or b"")
            if (status == 204 or raw_len == 0) and len(observed_responses) < 5:
                observed_responses.append(f"{response_url} status={status} body={raw_len} model={wire_model}")
                return
            if _is_per_user_quota_ambiguous_response(status, raw):
                if len(observed_responses) < 5:
                    observed_responses.append(f"{response_url} status={status} per_user_quota_ambiguous=true model={wire_model}")
                return
            if status >= 400:
                log.info(
                    "AI Studio native UI replay fallback matched upstream error: status=%s, wire_model=%s, response_head=%s",
                    status,
                    wire_model,
                    response_head(raw),
                )
            response_holder["status"] = status
            response_holder["body"] = raw

        try:
            if not self._select_text_model_sync(page, model):
                raise ModelNotFoundError(f"AI Studio text model not selected during native UI replay fallback: {model}")
            page.wait_for_timeout(1300)
            try:
                page.evaluate("mw:(() => { window.__pending_body = null; window.__hooked = false; window.__last_hook_url = ''; })()")
            except Exception:
                pass

            page.on("response", on_response)
            page.evaluate(DIALOG_CLEANUP_JS)
            textarea = page.query_selector("textarea")
            if textarea is None:
                raise RuntimeError("textarea not found during native UI replay fallback")
            self._fill_prompt_text_sync(page, textarea, prompt)
            page.wait_for_timeout(500)
            try:
                page.evaluate(DIALOG_CLEANUP_JS)
            except Exception:
                pass
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger native UI replay fallback")

            deadline = _t.time() + max(1.0, timeout_ms / 1000)
            while _t.time() < deadline:
                if response_holder:
                    break
                page.wait_for_timeout(100)
            if not response_holder:
                raise RuntimeError(f"native UI replay fallback timeout{observed_suffix()}")
            if "error" in response_holder:
                raise RuntimeError(f"native UI replay fallback failed: {response_holder['error']}")
            status = int(response_holder.get("status") or 0)
            raw = response_holder.get("body") or b""
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            return status, raw
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
            try:
                page.evaluate("mw:(() => { window.__pending_body = null; })()")
            except Exception:
                pass
            if probe_context is not None:
                try:
                    probe_context.close()
                except Exception:
                    pass
            elif close_probe_page:
                try:
                    page.close()
                except Exception:
                    pass

    def _goto_native_probe_page_sync(self, page, *, fresh_chat_routes: bool = False, model: str | None = None) -> None:
        if not fresh_chat_routes:
            self._goto_aistudio_with_options_sync(page, model=model)
            return

        saved_preferred_chat_url = self._preferred_chat_url
        saved_last_requested_chat_url = self._last_requested_chat_url
        saved_failed_chat_urls = set(self._failed_chat_urls)
        selected_preferred_chat_url = None
        selected_last_requested_chat_url = None
        success = False
        self._preferred_chat_url = None
        self._last_requested_chat_url = None
        self._failed_chat_urls.clear()
        try:
            self._goto_aistudio_with_options_sync(page, model=model)
            selected_preferred_chat_url = self._preferred_chat_url
            selected_last_requested_chat_url = self._last_requested_chat_url
            success = True
        finally:
            self._failed_chat_urls.clear()
            self._failed_chat_urls.update(saved_failed_chat_urls)
            if success and selected_preferred_chat_url:
                self._preferred_chat_url = selected_preferred_chat_url
                self._last_requested_chat_url = selected_last_requested_chat_url
            else:
                self._preferred_chat_url = saved_preferred_chat_url
                self._last_requested_chat_url = saved_last_requested_chat_url

    def _native_generate_content_probe_page_sync(self, *, fresh_chat_routes: bool = False, model: str | None = None):
        probe_context = None
        if self._browser is None and self._ctx is None and self._hook_page is not None:
            return self._ensure_hook_page_sync(), None, False
        if self._browser is None and self._ctx is not None:
            page = self._ctx.new_page()
            try:
                log.info("AI Studio warmup native probe: opening clean chat page")
                self._goto_native_probe_page_sync(page, fresh_chat_routes=fresh_chat_routes, model=model)
                return page, None, True
            except Exception:
                try:
                    page.close()
                except Exception:
                    pass
                raise

        self._ensure_browser_process_sync()
        probe_context = self._new_context_sync(install_init_scripts=not fresh_chat_routes)
        page = probe_context.new_page()
        try:
            log.info("AI Studio warmup native probe: opening clean chat page in isolated context")
            self._goto_native_probe_page_sync(page, fresh_chat_routes=fresh_chat_routes, model=model)
            return page, probe_context, False
        except Exception:
            try:
                probe_context.close()
            except Exception:
                pass
            raise

    def _probe_native_generate_content_sync(self, model: str, timeout_ms: int) -> tuple[int, bytes, str]:
        import time as _t

        page, probe_context, close_probe_page = self._native_generate_content_probe_page_sync(model=model)
        response_holder: dict[str, Any] = {}
        observed_responses: list[str] = []

        def observed_suffix() -> str:
            parts: list[str] = []
            if observed_responses:
                parts.append(f"responses={observed_responses[:5]}")
            return f"; {'; '.join(parts)}" if parts else ""

        def wire_model_from_body(body: str | None) -> str:
            if not body:
                return ""
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return ""
            if not isinstance(parsed, list) or not parsed:
                return ""
            wire_model = parsed[0]
            return str(wire_model or "") if isinstance(wire_model, str) else ""

        def on_response(response):
            if response_holder:
                return
            response_url = getattr(response, "url", "") or ""
            if "CountTokens" in response_url:
                return
            try:
                request_body = response.request.post_data
            except Exception:
                request_body = None
            is_generate = "GenerateContent" in response_url or self._is_template_capture_request(
                url=response_url,
                body=request_body,
                model_marker="",
                allow_text_markers=True,
            )
            if not is_generate:
                return
            wire_model = wire_model_from_body(request_body)
            try:
                status = int(response.status)
                raw = response.body()
            except Exception as exc:
                response_holder["error"] = exc
                return
            raw_len = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw or b"")
            if (status == 204 or raw_len == 0) and len(observed_responses) < 5:
                observed_responses.append(f"{response_url} status={status} body={raw_len} model={wire_model or '<unknown>'}")
                return
            response_holder["status"] = status
            response_holder["body"] = raw
            response_holder["wire_model"] = wire_model

        try:
            if not self._select_text_model_sync(page, model):
                raise ModelNotFoundError(f"AI Studio text model not selected during native GenerateContent permission probe: {model}")
            page.wait_for_timeout(1300)
            try:
                page.evaluate("mw:(() => { window.__pending_body = null; window.__hooked = false; window.__last_hook_url = ''; })()")
            except Exception:
                pass

            page.on("response", on_response)
            page.evaluate(DIALOG_CLEANUP_JS)
            textarea = page.query_selector("textarea")
            if textarea is None:
                raise RuntimeError("textarea not found during native GenerateContent permission probe")
            textarea.fill("1")
            page.wait_for_timeout(500)
            try:
                page.evaluate(DIALOG_CLEANUP_JS)
            except Exception:
                pass
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger native GenerateContent permission probe")

            deadline = _t.time() + max(1.0, timeout_ms / 1000)
            while _t.time() < deadline:
                if response_holder:
                    break
                page.wait_for_timeout(100)
            if not response_holder:
                raise RuntimeError(f"native GenerateContent permission probe timeout{observed_suffix()}")
            if "error" in response_holder:
                raise RuntimeError(f"native GenerateContent permission probe failed: {response_holder['error']}")
            status = int(response_holder.get("status") or 0)
            raw = response_holder.get("body") or b""
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            wire_model = str(response_holder.get("wire_model") or "")
            return status, raw, wire_model
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
            try:
                page.evaluate("mw:(() => { window.__pending_body = null; })()")
            except Exception:
                pass
            if probe_context is not None:
                try:
                    probe_context.close()
                except Exception:
                    pass
            elif close_probe_page:
                try:
                    page.close()
                except Exception:
                    pass

    def _switch_auth_sync(self, auth_file: str | None) -> None:
        self._close_native_worker_pool_sync()
        self._auth_file = auth_file
        self._preferred_chat_url = None
        self._last_requested_chat_url = None
        self._failed_chat_urls.clear()
        self._templates.clear()
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

        log.info("AI Studio browser: launching Camoufox (proxy=%s, geoip=%s)", bool(settings.proxy_server), settings.camoufox_geoip)
        self._cf = Camoufox(**self._browser_options_sync())
        self._browser = self._cf.__enter__()
        log.info("AI Studio browser: Camoufox launched")
        return self._browser

    def _ensure_browser_sync(self, navigation_timeout_ms: int | None = None, chat_ready_timeout_ms: int | None = None):
        if self._ctx is not None and self._hook_page is not None and not self._hook_page.is_closed():
            return self._ctx

        import time as _t
        _t0 = _t.time()

        self._close_sync()
        self._ensure_browser_process_sync()
        log.info("AI Studio browser: creating context (auth_file=%s)", "set" if self._auth_file else "none")
        self._ctx = self._new_context_sync()
        self._hook_page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        log.debug(f"[timing] browser launched in {_t.time()-_t0:.1f}s")
        log.info("AI Studio browser: navigating to chat runtime")
        self._goto_aistudio_with_options_sync(
            self._hook_page,
            navigation_timeout_ms=navigation_timeout_ms,
            chat_ready_timeout_ms=chat_ready_timeout_ms,
        )
        log.debug(f"[timing] page loaded in {_t.time()-_t0:.1f}s")
        log.info("AI Studio browser: installing page hooks")
        self._install_hooks_sync(self._hook_page)
        log.debug(f"[timing] hooks installed in {_t.time()-_t0:.1f}s")
        log.info("AI Studio browser: context ready in %.1fs", _t.time() - _t0)
        return self._ctx

    def _new_context_for_browser_sync(self, browser, *, install_init_scripts: bool = True):
        def maybe_with_init_scripts(ctx):
            return self._with_context_init_scripts_sync(ctx) if install_init_scripts else ctx

        context_options = {"service_workers": "block"}
        if self._auth_file:
            auth_path = Path(self._auth_file)
            if not auth_path.exists():
                raise FileNotFoundError(
                    f"Browser auth state file is missing: {auth_path}. "
                    "Activate an account or complete login again before browser preheat/capture."
                )
            try:
                return maybe_with_init_scripts(
                    browser.new_context(storage_state=self._auth_file, **context_options)
                )
            except Exception:
                ctx = browser.new_context(**context_options)
                try:
                    self._apply_storage_state_sync(ctx, self._auth_file)
                    return maybe_with_init_scripts(ctx)
                except Exception as fallback_exc:
                    raise RuntimeError(
                        f"Browser auth state file is invalid: {auth_path}. "
                        "Activate an account or complete login again before browser preheat/capture."
                    ) from fallback_exc
        return maybe_with_init_scripts(browser.new_context(**context_options))

    def _new_context_sync(self, *, install_init_scripts: bool = True):
        return self._new_context_for_browser_sync(self._browser, install_init_scripts=install_init_scripts)

    def _with_context_init_scripts_sync(self, ctx):
        add_init_script = getattr(ctx, "add_init_script", None)
        if add_init_script is not None:
            add_init_script(script=TRANSPORT_HOOKS_JS)
        return ctx

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

    def _ensure_hook_page_sync(self, navigation_timeout_ms: int | None = None, chat_ready_timeout_ms: int | None = None):
        if navigation_timeout_ms is None and chat_ready_timeout_ms is None:
            self._ensure_browser_sync()
        else:
            self._ensure_browser_sync(navigation_timeout_ms, chat_ready_timeout_ms)
        if not self._is_chat_runtime_ready_sync(self._hook_page):
            log.debug("hook page not chat-ready before install: %s", self._format_chat_runtime_diagnostics_sync(self._hook_page))
            self._goto_aistudio_with_options_sync(
                self._hook_page,
                navigation_timeout_ms=navigation_timeout_ms,
                chat_ready_timeout_ms=chat_ready_timeout_ms,
            )
        if self._complete_aistudio_onboarding_sync(self._hook_page):
            self._wait_for_chat_runtime_sync(
                self._hook_page,
                timeout_ms=chat_ready_timeout_ms or AI_STUDIO_CHAT_READY_TIMEOUT_MS,
            )
        self._install_hooks_sync(self._hook_page)
        return self._hook_page

    def _chat_url_candidates(self, model: str | None = None) -> tuple[str, ...]:
        urls = list(_aistudio_chat_urls(model))
        preferred_chat_url = _aistudio_chat_url_with_model(self._preferred_chat_url or "", model) if self._preferred_chat_url else None
        if preferred_chat_url and preferred_chat_url in urls:
            urls.remove(preferred_chat_url)
            urls.insert(0, preferred_chat_url)
        return tuple(urls)

    def _route_candidate_for_url(self, url: str | None) -> str | None:
        try:
            parsed = urlparse(url or "")
        except Exception:
            return None
        if parsed.hostname != AI_STUDIO_HOST:
            return None
        path = parsed.path or ""
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "u" and parts[1].isdigit() and parts[2] == "prompts":
            return _aistudio_chat_url_for_authuser(parts[1])
        if path.startswith("/prompts/new_chat"):
            return AI_STUDIO_URL_UNSCOPED_FALLBACK
        if path.startswith("/app/prompts/new_chat"):
            return AI_STUDIO_URL_LEGACY_FALLBACK
        if path in ("", "/"):
            return AI_STUDIO_HOME_URL
        return None

    def _advance_chat_route_after_auth_failure_sync(self) -> bool:
        current = None
        if self._hook_page is not None:
            current = self._route_candidate_for_url(getattr(self._hook_page, "url", None))
        current = current or self._preferred_chat_url
        for failed_url in (current, self._preferred_chat_url, self._last_requested_chat_url):
            if failed_url:
                self._failed_chat_urls.add(failed_url)
        self._preferred_chat_url = None
        self._last_requested_chat_url = None
        self._templates.clear()
        remaining = [url for url in _aistudio_chat_urls() if url not in self._failed_chat_urls]
        if not remaining:
            return False
        self._close_sync()
        return True

    def _ensure_botguard_service_sync(
        self,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
        botguard_timeout_ms: int | None = None,
    ):
        import time as _t
        _t0 = _t.time()
        log.info("AI Studio botguard: ensuring hook page")
        page = self._ensure_hook_page_with_options_sync(
            navigation_timeout_ms=navigation_timeout_ms,
            chat_ready_timeout_ms=chat_ready_timeout_ms,
        )
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
                route.abort()
                return
            route.continue_()

        route_pattern = "**/*"
        page.route(route_pattern, on_route)
        try:
            log.info("AI Studio botguard: triggering native send for service capture")
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger send while capturing BotGuardService")

            botguard_wait_ms = botguard_timeout_ms or AI_STUDIO_BOTGUARD_CAPTURE_TIMEOUT_MS
            attempts = max(1, (botguard_wait_ms + 999) // 1000)
            for i in range(attempts):
                page.wait_for_timeout(1000)
                if page.evaluate("mw:!!window.__bg_service"):
                    self._wait_until_idle_sync(page)
                    log.debug(f"[timing] botguard captured after {i+1}s, total {_t.time()-_t0:.1f}s")
                    log.info("AI Studio botguard: captured after %.1fs", _t.time() - _t0)
                    return page
        finally:
            page.unroute(route_pattern, on_route)

        raise RuntimeError("BotGuardService capture timeout")

    def _capture_template_sync(
        self,
        model: str,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
        botguard_timeout_ms: int | None = None,
        template_capture_timeout_ms: int | None = None,
        template_recovery_attempts: int | None = None,
    ) -> dict[str, Any]:
        import time as _t
        _t0 = _t.time()
        if model in self._templates:
            log.debug(f"[timing] template cached for {model}")
            return self._templates[model]

        if self._is_image_model(model):
            page = self._ensure_hook_page_with_options_sync(
                navigation_timeout_ms=navigation_timeout_ms,
                chat_ready_timeout_ms=chat_ready_timeout_ms,
            )
            self._prepare_model_onboarding_sync(page, model)
            self._install_hooks_sync(page)
            captured = self._capture_template_request_with_recovery_sync(
                page,
                model,
                navigation_timeout_ms=navigation_timeout_ms,
                chat_ready_timeout_ms=chat_ready_timeout_ms,
                template_capture_timeout_ms=template_capture_timeout_ms,
                recovery_attempts=template_recovery_attempts,
            )
            if not page.evaluate("mw:!!window.__bg_service"):
                self._goto_aistudio_with_options_sync(
                    page,
                    navigation_timeout_ms=navigation_timeout_ms,
                    chat_ready_timeout_ms=chat_ready_timeout_ms,
                )
                self._install_hooks_sync(page)
                self._ensure_botguard_service_with_options_sync(
                    navigation_timeout_ms=navigation_timeout_ms,
                    chat_ready_timeout_ms=chat_ready_timeout_ms,
                    botguard_timeout_ms=botguard_timeout_ms,
                )
            self._templates[model] = captured
            log.debug(f"[timing] image template captured for {model} in {_t.time()-_t0:.1f}s")
            return captured

        page = self._ensure_hook_page_with_options_sync(
            navigation_timeout_ms=navigation_timeout_ms,
            chat_ready_timeout_ms=chat_ready_timeout_ms,
        )
        if self._select_text_model_sync(page, model):
            self._install_hooks_sync(page)
        page = self._ensure_botguard_service_with_options_sync(
            navigation_timeout_ms=navigation_timeout_ms,
            chat_ready_timeout_ms=chat_ready_timeout_ms,
            botguard_timeout_ms=botguard_timeout_ms,
        )
        log.debug(f"[timing] botguard done in {_t.time()-_t0:.1f}s, starting template capture")
        log.info("AI Studio template: botguard ready; capturing template for model=%s", model)
        captured = self._capture_template_request_with_recovery_sync(
            page,
            model,
            navigation_timeout_ms=navigation_timeout_ms,
            chat_ready_timeout_ms=chat_ready_timeout_ms,
            template_capture_timeout_ms=template_capture_timeout_ms,
            recovery_attempts=template_recovery_attempts,
        )
        self._templates[model] = captured
        log.debug(f"[timing] template captured for {model} in {_t.time()-_t0:.1f}s")
        log.info("AI Studio template: captured for model=%s in %.1fs", model, _t.time() - _t0)
        return captured

    def _is_image_model(self, model: str) -> bool:
        return "image" in (model or "").lower()

    def _capture_template_request_sync(self, page, model: str, *, timeout_ms: int = AI_STUDIO_TEMPLATE_CAPTURE_TIMEOUT_MS) -> dict[str, Any]:
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

            attempts = max(1, (timeout_ms + AI_STUDIO_TEMPLATE_CAPTURE_POLL_MS - 1) // AI_STUDIO_TEMPLATE_CAPTURE_POLL_MS)
            for _ in range(attempts):
                if captured:
                    break
                page.wait_for_timeout(AI_STUDIO_TEMPLATE_CAPTURE_POLL_MS)
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

    def _capture_template_request_with_recovery_sync(
        self,
        page,
        model: str,
        *,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
        template_capture_timeout_ms: int | None = None,
        recovery_attempts: int | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        max_attempts = max(1, recovery_attempts) if recovery_attempts is not None else 2
        for attempt in range(max_attempts):
            if not self._is_chat_runtime_ready_sync(page):
                self._goto_aistudio_with_options_sync(
                    page,
                    navigation_timeout_ms=navigation_timeout_ms,
                    chat_ready_timeout_ms=chat_ready_timeout_ms,
                )
                self._install_hooks_sync(page)
                if not self._is_image_model(model) and self._select_text_model_sync(page, model):
                    self._install_hooks_sync(page)
            try:
                return self._capture_template_request_with_options_sync(
                    page,
                    model,
                    timeout_ms=template_capture_timeout_ms,
                )
            except Exception as exc:
                last_error = exc
                diagnostics = self._format_chat_runtime_diagnostics_sync(page)
                if attempt + 1 >= max_attempts:
                    raise
                if not self._is_chat_runtime_ready_sync(page):
                    log.info("AI Studio page left chat runtime during template capture; reopening before retry: %s", diagnostics)
                    self._goto_aistudio_with_options_sync(
                        page,
                        navigation_timeout_ms=navigation_timeout_ms,
                        chat_ready_timeout_ms=chat_ready_timeout_ms,
                    )
                    self._install_hooks_sync(page)
                    continue
                if self._is_image_model(model):
                    log.info("AI Studio image template capture failed; re-opening image model before retry: %s; %s", exc, diagnostics)
                    self._prepare_model_onboarding_sync(page, model)
                    self._install_hooks_sync(page)
                    continue
                message = str(exc).lower()
                if "template capture timeout" in message or "failed to trigger send during template capture" in message:
                    log.info("AI Studio template send did not produce a capture request; reopening before retry: %s; %s", exc, diagnostics)
                    self._goto_aistudio_with_options_sync(
                        page,
                        navigation_timeout_ms=navigation_timeout_ms,
                        chat_ready_timeout_ms=chat_ready_timeout_ms,
                    )
                    self._install_hooks_sync(page)
                    if self._select_text_model_sync(page, model):
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

    def _send_hooked_request_sync(
        self,
        body: str,
        url: str,
        headers: dict[str, str],
        timeout_ms: int,
        try_native: bool = True,
    ) -> tuple[int, bytes]:
        import time as _t
        _t0 = _t.time()
        status, raw = self._send_generate_content_with_fallback_sync(
            body=body,
            url=url,
            headers=headers,
            timeout_ms=timeout_ms,
            try_native=try_native,
        )
        log.debug("[timing] browser fetch replay done in %.1fs, status=%s", _t.time() - _t0, status)
        return status, raw

    def _goto_aistudio_with_options_sync(
        self,
        page,
        *,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
        model: str | None = None,
    ) -> None:
        if navigation_timeout_ms is None and chat_ready_timeout_ms is None:
            if model is None:
                self._goto_aistudio_sync(page)
            else:
                self._goto_aistudio_sync(page, model=model)
            return
        kwargs = {
            "navigation_timeout_ms": navigation_timeout_ms or AI_STUDIO_NAVIGATION_TIMEOUT_MS,
            "chat_ready_timeout_ms": chat_ready_timeout_ms or AI_STUDIO_CHAT_READY_TIMEOUT_MS,
        }
        if model is not None:
            kwargs["model"] = model
        self._goto_aistudio_sync(page, **kwargs)

    def _ensure_hook_page_with_options_sync(
        self,
        *,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
    ):
        if navigation_timeout_ms is None and chat_ready_timeout_ms is None:
            return self._ensure_hook_page_sync()
        return self._ensure_hook_page_sync(
            navigation_timeout_ms=navigation_timeout_ms,
            chat_ready_timeout_ms=chat_ready_timeout_ms,
        )

    def _ensure_botguard_service_with_options_sync(
        self,
        *,
        navigation_timeout_ms: int | None = None,
        chat_ready_timeout_ms: int | None = None,
        botguard_timeout_ms: int | None = None,
    ):
        if navigation_timeout_ms is None and chat_ready_timeout_ms is None and botguard_timeout_ms is None:
            return self._ensure_botguard_service_sync()
        return self._ensure_botguard_service_sync(
            navigation_timeout_ms=navigation_timeout_ms,
            chat_ready_timeout_ms=chat_ready_timeout_ms,
            botguard_timeout_ms=botguard_timeout_ms,
        )

    def _capture_template_request_with_options_sync(self, page, model: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
        if timeout_ms is None:
            return self._capture_template_request_sync(page, model)
        return self._capture_template_request_sync(page, model, timeout_ms=timeout_ms)

    def _goto_aistudio_sync(
        self,
        page,
        *,
        navigation_timeout_ms: int = AI_STUDIO_NAVIGATION_TIMEOUT_MS,
        chat_ready_timeout_ms: int = AI_STUDIO_CHAT_READY_TIMEOUT_MS,
        model: str | None = None,
    ) -> None:
        import time as _t
        last_error = None
        for url in self._chat_url_candidates(model):
            route_candidate = self._route_candidate_for_url(url)
            if url in self._failed_chat_urls or (route_candidate and route_candidate in self._failed_chat_urls):
                continue
            route_started_at = _t.time()
            goto_error = None
            log.info("AI Studio navigation: trying %s", url)
            try:
                page.goto(url, wait_until="commit", timeout=navigation_timeout_ms)
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
                if url != AI_STUDIO_URL_LEGACY_FALLBACK:
                    last_error = RuntimeError(f"AI Studio redirected to docs after navigating to {url}: {current_url}")
                    continue
                try:
                    page.goto(AI_STUDIO_URL_LEGACY_FALLBACK, wait_until="commit", timeout=navigation_timeout_ms)
                    page.wait_for_timeout(2500)
                    current_url = getattr(page, "url", "")
                except Exception as exc:
                    last_error = exc
                    continue

            if goto_error is not None and not self._is_aistudio_url(current_url):
                continue

            if self._wait_for_chat_runtime_sync(page, timeout_ms=chat_ready_timeout_ms):
                if self._complete_aistudio_onboarding_sync(page):
                    self._wait_for_chat_runtime_sync(page, timeout_ms=chat_ready_timeout_ms)
                final_chat_url = self._route_candidate_for_url(page.url)
                if final_chat_url and final_chat_url in self._failed_chat_urls:
                    last_error = RuntimeError(f"AI Studio redirected from {url} to failed chat route {final_chat_url}")
                    self._failed_chat_urls.add(url)
                    log.info("AI Studio navigation: skipping failed redirected chat route %s after trying %s", final_chat_url, url)
                    continue
                self._last_requested_chat_url = url
                self._preferred_chat_url = final_chat_url or url
                log.debug(f"[timing] UI ready (dms+textarea) after {_t.time()-route_started_at:.1f}s")
                log.info("AI Studio navigation: chat runtime ready in %.1fs url=%s", _t.time() - route_started_at, page.url)
                return

            diagnostics = self._format_chat_runtime_diagnostics_sync(page)
            last_error = RuntimeError(f"AI Studio chat runtime not ready after navigating to {url}: {diagnostics}")
            log.debug("[timing] UI not ready after %.1fs: %s", _t.time() - route_started_at, diagnostics)
            log.info("AI Studio navigation: chat runtime not ready after %.1fs: %s", _t.time() - route_started_at, diagnostics)
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
        path = parsed.path or ""
        if any(path.startswith(prefix) for prefix in AI_STUDIO_CHAT_PATH_PREFIXES):
            return True
        parts = [part for part in path.split("/") if part]
        return len(parts) >= 3 and parts[0] == "u" and parts[1].isdigit() and parts[2] == "prompts"

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

    def _wait_for_chat_runtime_sync(self, page, *, timeout_ms: int = AI_STUDIO_CHAT_READY_TIMEOUT_MS) -> bool:
        attempts = max(1, timeout_ms // AI_STUDIO_CHAT_READY_POLL_MS)
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

    def _select_text_model_sync(self, page, model: str) -> bool:
        if self._is_image_model(model):
            return False
        target_model = str(model or "").strip().removeprefix("models/")
        if not target_model:
            return False
        selected: dict[str, Any] | None = None
        current: dict[str, Any] | None = None

        def read_current() -> dict[str, Any] | None:
            try:
                raw_current = page.evaluate(AI_STUDIO_CURRENT_TEXT_MODEL_JS, target_model)
            except Exception as exc:
                log.debug("AI Studio current text model readback failed: %s", exc)
                return None
            return raw_current if isinstance(raw_current, dict) else None

        for attempt_index in range(2):
            try:
                page.evaluate(DIALOG_CLEANUP_JS)
            except Exception:
                pass
            current = read_current()
            if isinstance(current, dict) and current.get("matches") is True:
                return True
            try:
                opened = page.evaluate(AI_STUDIO_OPEN_MODEL_PICKER_JS, target_model)
                if isinstance(opened, dict) and opened.get("opened"):
                    page.wait_for_timeout(1000)
            except Exception as exc:
                log.debug("AI Studio text model picker open failed: %s", exc)
            try:
                raw_selected = page.evaluate(AI_STUDIO_SELECT_TEXT_MODEL_JS, target_model)
            except Exception as exc:
                log.debug("AI Studio text model selection failed: %s", exc)
                return False
            if not isinstance(raw_selected, dict):
                return False
            selected = raw_selected
            if selected.get("selected") is True:
                log.info("AI Studio text model selected: %s", selected.get("label", target_model))
                page.wait_for_timeout(1200)
                current = read_current()
                if isinstance(current, dict) and current.get("matches") is True:
                    return True
                log.info("AI Studio text model readback mismatch after click: %s", current)
                if attempt_index == 0:
                    page.wait_for_timeout(1000)
                    continue
            if selected.get("reason") == "already_selected":
                current = read_current()
                if isinstance(current, dict) and current.get("matches") is True:
                    return True
                log.info("AI Studio text model already-selected readback mismatch: %s", current)
                if attempt_index == 0:
                    page.wait_for_timeout(1000)
                    continue
            if attempt_index == 0 and selected.get("reason") == "text_model_not_found":
                page.wait_for_timeout(1000)
                continue
            break
        reason = selected.get("reason") if isinstance(selected, dict) else None
        if reason not in {"not_text_model"}:
            log.info("AI Studio text model picker did not select %s: %s current=%s", target_model, reason, current)
        return False

    def _aistudio_image_urls_for_model(self, model: str) -> tuple[str, ...]:
        return _aistudio_image_urls(model)

    def _is_aistudio_image_url(self, url: str | None) -> bool:
        try:
            parsed = urlparse(url or "")
        except Exception:
            return False
        if parsed.hostname != AI_STUDIO_HOST:
            return False
        path = parsed.path or ""
        if path.startswith("/prompts/new_image") or path.startswith("/app/prompts/new_image"):
            return True
        parts = [part for part in path.split("/") if part]
        return len(parts) >= 4 and parts[0] == "u" and parts[1].isdigit() and parts[2] == "prompts" and parts[3] == "new_image"

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
        self._install_transport_hooks_sync(page)
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

    def _install_transport_hooks_sync(self, page) -> None:
        try:
            result = page.evaluate(INSTALL_TRANSPORT_HOOKS_JS)
        except Exception as exc:
            diagnostics = self._format_chat_runtime_diagnostics_sync(page)
            raise RuntimeError(f"Transport hook install failed: {exc}; {diagnostics}") from exc
        if result in {"transport_hooked", "transport_already_hooked"}:
            return
        raise RuntimeError(f"Transport hook install failed: {result}")

    def _click_run_button_sync(self, page) -> bool:
        try:
            result = page.evaluate(AI_STUDIO_SEND_BUTTON_JS, True)
            if isinstance(result, dict) and result.get("clicked"):
                return True
        except Exception:
            pass

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
        try:
            result = page.evaluate(AI_STUDIO_SEND_BUTTON_JS, False)
            if isinstance(result, dict) and result.get("found"):
                return True
        except Exception:
            pass

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

    def _close_all_sync(self) -> None:
        self._close_sync()
        self._close_native_worker_pool_sync()
