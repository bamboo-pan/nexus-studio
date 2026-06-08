from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from statistics import median
from urllib.parse import quote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def response_path(response) -> str:
    return urlparse(response.url).path


def split_console_errors(console_errors: list[str], allowed_502_count: int) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    unexpected: list[str] = []
    remaining_expected_502 = allowed_502_count
    for message in console_errors:
        if remaining_expected_502 > 0 and "status of 502 (Bad Gateway)" in message:
            allowed.append(message)
            remaining_expected_502 -= 1
        else:
            unexpected.append(message)
    return allowed, unexpected


def copied_account_auth_files(accounts_dir: Path) -> list[tuple[str, Path]]:
    if not accounts_dir.is_dir():
        raise AssertionError(f"copied accounts directory is missing: {accounts_dir}")
    preferred_ids: list[str] = []
    registry_path = accounts_dir / "registry.json"
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            active_id = str(registry.get("active_account_id") or registry.get("active") or "").strip()
            if active_id:
                preferred_ids.append(active_id)
        except Exception:
            pass
    for account_dir in sorted(accounts_dir.iterdir()):
        if account_dir.is_dir() and (account_dir / "auth.json").is_file() and account_dir.name not in preferred_ids:
            preferred_ids.append(account_dir.name)
    candidates: list[tuple[str, Path]] = []
    for account_id in preferred_ids:
        auth_file = accounts_dir / account_id / "auth.json"
        if auth_file.is_file():
            candidates.append((account_id, auth_file))
    if candidates:
        return candidates
    raise AssertionError(f"no copied account auth.json found under {accounts_dir}")


def pick_copied_account_auth_file(accounts_dir: Path) -> tuple[str, Path]:
    return copied_account_auth_files(accounts_dir)[0]


def load_official_ai_studio_helpers(repo_src_dir: Path) -> dict[str, object]:
    if repo_src_dir.is_dir():
        src_text = str(repo_src_dir)
        if src_text not in sys.path:
            sys.path.insert(0, src_text)
    from aistudio_api.infrastructure.gateway import session as gateway_session

    return {
        "DIALOG_CLEANUP_JS": gateway_session.DIALOG_CLEANUP_JS,
        "AI_STUDIO_ONBOARDING_JS": gateway_session.AI_STUDIO_ONBOARDING_JS,
        "AI_STUDIO_CURRENT_TEXT_MODEL_JS": gateway_session.AI_STUDIO_CURRENT_TEXT_MODEL_JS,
        "AI_STUDIO_OPEN_MODEL_PICKER_JS": gateway_session.AI_STUDIO_OPEN_MODEL_PICKER_JS,
        "AI_STUDIO_SELECT_TEXT_MODEL_JS": gateway_session.AI_STUDIO_SELECT_TEXT_MODEL_JS,
        "AI_STUDIO_SEND_BUTTON_JS": gateway_session.AI_STUDIO_SEND_BUTTON_JS,
        "_aistudio_chat_urls": gateway_session._aistudio_chat_urls,
    }


def official_url_path(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.hostname != "aistudio.google.com":
        return f"{parsed.hostname or '<unknown>'}{parsed.path or ''}"
    return parsed.path or "/"


def official_is_generate_content_response_url(url: str) -> bool:
    return "GenerateContent" in str(url or "") and "CountTokens" not in str(url or "") and "PerUserQuota" not in str(url or "")


def safe_response_body_preview(raw: bytes | str | None, limit: int = 500) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    text = re.sub(r"(?i)(authorization|cookie|api[_-]?key|token|oauth|sapisid|sid|ssid|hsid)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
    text = re.sub(r"ya29\.[A-Za-z0-9._-]+", "ya29.***", text)
    text = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "AIza***", text)
    return text[:limit]


def official_wire_model_from_body(body: str | None) -> str:
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


def official_body_contains_prompt(body: str | None, prompt_marker: str) -> bool:
    if not body or not prompt_marker:
        return False
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return prompt_marker in body

    def walk(value: object) -> bool:
        if isinstance(value, str):
            return prompt_marker in value
        if isinstance(value, list):
            return any(walk(item) for item in value)
        if isinstance(value, dict):
            return any(walk(item) for item in value.values())
        return False

    return walk(parsed)


def official_wait_for_chat_ready(page, helpers: dict[str, object], timeout_ms: int) -> bool:
    deadline = time.perf_counter() + (timeout_ms / 1000)
    while time.perf_counter() < deadline:
        try:
            if page.evaluate("() => !!window.default_MakerSuite") and page.query_selector("textarea") is not None:
                return True
        except Exception:
            pass
        try:
            if page.evaluate("mw:!!window.default_MakerSuite") and page.query_selector("textarea") is not None:
                return True
        except Exception:
            pass
        try:
            page.evaluate(str(helpers["DIALOG_CLEANUP_JS"]))
            result = page.evaluate(str(helpers["AI_STUDIO_ONBOARDING_JS"]))
            if isinstance(result, dict) and result.get("submitted"):
                page.wait_for_timeout(1500)
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False


def official_chat_url_with_model(url: str, model: str | None = None) -> str:
    model_id = str(model or "").strip().removeprefix("models/")
    if not model_id or "new_chat" not in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}model={quote(model_id, safe='')}"


def official_authuser_chat_candidates(helpers: dict[str, object], model: str | None = None) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in helpers["_aistudio_chat_urls"](model):
        parsed = urlparse(str(url or ""))
        authuser = url_authuser(str(url))
        if parsed.hostname != "aistudio.google.com" or not authuser or "new_chat" not in parsed.path:
            continue
        if authuser in seen:
            continue
        seen.add(authuser)
        candidates.append((authuser, str(url)))
    if not candidates:
        candidates = [
            ("2", official_chat_url_with_model("https://aistudio.google.com/u/2/prompts/new_chat", model)),
            ("0", official_chat_url_with_model("https://aistudio.google.com/u/0/prompts/new_chat", model)),
        ]
    return candidates


def official_open_chat(page, helpers: dict[str, object], timeout_ms: int, required_authuser: str | None = None, model: str | None = None) -> list[str]:
    failures: list[str] = []
    deadline = time.perf_counter() + (timeout_ms / 1000)
    if required_authuser is None:
        urls = list(helpers["_aistudio_chat_urls"](model))
    else:
        urls = [url for authuser, url in official_authuser_chat_candidates(helpers, model) if authuser == required_authuser]
        if not urls:
            urls = [official_chat_url_with_model(f"https://aistudio.google.com/u/{required_authuser}/prompts/new_chat", model)]
    for url in urls:
        remaining_ms = int(max(0, (deadline - time.perf_counter()) * 1000))
        if remaining_ms <= 0:
            break
        try:
            page.goto(url, wait_until="commit", timeout=min(45_000, remaining_ms))
        except Exception as exc:
            failures.append(f"{official_url_path(url)} goto={type(exc).__name__}: {str(exc)[:120]} current={official_url_path(page.url)}")
            if "accounts.google.com" in str(page.url):
                continue
            if official_wait_for_chat_ready(page, helpers, min(12_000, remaining_ms)):
                if required_authuser is not None and url_authuser(page.url) != required_authuser:
                    failures.append(
                        f"{official_url_path(url)} authuser_mismatch expected={required_authuser} "
                        f"actual={url_authuser(page.url) or '<none>'} current={official_url_path(page.url)}"
                    )
                    continue
                return failures
            continue
        if "accounts.google.com" in str(page.url):
            failures.append(f"{official_url_path(url)} redirected_to_signin")
            continue
        remaining_ms = int(max(0, (deadline - time.perf_counter()) * 1000))
        if official_wait_for_chat_ready(page, helpers, remaining_ms):
            if required_authuser is not None and url_authuser(page.url) != required_authuser:
                failures.append(
                    f"{official_url_path(url)} authuser_mismatch expected={required_authuser} "
                    f"actual={url_authuser(page.url) or '<none>'} current={official_url_path(page.url)}"
                )
                continue
            return failures
        failures.append(f"{official_url_path(url)} not_ready current={official_url_path(page.url)}")
    raise AssertionError(f"official AI Studio chat runtime not ready after {timeout_ms}ms: {failures[:4]}")


def official_fill_prompt(page, prompt: str) -> None:
    textarea = page.query_selector("textarea")
    if textarea is None:
        raise AssertionError("official AI Studio textarea not found")
    textarea.fill(prompt)
    page.wait_for_timeout(500)


def official_click_run(page, helpers: dict[str, object]) -> bool:
    try:
        page.evaluate(str(helpers["DIALOG_CLEANUP_JS"]))
    except Exception:
        pass
    try:
        result = page.evaluate(str(helpers["AI_STUDIO_SEND_BUTTON_JS"]), True)
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


