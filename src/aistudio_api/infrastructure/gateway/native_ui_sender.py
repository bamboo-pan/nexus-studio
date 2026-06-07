"""Clean-process native AI Studio UI sender.

This helper intentionally runs outside the long-lived gateway browser process.
AI Studio accepts the same account/model/prompt in a clean Camoufox process while
same-process contexts can inherit request-hook state and receive permission 403s.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox

from aistudio_api.config import camoufox_proxy_identity_options, settings
from aistudio_api.infrastructure.gateway.session import (
    AI_STUDIO_HOST,
    AI_STUDIO_HOME_URL,
    AI_STUDIO_ONBOARDING_JS,
    AI_STUDIO_OPEN_MODEL_PICKER_JS,
    AI_STUDIO_SELECT_TEXT_MODEL_JS,
    AI_STUDIO_SEND_BUTTON_JS,
    DIALOG_CLEANUP_JS,
    _aistudio_chat_urls,
)

warnings.filterwarnings("ignore", message="When using a proxy, it is heavily recommended that you pass `geoip=True`.*")

_OPEN_CHAT_GOTO_TIMEOUT_MS = 45_000
_OPEN_CHAT_MAX_TIMEOUT_MS = 180_000
_OPEN_CHAT_INTERRUPTED_READY_WAIT_MS = 12_000
_MODEL_PICKER_OPEN_WAIT_MS = 2_000


def _browser_options() -> dict[str, object]:
    options: dict[str, object] = {
        "headless": settings.camoufox_headless,
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


def _safe_text(value: object, limit: int = 240) -> str:
    text = str(value or "")
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _trace_stage(stage: str, **details: object) -> None:
    parts = [f"stage={stage}"]
    for key, value in details.items():
        parts.append(f"{key}={_safe_text(value, 120)}")
    print("native_ui_sender " + " ".join(parts), file=sys.stderr, flush=True)


def _url_path(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.hostname != AI_STUDIO_HOST:
        return f"{parsed.hostname or '<unknown>'}{parsed.path or ''}"
    return parsed.path or "/"


def _wait_for_chat_ready(page, timeout_ms: int) -> bool:
    deadline = time.time() + max(1.0, timeout_ms / 1000)
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


def _open_chat(page, timeout_ms: int) -> None:
    failures: list[str] = []
    deadline = time.monotonic() + max(1.0, float(timeout_ms) / 1000.0)
    remaining_ms = int(max(0.0, (deadline - time.monotonic()) * 1000))
    if remaining_ms > 0:
        home_timeout_ms = min(remaining_ms, _OPEN_CHAT_GOTO_TIMEOUT_MS)
        _trace_stage("open_chat.prime_home", timeout_ms=home_timeout_ms, remaining_ms=remaining_ms)
        try:
            page.goto(AI_STUDIO_HOME_URL, wait_until="commit", timeout=home_timeout_ms)
        except Exception as exc:
            failures.append(f"home goto={_safe_text(exc, 120)}")
    for url in _aistudio_chat_urls():
        remaining_ms = int(max(0.0, (deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            break
        url_path = _url_path(url)
        goto_timeout_ms = min(remaining_ms, _OPEN_CHAT_GOTO_TIMEOUT_MS)
        _trace_stage("open_chat.goto", url_path=url_path, timeout_ms=goto_timeout_ms, remaining_ms=remaining_ms)
        try:
            page.goto(url, wait_until="commit", timeout=goto_timeout_ms)
        except Exception as exc:
            failures.append(f"{url_path} goto={_safe_text(exc, 120)}")
            current_path = _url_path(getattr(page, "url", ""))
            remaining_ms = int(max(0.0, (deadline - time.monotonic()) * 1000))
            if remaining_ms <= 0:
                break
            if "accounts.google.com" in str(getattr(page, "url", "")):
                failures.append(f"{url_path} redirected_to_signin")
                _trace_stage("open_chat.signin_redirect", url_path=url_path)
                continue
            interrupted_ready_wait_ms = min(remaining_ms, _OPEN_CHAT_INTERRUPTED_READY_WAIT_MS)
            _trace_stage(
                "open_chat.goto_interrupted",
                url_path=url_path,
                current_path=current_path,
                ready_wait_ms=interrupted_ready_wait_ms,
            )
            if _wait_for_chat_ready(page, interrupted_ready_wait_ms):
                _trace_stage("open_chat.ready", current_path=_url_path(getattr(page, "url", "")), after="goto_interrupted")
                return
            failures.append(f"{url_path} interrupted_not_ready current={current_path}")
            continue
        if "accounts.google.com" in str(getattr(page, "url", "")):
            failures.append(f"{url_path} redirected_to_signin")
            _trace_stage("open_chat.signin_redirect", url_path=url_path)
            continue
        remaining_ms = int(max(0.0, (deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            break
        if _wait_for_chat_ready(page, remaining_ms):
            _trace_stage("open_chat.ready", current_path=_url_path(getattr(page, "url", "")))
            return
        current_path = _url_path(getattr(page, "url", ""))
        failures.append(f"{url_path} not_ready current={current_path}")
        _trace_stage("open_chat.not_ready", url_path=url_path, current_path=current_path)
    raise RuntimeError(f"AI Studio chat runtime not ready in native UI sender after {timeout_ms}ms: {failures[:4]}")


def _open_chat_timeout_ms(timeout_ms: int) -> int:
    return min(max(1, int(timeout_ms)), _OPEN_CHAT_MAX_TIMEOUT_MS)


def _select_model(page, model: str, timeout_ms: int) -> None:
    if not model:
        return
    selected: dict[str, object] | None = None
    opened: dict[str, object] | None = None
    select_error: BaseException | None = None
    deadline = time.monotonic() + min(45.0, max(15.0, float(timeout_ms) / 1000.0 * 0.35))
    attempt_index = 0
    picker_open = False
    try:
        page.evaluate(DIALOG_CLEANUP_JS)
    except Exception:
        pass
    while time.monotonic() < deadline:
        attempt_index += 1
        if not picker_open:
            try:
                raw_opened = page.evaluate(AI_STUDIO_OPEN_MODEL_PICKER_JS)
                if isinstance(raw_opened, dict):
                    opened = raw_opened
                    _trace_stage("select_model.open_picker", attempt=attempt_index, result=opened)
                    if opened.get("opened"):
                        picker_open = True
                        page.wait_for_timeout(_MODEL_PICKER_OPEN_WAIT_MS)
            except Exception:
                pass
        try:
            raw_selected = page.evaluate(AI_STUDIO_SELECT_TEXT_MODEL_JS, model)
        except Exception as exc:
            select_error = exc
            raw_selected = None
        if isinstance(raw_selected, dict):
            selected = raw_selected
            _trace_stage("select_model.result", attempt=attempt_index, result=selected)
            if selected.get("selected") is True:
                page.wait_for_timeout(2500)
                return
            if selected.get("reason") == "already_selected":
                return
            if selected.get("reason") == "not_text_model":
                break
            if selected.get("reason") == "text_model_not_found" and opened and opened.get("opened"):
                picker_open = True
        delay_ms = min(2500, 1000 + attempt_index * 250)
        page.wait_for_timeout(delay_ms)
    diagnostics = f"result={_safe_text(selected, 220)} opened={_safe_text(opened, 180)}"
    if select_error is not None:
        diagnostics += f" select_error={type(select_error).__name__}: {_safe_text(select_error, 160)}"
    raise RuntimeError(f"AI Studio text model not selected in native UI sender: {model}; {diagnostics}")


def _fill_prompt(page, prompt: str) -> None:
    textarea = page.query_selector("textarea")
    if textarea is None:
        raise RuntimeError("textarea not found in native UI sender")
    textarea.fill(prompt)
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


def _wire_model_from_body(body: str | None) -> str:
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


def _body_contains_prompt(body: str | None, prompt_marker: str) -> bool:
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


def _is_generate_content_response_url(url: str) -> bool:
    return "GenerateContent" in url and "CountTokens" not in url and "PerUserQuota" not in url


def _validate_payload(payload: dict[str, object]) -> tuple[Path, str, str, int]:
    auth_file = str(payload.get("auth_file") or "")
    model = str(payload.get("model") or "")
    prompt = str(payload.get("prompt") or "")
    timeout_ms = int(payload.get("timeout_ms") or 120_000)
    auth_path = Path(auth_file)
    if not auth_path.exists():
        raise FileNotFoundError(f"auth_file missing: {auth_path}")
    if not prompt.strip():
        raise RuntimeError("prompt is required")
    return auth_path, model, prompt, timeout_ms


def _send_on_page(page, *, model: str, prompt: str, timeout_ms: int) -> dict[str, object]:

    response_holder: dict[str, object] = {}
    observed: list[str] = []
    target_model = model.strip().removeprefix("models/")
    prompt_marker = prompt.strip()[:80]

    def on_response(response) -> None:
        if response_holder:
            return
        response_url = getattr(response, "url", "") or ""
        if not _is_generate_content_response_url(response_url):
            return
        try:
            request_body = response.request.post_data
        except Exception:
            request_body = None
        wire_model = _wire_model_from_body(request_body)
        response_model = wire_model.removeprefix("models/") if wire_model else ""
        model_matches = bool(response_model and response_model == target_model)
        prompt_matches = _body_contains_prompt(request_body, prompt_marker)
        if not model_matches or not prompt_matches:
            if len(observed) < 5:
                observed.append(
                    f"{_url_path(response_url)} model={wire_model or '<unknown>'} "
                    f"model_match={model_matches} prompt_match={prompt_matches}"
                )
            return
        try:
            status = int(response.status)
            raw = response.body() or b""
        except Exception as exc:
            response_holder["error"] = f"{type(exc).__name__}: {_safe_text(exc, 200)}"
            return
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if (status == 204 or not raw) and len(observed) < 5:
            observed.append(f"{_url_path(response_url)} status={status} body={len(raw)} model={wire_model}")
            return
        response_holder.update(
            {
                "ok": True,
                "status": status,
                "body_b64": base64.b64encode(raw).decode("ascii"),
                "body_size": len(raw),
                "wire_model": wire_model,
                "url_path": _url_path(response_url),
            }
        )

    page.on("response", on_response)
    try:
        _trace_stage("send.start", model=model, timeout_ms=timeout_ms)
        _open_chat(page, _open_chat_timeout_ms(timeout_ms))
        _trace_stage("send.chat_ready", model=model)
        _select_model(page, model, timeout_ms)
        _trace_stage("send.model_selected", model=model)
        _fill_prompt(page, prompt)
        _trace_stage("send.prompt_filled")
        if not _click_run(page):
            raise RuntimeError("run button not found in native UI sender")
        _trace_stage("send.clicked_run")
        deadline = time.time() + max(1.0, timeout_ms / 1000)
        while time.time() < deadline:
            if response_holder:
                break
            page.wait_for_timeout(250)
        if response_holder.get("error"):
            raise RuntimeError(str(response_holder["error"]))
        if not response_holder:
            raise RuntimeError(f"native UI sender timeout; observed={observed[:5]}")
        _trace_stage("send.response_matched", status=response_holder.get("status"), url_path=response_holder.get("url_path"))
        return response_holder
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


def _send(payload: dict[str, object]) -> dict[str, object]:
    auth_path, model, prompt, timeout_ms = _validate_payload(payload)

    with Camoufox(**_browser_options()) as browser:
        context = browser.new_context(storage_state=str(auth_path), service_workers="block")
        page = context.new_page()
        try:
            return _send_on_page(page, model=model, prompt=prompt, timeout_ms=timeout_ms)
        finally:
            try:
                page.close()
            except Exception:
                pass
            context.close()


class NativeUiSenderWorker:
    def __init__(self) -> None:
        self._cf = None
        self._browser = None
        self._context = None
        self._auth_file: str | None = None

    def send(self, payload: dict[str, object]) -> dict[str, object]:
        auth_path, model, prompt, timeout_ms = _validate_payload(payload)
        context = self._ensure_context(str(auth_path))
        page = context.new_page()
        try:
            return _send_on_page(page, model=model, prompt=prompt, timeout_ms=timeout_ms)
        finally:
            try:
                page.close()
            except Exception:
                pass

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._cf is not None:
            try:
                self._cf.__exit__(None, None, None)
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._cf = None
        self._auth_file = None

    def _ensure_context(self, auth_file: str):
        if self._browser is None:
            _trace_stage("context.open_browser")
            self._cf = Camoufox(**_browser_options())
            self._browser = self._cf.__enter__()
        if self._context is not None and self._auth_file == auth_file:
            _trace_stage("context.reuse")
            return self._context
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        _trace_stage("context.new")
        self._context = self._browser.new_context(storage_state=auth_file, service_workers="block")
        self._auth_file = auth_file
        return self._context


def worker_main() -> int:
    worker = NativeUiSenderWorker()
    try:
        for line in sys.stdin:
            request_id: object = None
            try:
                message = json.loads(line or "{}")
                if not isinstance(message, dict):
                    raise RuntimeError("worker message must be a JSON object")
                request_id = message.get("id")
                payload = message.get("payload")
                if not isinstance(payload, dict):
                    raise RuntimeError("worker message payload must be a JSON object")
                result = worker.send(payload)
            except Exception as exc:
                result = {"ok": False, "error": f"{type(exc).__name__}: {_safe_text(exc, 500)}"}
            result["id"] = request_id
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        worker.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--worker" in argv:
        return worker_main()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("stdin payload must be a JSON object")
        result = _send(payload)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {_safe_text(exc, 500)}"}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
