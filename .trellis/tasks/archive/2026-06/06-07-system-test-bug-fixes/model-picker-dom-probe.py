from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from playwright.sync_api import sync_playwright


def load_helpers(repo_src_dir: Path) -> dict[str, object]:
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
    }


def copied_account_auth_files(accounts_dir: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    registry_path = accounts_dir / "registry.json"
    preferred_ids: list[str] = []
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
    for account_id in preferred_ids:
        auth_file = accounts_dir / account_id / "auth.json"
        if auth_file.is_file():
            candidates.append((account_id, auth_file))
    return candidates


def safe_text(value: object, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def url_authuser(url: str) -> str:
    parts = [part for part in urlparse(url or "").path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part == "u":
            return parts[index + 1]
    return ""


def wait_for_chat_ready(page, helpers: dict[str, object], timeout_ms: int) -> bool:
    deadline = time.perf_counter() + timeout_ms / 1000
    while time.perf_counter() < deadline:
        try:
            if page.evaluate("() => !!window.default_MakerSuite") and page.query_selector("textarea") is not None:
                return True
        except Exception:
            pass
        try:
            page.evaluate(str(helpers["DIALOG_CLEANUP_JS"]))
            page.evaluate(str(helpers["AI_STUDIO_ONBOARDING_JS"]))
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False


VISIBLE_MODEL_DOM_JS = r"""(model) => {
  const target = String(model || '').replace(/^models\//, '').toLowerCase();
  const textOf = (el) => String(
    el?.innerText || el?.textContent || el?.getAttribute?.('aria-label') || el?.getAttribute?.('title') ||
    el?.getAttribute?.('data-value') || el?.getAttribute?.('data-model') || ''
  ).replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const interesting = (text) => /gemini|gemma|deep research|learnlm|chat spark|spark playground|run settings|thinking level|code and chat|build chatbots|featured spark|model selector|models\/|3\.5|flash/i.test(text);
  const nodes = [];
  for (const el of Array.from(document.querySelectorAll('button, [role="button"], [role="option"], mat-option, mat-card, [tabindex], [data-value], [data-model], [aria-label], [title], .model-selector-card, .model-card, [class*="model"], mat-select, [role="combobox"]'))) {
    if (!visible(el)) continue;
    const text = textOf(el);
    if (!text || text.length > 700 || !interesting(text)) continue;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    nodes.push({
      text: text.slice(0, 260),
      tag: el.tagName,
      role: el.getAttribute('role') || '',
      aria: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      dataValue: el.getAttribute('data-value') || '',
      dataModel: el.getAttribute('data-model') || '',
      classes: String(el.className || '').slice(0, 160),
      href: el.getAttribute('href') || '',
      rect: {left: Math.round(rect.left), top: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height)},
      cursor: style.cursor,
      targetHint: target && text.toLowerCase().includes(target),
      dot35Hint: /3\.5|gemini\s*3\.5/i.test(text),
    });
  }
  return {
    url: location.href,
    authuser: (location.pathname.match(/\/u\/(\d+)\//) || [])[1] || '',
    bodySnippet: textOf(document.body).slice(0, 1200),
    nodes: nodes.slice(0, 120),
  };
}"""


def probe_account(
    browser,
    helpers: dict[str, object],
    account_id: str,
    auth_file: Path,
    authusers: list[str],
    model: str,
    artifact_dir: Path,
    hold_seconds: int,
    query_model: bool,
) -> dict[str, object]:
    context = browser.new_context(storage_state=str(auth_file), service_workers="block", viewport={"width": 1440, "height": 960})
    page = context.new_page()
    result: dict[str, object] = {"account_id": account_id, "auth_file": str(auth_file), "authuser_attempts": []}
    try:
        for authuser in authusers:
            attempt: dict[str, object] = {"authuser": authuser}
            result["authuser_attempts"].append(attempt)
            suffix = f"?model={quote(model, safe='')}" if query_model else ""
            url = f"https://aistudio.google.com/u/{authuser}/prompts/new_chat{suffix}"
            try:
                page.goto(url, wait_until="commit", timeout=60_000)
            except Exception as exc:
                attempt["goto_error"] = f"{type(exc).__name__}: {safe_text(exc, 240)}"
            attempt["page_url_after_goto"] = page.url
            attempt["page_authuser"] = url_authuser(page.url)
            attempt["ready"] = wait_for_chat_ready(page, helpers, 120_000)
            attempt["current_before"] = page.evaluate(str(helpers["AI_STUDIO_CURRENT_TEXT_MODEL_JS"]), model)
            attempt["dom_before"] = page.evaluate(VISIBLE_MODEL_DOM_JS, model)
            open_results: list[object] = []
            current_results: list[object] = []
            select_results: list[object] = []
            for step in range(4):
                open_results.append(page.evaluate(str(helpers["AI_STUDIO_OPEN_MODEL_PICKER_JS"]), model))
                page.wait_for_timeout(2500)
                current_results.append(page.evaluate(str(helpers["AI_STUDIO_CURRENT_TEXT_MODEL_JS"]), model))
                select_results.append(page.evaluate(str(helpers["AI_STUDIO_SELECT_TEXT_MODEL_JS"]), model))
                page.wait_for_timeout(2500)
            attempt["open_results"] = open_results
            attempt["current_results"] = current_results
            attempt["select_results"] = select_results
            attempt["current_after"] = page.evaluate(str(helpers["AI_STUDIO_CURRENT_TEXT_MODEL_JS"]), model)
            attempt["dom_after"] = page.evaluate(VISIBLE_MODEL_DOM_JS, model)
            screenshot_path = artifact_dir / f"model-picker-probe-{account_id}-u{authuser}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            attempt["screenshot"] = str(screenshot_path)
            if hold_seconds > 0:
                page.wait_for_timeout(hold_seconds * 1000)
    finally:
        context.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accounts-dir", required=True)
    parser.add_argument("--repo-src", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--model", default="gemini-3.5-flash")
    parser.add_argument("--authusers", default="2,0")
    parser.add_argument("--hold-seconds", type=int, default=0)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--query-model", action="store_true")
    args = parser.parse_args()

    accounts_dir = Path(args.accounts_dir)
    repo_src = Path(args.repo_src)
    artifact_dir = Path(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    helpers = load_helpers(repo_src)
    authusers = [value.strip() for value in args.authusers.split(",") if value.strip()]
    results: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless, slow_mo=200 if not args.headless else 0)
        try:
            for account_id, auth_file in copied_account_auth_files(accounts_dir):
                results.append(probe_account(
                    browser,
                    helpers,
                    account_id,
                    auth_file,
                    authusers,
                    args.model,
                    artifact_dir,
                    args.hold_seconds,
                    args.query_model,
                ))
        finally:
            browser.close()
    output = {"model": args.model, "authusers": authusers, "query_model": args.query_model, "results": results}
    output_path = artifact_dir / "model-picker-dom-probe.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"MODEL_PICKER_DOM_PROBE_OK output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())