def url_authuser(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part == "u":
            return parts[index + 1]
    return ""


def select_official_ai_studio_model(page, model: str, helpers: dict[str, object], timeout_ms: int = 180_000) -> dict[str, object]:
    cleanup_js = str(helpers["DIALOG_CLEANUP_JS"])
    current_model_js = str(helpers["AI_STUDIO_CURRENT_TEXT_MODEL_JS"])
    open_picker_js = str(helpers["AI_STUDIO_OPEN_MODEL_PICKER_JS"])
    select_model_js = str(helpers["AI_STUDIO_SELECT_TEXT_MODEL_JS"])
    deadline = time.perf_counter() + (timeout_ms / 1000)
    attempt = 0
    picker_open = False
    opened: dict[str, object] = {}
    selected: dict[str, object] = {}
    current: dict[str, object] = {}
    open_history: list[dict[str, object]] = []
    selection_history: list[dict[str, object]] = []
    current_history: list[dict[str, object]] = []

    def opened_picker(value: dict[str, object]) -> bool:
        return bool(isinstance(value, dict) and value.get("opened") and value.get("type") not in {"target_card", "text_category"})

    def read_current() -> dict[str, object]:
        try:
            raw_current = page.evaluate(current_model_js, model)
        except Exception as exc:
            return {"matches": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return raw_current if isinstance(raw_current, dict) else {"matches": False, "reason": "current_model_probe_non_dict"}

    while time.perf_counter() < deadline:
        attempt += 1
        try:
            page.evaluate(cleanup_js)
        except Exception:
            pass
        current = read_current()
        current_history.append(current)
        if current.get("matches") is True:
            return {
                "ok": True,
                "model": model,
                "attempts": attempt,
                "opened": opened,
                "open_history": open_history[-8:],
                "selected": selected,
                "selection_history": selection_history[-8:],
                "current_model": current,
                "current_history": current_history[-8:],
                "page_url": page.url,
                "url_path": urlparse(page.url).path,
                "authuser": url_authuser(page.url),
                "visible_candidates": current.get("candidates") or selected.get("visible") or opened.get("visible") or [],
            }
        if not picker_open:
            try:
                raw_opened = page.evaluate(open_picker_js, model)
                if isinstance(raw_opened, dict):
                    opened = raw_opened
                    open_history.append(opened)
                    picker_open = opened_picker(opened)
                    if opened.get("opened"):
                        page.wait_for_timeout(2_000)
                        if not picker_open:
                            continue
            except Exception as exc:
                opened = {"opened": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
                open_history.append(opened)
        try:
            raw_selected = page.evaluate(select_model_js, model)
        except Exception as exc:
            raw_selected = {"selected": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        if isinstance(raw_selected, dict):
            selected = raw_selected
            selection_history.append(selected)
            if selected.get("selected") is True or selected.get("reason") == "already_selected":
                page.wait_for_timeout(2_000)
                current = read_current()
                current_history.append(current)
                if current.get("matches") is True:
                    return {
                        "ok": True,
                        "model": model,
                        "attempts": attempt,
                        "opened": opened,
                        "open_history": open_history[-8:],
                        "selected": selected,
                        "selection_history": selection_history[-8:],
                        "current_model": current,
                        "current_history": current_history[-8:],
                        "page_url": page.url,
                        "url_path": urlparse(page.url).path,
                        "authuser": url_authuser(page.url),
                        "visible_candidates": current.get("candidates") or selected.get("visible") or opened.get("visible") or [],
                    }
                picker_open = False
            if selected.get("reason") == "not_text_model":
                break
            if selected.get("reason") == "text_model_not_found" and opened_picker(opened):
                picker_open = True
        page.wait_for_timeout(min(3_000, 1_000 + attempt * 300))
    return {
        "ok": False,
        "model": model,
        "attempts": attempt,
        "opened": opened,
        "open_history": open_history[-8:],
        "selected": selected,
        "selection_history": selection_history[-8:],
        "current_model": current,
        "current_history": current_history[-8:],
        "page_url": page.url,
        "url_path": urlparse(page.url).path,
        "authuser": url_authuser(page.url),
        "visible_candidates": current.get("candidates") or (selected.get("visible") if isinstance(selected, dict) else []),
    }


def official_body_count(page, text: str) -> int:
    try:
        return str(page.locator("body").inner_text(timeout=2_000) or "").count(text)
    except Exception:
        return 0


def official_exact_visible_text_count(page, text: str) -> int:
    return int(page.evaluate(
        """
        (target) => {
          const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const wanted = normalize(target);
          if (!wanted) return 0;
          const skipped = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'BUTTON', 'SELECT', 'OPTION']);
          const visible = (element) => {
            const style = window.getComputedStyle(element);
            if (style.visibility === 'hidden' || style.display === 'none') return false;
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };
          let count = 0;
          for (const element of document.querySelectorAll('body *')) {
            if (skipped.has(element.tagName) || !visible(element)) continue;
            if (normalize(element.innerText) === wanted) count += 1;
          }
          return count;
        }
        """,
        text,
    ))


def official_disallowed_errors(console_errors: list[str], network_events: list[dict[str, object]]) -> dict[str, list[object]]:
    bad_console: list[str] = []
    bad_network = [event for event in network_events if int(event.get("status") or 0) in {401, 403}]
    return {"console": bad_console, "network": bad_network}


def official_baseline_failure_reasons(
    model_selection: dict[str, object],
    samples: list[dict[str, object]],
    disallowed_errors: dict[str, list[object]],
    discarded_samples: list[dict[str, object]] | None = None,
) -> list[str]:
    reasons: list[str] = []
    if model_selection.get("ok") is not True:
        reasons.append("official_model_selection_failed")
    if len(samples) != 3:
        reasons.append(f"official_sample_count={len(samples)}")
    bad_statuses = [sample.get("status") for sample in samples if int(sample.get("status") or 0) != 200]
    if bad_statuses:
        reasons.append(f"official_sample_non_200_statuses={bad_statuses}")
    sample_failures = [sample.get("failure_reason") for sample in samples if sample.get("result") != "pass"]
    if sample_failures:
        reasons.append(f"official_sample_failures={sample_failures}")
    if len(samples) != 3 and discarded_samples:
        discarded_failures = [sample.get("failure_reason") for sample in discarded_samples if sample.get("failure_reason")]
        if discarded_failures:
            reasons.append(f"official_discarded_sample_failures={discarded_failures[-3:]}")
    if disallowed_errors.get("console"):
        reasons.append("official_console_auth_or_connection_errors")
    if disallowed_errors.get("network"):
        statuses = [event.get("status") for event in disallowed_errors["network"]]
        reasons.append(f"official_network_auth_statuses={statuses}")
    return reasons


def run_official_ai_studio_sample(page, helpers: dict[str, object], model: str, expected_text: str, timeout_ms: int = 420_000) -> dict[str, object]:
    prompt = f"Reply with exactly: {expected_text}"
    target_model = model.strip().removeprefix("models/")
    prompt_marker = prompt.strip()[:80]
    response_holder: dict[str, object] = {}
    observed: list[str] = []

    def on_response(response) -> None:
        if response_holder:
            return
        response_url = getattr(response, "url", "") or ""
        if not official_is_generate_content_response_url(response_url):
            return
        try:
            request_body = response.request.post_data
        except Exception:
            request_body = None
        wire_model = official_wire_model_from_body(request_body)
        response_model = wire_model.removeprefix("models/") if wire_model else ""
        model_matches = bool(response_model and response_model == target_model)
        prompt_matches = official_body_contains_prompt(request_body, prompt_marker)
        if not model_matches or not prompt_matches:
            if len(observed) < 5:
                observed.append(
                    f"{official_url_path(response_url)} model={wire_model or '<unknown>'} "
                    f"model_match={model_matches} prompt_match={prompt_matches}"
                )
            return
        try:
            status = int(response.status)
            raw = response.body() or b""
        except Exception as exc:
            response_holder["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            return
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if status != 200:
            response_holder.update({
                "status": status,
                "body_size": len(raw),
                "body_preview": safe_response_body_preview(raw),
                "wire_model": wire_model,
                "url_path": official_url_path(response_url),
            })
            return
        if (status == 204 or not raw) and len(observed) < 5:
            observed.append(f"{official_url_path(response_url)} status={status} body={len(raw)} model={wire_model}")
            return
        response_holder.update({"status": status, "body_size": len(raw), "wire_model": wire_model, "url_path": official_url_path(response_url)})

    page.on("response", on_response)
    started = time.perf_counter()
    try:
        official_fill_prompt(page, prompt)
        before_expected_count = official_exact_visible_text_count(page, expected_text)
        if not official_click_run(page, helpers):
            raise AssertionError("official AI Studio run button not found")
        first_visible_ms: int | None = None
        deadline = time.perf_counter() + (timeout_ms / 1000)
        while time.perf_counter() < deadline:
            if first_visible_ms is None and official_exact_visible_text_count(page, expected_text) > before_expected_count:
                first_visible_ms = round((time.perf_counter() - started) * 1000)
            if response_holder and int(response_holder.get("status") or 0) != 200:
                break
            if response_holder and first_visible_ms is not None:
                break
            page.wait_for_timeout(500)
        complete_ms = round((time.perf_counter() - started) * 1000)
        if response_holder.get("error"):
            return {
                "result": "fail",
                "expected_text": expected_text,
                "failure_reason": str(response_holder["error"]),
                "observed_ignored_responses": observed,
                "time_to_first_visible_token_ms": first_visible_ms,
                "time_to_complete_ms": complete_ms,
            }
        if not response_holder:
            raise AssertionError(f"official AI Studio response timeout; observed={observed[:5]}")
        status = int(response_holder.get("status") or 0)
        if status != 200:
            return {
                "result": "fail",
                "expected_text": expected_text,
                "status": status,
                "wire_model": response_holder.get("wire_model"),
                "url_path": response_holder.get("url_path"),
                "body_size": response_holder.get("body_size"),
                "body_preview": response_holder.get("body_preview"),
                "failure_reason": f"official AI Studio GenerateContent returned HTTP {status}",
                "time_to_first_visible_token_ms": first_visible_ms,
                "time_to_complete_ms": complete_ms,
                "observed_ignored_responses": observed,
            }
        if first_visible_ms is None:
            return {
                "result": "fail",
                "expected_text": expected_text,
                "status": status,
                "wire_model": response_holder.get("wire_model"),
                "url_path": response_holder.get("url_path"),
                "body_size": response_holder.get("body_size"),
                "failure_reason": "official AI Studio exact visible assistant text did not appear",
                "time_to_first_visible_token_ms": first_visible_ms,
                "time_to_complete_ms": complete_ms,
                "observed_ignored_responses": observed,
            }
        return {
            "result": "pass",
            "expected_text": expected_text,
            "status": status,
            "wire_model": response_holder.get("wire_model"),
            "url_path": response_holder.get("url_path"),
            "body_size": response_holder.get("body_size"),
            "time_to_first_visible_token_ms": first_visible_ms,
            "time_to_complete_ms": complete_ms,
            "observed_ignored_responses": observed,
        }
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


@contextmanager
def official_direct_browser(playwright_browser, *, headless: bool):
    engine = os.environ.get("HOST_OFFICIAL_BASELINE_ENGINE", "camoufox").strip().lower()
    if engine != "camoufox":
        if playwright_browser is None:
            raise RuntimeError("Chromium official baseline requires an existing Playwright browser")
        yield playwright_browser, "chromium"
        return
    from camoufox.sync_api import Camoufox
    from aistudio_api.infrastructure.gateway.native_ui_sender import _browser_options

    options = dict(_browser_options())
    options["headless"] = headless
    options["main_world_eval"] = True
    with Camoufox(**options) as camoufox_browser:
        yield camoufox_browser, "camoufox"


def run_official_ai_studio_baseline(browser, helpers: dict[str, object], accounts_dir: Path, model: str, artifact_dir: Path, actions: list[str]) -> dict[str, object]:
    account_attempts: list[dict[str, object]] = []
    headless = env_bool("HOST_UI_SMOKE_HEADLESS", False)
    with official_direct_browser(browser, headless=headless) as (official_browser, official_browser_engine):
        for account_id, auth_file in copied_account_auth_files(accounts_dir):
            for authuser_candidate, _authuser_url in official_authuser_chat_candidates(helpers, model):
                official_console_errors: list[str] = []
                official_network_events: list[dict[str, object]] = []
                measurement_network_enabled = {"value": False}
                official_context = official_browser.new_context(
                    storage_state=str(auth_file),
                    service_workers="block",
                    viewport={"width": 1440, "height": 960},
                )
                official_page = official_context.new_page()
                official_page.on(
                    "console",
                    lambda message, errors=official_console_errors: errors.append(f"{message.type}:{message.text}") if message.type == "error" else None,
                )

                def on_official_response(response, events=official_network_events, enabled=measurement_network_enabled):
                    if enabled["value"] and official_is_generate_content_response_url(response.url):
                        events.append({"url_path": official_url_path(response.url), "status": response.status})

                official_page.on("response", on_official_response)
                account_result: dict[str, object]
                try:
                    official_open_failures = official_open_chat(official_page, helpers, timeout_ms=180_000, required_authuser=authuser_candidate, model=model)
                    official_model_selection = select_official_ai_studio_model(official_page, model, helpers, timeout_ms=180_000)
                    official_warmup_samples: list[dict[str, object]] = []
                    official_samples: list[dict[str, object]] = []
                    official_discarded_samples: list[dict[str, object]] = []
                    if official_model_selection.get("ok") is True:
                        warmup_text = f"nexus-official-aistudio-warmup-ok-{int(time.time())}"
                        official_warmup_samples.append(run_official_ai_studio_sample(official_page, helpers, model, warmup_text, timeout_ms=180_000))
                        measurement_network_enabled["value"] = True
                        for attempt_index in range(5):
                            if len(official_samples) >= 3:
                                break
                            expected_text = f"nexus-official-aistudio-baseline-ok-{int(time.time())}-{attempt_index + 1}"
                            sample = run_official_ai_studio_sample(official_page, helpers, model, expected_text)
                            if sample.get("result") == "pass":
                                official_samples.append(sample)
                            else:
                                official_discarded_samples.append(sample)
                    disallowed_errors = official_disallowed_errors(official_console_errors, official_network_events)
                    failure_reasons = official_baseline_failure_reasons(
                        official_model_selection,
                        official_samples,
                        disallowed_errors,
                        official_discarded_samples,
                    )
                    screenshot_name = (
                        "host-ui-official-aistudio-baseline.png"
                        if not failure_reasons
                        else f"host-ui-official-aistudio-baseline-{account_id}-u{authuser_candidate}.png"
                    )
                    official_page.screenshot(path=str(artifact_dir / screenshot_name), full_page=True)
                    account_result = {
                        "result": "fail" if failure_reasons else "pass",
                        "tested_by": "headed_camoufox_visible" if official_browser_engine == "camoufox" else "headed_playwright_visible",
                        "official_browser_engine": official_browser_engine,
                        "account_id": account_id,
                        "authuser_candidate": authuser_candidate,
                        "model": model,
                        "model_selection": official_model_selection,
                        "open_chat_failures": official_open_failures,
                        "warmup_samples": official_warmup_samples,
                        "samples": official_samples,
                        "discarded_samples": official_discarded_samples,
                        "sample_attempt_count": len(official_samples) + len(official_discarded_samples),
                        "sample_statuses": [sample.get("status") for sample in official_samples],
                        "failure_reasons": failure_reasons,
                        "disallowed_errors": disallowed_errors,
                        "console_errors": official_console_errors,
                        "network_events": official_network_events[-20:],
                        "screenshot": screenshot_name,
                        "page_url": official_page.url,
                        "url_path": urlparse(official_page.url).path,
                        "authuser": url_authuser(official_page.url),
                    }
                except Exception as exc:
                    try:
                        screenshot_name = f"host-ui-official-aistudio-baseline-{account_id}-u{authuser_candidate}.png"
                        official_page.screenshot(path=str(artifact_dir / screenshot_name), full_page=True)
                    except Exception:
                        screenshot_name = ""
                    account_result = {
                        "result": "fail",
                        "tested_by": "headed_camoufox_visible" if official_browser_engine == "camoufox" else "headed_playwright_visible",
                        "official_browser_engine": official_browser_engine,
                        "account_id": account_id,
                        "authuser_candidate": authuser_candidate,
                        "model": model,
                        "failure_reasons": [f"official_baseline_exception={type(exc).__name__}: {str(exc)[:240]}"],
                        "samples": [],
                        "sample_statuses": [],
                        "console_errors": official_console_errors,
                        "network_events": official_network_events[-20:],
                        "screenshot": screenshot_name,
                        "page_url": official_page.url,
                        "url_path": urlparse(official_page.url).path,
                        "authuser": url_authuser(official_page.url),
                    }
                finally:
                    official_context.close()
                account_attempts.append(account_result)
                if account_result.get("result") == "pass":
                    account_result["candidate_results"] = [dict(attempt) for attempt in account_attempts]
                    actions.append(
                        "official AI Studio visible baseline "
                        f"engine={official_browser_engine} account={account_id} authuser={authuser_candidate} model selection and 3 samples"
                    )
                    return account_result

    return {
        "result": "fail",
        "tested_by": "headed_playwright_visible",
        "model": model,
        "account_id": account_attempts[-1].get("account_id") if account_attempts else "",
        "failure_reasons": ["no copied account produced a valid official AI Studio baseline"],
        "samples": [],
        "sample_statuses": [],
        "screenshot": account_attempts[-1].get("screenshot") if account_attempts else "",
        "screenshots": [attempt.get("screenshot") for attempt in account_attempts if attempt.get("screenshot")],
        "candidate_results": account_attempts,
    }


def compare_performance_budget(
    official_result: dict[str, object],
    local_samples: list[dict[str, object]],
    account_alignment: dict[str, object] | None = None,
) -> dict[str, object]:
    official_samples = list(official_result.get("samples") or [])
    local_valid = [sample for sample in local_samples if sample.get("time_to_first_visible_token_ms") is not None]
    if official_result.get("result") != "pass":
        return {"result": "fail", "reason": "official AI Studio baseline is not valid", "official_result": official_result, "local_samples": local_samples, "account_alignment": account_alignment or {}}
    if account_alignment and account_alignment.get("ok") is not True:
        return {"result": "fail", "reason": "local performance account was not aligned to official account", "official_result": official_result, "local_samples": local_samples, "account_alignment": account_alignment}
    if account_alignment and official_result.get("account_id") != account_alignment.get("account_id"):
        return {"result": "fail", "reason": "official/local account ids differ", "official_result": official_result, "local_samples": local_samples, "account_alignment": account_alignment}
    if account_alignment and int(account_alignment.get("success_delta") or 0) < len(local_valid):
        return {"result": "fail", "reason": "aligned account did not record every local performance success", "official_result": official_result, "local_samples": local_samples, "account_alignment": account_alignment}
    bad_official_statuses = [sample.get("status") for sample in official_samples if int(sample.get("status") or 0) != 200]
    if bad_official_statuses:
        return {"result": "fail", "reason": f"official samples contained non-200 statuses: {bad_official_statuses}", "official_result": official_result, "local_samples": local_samples, "account_alignment": account_alignment or {}}
    if not official_samples or len(local_valid) < len(official_samples):
        return {"result": "fail", "reason": "missing official or local performance samples", "official_result": official_result, "local_samples": local_samples, "account_alignment": account_alignment or {}}
    official_first = [int(sample["time_to_first_visible_token_ms"]) for sample in official_samples]
    official_complete = [int(sample["time_to_complete_ms"]) for sample in official_samples]
    local_first = [int(sample["time_to_first_visible_token_ms"]) for sample in local_valid]
    local_complete = [int(sample["time_to_complete_ms"]) for sample in local_valid]
    official_first_median = float(median(official_first))
    official_complete_median = float(median(official_complete))
    local_first_median = float(median(local_first))
    local_complete_median = float(median(local_complete))
    first_budget = max(official_first_median * 2, official_first_median + 3000)
    complete_budget = official_complete_median * 1.5 + 5000
    first_pass = local_first_median <= first_budget
    complete_pass = local_complete_median <= complete_budget
    return {
        "result": "pass" if first_pass and complete_pass else "fail",
        "official_result": official_result,
        "local_samples": local_samples,
        "official_median_first_token_ms": official_first_median,
        "official_median_complete_ms": official_complete_median,
        "local_median_first_token_ms": local_first_median,
        "local_median_complete_ms": local_complete_median,
        "first_token_budget_ms": first_budget,
        "complete_budget_ms": complete_budget,
        "first_token_budget_pass": first_pass,
        "complete_budget_pass": complete_pass,
        "account_alignment": account_alignment or {},
    }


def read_openai_compat_credentials(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise AssertionError(f"OpenAI-compatible key file is missing: {path}")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    base_url = os.environ.get("OPENAI_COMPAT_BASE_URL", "").strip()
    token = os.environ.get("OPENAI_COMPAT_API_KEY", "").strip()
    for line in lines:
        if line.startswith("OPENAI_BASE_URL="):
            base_url = line.split("=", 1)[1].strip()
        elif line.startswith("OPENAI_API_KEY="):
            token = line.split("=", 1)[1].strip()
        elif line.startswith(("http://", "https://")) and not base_url:
            base_url = line
        elif not token:
            token = line
    if token.startswith("Bearer "):
        token = token.removeprefix("Bearer ").strip()
    base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
    if not token:
        raise AssertionError("OpenAI-compatible key file is empty")
    return base_url, token

def api_request_json(api_request, method: str, url: str, payload: dict[str, object] | None = None, timeout_ms: int = 30_000) -> dict[str, object]:
    method_name = method.lower()
    if payload is None:
        response = getattr(api_request, method_name)(url, timeout=timeout_ms)
    else:
        response = getattr(api_request, method_name)(
            url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=timeout_ms,
        )
    text = response.text()
    try:
        data = response.json()
    except Exception:
        data = {"raw": text[:500]}
    if response.status >= 400:
        raise AssertionError(f"API {method.upper()} {url} failed: status={response.status} body={text[:500]}")
    return data if isinstance(data, dict) else {"data": data}

def account_success_count(rotation_status: dict[str, object], account_id: str) -> int:
    accounts = rotation_status.get("accounts")
    if not isinstance(accounts, dict):
        return 0
    stats = accounts.get(account_id)
    if not isinstance(stats, dict):
        return 0
    return int(stats.get("success") or 0)

def align_local_studio_performance_account(api_request, base_url: str, account_id: str) -> dict[str, object]:
    if not account_id:
        return {"ok": False, "reason": "official account id is empty"}
    before_rotation = api_request_json(api_request, "GET", f"{base_url}/rotation")
    before_mode = str(before_rotation.get("mode") or "")
    before_cooldown = int(before_rotation.get("cooldown_seconds") or 60)
    mode_response = api_request_json(
        api_request,
        "POST",
        f"{base_url}/rotation/mode",
        {"mode": "exhaustion", "cooldown_seconds": before_cooldown},
    )
    activated = api_request_json(api_request, "POST", f"{base_url}/accounts/{account_id}/activate", timeout_ms=180_000)
    active = api_request_json(api_request, "GET", f"{base_url}/accounts/active")
    after_rotation = api_request_json(api_request, "GET", f"{base_url}/rotation")
    after_accounts = after_rotation.get("accounts") if isinstance(after_rotation.get("accounts"), dict) else {}
    account_stats = after_accounts.get(account_id, {}) if isinstance(after_accounts, dict) else {}
    active_id = str(active.get("id") or "")
    return {
        "ok": active_id == account_id and after_rotation.get("mode") == "exhaustion",
        "account_id": account_id,
        "previous_rotation_mode": before_mode,
        "previous_cooldown_seconds": before_cooldown,
        "mode_response": mode_response,
        "activated_account_id": activated.get("id"),
        "active_account_id": active_id,
        "before_success": account_success_count(before_rotation, account_id),
        "after_activation_success": account_success_count(after_rotation, account_id),
        "account_available_after_activation": bool(account_stats.get("is_available")) if isinstance(account_stats, dict) else False,
        "account_cooldown_remaining_after_activation": account_stats.get("cooldown_remaining") if isinstance(account_stats, dict) else None,
    }

def finalize_local_studio_performance_account_alignment(api_request, base_url: str, alignment: dict[str, object], sample_count: int) -> dict[str, object]:
    account_id = str(alignment.get("account_id") or "")
    if not account_id:
        return {**alignment, "ok": False, "reason": "official account id is empty"}
    after_rotation = api_request_json(api_request, "GET", f"{base_url}/rotation")
    active = api_request_json(api_request, "GET", f"{base_url}/accounts/active")
    after_success = account_success_count(after_rotation, account_id)
    before_success = int(alignment.get("before_success") or 0)
    success_delta = after_success - before_success
    active_id = str(active.get("id") or "")
    return {
        **alignment,
        "ok": bool(alignment.get("ok")) and active_id == account_id and success_delta >= sample_count,
        "active_account_id_after_samples": active_id,
        "after_success": after_success,
        "success_delta": success_delta,
        "expected_success_delta_at_least": sample_count,
    }


def visible_text(locator, fallback: str = "") -> str:
    try:
        return (locator.inner_text(timeout=2_000) or "").strip()
    except Exception:
        return fallback


def first_visible_text(locator, fallback: str = "") -> str:
    try:
        count = locator.count()
    except Exception:
        return fallback
    for index in range(count):
        candidate = locator.nth(index)
        if locator_is_visible(candidate, timeout_ms=300):
            text = visible_text(candidate)
            if text:
                return text
    return fallback


def visible_texts(locator) -> list[str]:
    texts: list[str] = []
    try:
        count = locator.count()
    except Exception:
        return texts
    for index in range(count):
        candidate = locator.nth(index)
        if locator_is_visible(candidate, timeout_ms=300):
            text = visible_text(candidate)
            if text:
                texts.append(text)
    return texts


def wait_for_visible_count(locator, minimum: int, timeout: int = 30_000) -> int:
    if minimum <= 0:
        return locator.count()
    expect(locator.nth(minimum - 1)).to_be_visible(timeout=timeout)
    return locator.count()


def wait_for_count_below(page, locator, previous_count: int, timeout_ms: int = 30_000) -> int:
    deadline = time.perf_counter() + (timeout_ms / 1000)
    last_count = locator.count()
    while time.perf_counter() < deadline:
        last_count = locator.count()
        if last_count < previous_count:
            return last_count
        page.wait_for_timeout(500)
    raise AssertionError(f"locator count did not fall below {previous_count}; last_count={last_count}")


def wait_for_count_above(page, locator, previous_count: int, timeout_ms: int = 60_000) -> int:
    deadline = time.perf_counter() + (timeout_ms / 1000)
    last_count = locator.count()
    while time.perf_counter() < deadline:
        last_count = locator.count()
        if last_count > previous_count:
            return last_count
        page.wait_for_timeout(500)
    raise AssertionError(f"locator count did not rise above {previous_count}; last_count={last_count}")


def locator_is_visible(locator, timeout_ms: int = 2_000) -> bool:
    try:
        expect(locator).to_be_visible(timeout=timeout_ms)
        return True
    except (AssertionError, PlaywrightTimeoutError):
        return False


def current_button_enabled_text(toggle_button) -> str:
    text = visible_text(toggle_button)
    return "on" if text == "开启" else "off" if text == "关闭" else text


def set_local_studio_runtime_toggle(page, label: str, enabled: bool, actions: list[str]) -> dict[str, object]:
    toggle = page.locator("#studio-page .runtime-toggle", has_text=label).first
    if not locator_is_visible(toggle, timeout_ms=4_000):
        return {"label": label, "available": False, "requested": enabled}
    button = toggle.locator("button").first
    expect(button).to_be_visible(timeout=10_000)
    current = current_button_enabled_text(button)
    requested = "on" if enabled else "off"
    if current != requested:
        button.click()
        expect(button).to_have_text("开启" if enabled else "关闭", timeout=10_000)
    actions.append(f"set local studio {label}={requested}")
    return {"label": label, "available": True, "value": requested}


def select_local_studio_native_option(page, field_label: str, value: str | None = None, preferred_values: list[str] | None = None) -> dict[str, object]:
    field = page.locator("#studio-page label.local-studio-field", has_text=field_label).filter(has=page.locator("select")).first
    if not locator_is_visible(field, timeout_ms=4_000):
        return {"label": field_label, "available": False}
    select = field.locator("select").first
    expect(select).to_be_visible(timeout=10_000)
    if select.is_disabled():
        return {"label": field_label, "available": False, "disabled": True, "value": select.input_value(timeout=2_000)}
    options = select.locator("option")
    option_values: list[str] = []
    option_texts: list[str] = []
    for index in range(options.count()):
        option = options.nth(index)
        option_values.append(option.get_attribute("value") or visible_text(option))
        option_texts.append(visible_text(option))
    target = value if value in option_values else None
    if target is None:
        for preferred in preferred_values or []:
            if preferred in option_values:
                target = preferred
                break
    if target is None and option_values:
        target = option_values[0]
    if target is None:
        raise AssertionError(f"no native select options for {field_label}")
    select.select_option(value=target)
    expect(select).to_have_value(target, timeout=10_000)
    return {"label": field_label, "available": True, "value": target, "options": option_values, "option_texts": option_texts}


def set_local_studio_image_tool(page, enabled: bool, actions: list[str]) -> dict[str, object]:
    toggle = page.locator("#studio-page .local-studio-settings .request-switch").first
    if not locator_is_visible(toggle, timeout_ms=4_000):
        return {"available": False, "requested": enabled}
    current = "on" if "开启" in visible_text(toggle) else "off"
    requested = "on" if enabled else "off"
    if current != requested:
        toggle.click()
        expect(toggle).to_contain_text("开启" if enabled else "关闭", timeout=10_000)
    actions.append(f"set local studio image tool={requested}")
    return {"available": True, "value": requested}


def assert_local_studio_no_cache_or_residual_state(page) -> dict[str, object]:
    expect(page.locator("#studio-page .local-studio-message.is-assistant.is-pending")).to_be_hidden(timeout=30_000)
    textarea = page.locator("#studio-page textarea[placeholder*='Local Studio']")
    expect(textarea).to_be_enabled(timeout=30_000)
    page_text = visible_text(page.locator("#studio-page"))
    forbidden_markers = ["cache.hit", "cache namespace", "Cache Namespace", "cache hit"]
    visible_cache_markers = [marker for marker in forbidden_markers if marker.lower() in page_text.lower()]
    if visible_cache_markers:
        raise AssertionError(f"Local Studio exposed result-cache marker(s): {visible_cache_markers}")
    empty_assistant = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending)").filter(has_not=page.locator(".msg-body, .think-block, .local-studio-images, .msg-usage"))
    if empty_assistant.count() > 0:
        raise AssertionError("Local Studio left an empty assistant message card")
    return {"pending_hidden": True, "textarea_enabled": True, "cache_markers_absent": True}


def assert_secret_not_in_file(path: Path, secret: str, label: str) -> None:
    if secret and secret in path.read_text(encoding="utf-8", errors="replace"):
        raise AssertionError(f"OpenAI-compatible token leaked into {label}: {path}")


def navigate_visible_page(page, label: str, active_selector: str, action_name: str, actions: list[str]) -> None:
    page.get_by_role("button", name=label).click()
    expect(page.locator(active_selector)).to_be_visible(timeout=30_000)
    actions.append(action_name)


def click_select_option(page, field_label: str, option_text: str | None = None, preferred_prefixes: list[str] | None = None) -> str:
    field = page.locator(".local-studio-field", has_text=field_label).first
    field.locator(".cselect-btn").click()
    menu = page.locator(".cselect.open .cselect-menu").first
    expect(menu).to_be_visible(timeout=10_000)
    options = menu.locator(".cselect-opt:not([aria-disabled='true'])")
    count = options.count()
    if count <= 0:
        raise AssertionError(f"no selectable options for {field_label}")
    if option_text:
        matches = menu.locator(".cselect-opt:not([aria-disabled='true'])", has_text=option_text)
        if matches.count() > 0:
            matches.first.click()
            return option_text
    texts = [visible_text(options.nth(index)) for index in range(count)]
    for prefix in preferred_prefixes or []:
        for index, text in enumerate(texts):
            if text.startswith(prefix):
                options.nth(index).click()
                return text
    selected = texts[0]
    options.first.click()
    return selected


def load_local_studio_models_with_retries(page, actions: list[str], label: str, *, attempts: int = 3, timeout_ms: int = 180_000):
    statuses: list[int] = []
    button = page.get_by_role("button", name="加载模型")
    for attempt in range(1, attempts + 1):
        expect(button).to_be_enabled(timeout=30_000)
        with page.expect_response(lambda response: "/api/local-studio/models" in response.url, timeout=timeout_ms) as response_info:
            button.click()
        response = response_info.value
        statuses.append(int(response.status))
        actions.append(f"{label} models load attempt={attempt} status={response.status}")
        if response.status < 400:
            return response
        if attempt < attempts:
            page.wait_for_timeout(min(5_000, 1_000 * attempt))
    raise AssertionError(f"{label} model load failed after {attempts} attempts: statuses={statuses}")


def send_local_studio_message(
    page,
    prompt: str,
    label: str,
    *,
    expect_error: bool = False,
    timeout_ms: int = 420_000,
) -> dict[str, object]:
    textarea = page.locator("#studio-page textarea[placeholder*='Local Studio']")
    expect(textarea).to_be_visible(timeout=30_000)
    textarea.fill(prompt)
    send_button = page.locator("#studio-page .local-studio-compose-row button.send")
    expect(send_button).to_be_enabled(timeout=20_000)
    assistant_messages = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending)")
    assistant_bodies = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending) .msg-body:not(.error)")
    assistant_errors = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending) .msg-body.error")
    notice_errors = page.locator("#studio-page .notice.notice-error")
    before_assistant_count = assistant_messages.count()
    before_body_count = assistant_bodies.count()
    before_error_count = assistant_errors.count()
    before_notice_texts = set(visible_texts(notice_errors))
    started = time.perf_counter()
    pending_observed = False
    with page.expect_response(lambda response: "/api/local-studio/chat" in response.url, timeout=420_000) as response_info:
        send_button.click()
    chat_response = response_info.value
    pending_locator = page.locator("#studio-page .local-studio-message.is-assistant.is-pending")
    try:
        expect(pending_locator).to_be_visible(timeout=5_000)
        pending_observed = True
    except (AssertionError, PlaywrightTimeoutError):
        pending_observed = False
    first_visible_ms: int | None = None
    deadline = time.perf_counter() + (timeout_ms / 1000)
    outcome = "error" if chat_response.status >= 400 else "pending"
    assistant_text = ""
    error_text = ""
    if chat_response.status >= 400:
        error_deadline = min(deadline, time.perf_counter() + 10)
        while time.perf_counter() < error_deadline:
            new_notice = next((text for text in visible_texts(notice_errors) if text not in before_notice_texts), "")
            if new_notice:
                error_text = new_notice
                break
            if assistant_errors.count() > before_error_count:
                candidate = assistant_errors.nth(assistant_errors.count() - 1)
                if locator_is_visible(candidate, timeout_ms=500):
                    error_text = visible_text(candidate)
                    break
            page.wait_for_timeout(500)
        if not error_text:
            error_text = f"HTTP {chat_response.status}"
    else:
        while time.perf_counter() < deadline:
            if assistant_bodies.count() > before_body_count:
                candidate = assistant_bodies.nth(assistant_bodies.count() - 1)
                assistant_text = visible_text(candidate)
                if locator_is_visible(candidate, timeout_ms=500) and assistant_text:
                    first_visible_ms = round((time.perf_counter() - started) * 1000)
                    outcome = "success"
                    break
            if assistant_messages.count() > before_assistant_count and assistant_errors.count() > before_error_count:
                candidate = assistant_errors.nth(assistant_errors.count() - 1)
                if locator_is_visible(candidate, timeout_ms=500):
                    error_text = visible_text(candidate)
                    outcome = "error"
                    break
            new_notice = next((text for text in visible_texts(notice_errors) if text not in before_notice_texts), "")
            if new_notice:
                error_text = new_notice
                outcome = "error"
                break
            page.wait_for_timeout(500)
    if outcome == "pending":
        raise AssertionError(f"Local Studio message did not finish visibly: label={label}")
    expect(pending_locator).to_be_hidden(timeout=30_000)
    complete_ms = round((time.perf_counter() - started) * 1000)
    if not error_text:
        error_text = next((text for text in visible_texts(notice_errors) if text not in before_notice_texts), "")
    if expect_error and outcome != "error":
        raise AssertionError(f"expected Local Studio error for {label}, got {outcome}")
    if not expect_error and outcome != "success":
        raise AssertionError(f"expected Local Studio success for {label}, got error={error_text[:300]}")
    return {
        "prompt": prompt,
        "label": label,
        "chat_response_status": chat_response.status,
        "expected_error": expect_error,
        "outcome": outcome,
        "assistant_text_prefix": assistant_text[:500],
        "assistant_error_prefix": error_text[:500],
        "pending_observed": pending_observed,
        "time_to_first_visible_token_ms": first_visible_ms,
        "time_to_complete_ms": complete_ms,
    }


def open_request_logs(page, actions: list[str], action_name: str = "navigate #requests") -> int:
    with page.expect_response(lambda response: response.url.rstrip("/").endswith("/request-logs"), timeout=60_000):
        page.get_by_role("button", name="请求记录").click()
    expect(page.locator(".request-page.active")).to_be_visible(timeout=30_000)
    actions.append(action_name)
    rows = page.locator(".request-page.active .request-log-item")
    try:
        wait_for_visible_count(rows, 1, timeout=60_000)
    except PlaywrightTimeoutError:
        return 0
    return rows.count()


def exercise_request_log_ui(page, artifact_dir: Path, openai_token: str, repeated_prompt: str, before_repeat_count: int, actions: list[str]) -> dict[str, object]:
    after_repeat_count = open_request_logs(page, actions, "navigate #requests after repeated prompt")
    rows = page.locator(".request-page.active .request-log-item")
    if after_repeat_count < before_repeat_count + 2:
        raise AssertionError(
            f"request log rows did not increase by two repeated prompt sends: before={before_repeat_count} after={after_repeat_count}"
        )

    detail = page.locator(".request-page.active .request-detail-panel")
    matching_row_index: int | None = None
    for row_index in range(min(rows.count(), 12)):
        candidate = rows.nth(row_index)
        candidate.click()
        expect(detail).to_be_visible(timeout=30_000)
        try:
            expect(detail).to_contain_text(repeated_prompt, timeout=15_000)
            matching_row_index = row_index
            break
        except PlaywrightTimeoutError:
            continue
    if matching_row_index is None:
        raise AssertionError(f"no visible request-log detail contained repeated prompt: {repeated_prompt}")
    matched_row = rows.nth(matching_row_index)
    for phase_label in ("用户 → 后端", "后端 → AI Studio", "AI Studio → 后端", "后端 → 用户"):
        expect(detail).to_contain_text(phase_label, timeout=60_000)

    page.locator(".request-page.active .request-detail-head").get_by_role("button", name="复制完整 JSON").click()
    expect(page.locator(".toast.show")).to_contain_text("已复制", timeout=10_000)

    active_export_path = artifact_dir / "host-ui-request-log-active-export.json"
    with page.expect_download(timeout=30_000) as active_download_info:
        page.locator(".request-page.active .request-detail-head").get_by_role("button", name="导出当前").click()
    active_download_info.value.save_as(str(active_export_path))
    assert_secret_not_in_file(active_export_path, openai_token, "active request log export")

    matched_row.locator(".request-log-check input").check()
    selected_export_path = artifact_dir / "host-ui-request-log-selected-export.json"
    with page.expect_download(timeout=30_000) as selected_download_info:
        page.locator(".request-page.active .request-list-panel").get_by_role("button", name="导出所选").click()
    selected_download_info.value.save_as(str(selected_export_path))
    assert_secret_not_in_file(selected_export_path, openai_token, "selected request log export")

    active_id_before_delete = visible_text(page.locator(".request-page.active .request-metrics .metric-wide strong").first)
    before_delete_count = rows.count()
    with page.expect_response(lambda response: "/request-logs/groups/delete" in response.url, timeout=60_000) as delete_response_info:
        page.locator(".request-page.active .request-detail-head").get_by_role("button", name="删除当前").click()
    delete_response = delete_response_info.value
    after_delete_count = wait_for_count_below(page, rows, before_delete_count, timeout_ms=60_000)
    page.screenshot(path=str(artifact_dir / "host-ui-request-log-crud.png"), full_page=True)
    actions.append("request log detail copy export selected-export delete")
    return {
        "before_repeat_count": before_repeat_count,
        "after_repeat_count": after_repeat_count,
        "before_delete_count": before_delete_count,
        "after_delete_count": after_delete_count,
        "deleted_active_id": active_id_before_delete,
        "matched_row_index": matching_row_index,
        "delete_status": delete_response.status,
        "active_export": active_export_path.name,
        "selected_export": selected_export_path.name,
        "phase_labels_asserted": ["client_request", "upstream_request", "upstream_response", "client_response"],
        "repeated_prompt_visible_in_detail": True,
    }


def exercise_account_health_ui(page, artifact_dir: Path, actions: list[str]) -> dict[str, object]:
    page.get_by_role("button", name="账号管理").click()
    expect(page.locator(".page-wrap.active .account-table-panel")).to_be_visible(timeout=30_000)
    rows = page.locator(".page-wrap.active .account-table-panel tbody tr").filter(has=page.locator("button", has_text="健康检查"))
    account_count = wait_for_visible_count(rows, 1, timeout=60_000)
    active_rows = rows.filter(has_text="默认账号")
    first_row = active_rows.first if active_rows.count() else rows.first
    account_id = visible_text(first_row.locator(".acct-id").first)
    health_button = first_row.get_by_role("button", name="健康检查")
    with page.expect_response(lambda response: "/accounts/" in response.url and response.url.endswith("/test"), timeout=120_000) as health_response_info:
        health_button.click()
    health_response = health_response_info.value
    try:
        health_payload = health_response.json()
    except Exception:
        health_payload = {}
    expect(first_row.get_by_role("button", name="健康检查")).to_be_visible(timeout=120_000)
    page.screenshot(path=str(artifact_dir / "host-ui-accounts-health.png"), full_page=True)
    actions.append("account health check via UI")
    return {
        "account_count": account_count,
        "checked_account_id": account_id,
        "health_response_status": health_response.status,
        "health_ok": bool(health_payload.get("ok")),
        "health_status": health_payload.get("status"),
        "health_cell_text": visible_text(first_row.locator("td").nth(5))[:300],
    }


def exercise_local_studio_attachment_preview_ui(page, artifact_dir: Path, actions: list[str]) -> dict[str, object]:
    upload_path = artifact_dir / "host-ui-local-studio-attachment.txt"
    upload_path.write_text("Nexus Studio system test attachment.\n", encoding="utf-8")
    attach_button = page.locator("#studio-page .local-studio-compose-row .attach-btn")
    expect(attach_button).to_be_visible(timeout=30_000)
    with page.expect_file_chooser(timeout=30_000) as chooser_info:
        attach_button.click()
    chooser_info.value.set_files(str(upload_path))
    chip = page.locator("#studio-page .local-studio-file-strip .chat-attachment", has_text=upload_path.name).first
    expect(chip).to_be_visible(timeout=30_000)
    chip.locator(".thumb-remove").click()
    expect(chip).to_be_hidden(timeout=30_000)
    actions.append("local studio attachment chooser preview remove")
    return {"file_name": upload_path.name, "preview_visible": True, "remove_visible": True, "removed": True}


def rerun_local_studio_first_user_turn(page, label: str) -> dict[str, object]:
    before_assistant_count = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending)").count()
    started = time.perf_counter()
    rerun_button = page.locator("#studio-page .local-studio-message.is-user [aria-label='从此处重跑']").first
    expect(rerun_button).to_be_visible(timeout=30_000)
    with page.expect_response(lambda response: "/api/local-studio/chat" in response.url, timeout=420_000) as response_info:
        rerun_button.click()
    response = response_info.value
    expect(page.locator("#studio-page .local-studio-message.is-assistant.is-pending")).to_be_hidden(timeout=420_000)
    assistant = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending) .msg-body:not(.error)").last
    expect(assistant).to_be_visible(timeout=60_000)
    after_assistant_count = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending)").count()
    return {
        "label": label,
        "chat_response_status": response.status,
        "before_assistant_count": before_assistant_count,
        "after_assistant_count": after_assistant_count,
        "assistant_text_prefix": visible_text(assistant)[:500],
        "time_to_complete_ms": round((time.perf_counter() - started) * 1000),
    }


