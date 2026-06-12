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


_LAST_GOOGLE_REQUEST_AT = 0.0
GOOGLE_QUOTA_BLOCKER_EXIT_CODE = 77


class GoogleQuotaBlocker(RuntimeError):
    def __init__(
        self,
        *,
        source: str,
        label: str,
        status: int,
        text: str,
        extra: dict[str, object] | None = None,
    ) -> None:
        super().__init__(f"external_google_quota_exhausted source={source} label={label} status={status}")
        self.source = source
        self.label = label
        self.status = status
        self.text = text
        self.extra = extra or {}

    def to_artifact(self) -> dict[str, object]:
        return {
            "result": "blocked",
            "reason": "external_google_quota_exhausted",
            "source": self.source,
            "label": self.label,
            "status": self.status,
            "text_preview": safe_response_body_preview(self.text, 500),
            "google_request_interval_seconds": google_request_interval_seconds(),
            **self.extra,
        }


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


def google_request_interval_seconds() -> int:
    return max(0, env_int("SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS", 30))


def split_model_candidates(raw: str | None) -> list[str]:
    if not raw:
        return []
    normalized = raw.replace(";", ",").replace("\n", ",")
    return [candidate.strip() for candidate in normalized.split(",") if candidate.strip()]


def dedupe_model_candidates(candidates: list[str]) -> list[str]:
    deduped: list[str] = []
    for candidate in candidates:
        normalized = candidate.strip()
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped


def system_test_google_model_candidates(primary_model: str) -> list[str]:
    candidates: list[str] = []
    if primary_model.strip():
        candidates.append(primary_model.strip())
    candidates.extend(split_model_candidates(os.environ.get("SYSTEM_TEST_MODEL_CANDIDATES")))
    return dedupe_model_candidates(candidates)


def google_quota_exhausted_text(text: str) -> bool:
    normalized = (text or "").lower()
    markers = (
        "you exceeded your current quota",
        "ai.google.dev/gemini-api/docs/rate-limits",
        "quota for the day",
        "daily quota",
        "quota resets",
        "resource_exhausted",
    )
    return any(marker in normalized for marker in markers)


def google_permission_error_text(text: str) -> bool:
    normalized = (text or "").lower()
    markers = (
        "permission_denied",
        "caller does not have permission",
        "permission denied",
        "user location is not supported",
        "api key not valid",
        "request had invalid authentication credentials",
        "401",
        "403",
    )
    return any(marker in normalized for marker in markers)