def exercise_local_studio_conversation_crud_ui(page, artifact_dir: Path, actions: list[str]) -> dict[str, object]:
    title = f"Host UI conversation {int(time.time() * 1000)}"
    batch_title_prefix = f"Host UI batch {int(time.time() * 1000)}"
    conversation_rows = page.locator("#studio-page .local-studio-conversation")
    create_button = page.locator("#studio-page .local-studio-history").get_by_role("button", name="新建")
    with page.expect_response(lambda response: response.url.endswith("/api/local-studio/conversations") and response.request.method == "POST", timeout=60_000) as create_response_info:
        create_button.click()
    create_response = create_response_info.value
    expect(page.locator("#studio-page .local-studio-empty-chat")).to_be_visible(timeout=30_000)
    sent = send_local_studio_message(page, f"Reply with exactly: nexus-host-ui-conversation-ok-{int(time.time())}", "conversation-crud-seed")
    active_row = page.locator("#studio-page .local-studio-conversation.active").first
    expect(active_row).to_be_visible(timeout=30_000)
    active_row.get_by_role("button", name="重命名").click()
    rename_input = active_row.locator(".local-studio-rename .inline-input")
    expect(rename_input).to_be_visible(timeout=30_000)
    rename_input.fill(title)
    with page.expect_response(lambda response: "/api/local-studio/conversations/" in response.url and response.request.method == "PATCH", timeout=60_000) as rename_response_info:
        active_row.get_by_role("button", name="保存").click()
    rename_response = rename_response_info.value
    expect(page.locator("#studio-page .local-studio-conversation", has_text=title)).to_be_visible(timeout=30_000)
    page.reload(wait_until="networkidle", timeout=90_000)
    expect(page.locator("#studio-page.active")).to_be_visible(timeout=30_000)
    restored_row = page.locator("#studio-page .local-studio-conversation", has_text=title).first
    expect(restored_row).to_be_visible(timeout=60_000)
    with page.expect_response(lambda response: "/api/local-studio/conversations/" in response.url and response.request.method == "GET", timeout=60_000):
        restored_row.locator(".local-studio-conversation-main").click()
    expect(page.locator("#studio-page .local-studio-message.is-user")).to_be_visible(timeout=30_000)
    rerun = rerun_local_studio_first_user_turn(page, "conversation-rerun")
    with page.expect_response(lambda response: "/api/local-studio/conversations/" in response.url and response.request.method == "DELETE", timeout=60_000) as delete_response_info:
        page.locator("#studio-page .local-studio-conversation", has_text=title).first.get_by_role("button", name="删除").click()
    delete_response = delete_response_info.value
    expect(page.locator("#studio-page .local-studio-conversation", has_text=title)).to_be_hidden(timeout=30_000)

    created_batch_titles: list[str] = []
    for index in range(2):
        batch_title = f"{batch_title_prefix} {index + 1}"
        with page.expect_response(lambda response: response.url.endswith("/api/local-studio/conversations") and response.request.method == "POST", timeout=60_000):
            create_button.click()
        batch_row = page.locator("#studio-page .local-studio-conversation.active").first
        expect(batch_row).to_be_visible(timeout=30_000)
        batch_row.get_by_role("button", name="重命名").click()
        batch_row.locator(".local-studio-rename .inline-input").fill(batch_title)
        with page.expect_response(lambda response: "/api/local-studio/conversations/" in response.url and response.request.method == "PATCH", timeout=60_000):
            batch_row.get_by_role("button", name="保存").click()
        expect(page.locator("#studio-page .local-studio-conversation", has_text=batch_title)).to_be_visible(timeout=30_000)
        created_batch_titles.append(batch_title)
    for batch_title in created_batch_titles:
        page.locator("#studio-page .local-studio-conversation", has_text=batch_title).first.locator(".local-studio-check input").check()
    with page.expect_response(lambda response: response.url.endswith("/api/local-studio/conversations/bulk-delete"), timeout=60_000) as bulk_delete_response_info:
        page.locator("#studio-page .local-studio-history-actions").get_by_role("button", name="批量删除").click()
    bulk_delete_response = bulk_delete_response_info.value
    for batch_title in created_batch_titles:
        expect(page.locator("#studio-page .local-studio-conversation", has_text=batch_title)).to_be_hidden(timeout=30_000)
    page.screenshot(path=str(artifact_dir / "host-ui-local-studio-conversation-crud.png"), full_page=True)
    actions.append("local studio conversation create rename refresh restore rerun delete bulk-delete")
    return {
        "create_status": create_response.status,
        "seed_send": sent,
        "rename_status": rename_response.status,
        "restore_after_reload_visible": True,
        "rerun": rerun,
        "delete_status": delete_response.status,
        "bulk_delete_status": bulk_delete_response.status,
        "bulk_titles_deleted": created_batch_titles,
    }


def exercise_local_studio_invalid_provider_recovery_ui(page, base_url: str, openai_base_url: str, actions: list[str]) -> dict[str, object]:
    base_input = page.locator("#studio-page input[placeholder='https://api.openai.com/v1']")
    timeout_input = page.locator("#studio-page input[type='number']").first
    expect(base_input).to_be_visible(timeout=30_000)
    base_input.fill("http://127.0.0.1:9/v1")
    page.keyboard.press("Tab")
    timeout_input.fill("1")
    page.keyboard.press("Tab")
    set_local_studio_runtime_toggle(page, "Stream", False, actions)
    error_result = send_local_studio_message(
        page,
        f"This should fail with a controlled provider error {int(time.time())}",
        "openai-compatible-invalid-provider",
        expect_error=True,
        timeout_ms=75_000,
    )
    health_response = page.request.get(f"{base_url}/health", timeout=30_000)
    base_input.fill(openai_base_url)
    page.keyboard.press("Tab")
    timeout_input.fill("181")
    page.keyboard.press("Tab")
    recovery_result = send_local_studio_recovery_message_with_retry(page, actions)
    residual_state = assert_local_studio_no_cache_or_residual_state(page)
    actions.append("local studio invalid provider controlled error health recovery")
    return {
        "error_result": error_result,
        "health_status_after_error": health_response.status,
        "recovery_result": recovery_result,
        "residual_state": residual_state,
    }