def write_google_quota_blocker(blocker: GoogleQuotaBlocker) -> None:
    artifact_dir_value = os.environ.get("HOST_ARTIFACT_DIR")
    if not artifact_dir_value:
        return
    artifact_dir = Path(artifact_dir_value)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "google-quota-blocker.safe.json").write_text(
        json.dumps(blocker.to_artifact(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_reused_official_baseline(artifact_dir: Path, actions: list[str]) -> dict[str, object]:
    source_value = os.environ.get("HOST_OFFICIAL_BASELINE_RESULTS_FILE", "").strip()
    if not source_value:
        return {}
    source = Path(source_value)
    if not source.is_file():
        raise AssertionError(f"HOST_OFFICIAL_BASELINE_RESULTS_FILE is missing: {source}")
    result = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise AssertionError("HOST_OFFICIAL_BASELINE_RESULTS_FILE did not contain a JSON object")
    if result.get("result") != "pass" or len(result.get("samples") or []) < 3:
        raise AssertionError("HOST_OFFICIAL_BASELINE_RESULTS_FILE did not contain a passing 3-sample official baseline")
    result = dict(result)
    result["reused_from_artifact"] = str(source)
    result["reuse_mode"] = "low_quota_resume"
    target = artifact_dir / "host-official-aistudio-results.json"
    if source.resolve() != target.resolve():
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    actions.append(f"reuse official AI Studio visible baseline artifact={source}")
    return result


def throttle_google_request(label: str, actions: list[str] | None = None) -> float:
    global _LAST_GOOGLE_REQUEST_AT
    interval_seconds = google_request_interval_seconds()
    if interval_seconds <= 0:
        return 0.0
    now = time.perf_counter()
    waited_seconds = 0.0
    if _LAST_GOOGLE_REQUEST_AT:
        wait_seconds = interval_seconds - (now - _LAST_GOOGLE_REQUEST_AT)
        if wait_seconds > 0:
            if actions is not None:
                actions.append(f"throttle google request {label} wait_seconds={wait_seconds:.1f}")
            time.sleep(wait_seconds)
            now = time.perf_counter()
            waited_seconds = wait_seconds
    _LAST_GOOGLE_REQUEST_AT = now
    return waited_seconds


def response_path(response) -> str:
    return urlparse(response.url).path


def split_console_errors(console_errors: list[str], allowed_502_count: int, allowed_400_count: int = 0) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    unexpected: list[str] = []
    remaining_expected_502 = allowed_502_count
    remaining_expected_400 = allowed_400_count
    for message in console_errors:
        if remaining_expected_502 > 0 and "status of 502 (Bad Gateway)" in message:
            allowed.append(message)
            remaining_expected_502 -= 1
        elif remaining_expected_400 > 0 and "status of 400 (Bad Request)" in message:
            allowed.append(message)
            remaining_expected_400 -= 1
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
        r"""
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


def run_official_ai_studio_sample(
    page,
    helpers: dict[str, object],
    model: str,
    expected_text: str,
    timeout_ms: int = 420_000,
    actions: list[str] | None = None,
) -> dict[str, object]:
    throttle_google_request("official-ai-studio", actions)
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
            body_preview = str(response_holder.get("body_preview") or "")
            if google_quota_exhausted_text(body_preview):
                raise GoogleQuotaBlocker(
                    source="official-ai-studio",
                    label=expected_text,
                    status=status,
                    text=body_preview,
                    extra={
                        "wire_model": response_holder.get("wire_model"),
                        "url_path": response_holder.get("url_path"),
                    },
                )
            return {
                "result": "fail",
                "expected_text": expected_text,
                "status": status,
                "wire_model": response_holder.get("wire_model"),
                "url_path": response_holder.get("url_path"),
                "body_size": response_holder.get("body_size"),
                "body_preview": body_preview,
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
                        official_warmup_samples.append(run_official_ai_studio_sample(official_page, helpers, model, warmup_text, timeout_ms=180_000, actions=actions))
                        measurement_network_enabled["value"] = True
                        for attempt_index in range(5):
                            if len(official_samples) >= 3:
                                break
                            expected_text = f"nexus-official-aistudio-baseline-ok-{int(time.time())}-{attempt_index + 1}"
                            sample = run_official_ai_studio_sample(official_page, helpers, model, expected_text, actions=actions)
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
                except GoogleQuotaBlocker:
                    raise
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


def recover_aligned_account_after_unavailable(api_request, base_url: str, alignment: dict[str, object], actions: list[str], label: str) -> dict[str, object]:
    account_id = str(alignment.get("account_id") or alignment.get("active_account_id") or "")
    if not account_id:
        return {"ok": False, "reason": "no aligned account id"}
    try:
        before_rotation = api_request_json(api_request, "GET", f"{base_url}/rotation")
        activated = api_request_json(api_request, "POST", f"{base_url}/accounts/{account_id}/activate", timeout_ms=180_000)
        after_rotation = api_request_json(api_request, "GET", f"{base_url}/rotation")
    except AssertionError as exc:
        actions.append(f"recover google account unavailable failed label={label}: {str(exc)[:160]}")
        return {"ok": False, "account_id": account_id, "reason": str(exc)[:500]}
    before_accounts = before_rotation.get("accounts") if isinstance(before_rotation.get("accounts"), dict) else {}
    after_accounts = after_rotation.get("accounts") if isinstance(after_rotation.get("accounts"), dict) else {}
    before_stats = before_accounts.get(account_id, {}) if isinstance(before_accounts, dict) else {}
    after_stats = after_accounts.get(account_id, {}) if isinstance(after_accounts, dict) else {}
    result = {
        "ok": bool(after_stats.get("is_available")) if isinstance(after_stats, dict) else False,
        "account_id": account_id,
        "activated_account_id": activated.get("id"),
        "before_cooldown_remaining": before_stats.get("cooldown_remaining") if isinstance(before_stats, dict) else None,
        "after_cooldown_remaining": after_stats.get("cooldown_remaining") if isinstance(after_stats, dict) else None,
        "before_available": bool(before_stats.get("is_available")) if isinstance(before_stats, dict) else False,
        "after_available": bool(after_stats.get("is_available")) if isinstance(after_stats, dict) else False,
    }
    actions.append(f"recover google account unavailable label={label} ok={result.get('ok')} account={account_id}")
    return result


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


def latest_local_studio_assistant_state(page) -> dict[str, object]:
    messages = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending)")
    if messages.count() <= 0:
        return {"assistant_visible": False}
    message = messages.last
    thinking_block = message.locator(".think-block").first
    images = message.locator(".local-studio-images img")
    usage = message.locator(".msg-usage").first
    image_sources: list[str] = []
    for index in range(images.count()):
        source = images.nth(index).get_attribute("src") or ""
        if source:
            image_sources.append(source)
    return {
        "assistant_visible": True,
        "reasoning_summary_visible": locator_is_visible(thinking_block, timeout_ms=1_000),
        "image_count": images.count(),
        "image_sources": image_sources,
        "usage_visible": locator_is_visible(usage, timeout_ms=1_000),
        "message_text_prefix": visible_text(message)[:500],
    }


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


def click_select_option_if_present(page, field_label: str, option_text: str) -> str:
    if not option_text:
        return ""
    field = page.locator(".local-studio-field", has_text=field_label).first
    field.locator(".cselect-btn").click()
    menu = page.locator(".cselect.open .cselect-menu").first
    expect(menu).to_be_visible(timeout=10_000)
    options = menu.locator(".cselect-opt:not([aria-disabled='true'])")
    count = options.count()
    texts = [visible_text(options.nth(index)) for index in range(count)]
    for index, text in enumerate(texts):
        if text == option_text or option_text in text:
            options.nth(index).click()
            return text
    page.keyboard.press("Escape")
    return ""


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
    allow_error: bool = False,
    timeout_ms: int = 420_000,
    throttle_google: bool = False,
    actions: list[str] | None = None,
) -> dict[str, object]:
    textarea = page.locator("#studio-page textarea[placeholder*='Local Studio']")
    expect(textarea).to_be_visible(timeout=30_000)
    textarea.fill(prompt)
    send_button = page.locator("#studio-page .local-studio-compose-row button.send")
    expect(send_button).to_be_enabled(timeout=20_000)
    assistant_messages = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending)")
    assistant_bodies = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending) .msg-body:not(.error)")
    assistant_errors = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending) .msg-body.error")
    assistant_images = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending) .local-studio-images img")
    notice_errors = page.locator("#studio-page .notice.notice-error")
    before_assistant_count = assistant_messages.count()
    before_body_count = assistant_bodies.count()
    before_error_count = assistant_errors.count()
    before_image_count = assistant_images.count()
    before_notice_texts = set(visible_texts(notice_errors))
    google_backed_send = throttle_google or label.startswith("google")
    google_throttle_wait_ms = 0
    if google_backed_send:
        google_throttle_wait_ms = round(throttle_google_request(f"local-studio:{label}", actions) * 1000)
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
            if assistant_images.count() > before_image_count:
                candidate = assistant_images.nth(assistant_images.count() - 1)
                if locator_is_visible(candidate, timeout_ms=500):
                    first_visible_ms = round((time.perf_counter() - started) * 1000)
                    outcome = "success"
                    break
            if assistant_messages.count() > before_assistant_count:
                candidate_message = assistant_messages.nth(assistant_messages.count() - 1)
                candidate_images = candidate_message.locator(".local-studio-images img")
                if candidate_images.count() > 0 and locator_is_visible(candidate_images.last, timeout_ms=500):
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
    if google_backed_send and outcome == "error" and google_quota_exhausted_text(error_text):
        raise GoogleQuotaBlocker(
            source="local-studio",
            label=label,
            status=chat_response.status,
            text=error_text,
        )
    assistant_state = latest_local_studio_assistant_state(page)
    residual_state = assert_local_studio_no_cache_or_residual_state(page)
    if not allow_error:
        if expect_error and outcome != "error":
            raise AssertionError(f"expected Local Studio error for {label}, got {outcome}")
        if not expect_error and outcome != "success":
            raise AssertionError(f"expected Local Studio success for {label}, got error={error_text[:300]}")
    return {
        "prompt": prompt,
        "label": label,
        "chat_response_status": chat_response.status,
        "expected_error": expect_error,
        "allow_error": allow_error,
        "outcome": outcome,
        "assistant_text_prefix": assistant_text[:500],
        "assistant_error_prefix": error_text[:500],
        "pending_observed": pending_observed,
        "time_to_first_visible_token_ms": first_visible_ms,
        "time_to_complete_ms": complete_ms,
        "google_throttle_wait_ms": google_throttle_wait_ms,
        "assistant_state": assistant_state,
        "residual_state": residual_state,
    }


TRANSIENT_UPSTREAM_ERROR_MARKERS = (
    "connecterror",
    "readerror",
    "remoteprotocolerror",
    "connecttimeout",
    "enetunreach",
    "ehostunreach",
    "econnreset",
    "econnrefused",
    "network is unreachable",
    "http 0",
    "unexpected_eof_while_reading",
    "eof occurred in violation of protocol",
    "connection reset",
    "temporarily unavailable",
    "api request context.post: timeout",
    "apirequestcontext.post: timeout",
    "timeout 120000ms exceeded",
    "upstream request timed out",
    "http 504",
    "gateway timeout",
    "ai studio returned no response content",
    "returned no response content",
)


def transient_upstream_error_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in TRANSIENT_UPSTREAM_ERROR_MARKERS)


def send_local_studio_message_with_transient_retry(
    page,
    prompt: str,
    label: str,
    *,
    actions: list[str],
    attempts: int = 3,
    new_conversation_each_attempt: bool = False,
    **kwargs,
) -> dict[str, object]:
    last_error: AssertionError | None = None
    for attempt_index in range(1, attempts + 1):
        if new_conversation_each_attempt and attempt_index > 1:
            create_local_studio_conversation_via_ui(page, actions, f"{label}-retry-{attempt_index}")
        try:
            return send_local_studio_message(page, prompt, label, **kwargs)
        except AssertionError as exc:
            if not transient_upstream_error_text(str(exc)) or attempt_index >= attempts:
                raise
            last_error = exc
            actions.append(f"retry local studio send after transient upstream error label={label} attempt={attempt_index}")
            page.wait_for_timeout(2_000 + attempt_index * 2_000)
    assert last_error is not None
    raise last_error


def create_local_studio_conversation_via_ui(page, actions: list[str], label: str) -> dict[str, object]:
    create_button = page.locator("#studio-page .local-studio-history").get_by_role("button", name="新建")
    with page.expect_response(lambda response: response.url.endswith("/api/local-studio/conversations") and response.request.method == "POST", timeout=60_000) as create_response_info:
        create_button.click()
    create_response = create_response_info.value
    expect(page.locator("#studio-page .local-studio-empty-chat")).to_be_visible(timeout=30_000)
    try:
        create_data = create_response.json()
    except Exception:
        create_data = {}
    conversation_id = str(create_data.get("id") or "") if isinstance(create_data, dict) else ""
    actions.append(f"create local studio isolated conversation {label}: status={create_response.status}")
    return {"status": create_response.status, "conversation_id": conversation_id}


def local_studio_result_controlled(result: dict[str, object], *, allow_error: bool = False) -> bool:
    if result.get("outcome") == "success":
        state = result.get("assistant_state") if isinstance(result.get("assistant_state"), dict) else {}
        has_visible_output = bool(str(result.get("assistant_text_prefix") or "").strip()) or int(state.get("image_count") or 0) > 0
        return int(result.get("chat_response_status") or 500) < 400 and has_visible_output
    if not allow_error:
        return False
    return bool(str(result.get("assistant_error_prefix") or "").strip()) and int(result.get("chat_response_status") or 500) < 500


def select_local_studio_interface_model(
    page,
    actions: list[str],
    *,
    interface_label: str,
    model: str | None,
    load_label: str,
    preferred_prefixes: list[str] | None = None,
) -> dict[str, object]:
    selected_interface = click_select_option(page, "Interface", interface_label)
    response = load_local_studio_models_with_retries(page, actions, load_label)
    selected_model = click_select_option(page, "Conversation Model", model, preferred_prefixes=preferred_prefixes)
    actions.append(f"select local studio interface matrix {load_label}: {selected_interface} / {selected_model}")
    return {"interface": selected_interface, "model": selected_model, "models_status": response.status}


def create_openai_compatible_provider_via_ui(
    page,
    *,
    name: str,
    base_url: str,
    token: str,
    timeout: int,
    interface_label: str,
    actions: list[str],
) -> dict[str, object]:
    page.locator("#studio-page .local-studio-provider-row button", has_text="新增").click()
    page.locator("#studio-page input[placeholder='Google AI Studio']").fill(name)
    page.locator("#studio-page input[placeholder='https://api.openai.com/v1']").fill(base_url)
    page.locator("#studio-page input[placeholder*='兼容服务 token']").fill(token)
    page.locator("#studio-page input[type='number']").first.fill(str(timeout))
    selected_interface = click_select_option(page, "Interface", interface_label)
    actions.append(f"add openai-compatible provider {name} interface={selected_interface}")
    return {"name": name, "timeout": timeout, "interface": selected_interface}


def exercise_local_studio_provider_persistence_ui(
    page,
    openai_base_url: str,
    openai_token: str,
    primary_provider_name: str,
    actions: list[str],
) -> dict[str, object]:
    secondary_name = f"Second OpenAI-compatible {int(time.time() * 1000)}"
    secondary = create_openai_compatible_provider_via_ui(
        page,
        name=secondary_name,
        base_url=openai_base_url,
        token=openai_token,
        timeout=182,
        interface_label="OpenAI 兼容",
        actions=actions,
    )
    selected_primary = click_select_option(page, "Provider", primary_provider_name)
    primary_base_before = page.locator("#studio-page input[placeholder='https://api.openai.com/v1']").input_value(timeout=5_000)
    primary_timeout_before = page.locator("#studio-page input[type='number']").first.input_value(timeout=5_000)
    selected_secondary = click_select_option(page, "Provider", secondary_name)
    secondary_base_before = page.locator("#studio-page input[placeholder='https://api.openai.com/v1']").input_value(timeout=5_000)
    secondary_timeout_before = page.locator("#studio-page input[type='number']").first.input_value(timeout=5_000)
    page.reload(wait_until="networkidle", timeout=90_000)
    expect(page.locator("#studio-page.active")).to_be_visible(timeout=30_000)
    provider_button = page.locator("#studio-page .local-studio-provider-row .cselect-btn").first
    expect(provider_button).to_contain_text(secondary_name, timeout=30_000)
    secondary_base_after = page.locator("#studio-page input[placeholder='https://api.openai.com/v1']").input_value(timeout=5_000)
    secondary_timeout_after = page.locator("#studio-page input[type='number']").first.input_value(timeout=5_000)
    secondary_interface_after = visible_text(page.locator("#studio-page label.local-studio-field", has_text="Interface").first)
    page.locator("#studio-page .local-studio-provider-row button", has_text="删除").click()
    expect(page.locator("#studio-page .local-studio-provider-row .cselect-btn")).not_to_contain_text(secondary_name, timeout=10_000)
    restored_primary = click_select_option(page, "Provider", primary_provider_name)
    actions.append("local studio two openai providers switch reload restore delete secondary")
    return {
        "primary_provider_name": primary_provider_name,
        "secondary_provider_name": secondary_name,
        "secondary": secondary,
        "selected_primary": selected_primary,
        "selected_secondary": selected_secondary,
        "restored_primary": restored_primary,
        "primary_base_before": primary_base_before,
        "primary_timeout_before": primary_timeout_before,
        "secondary_base_before": secondary_base_before,
        "secondary_timeout_before": secondary_timeout_before,
        "secondary_base_after_reload": secondary_base_after,
        "secondary_timeout_after_reload": secondary_timeout_after,
        "secondary_interface_after_reload": secondary_interface_after,
        "provider_restored_after_cleanup": primary_provider_name in restored_primary,
        "ok": bool(
            primary_provider_name in selected_primary
            and secondary_name in selected_secondary
            and secondary_base_before == openai_base_url
            and secondary_timeout_before == "182"
            and secondary_base_after == openai_base_url
            and secondary_timeout_after == "182"
            and primary_provider_name in restored_primary
        ),
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


def rerun_local_studio_first_user_turn(
    page,
    label: str,
    *,
    throttle_google: bool = False,
    actions: list[str] | None = None,
) -> dict[str, object]:
    before_assistant_count = page.locator("#studio-page .local-studio-message.is-assistant:not(.is-pending)").count()
    started = time.perf_counter()
    rerun_button = page.locator("#studio-page .local-studio-message.is-user [aria-label='从此处重跑']").first
    expect(rerun_button).to_be_visible(timeout=30_000)
    if throttle_google:
        throttle_google_request(f"local-studio:{label}", actions)
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
    sent = send_local_studio_message(
        page,
        f"Reply with exactly: nexus-host-ui-conversation-ok-{int(time.time())}",
        "conversation-crud-seed",
        throttle_google=True,
        actions=actions,
    )
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
    rerun = rerun_local_studio_first_user_turn(page, "conversation-rerun", throttle_google=True, actions=actions)
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
    last_error: AssertionError | None = None
    for attempt_index in range(3):
        try:
            return send_local_studio_message(
                page,
                f"Reply with exactly: nexus-host-ui-recovery-ok-{int(time.time())}-{attempt_index + 1}",
                "openai-compatible-recovery-after-invalid-provider",
            )
        except AssertionError as exc:
            if not transient_upstream_error_text(str(exc)):
                raise
            last_error = exc
            actions.append(f"retry openai-compatible recovery after transient upstream error attempt={attempt_index + 1}")
            page.wait_for_timeout(2_000 + attempt_index * 2_000)
    assert last_error is not None
    raise last_error


def set_playground_runtime_toggle(page, label: str, enabled: bool, actions: list[str]) -> dict[str, object]:
    toggle = page.locator("#chat-page .runtime-toggle", has_text=label).first
    if not locator_is_visible(toggle, timeout_ms=4_000):
        return {"label": label, "available": False, "requested": enabled}
    button = toggle.locator("button").first
    expect(button).to_be_visible(timeout=10_000)
    current = current_button_enabled_text(button)
    requested = "on" if enabled else "off"
    if current != requested:
        button.click()
        expect(button).to_have_text("开启" if enabled else "关闭", timeout=10_000)
    actions.append(f"set playground {label}={requested}")
    return {"label": label, "available": True, "value": requested}


def send_playground_message(
    page,
    prompt: str,
    label: str,
    *,
    allow_error: bool = False,
    timeout_ms: int = 420_000,
    throttle_google: bool = False,
    actions: list[str] | None = None,
) -> dict[str, object]:
    textarea = page.locator("#chat-page textarea[placeholder*='AI Studio']")
    expect(textarea).to_be_visible(timeout=30_000)
    textarea.fill(prompt)
    assistant_bodies = page.locator("#chat-page .msg.msg-ai .msg-body:not(.error)")
    assistant_errors = page.locator("#chat-page .msg.msg-ai .msg-body.error")
    before_body_count = assistant_bodies.count()
    before_error_count = assistant_errors.count()
    started = time.perf_counter()
    if throttle_google:
        throttle_google_request(f"playground:{label}", actions)
    with page.expect_response(
        lambda response: response_path(response).startswith("/v1beta/models/") or response_path(response) in {"/v1/responses", "/v1/chat/completions", "/v1/messages"},
        timeout=timeout_ms,
    ) as response_info:
        page.locator("#chat-page button.send").click()
    response = response_info.value
    assistant_text = ""
    error_text = ""
    outcome = "error" if response.status >= 400 else "pending"
    deadline = time.perf_counter() + (timeout_ms / 1000)
    while time.perf_counter() < deadline:
        if assistant_bodies.count() > before_body_count:
            candidate = assistant_bodies.nth(assistant_bodies.count() - 1)
            assistant_text = visible_text(candidate)
            if locator_is_visible(candidate, timeout_ms=500) and assistant_text:
                outcome = "success"
                break
        if assistant_errors.count() > before_error_count:
            candidate = assistant_errors.nth(assistant_errors.count() - 1)
            if locator_is_visible(candidate, timeout_ms=500):
                error_text = visible_text(candidate)
                outcome = "error"
                break
        page.wait_for_timeout(500)
    expect(textarea).to_be_enabled(timeout=30_000)
    if outcome == "pending":
        raise AssertionError(f"Playground message did not finish visibly: label={label}")
    if outcome != "success" and not allow_error:
        if throttle_google and google_quota_exhausted_text(error_text):
            raise GoogleQuotaBlocker(
                source="playground",
                label=label,
                status=response.status,
                text=error_text,
            )
        raise AssertionError(f"expected Playground success for {label}, got error={error_text[:300]}")
    return {
        "label": label,
        "prompt": prompt,
        "chat_response_status": response.status,
        "outcome": outcome,
        "assistant_text_prefix": assistant_text[:500],
        "assistant_error_prefix": error_text[:500],
        "time_to_complete_ms": round((time.perf_counter() - started) * 1000),
    }


def select_playground_model_if_present(page, model: str) -> str:
    if not model:
        return ""
    model_button = page.locator("#chat-page .playground-model-select .cselect-btn")
    expect(model_button).to_be_visible(timeout=30_000)
    model_button.click()
    model_menu = page.locator(".cselect.open .cselect-menu").first
    expect(model_menu).to_be_visible(timeout=30_000)
    options = model_menu.locator(".cselect-opt:not([aria-disabled='true'])")
    count = options.count()
    texts = [visible_text(options.nth(index)) for index in range(count)]
    for index, text in enumerate(texts):
        if text == model or model in text:
            options.nth(index).click()
            return text
    page.keyboard.press("Escape")
    return ""


def select_playground_gemini_interface(page) -> str:
    interface = page.locator(".api-toolbar .cselect-btn").first
    expect(interface).to_be_visible(timeout=30_000)
    interface.click()
    menu = page.locator(".cselect.open .cselect-menu").first
    expect(menu).to_be_visible(timeout=10_000)
    gemini_option = menu.locator(".cselect-opt", has_text="Gemini").first
    if gemini_option.count() > 0:
        selected_interface = visible_text(gemini_option)
        gemini_option.click()
        return selected_interface
    fallback = menu.locator(".cselect-opt").first
    selected_interface = visible_text(fallback)
    fallback.click()
    return selected_interface


def exercise_playground_base_chat_ui(page, model_candidates: list[str], actions: list[str]) -> dict[str, object]:
    page.get_by_role("button", name="Playground").click()
    expect(page.locator("#chat-page.active")).to_be_visible(timeout=30_000)
    selected_interface = select_playground_gemini_interface(page)
    with page.expect_response(lambda response: response_path(response) in {"/v1beta/models", "/v1/models"} and response.request.method == "GET", timeout=180_000) as models_response_info:
        page.locator("#chat-page .model-refresh-btn").click()
    models_response = models_response_info.value
    candidate_results: list[dict[str, object]] = []
    quota_blockers: list[dict[str, object]] = []
    for candidate_model in dedupe_model_candidates(model_candidates):
        selected_model = select_playground_model_if_present(page, candidate_model)
        if not selected_model:
            candidate_results.append({"model": candidate_model, "result": "skipped", "reason": "model candidate was not selectable in Playground"})
            actions.append(f"skip playground base chat model candidate not selectable: {candidate_model}")
            continue
        prompt = f"Reply with exactly: nexus-host-ui-playground-ok-{int(time.time())}"
        try:
            sent = send_playground_message(page, prompt, "playground-base-chat", throttle_google=True, actions=actions)
        except GoogleQuotaBlocker as exc:
            blocker = exc.to_artifact()
            blocker["model_candidate"] = selected_model
            quota_blockers.append(blocker)
            candidate_results.append({"model": selected_model, "result": "quota_exhausted", "blocker": blocker})
            actions.append(f"playground base chat model candidate quota-limited; trying next candidate: {selected_model}")
            continue
        sent["selected_model"] = selected_model
        candidate_results.append({"model": selected_model, "result": "pass", "send": sent})
        actions.append(f"playground gemini model candidate basic visible send: {selected_model}")
        return {
            "models_status": models_response.status,
            "selected_interface": selected_interface,
            "selected_model": selected_model,
            "candidate_results": candidate_results,
            **sent,
        }
    if quota_blockers:
        raise GoogleQuotaBlocker(
            source="playground",
            label="playground-base-chat-candidates",
            status=int(quota_blockers[-1].get("status") or 200),
            text="\n".join(str(blocker.get("text_preview") or "") for blocker in quota_blockers),
            extra={
                "quota_or_unavailable_scope": "model_candidates_exhausted",
                "model_candidates": dedupe_model_candidates(model_candidates),
                "failed_candidate_reasons": quota_blockers,
            },
        )
    raise AssertionError(f"no selectable Playground base chat model candidate from {dedupe_model_candidates(model_candidates)}")


def exercise_playground_search_chat_ui(page, model_candidates: list[str], actions: list[str]) -> dict[str, object]:
    expect(page.locator("#chat-page.active")).to_be_visible(timeout=30_000)
    search_toggle = set_playground_runtime_toggle(page, "Search", True, actions)
    candidate_results: list[dict[str, object]] = []
    quota_blockers: list[dict[str, object]] = []
    for candidate_model in dedupe_model_candidates(model_candidates):
        selected_model = select_playground_model_if_present(page, candidate_model)
        if not selected_model:
            candidate_results.append({"model": candidate_model, "result": "skipped", "reason": "model candidate was not selectable in Playground"})
            actions.append(f"skip playground search model candidate not selectable: {candidate_model}")
            continue
        prompt = f"Search today's AI technology news and reply in one short sentence. marker={int(time.time())}"
        try:
            sent = send_playground_message(page, prompt, "playground-search-chat", throttle_google=True, actions=actions)
        except GoogleQuotaBlocker as exc:
            blocker = exc.to_artifact()
            blocker["model_candidate"] = selected_model
            quota_blockers.append(blocker)
            candidate_results.append({"model": selected_model, "result": "quota_exhausted", "blocker": blocker})
            actions.append(f"playground search model candidate quota-limited; trying next candidate: {selected_model}")
            continue
        sent["selected_model"] = selected_model
        candidate_results.append({"model": selected_model, "result": "pass", "send": sent})
        actions.append(f"playground search visible send: {selected_model}")
        return {"search_toggle": search_toggle, "selected_model": selected_model, "candidate_results": candidate_results, "send": sent}
    if quota_blockers:
        raise GoogleQuotaBlocker(
            source="playground",
            label="playground-search-chat-candidates",
            status=int(quota_blockers[-1].get("status") or 200),
            text="\n".join(str(blocker.get("text_preview") or "") for blocker in quota_blockers),
            extra={
                "quota_or_unavailable_scope": "model_candidates_exhausted",
                "model_candidates": dedupe_model_candidates(model_candidates),
                "failed_candidate_reasons": quota_blockers,
            },
        )
    raise AssertionError(f"no selectable Playground search model candidate from {dedupe_model_candidates(model_candidates)}")


def exercise_local_studio_interface_matrix_ui(
    page,
    base_url: str,
    provider_label: str,
    model: str,
    actions: list[str],
    *,
    provider_kind: str,
    preferred_prefixes: list[str] | None = None,
) -> dict[str, object]:
    results: dict[str, object] = {}

    def run_mode(key: str, interface_label: str, *, allow_error: bool = False, search: bool = False, stream: bool = False) -> dict[str, object]:
        try:
            selected = select_local_studio_interface_model(
                page,
                actions,
                interface_label=interface_label,
                model=model or None,
                load_label=f"{provider_kind}-{key}",
                preferred_prefixes=preferred_prefixes,
            )
            set_local_studio_runtime_toggle(page, "Stream", stream, actions)
            set_local_studio_runtime_toggle(page, "Web search", search, actions)
            set_local_studio_image_tool(page, False, actions)
            select_local_studio_native_option(page, "Effort", value="off")
            prompt = f"Reply with exactly: nexus-{provider_kind}-{key}-ok-{int(time.time())}"
            sent = send_local_studio_message_with_transient_retry(
                page,
                prompt,
                f"{provider_kind}-{key}",
                allow_error=allow_error,
                actions=actions,
                attempts=2,
                new_conversation_each_attempt=True,
            )
            repeat_sent = None
            if "repeat" in key:
                repeat_sent = send_local_studio_message_with_transient_retry(
                    page,
                    prompt,
                    f"{provider_kind}-{key}-repeat",
                    allow_error=allow_error,
                    actions=actions,
                    attempts=2,
                    new_conversation_each_attempt=True,
                )
            health = page.request.get(f"{base_url}/health", timeout=30_000)
            result = {"selection": selected, "send": sent, "repeat_send": repeat_sent, "health_status": health.status, "allow_error": allow_error, "search": search, "stream": stream}
        except (AssertionError, PlaywrightTimeoutError) as exc:
            if not allow_error:
                raise
            health = page.request.get(f"{base_url}/health", timeout=30_000)
            result = {
                "selection_error_prefix": str(exc)[:500],
                "send": {"outcome": "error", "assistant_error_prefix": str(exc)[:500], "chat_response_status": 400},
                "repeat_send": None,
                "health_status": health.status,
                "allow_error": True,
                "controlled_before_send": True,
                "search": search,
                "stream": stream,
            }
            actions.append(f"controlled local studio interface matrix failure {provider_kind}-{key}: {type(exc).__name__}")
        results[key] = result
        return result

    click_select_option(page, "Provider", provider_label)
    google_provider = provider_kind == "google"
    results["gemini_basic"] = run_mode("gemini-basic", "Gemini", allow_error=not google_provider, search=False, stream=False)
    results["gemini_search_repeat"] = run_mode("gemini-search-repeat", "Gemini", allow_error=not google_provider, search=True, stream=True)
    results["openai_chat_basic"] = run_mode("openai-chat-basic", "OpenAI 兼容", allow_error=False, search=False, stream=False)
    results["openai_chat_search_repeat"] = run_mode("openai-chat-search-repeat", "OpenAI 兼容", allow_error=False, search=True, stream=True)
    results["claude_basic"] = run_mode("claude-basic", "Claude", allow_error=True, search=False, stream=False)
    results["claude_search_repeat"] = run_mode("claude-search-repeat", "Claude", allow_error=True, search=True, stream=True)
    actions.append(f"local studio interface matrix provider={provider_kind}")
    return results


def interface_matrix_entry_ok(entry: dict[str, object], *, allow_error: bool = False) -> bool:
    sent = entry.get("send") if isinstance(entry.get("send"), dict) else {}
    repeat_sent = entry.get("repeat_send") if isinstance(entry.get("repeat_send"), dict) else None
    if allow_error and entry.get("controlled_before_send") is True:
        return bool(entry.get("health_status") == 200 and str(entry.get("selection_error_prefix") or "").strip())
    return bool(
        entry.get("health_status") == 200
        and local_studio_result_controlled(sent, allow_error=allow_error)
        and (repeat_sent is None or local_studio_result_controlled(repeat_sent, allow_error=allow_error))
    )


def controlled_interface_matrix_400_count(results: dict[str, object]) -> int:
    count = 0
    for entry in results.values():
        if not isinstance(entry, dict) or entry.get("allow_error") is not True:
            continue
        for key in ("send", "repeat_send"):
            sent = entry.get(key)
            if isinstance(sent, dict) and sent.get("outcome") == "error" and int(sent.get("chat_response_status") or 0) == 400:
                count += 1
    return count


def reasoning_matrix_summary_visible(result: dict[str, object]) -> bool:
    return bool(
        ((result.get("stream") or {}).get("assistant_state") or {}).get("reasoning_summary_visible")
        or ((result.get("nonstream") or {}).get("assistant_state") or {}).get("reasoning_summary_visible")
        or ((result.get("repeat") or {}).get("assistant_state") or {}).get("reasoning_summary_visible")
        or (result.get("stream_refresh_state") or {}).get("reasoning_summary_visible")
    )


def exercise_local_studio_reasoning_matrix_attempt_ui(
    page,
    provider_label: str,
    model: str,
    actions: list[str],
    *,
    provider_kind: str,
    preferred_prefixes: list[str] | None = None,
) -> dict[str, object]:
    click_select_option(page, "Provider", provider_label)
    selected: dict[str, object] = {}
    selected_after_refresh: dict[str, object] = {}
    runtime_stream: dict[str, object] = {}
    runtime_nonstream: dict[str, object] = {}
    effort: dict[str, object] = {}
    summary: dict[str, object] = {}
    isolated_conversation: dict[str, object] = {}
    stream_result: dict[str, object] = {}
    stream_refresh_state: dict[str, object] = {}
    nonstream_result: dict[str, object] = {}
    repeat_result: dict[str, object] = {}
    failure_prefix = ""
    stream_prompt = "请分步骤判断 17*23 是否大于 390，并给出简短结论。"
    try:
        selected = select_local_studio_interface_model(
            page,
            actions,
            interface_label="OpenAI Responses",
            model=model or None,
            load_label=f"{provider_kind}-responses-reasoning",
            preferred_prefixes=preferred_prefixes,
        )
        set_local_studio_image_tool(page, False, actions)
        runtime_stream = set_local_studio_runtime_toggle(page, "Stream", True, actions)
        set_local_studio_runtime_toggle(page, "Web search", False, actions)
        effort = select_local_studio_native_option(page, "Effort", value="high", preferred_values=["high", "medium", "low"])
        summary = select_local_studio_native_option(page, "Summary", value="auto", preferred_values=["auto", "concise", "detailed"])
        isolated_conversation = create_local_studio_conversation_via_ui(page, actions, f"{provider_kind}-reasoning")
        stream_result = send_local_studio_message(page, stream_prompt, f"{provider_kind}-reasoning-stream")
        page.reload(wait_until="networkidle", timeout=90_000)
        expect(page.locator("#studio-page.active")).to_be_visible(timeout=30_000)
        stream_refresh_state = latest_local_studio_assistant_state(page)
        selected_model = str(selected.get("model") or model or "")
        click_select_option(page, "Provider", provider_label)
        selected_after_refresh = select_local_studio_interface_model(
            page,
            actions,
            interface_label="OpenAI Responses",
            model=selected_model or None,
            load_label=f"{provider_kind}-responses-reasoning-after-refresh",
            preferred_prefixes=preferred_prefixes,
        )
        set_local_studio_image_tool(page, False, actions)
        set_local_studio_runtime_toggle(page, "Web search", False, actions)
        select_local_studio_native_option(page, "Effort", value="high", preferred_values=["high", "medium", "low"])
        select_local_studio_native_option(page, "Summary", value="auto", preferred_values=["auto", "concise", "detailed"])
        runtime_nonstream = set_local_studio_runtime_toggle(page, "Stream", False, actions)
        nonstream_result = send_local_studio_message(page, stream_prompt, f"{provider_kind}-reasoning-nonstream")
        repeat_result = send_local_studio_message(page, stream_prompt, f"{provider_kind}-reasoning-repeat")
        actions.append(f"local studio reasoning matrix provider={provider_kind}")
    except (AssertionError, PlaywrightTimeoutError) as exc:
        failure_prefix = str(exc)[:500]
        actions.append(f"local studio reasoning matrix failure provider={provider_kind}: {type(exc).__name__}")
    result = {
        "selection": selected,
        "selection_after_refresh": selected_after_refresh,
        "runtime_stream": runtime_stream,
        "runtime_nonstream": runtime_nonstream,
        "effort": effort,
        "summary": summary,
        "isolated_conversation": isolated_conversation,
        "stream": stream_result,
        "stream_refresh_state": stream_refresh_state,
        "nonstream": nonstream_result,
        "repeat": repeat_result,
        "failure_prefix": failure_prefix,
        "controls_available": bool(effort.get("available") is True and summary.get("available") is True),
        "refresh_restored_assistant": bool(stream_refresh_state.get("assistant_visible") is True),
        "all_text_visible": all(
            local_studio_result_controlled(item)
            for item in (stream_result, nonstream_result, repeat_result)
        ),
    }
    result["any_reasoning_summary_visible"] = reasoning_matrix_summary_visible(result)
    return result


def exercise_local_studio_reasoning_matrix_ui(
    page,
    provider_label: str,
    model: str,
    actions: list[str],
    *,
    provider_kind: str,
    preferred_prefixes: list[str] | None = None,
) -> dict[str, object]:
    max_attempts = 5 if provider_kind == "openai" else 1
    attempts: list[dict[str, object]] = []
    final_result: dict[str, object] = {}
    for attempt_index in range(1, max_attempts + 1):
        result = exercise_local_studio_reasoning_matrix_attempt_ui(
            page,
            provider_label,
            model,
            actions,
            provider_kind=provider_kind,
            preferred_prefixes=preferred_prefixes,
        )
        result["attempt"] = attempt_index
        attempts.append(result)
        final_result = result
        if reasoning_matrix_ui_ok(result):
            break
        if provider_kind != "openai":
            break
        missing: list[str] = []
        if not reasoning_matrix_summary_visible(result):
            missing.append("visible reasoning summary")
        if result.get("all_text_visible") is not True:
            missing.append("stream/nonstream/repeat visible text")
        if result.get("failure_prefix"):
            missing.append("transient provider error")
        actions.append(f"retry local studio reasoning matrix provider={provider_kind} missing={'+'.join(missing) or 'unknown'} attempt={attempt_index}")
        page.wait_for_timeout(min(5_000, 1_000 * attempt_index))
    final_result = dict(final_result)
    final_result["attempts"] = attempts
    final_result["attempt_count"] = len(attempts)
    final_result["any_reasoning_summary_visible"] = reasoning_matrix_summary_visible(final_result)
    return final_result


def reasoning_matrix_ui_ok(result: dict[str, object]) -> bool:
    return bool(
        result.get("controls_available") is True
        and result.get("all_text_visible") is True
        and result.get("refresh_restored_assistant") is True
        and result.get("any_reasoning_summary_visible") is True
    )


def local_studio_success_no_images(result: dict[str, object]) -> bool:
    state = result.get("assistant_state") if isinstance(result.get("assistant_state"), dict) else {}
    return bool(local_studio_result_controlled(result) and int(state.get("image_count") or 0) == 0)


def local_studio_success_images(result: dict[str, object], *, exact_count: int | None = 1) -> bool:
    state = result.get("assistant_state") if isinstance(result.get("assistant_state"), dict) else {}
    image_count = int(state.get("image_count") or 0)
    count_ok = image_count > 0 if exact_count is None else image_count == exact_count
    return bool(int(result.get("chat_response_status") or 500) < 400 and result.get("outcome") == "success" and count_ok)


def local_studio_controlled_image_tool_result(result: dict[str, object]) -> bool:
    return bool(
        local_studio_success_images(result)
        or (
            result.get("outcome") == "error"
            and int(result.get("chat_response_status") or 500) < 500
            and str(result.get("assistant_error_prefix") or "").strip()
            and int(result.get("health_status") or 500) == 200
        )
    )


def click_custom_select_option(
    page,
    button_locator,
    option_text: str | None = None,
    preferred_prefixes: list[str] | None = None,
    excluded_texts: list[str] | None = None,
) -> str:
    expect(button_locator).to_be_visible(timeout=30_000)
    button_locator.click()
    menu = page.locator(".cselect.open .cselect-menu").first
    expect(menu).to_be_visible(timeout=10_000)
    options = menu.locator(".cselect-opt:not([aria-disabled='true'])")
    count = options.count()
    if count <= 0:
        raise AssertionError("no selectable options in custom select")
    if option_text:
        matches = menu.locator(".cselect-opt:not([aria-disabled='true'])", has_text=option_text)
        if matches.count() > 0:
            selected = visible_text(matches.first)
            matches.first.click()
            return selected
    texts = [visible_text(options.nth(index)) for index in range(count)]
    excluded = [text for text in (excluded_texts or []) if text]
    for prefix in preferred_prefixes or []:
        if not prefix:
            continue
        for index, text in enumerate(texts):
            if any(text == excluded_text or excluded_text in text for excluded_text in excluded):
                continue
            if text.startswith(prefix) or prefix in text:
                options.nth(index).click()
                return text
    for index, text in enumerate(texts):
        if any(text == excluded_text or excluded_text in text for excluded_text in excluded):
            continue
        options.nth(index).click()
        return text
    selected = texts[0]
    options.first.click()
    return selected


def select_global_interface_mode(page, label: str, actions: list[str]) -> str:
    selected = click_custom_select_option(page, page.locator(".api-toolbar .api-select .cselect-btn").first, label)
    actions.append(f"select global interface mode: {selected}")
    return selected


def refresh_image_page_models(page, actions: list[str]) -> int:
    button = page.locator(".image-page.active .image-model-picker-row .model-refresh-btn").first
    expect(button).to_be_enabled(timeout=30_000)
    with page.expect_response(lambda response: response_path(response) in {"/v1beta/models", "/v1/models"}, timeout=180_000) as response_info:
        button.click()
    response = response_info.value
    actions.append(f"refresh #images models status={response.status}")
    return int(response.status)


def select_image_page_model(page, preferred_prefixes: list[str] | None = None, excluded_texts: list[str] | None = None) -> str:
    return click_custom_select_option(
        page,
        page.locator(".image-page.active .image-model-control .cselect-btn").first,
        None,
        preferred_prefixes=preferred_prefixes,
        excluded_texts=excluded_texts,
    )


def image_page_urls(page) -> list[str]:
    images = page.locator(".image-page.active .image-result-gallery img")
    urls: list[str] = []
    for index in range(images.count()):
        source = images.nth(index).get_attribute("src") or ""
        if source:
            urls.append(source)
    return urls


def exercise_local_studio_google_image_generation_ui(page, google_model: str, actions: list[str]) -> dict[str, object]:
    results: dict[str, object] = {}

    def configure_google_image_case(*, stream: bool, search: bool, label: str) -> dict[str, object]:
        provider = click_select_option(page, "Provider", "Google AI Studio")
        selected = select_local_studio_interface_model(
            page,
            actions,
            interface_label="OpenAI Responses",
            model=google_model or None,
            load_label=label,
        )
        runtime_stream = set_local_studio_runtime_toggle(page, "Stream", stream, actions)
        runtime_search = set_local_studio_runtime_toggle(page, "Web search", search, actions)
        image_tool = set_local_studio_image_tool(page, True, actions)
        effort = select_local_studio_native_option(page, "Effort", value="off")
        image_model = select_local_studio_native_option(
            page,
            "Image Model",
            preferred_values=["gemini-3.1-flash-image-preview", "gemini-3-flash-image-preview", "imagen-4.0-generate-preview-06-06"],
        )
        size = select_local_studio_native_option(page, "Size", value="1024x1024")
        quality = select_local_studio_native_option(page, "Quality", preferred_values=["auto", "medium", "high"])
        conversation = create_local_studio_conversation_via_ui(page, actions, label)
        return {
            "provider": provider,
            "selection": selected,
            "runtime_stream": runtime_stream,
            "runtime_search": runtime_search,
            "image_tool": image_tool,
            "effort": effort,
            "image_model": image_model,
            "size": size,
            "quality": quality,
            "conversation": conversation,
        }

    def run_google_image_case_with_retry(label: str, run_case) -> dict[str, object]:
        attempts: list[dict[str, object]] = []
        last_error = ""
        for attempt_index in range(1, 3):
            try:
                result = run_case()
                if attempts:
                    result["attempts"] = attempts
                return result
            except (AssertionError, PlaywrightTimeoutError) as exc:
                last_error = str(exc)
                attempts.append({"attempt": attempt_index, "error_prefix": last_error[:500]})
                if not transient_upstream_error_text(last_error) or attempt_index >= 2:
                    break
                actions.append(f"retry {label} after transient upstream image error attempt={attempt_index}")
                page.wait_for_timeout(2_000)
        return {"outcome": "error", "chat_response_status": 0, "assistant_error_prefix": last_error[:500], "failure_prefix": last_error[:500], "attempts": attempts}

    def run_stream_case() -> dict[str, object]:
        setup = configure_google_image_case(stream=True, search=False, label="google-image-tool-stream-ui")
        sent = send_local_studio_message(
            page,
            "Create exactly one image: a simple blue square icon on a plain white background. Do not return text only.",
            "google-image-tool-stream-ui",
            throttle_google=True,
            actions=actions,
        )
        sent.update({"setup": setup})
        return sent

    results["google_image_tool_stream"] = run_google_image_case_with_retry("google image tool stream UI", run_stream_case)
    if (results["google_image_tool_stream"] or {}).get("outcome") != "success":
        actions.append("google image tool stream UI failed after retries")

    def run_multiturn_case() -> dict[str, object]:
        setup = configure_google_image_case(stream=False, search=False, label="google-search-image-multiturn-ui")
        turn_hello = send_local_studio_message(page, "你好，只回复一句简短问候。", "google-search-image-multiturn-hello-ui", throttle_google=True, actions=actions)
        turn_identity = send_local_studio_message(page, "你是谁？请用一句话回答。", "google-search-image-multiturn-identity-ui", throttle_google=True, actions=actions)
        set_local_studio_runtime_toggle(page, "Web search", True, actions)
        turn_news = send_local_studio_message(page, "请搜索今天 AI 技术新闻并用一句中文概括。", "google-search-image-multiturn-news-ui", throttle_google=True, actions=actions)
        set_local_studio_runtime_toggle(page, "Stream", True, actions)
        final = send_local_studio_message(
            page,
            "基于刚才的新闻，生成一张只有一个主题图标的方形信息图。只生成一张图片，不要返回多张。",
            "google-search-image-multiturn-ui",
            throttle_google=True,
            actions=actions,
        )
        final.update({"setup": setup, "turns": [turn_hello, turn_identity, turn_news]})
        return final

    results["google_search_image_multiturn"] = run_google_image_case_with_retry("google search image multiturn UI", run_multiturn_case)
    if (results["google_search_image_multiturn"] or {}).get("outcome") != "success":
        actions.append("google search image multiturn UI failed after retries")

    def run_nonstream_case() -> dict[str, object]:
        setup = configure_google_image_case(stream=False, search=True, label="google-search-image-nonstream-ui")
        sent = send_local_studio_message(
            page,
            "Search for current AI hardware news and generate exactly one square infographic image about it. Do not return text only.",
            "google-search-image-nonstream-ui",
            throttle_google=True,
            actions=actions,
        )
        sent.update({"setup": setup})
        return sent

    results["google_search_image_nonstream"] = run_google_image_case_with_retry("google search image nonstream UI", run_nonstream_case)
    if (results["google_search_image_nonstream"] or {}).get("outcome") != "success":
        actions.append("google search image nonstream UI failed after retries")

    actions.append("local studio google image generation visible output matrix")
    return results


def exercise_local_studio_openai_image_generation_ui(page, base_url: str, provider_label: str, openai_model: str, actions: list[str]) -> dict[str, object]:
    result: dict[str, object]
    try:
        provider = click_select_option(page, "Provider", provider_label)
        selected = select_local_studio_interface_model(
            page,
            actions,
            interface_label="OpenAI Responses",
            model=openai_model or None,
            load_label="openai-compatible-image-tool-ui",
            preferred_prefixes=[openai_model, "gpt-5.4-mini", "gpt-5-mini", "gpt-5", "gpt-4.1-mini", "gpt-4o-mini"],
        )
        runtime_stream = set_local_studio_runtime_toggle(page, "Stream", True, actions)
        runtime_search = set_local_studio_runtime_toggle(page, "Web search", False, actions)
        image_tool = set_local_studio_image_tool(page, True, actions)
        effort = select_local_studio_native_option(page, "Effort", value="off")
        image_model = select_local_studio_native_option(page, "Image Model")
        size = select_local_studio_native_option(page, "Size", value="1024x1024")
        quality = select_local_studio_native_option(page, "Quality", preferred_values=["auto", "medium", "high"])
        background = select_local_studio_native_option(page, "Background", preferred_values=["auto", "opaque", "transparent"])
        fmt = select_local_studio_native_option(page, "Format", preferred_values=["png", "webp", "jpeg"])
        conversation = create_local_studio_conversation_via_ui(page, actions, "openai-compatible-image-tool-ui")
        sent = send_local_studio_message(
            page,
            "Create exactly one image: a clean blue square icon on a white background. If this provider does not support image tools, show the controlled error.",
            "openai-compatible-image-tool-ui",
            allow_error=True,
            timeout_ms=420_000,
        )
        health = page.request.get(f"{base_url}/health", timeout=30_000)
        sent.update({
            "provider": provider,
            "selection": selected,
            "runtime_stream": runtime_stream,
            "runtime_search": runtime_search,
            "image_tool": image_tool,
            "effort": effort,
            "image_model": image_model,
            "size": size,
            "quality": quality,
            "background": background,
            "format": fmt,
            "conversation": conversation,
            "health_status": health.status,
        })
        result = sent
    except (AssertionError, PlaywrightTimeoutError) as exc:
        health = page.request.get(f"{base_url}/health", timeout=30_000)
        result = {
            "outcome": "error",
            "chat_response_status": 400,
            "assistant_error_prefix": str(exc)[:500],
            "failure_prefix": str(exc)[:500],
            "health_status": health.status,
            "controlled_before_send": True,
        }
        actions.append(f"openai-compatible image tool UI controlled failure: {type(exc).__name__}")
    actions.append("local studio openai-compatible image tool visible controlled path")
    return result


def attach_local_studio_file_for_send(page, file_path: Path, actions: list[str]) -> dict[str, object]:
    attach_button = page.locator("#studio-page .local-studio-compose-row .attach-btn")
    expect(attach_button).to_be_visible(timeout=30_000)
    with page.expect_file_chooser(timeout=30_000) as chooser_info:
        attach_button.click()
    chooser_info.value.set_files(str(file_path))
    chip = page.locator("#studio-page .local-studio-file-strip .chat-attachment", has_text=file_path.name).first
    expect(chip).to_be_visible(timeout=30_000)
    actions.append(f"local studio attach file for send {file_path.name}")
    return {"file_name": file_path.name, "chip_visible": True}


def exercise_local_studio_cache_isolation_ui(
    page,
    artifact_dir: Path,
    base_url: str,
    *,
    google_model: str,
    openai_provider_label: str,
    openai_model: str,
    actions: list[str],
) -> dict[str, object]:
    prompt = "Reply with exactly: nexus-cache-ui-isolation-ok"
    variations: dict[str, dict[str, object]] = {}
    attachment_path = artifact_dir / "host-ui-cache-isolation-attachment.txt"
    attachment_path.write_text("nexus-system-test-cache-isolation-attachment\n", encoding="utf-8")

    def run_variation(
        key: str,
        *,
        provider_label: str,
        provider_kind: str,
        interface_label: str,
        model: str,
        stream: bool,
        search: bool,
        image_tool: bool,
        effort: str = "off",
        attach_file: bool = False,
        prompt_override: str | None = None,
    ) -> dict[str, object]:
        try:
            provider = click_select_option(page, "Provider", provider_label)
            selected = select_local_studio_interface_model(
                page,
                actions,
                interface_label=interface_label,
                model=model or None,
                load_label=f"cache-ui-{key}",
                preferred_prefixes=[model] if model else None,
            )
            runtime_stream = set_local_studio_runtime_toggle(page, "Stream", stream, actions)
            runtime_search = set_local_studio_runtime_toggle(page, "Web search", search, actions)
            image_tool_state = set_local_studio_image_tool(page, image_tool, actions)
            effort_state = select_local_studio_native_option(page, "Effort", value=effort, preferred_values=[effort, "off", "medium", "high"])
            summary_state = select_local_studio_native_option(page, "Summary", value="auto", preferred_values=["auto", "concise", "detailed"])
            conversation = create_local_studio_conversation_via_ui(page, actions, f"cache-ui-{key}")
            attachment_state: dict[str, object] = {}
            if attach_file:
                attachment_state = attach_local_studio_file_for_send(page, attachment_path, actions)
            sent = send_local_studio_message(
                page,
                prompt_override or ("Read the attached text and reply with exactly: nexus-cache-ui-isolation-ok" if attach_file else prompt),
                f"cache-ui-{key}",
                throttle_google=provider_kind == "google",
                actions=actions,
            )
            user_attachment_visible = True
            if attach_file:
                user_attachment = page.locator("#studio-page .local-studio-message.is-user .local-studio-file", has_text=attachment_path.name).last
                user_attachment_visible = locator_is_visible(user_attachment, timeout_ms=30_000)
            health = page.request.get(f"{base_url}/health", timeout=30_000)
            result = {
                "provider": provider,
                "selection": selected,
                "runtime_stream": runtime_stream,
                "runtime_search": runtime_search,
                "image_tool": image_tool_state,
                "effort": effort_state,
                "summary": summary_state,
                "conversation": conversation,
                "attachment": attachment_state,
                "user_attachment_visible": user_attachment_visible,
                "send": sent,
                "health_status": health.status,
                "ok": bool(local_studio_success_no_images(sent) and health.status == 200 and (not attach_file or user_attachment_visible)),
            }
        except (AssertionError, PlaywrightTimeoutError) as exc:
            health = page.request.get(f"{base_url}/health", timeout=30_000)
            result = {"ok": False, "failure_prefix": str(exc)[:500], "health_status": health.status}
            actions.append(f"cache isolation UI variation failed key={key}: {type(exc).__name__}")
        variations[key] = result
        return result

    run_variation(
        "google-provider",
        provider_label="Google AI Studio",
        provider_kind="google",
        interface_label="OpenAI Responses",
        model=google_model,
        stream=False,
        search=False,
        image_tool=False,
    )
    run_variation(
        "openai-provider",
        provider_label=openai_provider_label,
        provider_kind="openai",
        interface_label="OpenAI Responses",
        model=openai_model,
        stream=False,
        search=False,
        image_tool=False,
    )
    run_variation(
        "openai-interface",
        provider_label=openai_provider_label,
        provider_kind="openai",
        interface_label="OpenAI 兼容",
        model=openai_model,
        stream=False,
        search=False,
        image_tool=False,
    )
    run_variation(
        "openai-search-image-reasoning",
        provider_label=openai_provider_label,
        provider_kind="openai",
        interface_label="OpenAI Responses",
        model=openai_model,
        stream=False,
        search=True,
        image_tool=True,
        effort="high",
        prompt_override="Do not search and do not create an image. Reply with exactly: nexus-cache-ui-isolation-ok",
    )
    run_variation(
        "openai-token-compatible-shape",
        provider_label=openai_provider_label,
        provider_kind="openai",
        interface_label="OpenAI Responses",
        model=openai_model,
        stream=True,
        search=False,
        image_tool=False,
    )
    run_variation(
        "openai-attachment",
        provider_label=openai_provider_label,
        provider_kind="openai",
        interface_label="OpenAI Responses",
        model=openai_model,
        stream=False,
        search=False,
        image_tool=False,
        attach_file=True,
    )
    ok = all(item.get("ok") is True for item in variations.values()) and len(variations) >= 6
    actions.append(f"local studio visible cache isolation matrix ok={ok}")
    return {
        "ok": ok,
        "variation_count": len(variations),
        "covered_dimensions": ["provider", "interface", "model", "search", "image_tool", "reasoning", "attachment", "token-compatible-shape"],
        "variations": variations,
    }


def exercise_base_image_generation_ui(
    page,
    artifact_dir: Path,
    actions: list[str],
    base_url: str = "",
    google_account_alignment: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {"generation_ok": False, "edit_retry_ok": False}
    try:
        navigate_visible_page(page, "图片生成", ".image-page.active", "navigate #images for generation", actions)
        new_session_button = page.locator(".image-page.active .image-new-session-btn").first
        if locator_is_visible(new_session_button, timeout_ms=2_000) and new_session_button.is_enabled():
            new_session_button.click()
            expect(page.locator(".image-page.active .image-result-gallery img")).to_have_count(0, timeout=10_000)
        selected_interface = select_global_interface_mode(page, "Gemini", actions)
        models_status = refresh_image_page_models(page, actions)
        selected_model = select_image_page_model(page, ["gemini-3.1-flash-image-preview", "gemini-3-flash-image-preview", "imagen", "image"])
        count_input = page.locator(".image-page.active .image-count-control input.image-count").first
        expect(count_input).to_be_visible(timeout=30_000)
        count_input.fill("1")
        prompt = page.locator(".image-page.active textarea.image-prompt")
        expect(prompt).to_be_visible(timeout=30_000)
        prompt_text = f"A simple blue square icon on a white background, system test marker {int(time.time())}"
        prompt.fill(prompt_text)
        before_images = page.locator(".image-page.active .image-result-gallery img").count()
        throttle_google_request("base-images-ui", actions)
        with page.expect_response(lambda response: response_path(response) == "/v1/images/generations", timeout=420_000) as response_info:
            page.locator(".image-page.active .image-generate-btn").click()
        generation_response = response_info.value
        raw_text = ""
        try:
            raw_text = generation_response.text()
        except Exception:
            raw_text = ""
        if generation_response.status >= 400:
            error_text = visible_text(page.locator(".image-page.active .image-error")) or raw_text
            if google_quota_exhausted_text(error_text):
                raise GoogleQuotaBlocker(source="base-images-ui", label="base-images-generation-ui", status=generation_response.status, text=error_text)
        after_images = wait_for_count_above(page, page.locator(".image-page.active .image-result-gallery img"), before_images, timeout_ms=420_000)
        result_card = page.locator(".image-page.active .image-result-card").first
        expect(result_card).to_be_visible(timeout=30_000)
        result.update({
            "generation_ok": bool(generation_response.status < 400 and after_images > before_images),
            "interface": selected_interface,
            "models_status": models_status,
            "selected_model": selected_model,
            "generation_status": generation_response.status,
            "visible_image_count_after_generation": after_images,
            "image_urls_after_generation": image_page_urls(page),
        })
        result_card.get_by_role("button", name="设为基图").click()
        expect(page.locator(".image-page.active .image-ref-base img")).to_be_visible(timeout=30_000)
        result_card.get_by_role("button", name="作参考").click()
        expect(page.locator(".image-page.active .image-ref-thumb", has_text="参考").first).to_be_visible(timeout=30_000)
        timeout_input = page.locator(".image-page.active .image-timeout-control input").first
        if locator_is_visible(timeout_input, timeout_ms=2_000):
            timeout_input.fill("240")
        edit_attempts: list[dict[str, object]] = []
        retry_response = None
        last_edit_error = ""
        edit_model_attempts = [selected_model]
        account_recoveries: list[dict[str, object]] = []
        for edit_attempt in range(1, 4):
            trigger = result_card.get_by_role("button", name="重试") if edit_attempt == 1 else page.locator(".image-page.active .image-error").get_by_role("button", name="重试")
            expect(trigger).to_be_enabled(timeout=30_000)
            with page.expect_response(lambda response: response_path(response) == "/v1/images/generations", timeout=420_000) as retry_response_info:
                trigger.click()
            retry_response = retry_response_info.value
            retry_raw = ""
            if retry_response.status >= 400:
                try:
                    retry_raw = retry_response.text()
                except Exception:
                    retry_raw = ""
                error_text = visible_text(page.locator(".image-page.active .image-error")) or retry_raw
                last_edit_error = error_text
                edit_attempts.append({"attempt": edit_attempt, "model": selected_model, "status": retry_response.status, "error_prefix": error_text[:300]})
                result.update({"retry_status": retry_response.status, "edit_retry_attempts": edit_attempts, "edit_model_attempts": edit_model_attempts, "last_edit_error_prefix": last_edit_error[:500]})
                if google_quota_exhausted_text(error_text):
                    raise GoogleQuotaBlocker(source="base-images-ui", label="base-images-retry-ui", status=retry_response.status, text=error_text)
                if google_permission_error_text(error_text) and base_url and google_account_alignment and edit_attempt < 3:
                    recovery = recover_aligned_account_after_unavailable(
                        page.request,
                        base_url,
                        google_account_alignment,
                        actions,
                        f"base-images-retry-ui-{edit_attempt}",
                    )
                    account_recoveries.append(recovery)
                    result.update({"edit_account_recoveries": account_recoveries})
                    if recovery.get("ok"):
                        actions.append(f"base #images edit retry after google permission recovery attempt={edit_attempt}")
                        page.wait_for_timeout(2_000)
                        continue
                if transient_upstream_error_text(error_text) and edit_attempt < 3:
                    actions.append(f"base #images edit retry after transient upstream error attempt={edit_attempt}")
                    page.wait_for_timeout(2_000 + edit_attempt * 2_000)
                    continue
                break
            edit_attempts.append({"attempt": edit_attempt, "model": selected_model, "status": retry_response.status, "error_prefix": ""})
            result.update({"retry_status": retry_response.status, "edit_retry_attempts": edit_attempts, "edit_model_attempts": edit_model_attempts, "last_edit_error_prefix": ""})
            break
        assert retry_response is not None
        if retry_response.status >= 400 and (transient_upstream_error_text(last_edit_error) or google_permission_error_text(last_edit_error)):
            fallback_model = select_image_page_model(
                page,
                ["gemini-3-pro-image-preview", "gemini-3.1-flash-image-preview", "imagen", "image"],
                excluded_texts=[selected_model],
            )
            if fallback_model != selected_model:
                selected_model = fallback_model
                edit_model_attempts.append(selected_model)
                actions.append(f"base #images edit fallback image model: {selected_model}")
                for fallback_attempt in range(1, 3):
                    trigger = page.locator(".image-page.active .image-generate-btn")
                    expect(trigger).to_be_enabled(timeout=30_000)
                    with page.expect_response(lambda response: response_path(response) == "/v1/images/generations", timeout=420_000) as fallback_response_info:
                        trigger.click()
                    retry_response = fallback_response_info.value
                    if retry_response.status >= 400:
                        try:
                            fallback_raw = retry_response.text()
                        except Exception:
                            fallback_raw = ""
                        last_edit_error = visible_text(page.locator(".image-page.active .image-error")) or fallback_raw
                        edit_attempts.append({"attempt": f"fallback-{fallback_attempt}", "model": selected_model, "status": retry_response.status, "error_prefix": last_edit_error[:300]})
                        result.update({"retry_status": retry_response.status, "edit_retry_attempts": edit_attempts, "edit_model_attempts": edit_model_attempts, "last_edit_error_prefix": last_edit_error[:500]})
                        if google_quota_exhausted_text(last_edit_error):
                            raise GoogleQuotaBlocker(source="base-images-ui", label="base-images-fallback-ui", status=retry_response.status, text=last_edit_error)
                        if google_permission_error_text(last_edit_error) and base_url and google_account_alignment and fallback_attempt < 2:
                            recovery = recover_aligned_account_after_unavailable(
                                page.request,
                                base_url,
                                google_account_alignment,
                                actions,
                                f"base-images-fallback-ui-{fallback_attempt}",
                            )
                            account_recoveries.append(recovery)
                            result.update({"edit_account_recoveries": account_recoveries})
                            if recovery.get("ok"):
                                actions.append(f"base #images edit fallback retry after google permission recovery attempt={fallback_attempt}")
                                page.wait_for_timeout(2_000)
                                continue
                        if transient_upstream_error_text(last_edit_error) and fallback_attempt < 2:
                            actions.append(f"base #images edit fallback retry after transient upstream error attempt={fallback_attempt}")
                            page.wait_for_timeout(3_000)
                            continue
                        break
                    last_edit_error = ""
                    edit_attempts.append({"attempt": f"fallback-{fallback_attempt}", "model": selected_model, "status": retry_response.status, "error_prefix": ""})
                    result.update({"retry_status": retry_response.status, "edit_retry_attempts": edit_attempts, "edit_model_attempts": edit_model_attempts, "last_edit_error_prefix": ""})
                    break
        if retry_response.status >= 400:
            raise AssertionError(f"base #images edit/retry failed after {len(edit_attempts)} attempts: {last_edit_error[:500]}")
        expect(page.locator(".image-page.active .image-result-gallery img").first).to_be_visible(timeout=420_000)
        history_count = page.locator(".image-page.active .history-gallery img").count()
        session_images = page.locator(".image-page.active .image-session-log img").count()
        page.screenshot(path=str(artifact_dir / "host-ui-base-images.png"), full_page=True)
        actions.append("base #images generate set-base reference retry visible")
        result.update({
            "generation_ok": bool(generation_response.status < 400 and after_images > before_images),
            "edit_retry_ok": bool(retry_response.status < 400 and history_count >= 1 and session_images >= 1),
            "interface": selected_interface,
            "models_status": models_status,
            "selected_model": selected_model,
            "generation_status": generation_response.status,
            "retry_status": retry_response.status,
            "edit_retry_attempts": edit_attempts,
            "edit_model_attempts": edit_model_attempts,
            "edit_account_recoveries": account_recoveries,
            "visible_image_count_after_generation": after_images,
            "history_image_count": history_count,
            "session_image_count": session_images,
            "image_urls": image_page_urls(page),
        })
    except (AssertionError, PlaywrightTimeoutError) as exc:
        try:
            page.screenshot(path=str(artifact_dir / "host-ui-base-images.png"), full_page=True)
        except Exception:
            pass
        result.update({"failure_prefix": str(exc)[:500]})
        actions.append(f"base #images visible generation failed: {type(exc).__name__}")
    return result


def interface_matrix_ok(
    results: dict[str, object],
    *,
    strict_keys: tuple[str, ...],
    controlled_keys: tuple[str, ...],
) -> bool:
    for key in strict_keys:
        entry = results.get(key) if isinstance(results.get(key), dict) else {}
        if not interface_matrix_entry_ok(entry, allow_error=False):
            return False
    for key in controlled_keys:
        entry = results.get(key) if isinstance(results.get(key), dict) else {}
        if not interface_matrix_entry_ok(entry, allow_error=True):
            return False
    return True


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
    openai_reasoning_model = os.environ.get("OPENAI_COMPAT_REASONING_MODEL", "").strip()
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
    google_interval_seconds = google_request_interval_seconds()

    console_errors: list[str] = []
    network_events: list[dict[str, object]] = []
    actions: list[str] = [f"launch chromium headless={headless} slow_mo_ms={slow_mo_ms} google_interval_seconds={google_interval_seconds}"]
    provider_results: list[dict[str, object]] = []
    local_google_performance_samples: list[dict[str, object]] = []
    local_google_performance_candidate_results: list[dict[str, object]] = []
    official_ai_studio_result: dict[str, object] = {}
    local_google_account_alignment: dict[str, object] = {}
    performance_comparison_result: dict[str, object] = {}
    google_model_candidates = system_test_google_model_candidates(google_model)
    google_selected_model = ""
    runtime_controls: dict[str, dict[str, object]] = {}
    reasoning_controls: dict[str, dict[str, object]] = {}
    image_tool_controls: dict[str, dict[str, object]] = {}
    provider_persistence_result: dict[str, object] = {}
    google_repeat_results: list[dict[str, object]] = []
    optional_tool_text_results: dict[str, dict[str, object]] = {}
    reasoning_matrix_results: dict[str, dict[str, object]] = {}
    interface_matrix_results: dict[str, dict[str, object]] = {}
    attachment_preview_result: dict[str, object] = {}
    invalid_provider_recovery_result: dict[str, object] = {}
    conversation_crud_result: dict[str, object] = {}
    request_log_result: dict[str, object] = {}
    account_health_result: dict[str, object] = {}
    playground_result: dict[str, object] = {}
    provider_manager_result: dict[str, object] = {}
    playground_search_result: dict[str, object] = {}
    local_studio_image_generation_results: dict[str, object] = {}
    base_image_result: dict[str, object] = {}
    cache_isolation_result: dict[str, object] = {}

    def build_visible_result(*, partial: bool = False, partial_reason: str = "") -> dict[str, object]:
        retried_model_load_502_count = sum(1 for action in actions if "models load attempt=" in action and "status=502" in action)
        invalid_provider_error_status = int(((invalid_provider_recovery_result.get("error_result") or {}).get("chat_response_status") or 0))
        expected_invalid_provider_console_502_count = 2 if invalid_provider_error_status >= 500 else 0
        expected_controlled_console_400_count = sum(
            controlled_interface_matrix_400_count(result)
            for result in interface_matrix_results.values()
            if isinstance(result, dict)
        )
        openai_image_tool_ui = local_studio_image_generation_results.get("openai_image_tool") if isinstance(local_studio_image_generation_results.get("openai_image_tool"), dict) else {}
        if (
            openai_image_tool_ui.get("outcome") == "error"
            and not local_studio_success_images(openai_image_tool_ui)
            and 400 <= int(openai_image_tool_ui.get("chat_response_status") or 0) < 500
        ):
            expected_controlled_console_400_count += 1
        allowed_console_errors, unexpected_console_errors = split_console_errors(
            console_errors,
            allowed_502_count=expected_invalid_provider_console_502_count + retried_model_load_502_count,
            allowed_400_count=expected_controlled_console_400_count,
        )
        official_screenshot_names = []
        if official_ai_studio_result.get("screenshot"):
            official_screenshot_names.append(str(official_ai_studio_result["screenshot"]))
        official_screenshot_names.extend(str(name) for name in official_ai_studio_result.get("screenshots") or [] if name)
        for candidate in official_ai_studio_result.get("candidate_results") or []:
            if isinstance(candidate, dict) and candidate.get("screenshot"):
                official_screenshot_names.append(str(candidate["screenshot"]))
        official_screenshot_names = list(dict.fromkeys(official_screenshot_names))
        return {
            "page_url": f"{base_url}/static/index.html#studio",
            "coverage_scope": "expanded_visible_ui_matrix_subset",
            "full_system_plan_coverage": False,
            "partial": partial,
            "partial_reason": partial_reason,
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
                "playground_search_chat_ui",
                "local_studio_google_repeat_same_prompt_ui",
                "local_studio_optional_search_image_text_ui",
                "local_studio_google_image_generation_visible_output",
                "local_studio_search_image_multiturn_visible_output",
                "local_studio_openai_image_tool_visible_controlled_output",
                "local_studio_reasoning_stream_nonstream_repeat_ui",
                "local_studio_interface_mode_matrix_ui",
                "local_studio_openai_provider_persistence_ui",
                "base_image_generation_reference_retry_ui",
                "local_studio_cache_isolation_matrix_ui",
            ],
            "known_missing_plan_items": [
                "complete_P0_P1_UI_matrix",
                "full_search_image_reasoning_generation_matrix",
                "attachment_send_upstream_matrix",
                "accounts_switch_delete_UI",
                "provider_manager_rollout_phase_gate_and_discovery_health_complete",
            ],
            "browser": {
                "engine": "chromium",
                "headless": headless,
                "slow_mo_ms": slow_mo_ms,
                "hold_seconds": hold_seconds,
                "google_request_interval_seconds": google_interval_seconds,
            },
            "google_model": google_model,
            "google_model_candidates": google_model_candidates,
            "google_selected_model": google_selected_model,
            "openai_key_file": str(openai_key_file),
            "openai_key_file_nonempty": True,
            "openai_base_url": openai_base_url,
            "openai_model_requested": openai_model,
            "openai_reasoning_model_requested": openai_reasoning_model,
            "actions": actions,
            "provider_results": provider_results,
            "google_repeat_results": google_repeat_results,
            "optional_tool_text_results": optional_tool_text_results,
            "reasoning_matrix_results": reasoning_matrix_results,
            "interface_matrix_results": interface_matrix_results,
            "local_studio_image_generation_results": local_studio_image_generation_results,
            "base_image_result": base_image_result,
            "cache_isolation_result": cache_isolation_result,
            "provider_persistence_result": provider_persistence_result,
            "official_ai_studio_result": official_ai_studio_result,
            "local_google_account_alignment": local_google_account_alignment,
            "local_google_performance_samples": local_google_performance_samples,
            "local_google_performance_candidate_results": local_google_performance_candidate_results,
            "performance_comparison_result": performance_comparison_result,
            "runtime_controls": runtime_controls,
            "reasoning_controls": reasoning_controls,
            "image_tool_controls": image_tool_controls,
            "attachment_preview_result": attachment_preview_result,
            "invalid_provider_recovery_result": invalid_provider_recovery_result,
            "conversation_crud_result": conversation_crud_result,
            "playground_result": playground_result,
            "playground_search_result": playground_search_result,
            "request_log_result": request_log_result,
            "account_health_result": account_health_result,
            "provider_manager_result": provider_manager_result,
            "console_errors": console_errors,
            "allowed_console_errors": allowed_console_errors,
            "unexpected_console_errors": unexpected_console_errors,
            "allowed_console_502_count": expected_invalid_provider_console_502_count + retried_model_load_502_count,
            "allowed_console_400_count": expected_controlled_console_400_count,
            "network_events": network_events[-80:],
            "screenshots": [
                "host-ui-google-local-studio.png",
                "host-ui-local-studio-image-generation.png",
                *official_screenshot_names,
                "host-ui-openai-local-studio.png",
                "host-ui-base-images.png",
                "host-ui-cache-isolation.png",
                "host-ui-request-log-crud.png",
                "host-ui-requests.png",
                "host-ui-accounts-health.png",
                "host-ui-accounts.png",
                "host-ui-provider-manager-crud.png",
                "host-ui-local-studio-conversation-crud.png",
                "host-ui-playground-basic-chat.png",
            ],
        }

    def write_visible_result_artifact(*, partial: bool = False, partial_reason: str = "") -> dict[str, object]:
        result = build_visible_result(partial=partial, partial_reason=partial_reason)
        serialized = json.dumps(result, ensure_ascii=False, indent=2)
        if openai_token in serialized:
            raise AssertionError("OpenAI-compatible token leaked into UI result artifact")
        (artifact_dir / "mcp-visible-ui-results.json").write_text(serialized, encoding="utf-8")
        (artifact_dir / "host-performance-comparison-results.json").write_text(
            json.dumps(performance_comparison_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    official_helpers = load_official_ai_studio_helpers(repo_src_dir)
    official_ai_studio_result = load_reused_official_baseline(artifact_dir, actions)
    if not official_ai_studio_result and os.environ.get("HOST_OFFICIAL_BASELINE_ENGINE", "camoufox").strip().lower() == "camoufox":
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
        if official_ai_studio_result.get("result") == "pass":
            local_google_account_alignment = align_local_studio_performance_account(
                page.request,
                base_url,
                str(official_ai_studio_result.get("account_id") or ""),
            )
            actions.append(
                "align local studio google account before base images="
                f"{local_google_account_alignment.get('account_id')} ok={local_google_account_alignment.get('ok')}"
            )
        else:
            local_google_account_alignment = {
                "ok": False,
                "reason": "official baseline invalid; local account alignment skipped",
                "account_id": official_ai_studio_result.get("account_id"),
            }
        provider_manager_result = exercise_provider_manager_crud_ui(page, artifact_dir, actions)
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_provider_manager")
        base_image_result = exercise_base_image_generation_ui(
            page,
            artifact_dir,
            actions,
            base_url,
            local_google_account_alignment,
        )
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_base_images_ui")
        navigate_visible_page(page, "Local Studio", "#studio-page.active", "navigate back #studio", actions)

        google_provider = click_select_option(page, "Provider", "Google AI Studio")
        actions.append(f"select provider: {google_provider}")
        google_interface = click_select_option(page, "Interface", "OpenAI Responses")
        actions.append(f"select interface: {google_interface}")
        google_models_response = load_local_studio_models_with_retries(page, actions, "google")
        if not google_model_candidates:
            google_model_candidates.append(google_model or "")
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
        quota_blockers: list[dict[str, object]] = []
        transient_candidate_blockers: list[dict[str, object]] = []
        for candidate_model in google_model_candidates:
            selected_candidate = click_select_option_if_present(page, "Conversation Model", candidate_model) if candidate_model else ""
            if not selected_candidate:
                local_google_performance_candidate_results.append({
                    "model": candidate_model,
                    "result": "skipped",
                    "reason": "model candidate was not selectable in Local Studio",
                })
                actions.append(f"skip google performance model candidate not selectable: {candidate_model}")
                continue
            google_selected_model = selected_candidate
            actions.append(f"select google model candidate: {google_selected_model}")
            candidate_samples: list[dict[str, object]] = []
            try:
                for sample_index in range(3):
                    create_local_studio_conversation_via_ui(page, actions, f"google-ai-studio-performance-{sample_index + 1}")
                    expected_text = f"nexus-local-aistudio-baseline-ok-{int(time.time())}-{sample_index + 1}"
                    sample = send_local_studio_message_with_transient_retry(
                        page,
                        f"Reply with exactly: {expected_text}",
                        f"google-ai-studio-performance-{sample_index + 1}",
                        actions=actions,
                        attempts=1,
                        new_conversation_each_attempt=True,
                    )
                    sample["expected_text"] = expected_text
                    sample["model_candidate"] = google_selected_model
                    candidate_samples.append(sample)
                    local_google_performance_samples = candidate_samples
                    write_visible_result_artifact(partial=True, partial_reason=f"checkpoint_after_google_performance_sample_{sample_index + 1}")
            except GoogleQuotaBlocker as exc:
                blocker = exc.to_artifact()
                blocker["model_candidate"] = google_selected_model
                quota_blockers.append(blocker)
                local_google_performance_candidate_results.append({
                    "model": google_selected_model,
                    "result": "quota_exhausted",
                    "sample_count_before_quota": len(candidate_samples),
                    "blocker": blocker,
                })
                local_google_performance_samples = []
                actions.append(f"google performance model candidate quota-limited; trying next candidate: {google_selected_model}")
                continue
            except AssertionError as exc:
                if "no available account" in str(exc).lower():
                    recovery = recover_aligned_account_after_unavailable(
                        page.request,
                        base_url,
                        local_google_account_alignment,
                        actions,
                        f"google-ai-studio-performance-{len(candidate_samples) + 1}",
                    )
                    blocker = {
                        "model_candidate": google_selected_model,
                        "error": str(exc)[:500],
                        "sample_count_before_error": len(candidate_samples),
                        "account_recovery": recovery,
                    }
                    transient_candidate_blockers.append(blocker)
                    local_google_performance_candidate_results.append({
                        "model": google_selected_model,
                        "result": "account_unavailable_recovered" if recovery.get("ok") else "account_unavailable",
                        "sample_count_before_error": len(candidate_samples),
                        "blocker": blocker,
                    })
                    if recovery.get("ok"):
                        actions.append(f"google performance model candidate recovered account; trying next candidate: {google_selected_model}")
                        continue
                if not transient_upstream_error_text(str(exc)):
                    raise
                blocker = {
                    "model_candidate": google_selected_model,
                    "error": str(exc)[:500],
                    "sample_count_before_error": len(candidate_samples),
                }
                transient_candidate_blockers.append(blocker)
                local_google_performance_candidate_results.append({
                    "model": google_selected_model,
                    "result": "transient_upstream_error",
                    "sample_count_before_error": len(candidate_samples),
                    "blocker": blocker,
                })
                local_google_performance_samples = []
                actions.append(f"google performance model candidate transient upstream error; trying next candidate: {google_selected_model}")
                continue
            local_google_performance_samples = candidate_samples
            provider_results.extend(candidate_samples)
            local_google_performance_candidate_results.append({
                "model": google_selected_model,
                "result": "pass",
                "sample_count": len(candidate_samples),
            })
            break
        else:
            if quota_blockers and not transient_candidate_blockers:
                raise GoogleQuotaBlocker(
                    source="local-studio",
                    label="google-ai-studio-performance-candidates",
                    status=int(quota_blockers[-1].get("status") or 200),
                    text="\n".join(str(blocker.get("text_preview") or "") for blocker in quota_blockers),
                    extra={
                        "quota_or_unavailable_scope": "model_candidates_exhausted",
                        "model_candidates": google_model_candidates,
                        "failed_candidate_reasons": quota_blockers,
                    },
                )
            if quota_blockers or transient_candidate_blockers:
                raise AssertionError(
                    "google performance model candidates exhausted: "
                    f"quota_blockers={quota_blockers[:3]} transient_blockers={transient_candidate_blockers[:3]}"
                )
            raise AssertionError(f"no selectable Google performance model candidate from {google_model_candidates}")
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
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_google_performance_comparison")
        runtime_controls["google_stream_off"] = set_local_studio_runtime_toggle(page, "Stream", False, actions)
        runtime_controls["google_search_off_repeat"] = set_local_studio_runtime_toggle(page, "Web search", False, actions)
        reasoning_controls["google_effort_off"] = select_local_studio_native_option(page, "Effort", value="off")
        reasoning_controls["google_summary_auto"] = select_local_studio_native_option(page, "Summary", value="auto")
        google_repeat_prompt = f"Reply with exactly: nexus-host-ui-google-repeat-ok-{int(time.time())}"
        for repeat_index in range(2):
            repeat_result = send_local_studio_message(page, google_repeat_prompt, f"google-ai-studio-repeat-{repeat_index + 1}")
            google_repeat_results.append(repeat_result)
            provider_results.append(repeat_result)
        runtime_controls["google_search_on"] = set_local_studio_runtime_toggle(page, "Web search", True, actions)
        image_tool_controls["google_enabled"] = set_local_studio_image_tool(page, True, actions)
        image_tool_controls["google_image_model"] = select_local_studio_native_option(page, "Image Model")
        image_tool_controls["google_size"] = select_local_studio_native_option(page, "Size", value="1024x1024")
        image_tool_controls["google_quality"] = select_local_studio_native_option(page, "Quality", preferred_values=["auto", "medium", "high"])
        google_optional_text = send_local_studio_message(
            page,
            f"Do not create an image. Reply with exactly: nexus-host-ui-google-search-image-text-ok-{int(time.time())}",
            "google-ai-studio-search-image-tool-text",
        )
        optional_tool_text_results["google_search_image_text"] = google_optional_text
        provider_results.append(google_optional_text)
        local_studio_image_generation_results.update(
            exercise_local_studio_google_image_generation_ui(page, google_selected_model, actions)
        )
        page.screenshot(path=str(artifact_dir / "host-ui-local-studio-image-generation.png"), full_page=True)
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_google_image_generation_ui")
        image_tool_controls["google_disabled"] = set_local_studio_image_tool(page, False, actions)
        attachment_preview_result = exercise_local_studio_attachment_preview_ui(page, artifact_dir, actions)
        provider_results.append(send_local_studio_message(page, f"Reply with exactly: nexus-host-ui-google-ok-{int(time.time())}", "google-ai-studio-nonstream-search"))
        reasoning_matrix_results["google"] = exercise_local_studio_reasoning_matrix_ui(
            page,
            "Google AI Studio",
            google_selected_model,
            actions,
            provider_kind="google",
        )
        interface_matrix_results["google"] = exercise_local_studio_interface_matrix_ui(
            page,
            base_url,
            "Google AI Studio",
            google_selected_model,
            actions,
            provider_kind="google",
        )
        google_interface_restore = click_select_option(page, "Interface", "OpenAI Responses")
        load_local_studio_models_with_retries(page, actions, "google restore responses after interface matrix")
        google_model_restore = click_select_option(page, "Conversation Model", google_selected_model)
        actions.append(f"restore google responses after interface matrix: {google_interface_restore} / {google_model_restore}")
        runtime_controls["google_search_off"] = set_local_studio_runtime_toggle(page, "Web search", False, actions)
        page.screenshot(path=str(artifact_dir / "host-ui-google-local-studio.png"), full_page=True)
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_google_local_studio_matrix")

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
            preferred_prefixes=["gpt-5.4-mini", "gpt-5-mini", "gpt-5", "gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1", "gpt-4o", "codex-auto-review"],
        )
        actions.append(f"select openai model: {openai_selected_model}")
        provider_persistence_result = exercise_local_studio_provider_persistence_ui(
            page,
            openai_base_url,
            openai_token,
            "Real OpenAI-compatible",
            actions,
        )
        persistence_interface_restore = click_select_option(page, "Interface", "OpenAI Responses")
        persistence_models_response = load_local_studio_models_with_retries(page, actions, "openai-compatible after provider persistence")
        persistence_model_restore = click_select_option(page, "Conversation Model", openai_selected_model)
        provider_persistence_result.update(
            {
                "restore_interface": persistence_interface_restore,
                "restore_models_status": persistence_models_response.status,
                "restore_model": persistence_model_restore,
            }
        )
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
        runtime_controls["openai_search_on_optional_tools"] = set_local_studio_runtime_toggle(page, "Web search", True, actions)
        reasoning_controls["openai_effort_off_optional_tools"] = select_local_studio_native_option(page, "Effort", value="off")
        optional_tool_text_results["openai_search_image_text"] = send_local_studio_message(
            page,
            f"Do not create an image. Reply with exactly: nexus-host-ui-openai-search-image-text-ok-{int(time.time())}",
            "openai-compatible-search-image-tool-text",
        )
        provider_results.append(optional_tool_text_results["openai_search_image_text"])
        reasoning_controls["openai_effort_high_optional_tools"] = select_local_studio_native_option(page, "Effort", preferred_values=["high", "medium", "low"])
        reasoning_controls["openai_summary_optional_tools"] = select_local_studio_native_option(page, "Summary", preferred_values=["auto", "concise", "detailed"])
        optional_tool_text_results["openai_search_image_reasoning_text"] = send_local_studio_message(
            page,
            f"Do not create an image. Reply with exactly: nexus-host-ui-openai-search-image-reasoning-text-ok-{int(time.time())}",
            "openai-compatible-search-image-reasoning-tool-text",
        )
        provider_results.append(optional_tool_text_results["openai_search_image_reasoning_text"])
        local_studio_image_generation_results["openai_image_tool"] = exercise_local_studio_openai_image_generation_ui(
            page,
            base_url,
            "Real OpenAI-compatible",
            openai_selected_model,
            actions,
        )
        image_tool_controls["openai_disabled"] = set_local_studio_image_tool(page, False, actions)
        runtime_controls["openai_search_off_before_basic"] = set_local_studio_runtime_toggle(page, "Web search", False, actions)
        reasoning_controls["openai_effort_off_before_basic"] = select_local_studio_native_option(page, "Effort", value="off")
        provider_results.append(send_local_studio_message(page, f"Reply with exactly: nexus-host-ui-openai-ok-{int(time.time())}", "openai-compatible"))
        runtime_controls["openai_search_on"] = set_local_studio_runtime_toggle(page, "Web search", True, actions)
        provider_results.append(send_local_studio_message(page, f"Reply with exactly: nexus-host-ui-openai-search-ok-{int(time.time())}", "openai-compatible-search"))
        runtime_controls["openai_search_off_after_send"] = set_local_studio_runtime_toggle(page, "Web search", False, actions)
        reasoning_matrix_results["openai"] = exercise_local_studio_reasoning_matrix_ui(
            page,
            "Real OpenAI-compatible",
            openai_reasoning_model or openai_selected_model,
            actions,
            provider_kind="openai",
            preferred_prefixes=([openai_reasoning_model] if openai_reasoning_model else []) + ["gpt-5.4-mini", "codex-auto-review", "gpt-5-mini", "gpt-5", "o4-mini", "o3-mini", "gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1", "gpt-4o"],
        )
        interface_matrix_results["openai"] = exercise_local_studio_interface_matrix_ui(
            page,
            base_url,
            "Real OpenAI-compatible",
            openai_selected_model,
            actions,
            provider_kind="openai",
            preferred_prefixes=["gpt-5.4-mini", "gpt-5-mini", "gpt-5", "gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1", "gpt-4o"],
        )
        openai_interface_restore = click_select_option(page, "Interface", "OpenAI Responses")
        load_local_studio_models_with_retries(page, actions, "openai-compatible restore responses after interface matrix")
        openai_model_restore = click_select_option(page, "Conversation Model", openai_selected_model)
        actions.append(f"restore openai-compatible responses after interface matrix: {openai_interface_restore} / {openai_model_restore}")
        page.screenshot(path=str(artifact_dir / "host-ui-openai-local-studio.png"), full_page=True)
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_openai_local_studio_matrix")

        page.locator("#studio-page input[placeholder='Google AI Studio']").fill("Real OpenAI-compatible Edited")
        page.keyboard.press("Tab")
        page.locator("#studio-page input[type='number']").first.fill("181")
        page.keyboard.press("Tab")
        expect(page.locator("#studio-page .local-studio-provider-row .cselect-btn")).to_contain_text("Real OpenAI-compatible Edited", timeout=10_000)
        actions.append("edit local studio openai-compatible provider")
        invalid_provider_recovery_result = exercise_local_studio_invalid_provider_recovery_ui(page, base_url, openai_base_url, actions)
        conversation_crud_provider = click_select_option(page, "Provider", "Google AI Studio")
        conversation_crud_interface = click_select_option(page, "Interface", "OpenAI Responses")
        conversation_crud_models_response = load_local_studio_models_with_retries(page, actions, "google before conversation CRUD")
        conversation_crud_model = click_select_option(page, "Conversation Model", google_selected_model)
        actions.append(f"select google provider before conversation CRUD: {conversation_crud_provider} / {conversation_crud_interface} / {conversation_crud_model}")
        conversation_crud_result = exercise_local_studio_conversation_crud_ui(page, artifact_dir, actions)
        conversation_crud_result["provider_selection"] = {
            "provider": conversation_crud_provider,
            "interface": conversation_crud_interface,
            "models_status": conversation_crud_models_response.status,
            "model": conversation_crud_model,
        }
        restored_provider = click_select_option(page, "Provider", "Real OpenAI-compatible Edited")
        restored_interface = click_select_option(page, "Interface", "OpenAI Responses")
        restored_models_response = load_local_studio_models_with_retries(page, actions, "openai-compatible reload after conversation CRUD")
        restored_model = click_select_option(page, "Conversation Model", openai_selected_model)
        actions.append(f"restore openai-compatible provider after conversation CRUD: {restored_provider} / {restored_interface} / {restored_model}")

        cache_isolation_result = exercise_local_studio_cache_isolation_ui(
            page,
            artifact_dir,
            base_url,
            google_model=google_selected_model,
            openai_provider_label="Real OpenAI-compatible Edited",
            openai_model=openai_selected_model,
            actions=actions,
        )
        page.screenshot(path=str(artifact_dir / "host-ui-cache-isolation.png"), full_page=True)
        restored_provider_after_cache = click_select_option(page, "Provider", "Real OpenAI-compatible Edited")
        restored_interface_after_cache = click_select_option(page, "Interface", "OpenAI Responses")
        load_local_studio_models_with_retries(page, actions, "openai-compatible reload after cache isolation")
        restored_model_after_cache = click_select_option(page, "Conversation Model", openai_selected_model)
        actions.append(
            "restore openai-compatible provider after cache isolation: "
            f"{restored_provider_after_cache} / {restored_interface_after_cache} / {restored_model_after_cache}"
        )
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_cache_isolation_ui")

        before_repeat_count = open_request_logs(page, actions, "navigate #requests before repeated prompt")
        navigate_visible_page(page, "Local Studio", "#studio-page.active", "navigate #studio for repeated prompt", actions)
        repeated_prompt = f"Reply with exactly: nexus-host-ui-repeat-ok-{int(time.time())}"
        provider_results.append(send_local_studio_message_with_transient_retry(page, repeated_prompt, "openai-compatible-repeat-1", actions=actions))
        provider_results.append(send_local_studio_message_with_transient_retry(page, repeated_prompt, "openai-compatible-repeat-2", actions=actions))
        request_log_result = exercise_request_log_ui(page, artifact_dir, openai_token, repeated_prompt, before_repeat_count, actions)
        page.screenshot(path=str(artifact_dir / "host-ui-requests.png"), full_page=True)
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_request_log_ui")

        account_health_result = exercise_account_health_ui(page, artifact_dir, actions)
        page.screenshot(path=str(artifact_dir / "host-ui-accounts.png"), full_page=True)
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_account_health_ui")

        playground_model_candidates = dedupe_model_candidates([google_selected_model, *google_model_candidates])
        playground_result = exercise_playground_base_chat_ui(page, playground_model_candidates, actions)
        playground_search_candidates = dedupe_model_candidates([
            str(playground_result.get("selected_model") or ""),
            *playground_model_candidates,
        ])
        playground_search_result = exercise_playground_search_chat_ui(page, playground_search_candidates, actions)
        page.screenshot(path=str(artifact_dir / "host-ui-playground-basic-chat.png"), full_page=True)
        write_visible_result_artifact(partial=True, partial_reason="checkpoint_after_playground_ui")

        navigate_visible_page(page, "Local Studio", "#studio-page.active", "navigate #studio for provider delete", actions)
        page.locator("#studio-page .local-studio-provider-row button", has_text="删除").click()
        expect(page.locator("#studio-page .local-studio-size-note", has_text="Provider Type: Google AI Studio")).to_be_visible(timeout=10_000)
        actions.append("delete local studio openai-compatible provider")
        if hold_seconds:
            page.wait_for_timeout(hold_seconds * 1000)
        browser.close()

    result = write_visible_result_artifact(partial=False)
    unexpected_console_errors = result.get("unexpected_console_errors") or []

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
    if len(google_repeat_results) < 2 or not all(local_studio_result_controlled(item) for item in google_repeat_results):
        failures.append("google_repeat_same_prompt_missing")
    if not local_studio_success_no_images(optional_tool_text_results.get("google_search_image_text") or {}):
        failures.append("google_optional_search_image_text_failed_or_generated_image")
    if not local_studio_success_no_images(optional_tool_text_results.get("openai_search_image_text") or {}):
        failures.append("openai_optional_search_image_text_failed_or_generated_image")
    if not local_studio_success_no_images(optional_tool_text_results.get("openai_search_image_reasoning_text") or {}):
        failures.append("openai_optional_search_image_reasoning_text_failed_or_generated_image")
    google_image_ui = local_studio_image_generation_results.get("google_image_tool_stream") if isinstance(local_studio_image_generation_results.get("google_image_tool_stream"), dict) else {}
    google_multiturn_image_ui = local_studio_image_generation_results.get("google_search_image_multiturn") if isinstance(local_studio_image_generation_results.get("google_search_image_multiturn"), dict) else {}
    google_nonstream_image_ui = local_studio_image_generation_results.get("google_search_image_nonstream") if isinstance(local_studio_image_generation_results.get("google_search_image_nonstream"), dict) else {}
    openai_image_ui = local_studio_image_generation_results.get("openai_image_tool") if isinstance(local_studio_image_generation_results.get("openai_image_tool"), dict) else {}
    if not local_studio_success_images(google_image_ui):
        failures.append("google_image_tool_visible_output_missing")
    if not local_studio_success_images(google_multiturn_image_ui):
        failures.append("google_search_image_multiturn_visible_output_missing")
    if not local_studio_success_images(google_nonstream_image_ui):
        failures.append("google_search_image_nonstream_visible_output_missing")
    if not local_studio_controlled_image_tool_result(openai_image_ui):
        failures.append("openai_image_tool_visible_controlled_output_missing")
    if base_image_result.get("generation_ok") is not True:
        failures.append("base_image_generation_visible_output_missing")
    if base_image_result.get("edit_retry_ok") is not True:
        failures.append("base_image_reference_retry_visible_output_missing")
    if cache_isolation_result.get("ok") is not True:
        failures.append("local_studio_cache_isolation_ui_failed")
    if provider_persistence_result.get("ok") is not True:
        failures.append("openai_provider_persistence_failed")
    if int(provider_persistence_result.get("restore_models_status") or 500) >= 400:
        failures.append(f"openai_provider_persistence_restore_models_status={provider_persistence_result.get('restore_models_status')}")
    if not reasoning_matrix_ui_ok(reasoning_matrix_results.get("google") or {}):
        failures.append("google_reasoning_matrix_ui_failed")
    if not reasoning_matrix_ui_ok(reasoning_matrix_results.get("openai") or {}):
        failures.append("openai_reasoning_matrix_ui_failed")
    if not interface_matrix_ok(
        interface_matrix_results.get("google") or {},
        strict_keys=("gemini_basic", "gemini_search_repeat", "openai_chat_basic", "openai_chat_search_repeat"),
        controlled_keys=("claude_basic", "claude_search_repeat"),
    ):
        failures.append("google_interface_matrix_ui_failed")
    if not interface_matrix_ok(
        interface_matrix_results.get("openai") or {},
        strict_keys=("openai_chat_basic",),
        controlled_keys=("gemini_basic", "gemini_search_repeat", "openai_chat_search_repeat", "claude_basic", "claude_search_repeat"),
    ):
        failures.append("openai_interface_matrix_ui_failed")
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
    playground_search_send = playground_search_result.get("send") if isinstance(playground_search_result.get("send"), dict) else {}
    if (playground_search_result.get("search_toggle") or {}).get("available") is not True:
        failures.append("playground_search_toggle_unavailable")
    if playground_search_send.get("chat_response_status") != 200 or not str(playground_search_send.get("assistant_text_prefix") or "").strip():
        failures.append("playground_search_chat_visible_result_missing")
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
    try:
        raise SystemExit(main())
    except GoogleQuotaBlocker as exc:
        write_google_quota_blocker(exc)
        print("HOST_UI_SMOKE_BLOCKED external_google_quota_exhausted " + json.dumps(exc.to_artifact(), ensure_ascii=False))
        raise SystemExit(GOOGLE_QUOTA_BLOCKER_EXIT_CODE)