def send_local_studio_recovery_message_with_retry(page, actions: list[str]) -> dict[str, object]:
    transient_markers = (
        "connecterror",
        "readerror",
        "remoteprotocolerror",
        "connecttimeout",
        "unexpected_eof_while_reading",
        "eof occurred in violation of protocol",
        "connection reset",
        "temporarily unavailable",
    )
    last_error: AssertionError | None = None
    for attempt_index in range(3):
        try:
            return send_local_studio_message(
                page,
                f"Reply with exactly: nexus-host-ui-recovery-ok-{int(time.time())}-{attempt_index + 1}",
                "openai-compatible-recovery-after-invalid-provider",
            )
        except AssertionError as exc:
            lowered = str(exc).lower()
            if not any(marker in lowered for marker in transient_markers):
                raise
            last_error = exc
            actions.append(f"retry openai-compatible recovery after transient upstream error attempt={attempt_index + 1}")
            page.wait_for_timeout(2_000 + attempt_index * 2_000)
    assert last_error is not None
    raise last_error


def exercise_playground_base_chat_ui(page, google_model: str, actions: list[str]) -> dict[str, object]:
    page.get_by_role("button", name="Playground").click()
    expect(page.locator("#chat-page.active")).to_be_visible(timeout=30_000)
    interface = page.locator(".api-toolbar .cselect-btn").first
    expect(interface).to_be_visible(timeout=30_000)
    interface.click()
    menu = page.locator(".cselect.open .cselect-menu").first
    expect(menu).to_be_visible(timeout=10_000)
    gemini_option = menu.locator(".cselect-opt", has_text="Gemini").first
    if gemini_option.count() > 0:
        gemini_option.click()
    else:
        menu.locator(".cselect-opt").first.click()
    with page.expect_response(lambda response: response_path(response) in {"/v1beta/models", "/v1/models"} and response.request.method == "GET", timeout=180_000) as models_response_info:
        page.locator("#chat-page .model-refresh-btn").click()
    models_response = models_response_info.value
    model_button = page.locator("#chat-page .playground-model-select .cselect-btn")
    model_button.click()
    model_menu = page.locator(".cselect.open .cselect-menu").first
    expect(model_menu).to_be_visible(timeout=30_000)
    preferred = model_menu.locator(".cselect-opt", has_text=google_model).first if google_model else model_menu.locator(".cselect-opt").first
    if preferred.count() > 0:
        selected_model = visible_text(preferred)
        preferred.click()
    else:
        selected_model = visible_text(model_menu.locator(".cselect-opt").first)
        model_menu.locator(".cselect-opt").first.click()
    prompt = f"Reply with exactly: nexus-host-ui-playground-ok-{int(time.time())}"
    textarea = page.locator("#chat-page textarea[placeholder*='AI Studio']")
    textarea.fill(prompt)
    started = time.perf_counter()
    with page.expect_response(lambda response: response_path(response).startswith("/v1beta/models/") or response_path(response) in {"/v1/responses", "/v1/chat/completions"}, timeout=420_000) as chat_response_info:
        page.locator("#chat-page button.send").click()
    chat_response = chat_response_info.value
    assistant = page.locator("#chat-page .msg.msg-ai .msg-body:not(.error)").last
    expect(assistant).to_be_visible(timeout=420_000)
    expect(page.locator("#chat-page textarea[placeholder*='AI Studio']")).to_be_enabled(timeout=30_000)
    actions.append("playground gemini model load basic visible send")
    return {
        "models_status": models_response.status,
        "chat_response_status": chat_response.status,
        "selected_model": selected_model,
        "assistant_text_prefix": visible_text(assistant)[:500],
        "time_to_complete_ms": round((time.perf_counter() - started) * 1000),
    }


def exercise_provider_manager_crud_ui(page, artifact_dir: Path, actions: list[str]) -> dict[str, object]:
    provider_name = f"System Test PM UI {int(time.time() * 1000)}"
    edited_name = provider_name + " Edited"
    model_id = "pm-ui-model"
    model_aliases = "pm-fast, pm-default"
    page.get_by_role("button", name="Provider Manager").click()
    expect(page.locator(".provider-page.active")).to_be_visible(timeout=30_000)
    expect(page.locator(".provider-list-panel")).to_be_visible(timeout=30_000)
    actions.append("navigate #providers")

    built_in_row = page.locator(".provider-list-panel .provider-row", has_text="Google").first
    expect(built_in_row).to_be_visible(timeout=30_000)
    built_in_row.click()
    detail_panel = page.locator(".provider-detail-panel")
    expect(detail_panel).to_contain_text("内置", timeout=30_000)
    provider_action_buttons = detail_panel.locator(".provider-actions button")
    expect(provider_action_buttons.nth(0)).to_be_disabled(timeout=10_000)
    expect(provider_action_buttons.nth(1)).to_be_disabled(timeout=10_000)
    expect(provider_action_buttons.nth(2)).to_be_disabled(timeout=10_000)
    built_in_boundary = {
        "google_provider_visible": True,
        "base_url_shows_builtin": True,
        "edit_disabled": True,
        "toggle_disabled": True,
        "delete_disabled": True,
    }

    page.get_by_role("button", name="新建 provider").click()
    modal = page.locator(".provider-modal")
    expect(modal).to_be_visible(timeout=30_000)
    modal.locator("label", has_text="Name").locator("input").fill(provider_name)
    modal.locator("label", has_text="Base URL").locator("input").fill("https://provider-manager-ui.invalid/v1")
    modal.locator("label", has_text="Timeout").locator("input").fill("123")
    modal.locator("label", has_text="Health").locator("select").select_option("ready")
    token_input = modal.locator("label", has_text="API Token").locator("input")
    token_input.fill("pm-ui-token-not-secret")
    expect(token_input).to_have_attribute("type", "password", timeout=10_000)
    modal.locator(".provider-token-toggle").click()
    expect(token_input).to_have_attribute("type", "text", timeout=10_000)
    modal.locator(".provider-token-toggle").click()
    expect(token_input).to_have_attribute("type", "password", timeout=10_000)
    modal.locator(".provider-manual-add input[placeholder='model id']").fill(model_id)
    modal.locator(".provider-manual-add").get_by_role("button", name="添加").click()
    draft_model = modal.locator(".provider-draft-model").first
    expect(draft_model).to_be_visible(timeout=10_000)
    expect(draft_model.locator("label", has_text="Model ID").locator("input")).to_have_value(model_id, timeout=10_000)
    draft_model.locator("label", has_text="Aliases").locator("input").fill(model_aliases)
    draft_model.get_by_role("button", name="默认文本").click()
    with page.expect_response(
        lambda response: response.url.endswith("/api/provider-manager/providers") and response.request.method == "POST",
        timeout=60_000,
    ) as create_response_info:
        modal.get_by_role("button", name="保存 provider").click()
    create_response = create_response_info.value
    expect(page.locator(".provider-list-panel .provider-row", has_text=provider_name)).to_be_visible(timeout=30_000)
    page.locator(".provider-list-panel .provider-row", has_text=provider_name).first.click()
    expect(page.locator(".provider-models-panel")).to_contain_text(model_id, timeout=30_000)
    expect(page.locator(".provider-detail-panel")).to_contain_text("pm-fast", timeout=30_000)

    page.locator(".provider-list-panel .provider-row", has_text=provider_name).first.click()
    page.locator(".provider-detail-panel .provider-actions").get_by_role("button", name="编辑").click()
    expect(modal).to_be_visible(timeout=30_000)
    modal.locator("label", has_text="Name").locator("input").fill(edited_name)
    modal.locator("label", has_text="Timeout").locator("input").fill("124")
    with page.expect_response(
        lambda response: "/api/provider-manager/providers/" in response.url and response.request.method == "PATCH",
        timeout=60_000,
    ) as update_response_info:
        modal.get_by_role("button", name="保存 provider").click()
    update_response = update_response_info.value
    expect(page.locator(".provider-list-panel .provider-row", has_text=edited_name)).to_be_visible(timeout=30_000)

    page.locator(".provider-list-panel .provider-row", has_text=edited_name).first.click()
    with page.expect_response(lambda response: response.url.endswith("/enabled"), timeout=60_000) as toggle_response_info:
        page.locator(".provider-detail-panel .provider-actions").get_by_role("button", name="停用").click()
    toggle_response = toggle_response_info.value
    expect(page.locator(".provider-detail-panel")).to_contain_text("Disabled", timeout=30_000)

    with page.expect_response(
        lambda response: "/api/provider-manager/providers/" in response.url and response.request.method == "DELETE",
        timeout=60_000,
    ) as delete_response_info:
        page.locator(".provider-detail-panel .provider-actions").get_by_role("button", name="删除").click()
    delete_response = delete_response_info.value
    expect(page.locator(".provider-list-panel .provider-row", has_text=edited_name)).to_be_hidden(timeout=30_000)
    expect(page.locator(".provider-audit-panel")).to_contain_text("provider.deleted", timeout=30_000)
    page.screenshot(path=str(artifact_dir / "host-ui-provider-manager-crud.png"), full_page=True)
    actions.append("provider manager built-in boundary token-toggle aliases defaults audit custom CRUD")
    return {
        "provider_name": provider_name,
        "edited_name": edited_name,
        "model_id": model_id,
        "model_aliases": model_aliases,
        "built_in_boundary": built_in_boundary,
        "token_visibility_toggle": True,
        "manual_model_default_text": True,
        "audit_deleted_visible": True,
        "create_status": create_response.status,
        "update_status": update_response.status,
        "toggle_status": toggle_response.status,
        "delete_status": delete_response.status,
    }


def main() -> int:
    port = os.environ["AISTUDIO_PORT"]
    google_model = os.environ.get("SYSTEM_TEST_MODEL", "").strip()
    openai_model = os.environ.get("OPENAI_COMPAT_TEXT_MODEL", "").strip()
    openai_key_file = Path(os.environ["OPENAI_COMPAT_KEY_FILE"])
    openai_base_url, openai_token = read_openai_compat_credentials(openai_key_file)
    artifact_dir = Path(os.environ["HOST_ARTIFACT_DIR"])
    accounts_dir = Path(os.environ["HOST_ACCOUNTS_DIR"])
    repo_src_dir = Path(os.environ["HOST_REPO_SRC_DIR"])
    artifact_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{port}"
    headless = env_bool("HOST_UI_SMOKE_HEADLESS", False)
    slow_mo_ms = max(0, env_int("HOST_UI_SMOKE_SLOW_MO_MS", 200 if not headless else 0))
    hold_seconds = max(0, env_int("HOST_UI_SMOKE_HOLD_SECONDS", 30 if not headless else 0))

    console_errors: list[str] = []
    network_events: list[dict[str, object]] = []
    actions: list[str] = [f"launch chromium headless={headless} slow_mo_ms={slow_mo_ms}"]
    provider_results: list[dict[str, object]] = []
    local_google_performance_samples: list[dict[str, object]] = []
    official_ai_studio_result: dict[str, object] = {}
    local_google_account_alignment: dict[str, object] = {}
    performance_comparison_result: dict[str, object] = {}
    runtime_controls: dict[str, dict[str, object]] = {}
    reasoning_controls: dict[str, dict[str, object]] = {}
    image_tool_controls: dict[str, dict[str, object]] = {}

    official_helpers = load_official_ai_studio_helpers(repo_src_dir)
    if os.environ.get("HOST_OFFICIAL_BASELINE_ENGINE", "camoufox").strip().lower() == "camoufox":
        official_ai_studio_result = run_official_ai_studio_baseline(None, official_helpers, accounts_dir, google_model, artifact_dir, actions)
        (artifact_dir / "host-official-aistudio-results.json").write_text(
            json.dumps(official_ai_studio_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        if not official_ai_studio_result:
            official_ai_studio_result = run_official_ai_studio_baseline(browser, official_helpers, accounts_dir, google_model, artifact_dir, actions)
            (artifact_dir / "host-official-aistudio-results.json").write_text(
                json.dumps(official_ai_studio_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        page = browser.new_page(viewport={"width": 1440, "height": 960}, accept_downloads=True)
        page.on(
            "console",
            lambda message: console_errors.append(f"{message.type}:{message.text}") if message.type == "error" else None,
        )
        page.on(
            "response",
            lambda response: network_events.append({"url": response.url, "status": response.status})
            if any(marker in response.url for marker in ("/api/local-studio/", "/request-logs", "/api/provider-manager", "/accounts", "/health"))
            else None,
        )

        page.goto(f"{base_url}/static/index.html#studio", wait_until="networkidle", timeout=90_000)
        expect(page.get_by_role("button", name="Local Studio")).to_be_visible(timeout=30_000)
        expect(page.locator("#studio-page.active")).to_be_visible(timeout=30_000)
        actions.append("open #studio")

        navigate_visible_page(page, "Playground", "#chat-page.active", "navigate #chat", actions)
        navigate_visible_page(page, "图片生成", ".image-page.active", "navigate #images", actions)
        navigate_visible_page(page, "请求记录", ".request-page.active", "navigate #requests initial", actions)
        navigate_visible_page(page, "系统配置", ".config-page.active", "navigate #config", actions)
        navigate_visible_page(page, "账号管理", ".page-wrap.active .account-table-panel", "navigate #accounts initial", actions)
        provider_manager_result = exercise_provider_manager_crud_ui(page, artifact_dir, actions)
        navigate_visible_page(page, "Local Studio", "#studio-page.active", "navigate back #studio", actions)

        google_provider = click_select_option(page, "Provider", "Google AI Studio")
        actions.append(f"select provider: {google_provider}")
        google_interface = click_select_option(page, "Interface", "OpenAI Responses")
        actions.append(f"select interface: {google_interface}")
        google_models_response = load_local_studio_models_with_retries(page, actions, "google")
        google_selected_model = click_select_option(page, "Conversation Model", google_model or None)
        actions.append(f"select google model: {google_selected_model}")
        runtime_controls["google_stream_on_performance"] = set_local_studio_runtime_toggle(page, "Stream", True, actions)
        runtime_controls["google_search_off_performance"] = set_local_studio_runtime_toggle(page, "Web search", False, actions)
        image_tool_controls["google_disabled_for_performance"] = set_local_studio_image_tool(page, False, actions)
        if official_ai_studio_result.get("result") == "pass":
            local_google_account_alignment = align_local_studio_performance_account(
                page.request,
                base_url,
                str(official_ai_studio_result.get("account_id") or ""),
            )
            actions.append(
                "align local studio google performance account="
                f"{local_google_account_alignment.get('account_id')} ok={local_google_account_alignment.get('ok')}"
            )
        else:
            local_google_account_alignment = {
                "ok": False,
                "reason": "official baseline invalid; local account alignment skipped",
                "account_id": official_ai_studio_result.get("account_id"),
            }
        for sample_index in range(3):
            expected_text = f"nexus-local-aistudio-baseline-ok-{int(time.time())}-{sample_index + 1}"
            sample = send_local_studio_message(page, f"Reply with exactly: {expected_text}", f"google-ai-studio-performance-{sample_index + 1}")
            sample["expected_text"] = expected_text
            local_google_performance_samples.append(sample)
            provider_results.append(sample)
        if official_ai_studio_result.get("result") == "pass":
            local_google_account_alignment = finalize_local_studio_performance_account_alignment(
                page.request,
                base_url,
                local_google_account_alignment,
                len(local_google_performance_samples),
            )
            previous_mode = str(local_google_account_alignment.get("previous_rotation_mode") or "round_robin")
            previous_cooldown = int(local_google_account_alignment.get("previous_cooldown_seconds") or 60)
            local_google_account_alignment["restore_rotation_mode_response"] = api_request_json(
                page.request,
                "POST",
                f"{base_url}/rotation/mode",
                {"mode": previous_mode, "cooldown_seconds": previous_cooldown},
            )
        performance_comparison_result = compare_performance_budget(
            official_ai_studio_result,
            local_google_performance_samples,
            local_google_account_alignment,
        )
        (artifact_dir / "host-performance-comparison-results.json").write_text(
            json.dumps(performance_comparison_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        actions.append(f"local studio google visible performance comparison result={performance_comparison_result.get('result')}")
        runtime_controls["google_stream_off"] = set_local_studio_runtime_toggle(page, "Stream", False, actions)
        runtime_controls["google_search_on"] = set_local_studio_runtime_toggle(page, "Web search", True, actions)
        reasoning_controls["google_effort_off"] = select_local_studio_native_option(page, "Effort", value="off")
        reasoning_controls["google_summary_auto"] = select_local_studio_native_option(page, "Summary", value="auto")
        image_tool_controls["google_enabled"] = set_local_studio_image_tool(page, True, actions)
        image_tool_controls["google_image_model"] = select_local_studio_native_option(page, "Image Model")
        image_tool_controls["google_size"] = select_local_studio_native_option(page, "Size", value="1024x1024")
        image_tool_controls["google_quality"] = select_local_studio_native_option(page, "Quality", preferred_values=["auto", "medium", "high"])
        image_tool_controls["google_disabled"] = set_local_studio_image_tool(page, False, actions)
        attachment_preview_result = exercise_local_studio_attachment_preview_ui(page, artifact_dir, actions)
        provider_results.append(send_local_studio_message(page, f"Reply with exactly: nexus-host-ui-google-ok-{int(time.time())}", "google-ai-studio-nonstream-search"))
        runtime_controls["google_search_off"] = set_local_studio_runtime_toggle(page, "Web search", False, actions)
        page.screenshot(path=str(artifact_dir / "host-ui-google-local-studio.png"), full_page=True)

        page.locator("#studio-page .local-studio-provider-row button", has_text="新增").click()
        actions.append("add openai-compatible provider")
        page.locator("#studio-page input[placeholder='Google AI Studio']").fill("Real OpenAI-compatible")
        page.locator("#studio-page input[placeholder='https://api.openai.com/v1']").fill(openai_base_url)
        page.locator("#studio-page input[placeholder*='兼容服务 token']").fill(openai_token)
        page.locator("#studio-page input[type='number']").first.fill("180")
        openai_interface = click_select_option(page, "Interface", "OpenAI Responses")
        actions.append(f"select openai interface: {openai_interface}")
        openai_models_response = load_local_studio_models_with_retries(page, actions, "openai-compatible")
        openai_selected_model = click_select_option(
            page,
            "Conversation Model",
            openai_model or None,
            preferred_prefixes=["gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1", "gpt-4o", "gpt-5-mini", "gpt-5", "codex-auto-review"],
        )
        actions.append(f"select openai model: {openai_selected_model}")
        runtime_controls["openai_stream_on"] = set_local_studio_runtime_toggle(page, "Stream", True, actions)
        runtime_controls["openai_search_off"] = set_local_studio_runtime_toggle(page, "Web search", False, actions)
        reasoning_controls["openai_effort"] = select_local_studio_native_option(page, "Effort", preferred_values=["high", "medium", "low", "off"])
        reasoning_controls["openai_summary"] = select_local_studio_native_option(page, "Summary", preferred_values=["auto", "concise", "detailed"])
        image_tool_controls["openai_enabled"] = set_local_studio_image_tool(page, True, actions)
        image_tool_controls["openai_image_model"] = select_local_studio_native_option(page, "Image Model")
        image_tool_controls["openai_size"] = select_local_studio_native_option(page, "Size", value="1024x1024")
        image_tool_controls["openai_quality"] = select_local_studio_native_option(page, "Quality", preferred_values=["auto", "medium", "high"])
        image_tool_controls["openai_background"] = select_local_studio_native_option(page, "Background", preferred_values=["auto", "opaque", "transparent"])
        image_tool_controls["openai_format"] = select_local_studio_native_option(page, "Format", preferred_values=["png", "webp", "jpeg"])
        image_tool_controls["openai_disabled"] = set_local_studio_image_tool(page, False, actions)
        provider_results.append(send_local_studio_message(page, f"Reply with exactly: nexus-host-ui-openai-ok-{int(time.time())}", "openai-compatible"))
        runtime_controls["openai_search_on"] = set_local_studio_runtime_toggle(page, "Web search", True, actions)
        provider_results.append(send_local_studio_message(page, f"Reply with exactly: nexus-host-ui-openai-search-ok-{int(time.time())}", "openai-compatible-search"))
        runtime_controls["openai_search_off_after_send"] = set_local_studio_runtime_toggle(page, "Web search", False, actions)
        page.screenshot(path=str(artifact_dir / "host-ui-openai-local-studio.png"), full_page=True)

        page.locator("#studio-page input[placeholder='Google AI Studio']").fill("Real OpenAI-compatible Edited")
        page.keyboard.press("Tab")
        page.locator("#studio-page input[type='number']").first.fill("181")
        page.keyboard.press("Tab")
        expect(page.locator("#studio-page .local-studio-provider-row .cselect-btn")).to_contain_text("Real OpenAI-compatible Edited", timeout=10_000)
        actions.append("edit local studio openai-compatible provider")
        invalid_provider_recovery_result = exercise_local_studio_invalid_provider_recovery_ui(page, base_url, openai_base_url, actions)
        conversation_crud_result = exercise_local_studio_conversation_crud_ui(page, artifact_dir, actions)
        restored_provider = click_select_option(page, "Provider", "Real OpenAI-compatible Edited")
        restored_interface = click_select_option(page, "Interface", "OpenAI Responses")
        restored_models_response = load_local_studio_models_with_retries(page, actions, "openai-compatible reload after conversation CRUD")
        restored_model = click_select_option(page, "Conversation Model", openai_selected_model)
        actions.append(f"restore openai-compatible provider after conversation CRUD: {restored_provider} / {restored_interface} / {restored_model}")

        before_repeat_count = open_request_logs(page, actions, "navigate #requests before repeated prompt")
        navigate_visible_page(page, "Local Studio", "#studio-page.active", "navigate #studio for repeated prompt", actions)
        repeated_prompt = f"Reply with exactly: nexus-host-ui-repeat-ok-{int(time.time())}"
        provider_results.append(send_local_studio_message(page, repeated_prompt, "openai-compatible-repeat-1"))
        provider_results.append(send_local_studio_message(page, repeated_prompt, "openai-compatible-repeat-2"))
        request_log_result = exercise_request_log_ui(page, artifact_dir, openai_token, repeated_prompt, before_repeat_count, actions)
        page.screenshot(path=str(artifact_dir / "host-ui-requests.png"), full_page=True)

        account_health_result = exercise_account_health_ui(page, artifact_dir, actions)
        page.screenshot(path=str(artifact_dir / "host-ui-accounts.png"), full_page=True)

        playground_result = exercise_playground_base_chat_ui(page, google_model, actions)
        page.screenshot(path=str(artifact_dir / "host-ui-playground-basic-chat.png"), full_page=True)

        navigate_visible_page(page, "Local Studio", "#studio-page.active", "navigate #studio for provider delete", actions)
        page.locator("#studio-page .local-studio-provider-row button", has_text="删除").click()
        expect(page.locator("#studio-page .local-studio-size-note", has_text="Provider Type: Google AI Studio")).to_be_visible(timeout=10_000)
        actions.append("delete local studio openai-compatible provider")
        if hold_seconds:
            page.wait_for_timeout(hold_seconds * 1000)
        browser.close()

    retried_model_load_502_count = sum(1 for action in actions if "models load attempt=" in action and "status=502" in action)
    allowed_console_errors, unexpected_console_errors = split_console_errors(
        console_errors,
        allowed_502_count=1 + retried_model_load_502_count,
    )
    official_screenshot_names = []
    if official_ai_studio_result.get("screenshot"):
        official_screenshot_names.append(str(official_ai_studio_result["screenshot"]))
    official_screenshot_names.extend(str(name) for name in official_ai_studio_result.get("screenshots") or [] if name)
    for candidate in official_ai_studio_result.get("candidate_results") or []:
        if isinstance(candidate, dict) and candidate.get("screenshot"):
            official_screenshot_names.append(str(candidate["screenshot"]))
    official_screenshot_names = list(dict.fromkeys(official_screenshot_names))

    result = {
        "page_url": f"{base_url}/static/index.html#studio",
        "coverage_scope": "expanded_visible_ui_matrix_subset",
        "full_system_plan_coverage": False,
        "covered_plan_items": [
            "headed_browser_launch",
            "open_#studio",
            "navigate_#chat",
            "navigate_#images",
            "navigate_#requests",
            "navigate_#config",
            "navigate_#accounts",
            "navigate_#providers",
            "provider_manager_builtin_boundary_token_alias_default_audit_crud",
            "google_ai_studio_responses_model_load",
            "google_ai_studio_nonstream_search_text_send",
            "official_ai_studio_visible_model_selection",
            "official_ai_studio_performance_comparison",
            "local_studio_runtime_stream_search_toggles",
            "local_studio_image_tool_controls",
            "local_studio_attachment_preview_remove",
            "openai_compatible_provider_create",
            "openai_compatible_provider_edit",
            "openai_compatible_provider_delete",
            "openai_compatible_responses_model_load",
            "openai_compatible_stream_text_send",
            "openai_compatible_search_text_send",
            "openai_compatible_reasoning_controls",
            "openai_compatible_invalid_provider_error_and_recovery",
            "local_studio_conversation_create_rename_reload_restore_rerun_delete_bulk_delete",
            "repeated_prompt_fresh_request_log_ui",
            "request_log_detail_copy_export_delete_ui",
            "account_health_check_ui",
            "playground_basic_gemini_chat_ui",
        ],
        "known_missing_plan_items": [
            "complete_P0_P1_UI_matrix",
            "full_search_image_reasoning_generation_matrix",
            "attachment_send_upstream_matrix",
            "image_generation_real_visible_output_matrix",
            "accounts_switch_delete_UI",
            "provider_manager_rollout_phase_gate_and_discovery_health_complete",
        ],
        "browser": {
            "engine": "chromium",
            "headless": headless,
            "slow_mo_ms": slow_mo_ms,
            "hold_seconds": hold_seconds,
        },
        "google_model": google_model,
        "openai_key_file": str(openai_key_file),
        "openai_key_file_nonempty": True,
        "openai_base_url": openai_base_url,
        "openai_model_requested": openai_model,
        "actions": actions,
        "provider_results": provider_results,
        "official_ai_studio_result": official_ai_studio_result,
        "local_google_account_alignment": local_google_account_alignment,
        "local_google_performance_samples": local_google_performance_samples,
        "performance_comparison_result": performance_comparison_result,
        "runtime_controls": runtime_controls,
        "reasoning_controls": reasoning_controls,
        "image_tool_controls": image_tool_controls,
        "attachment_preview_result": attachment_preview_result,
        "invalid_provider_recovery_result": invalid_provider_recovery_result,
        "conversation_crud_result": conversation_crud_result,
        "playground_result": playground_result,
        "request_log_result": request_log_result,
        "account_health_result": account_health_result,
        "provider_manager_result": provider_manager_result,
        "console_errors": console_errors,
        "allowed_console_errors": allowed_console_errors,
        "unexpected_console_errors": unexpected_console_errors,
        "allowed_console_502_count": 1 + retried_model_load_502_count,
        "network_events": network_events[-80:],
        "screenshots": [
            "host-ui-google-local-studio.png",
            *official_screenshot_names,
            "host-ui-openai-local-studio.png",
            "host-ui-request-log-crud.png",
            "host-ui-requests.png",
            "host-ui-accounts-health.png",
            "host-ui-accounts.png",
            "host-ui-provider-manager-crud.png",
            "host-ui-local-studio-conversation-crud.png",
            "host-ui-playground-basic-chat.png",
        ],
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if openai_token in serialized:
        raise AssertionError("OpenAI-compatible token leaked into UI result artifact")
    (artifact_dir / "mcp-visible-ui-results.json").write_text(serialized, encoding="utf-8")
    (artifact_dir / "host-performance-comparison-results.json").write_text(
        json.dumps(performance_comparison_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    failures: list[str] = []
    if unexpected_console_errors:
        failures.append("unexpected_console_errors")
    if headless:
        failures.append("host_ui_smoke_ran_headless")
    if result["full_system_plan_coverage"] is not False:
        failures.append("unexpected_full_system_coverage_claim")
    if official_ai_studio_result.get("result") != "pass":
        failures.append(f"official_ai_studio_result={official_ai_studio_result.get('result')}")
    if len(official_ai_studio_result.get("samples") or []) < 3:
        failures.append("official_ai_studio_samples_missing")
    if performance_comparison_result.get("result") != "pass":
        failures.append(f"performance_comparison_result={performance_comparison_result.get('result')}")
    for item in provider_results:
        label = item["label"]
        expected_error = bool(item.get("expected_error"))
        if expected_error:
            if item.get("outcome") != "error":
                failures.append(f"{label}_expected_error_missing")
            if not str(item.get("assistant_error_prefix") or "").strip():
                failures.append(f"{label}_error_text_missing")
        else:
            if item.get("chat_response_status") != 200:
                failures.append(f"{label}_chat_response_status={item.get('chat_response_status')}")
            if item.get("assistant_error_prefix"):
                failures.append(f"{label}_assistant_error={item.get('assistant_error_prefix')}")
            if not str(item.get("assistant_text_prefix") or "").strip():
                failures.append(f"{label}_assistant_text_missing")
            if item.get("time_to_first_visible_token_ms") is None:
                failures.append(f"{label}_first_visible_token_missing")
    for control_key in ("google_stream_off", "google_search_on", "openai_stream_on", "openai_search_on"):
        if runtime_controls.get(control_key, {}).get("available") is not True:
            failures.append(f"runtime_control_unavailable={control_key}")
    if attachment_preview_result.get("removed") is not True:
        failures.append("local_studio_attachment_remove_failed")
    for key in ("create_status", "rename_status", "delete_status", "bulk_delete_status"):
        if conversation_crud_result.get(key, 500) >= 400:
            failures.append(f"conversation_{key}={conversation_crud_result.get(key)}")
    if conversation_crud_result.get("restore_after_reload_visible") is not True:
        failures.append("conversation_restore_after_reload_missing")
    rerun_result = conversation_crud_result.get("rerun") or {}
    if rerun_result.get("chat_response_status") != 200 or not str(rerun_result.get("assistant_text_prefix") or "").strip():
        failures.append("conversation_rerun_visible_result_missing")
    invalid_error = (invalid_provider_recovery_result.get("error_result") or {})
    invalid_recovery = (invalid_provider_recovery_result.get("recovery_result") or {})
    if invalid_error.get("outcome") != "error" or not str(invalid_error.get("assistant_error_prefix") or "").strip():
        failures.append("invalid_provider_controlled_error_missing")
    if invalid_provider_recovery_result.get("health_status_after_error") != 200:
        failures.append(f"invalid_provider_health_status={invalid_provider_recovery_result.get('health_status_after_error')}")
    if invalid_recovery.get("chat_response_status") != 200 or not str(invalid_recovery.get("assistant_text_prefix") or "").strip():
        failures.append("invalid_provider_recovery_chat_missing")
    if playground_result.get("models_status", 500) >= 400:
        failures.append(f"playground_models_status={playground_result.get('models_status')}")
    if playground_result.get("chat_response_status") != 200 or not str(playground_result.get("assistant_text_prefix") or "").strip():
        failures.append("playground_basic_chat_visible_result_missing")
    for key in ("create_status", "update_status", "toggle_status", "delete_status"):
        if provider_manager_result.get(key, 500) >= 400:
            failures.append(f"provider_manager_{key}={provider_manager_result.get(key)}")
    if provider_manager_result.get("token_visibility_toggle") is not True:
        failures.append("provider_manager_token_toggle_missing")
    if provider_manager_result.get("manual_model_default_text") is not True:
        failures.append("provider_manager_default_text_missing")
    if provider_manager_result.get("audit_deleted_visible") is not True:
        failures.append("provider_manager_audit_delete_missing")
    for key, value in (provider_manager_result.get("built_in_boundary") or {}).items():
        if value is not True:
            failures.append(f"provider_manager_builtin_boundary_{key}=false")
    if request_log_result.get("delete_status", 500) >= 400:
        failures.append(f"request_log_delete_status={request_log_result.get('delete_status')}")
    if request_log_result.get("after_repeat_count", 0) < request_log_result.get("before_repeat_count", 0) + 2:
        failures.append("request_log_repeat_count_missing")
    if request_log_result.get("after_delete_count", 999999) >= request_log_result.get("before_delete_count", 0):
        failures.append("request_log_delete_did_not_remove_row")
    if account_health_result.get("health_response_status", 500) >= 400:
        failures.append(f"account_health_status={account_health_result.get('health_response_status')}")
    if account_health_result.get("health_ok") is not True:
        failures.append(f"account_health_ok={account_health_result.get('health_ok')} status={account_health_result.get('health_status')}")
    if failures:
        print("HOST_UI_SMOKE_FAIL " + " ".join(str(item) for item in failures))
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print("HOST_UI_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
