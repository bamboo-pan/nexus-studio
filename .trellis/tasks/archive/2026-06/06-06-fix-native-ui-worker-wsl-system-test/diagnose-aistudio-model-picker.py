from __future__ import annotations

import json
import os
import re
from pathlib import Path

from camoufox.sync_api import Camoufox

from aistudio_api.infrastructure.gateway.native_ui_sender import _browser_options, _open_chat
from aistudio_api.infrastructure.gateway.session import AI_STUDIO_OPEN_MODEL_PICKER_JS, AI_STUDIO_SELECT_TEXT_MODEL_JS


def _safe_text(value: object, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


INSPECT_JS = r"""() => {
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
    const summarize = (el) => ({
        tag: el.tagName,
        role: el.getAttribute('role') || '',
        ariaLabel: el.getAttribute('aria-label') || '',
        title: el.getAttribute('title') || '',
        dataValue: el.getAttribute('data-value') || '',
        dataModel: el.getAttribute('data-model') || '',
        className: String(el.className || '').slice(0, 100),
        text: textOf(el).slice(0, 240),
    });
    const selectors = 'button, [role="button"], [role="combobox"], [role="option"], mat-select, mat-option, [data-value], [data-model], .model-selector-card';
    const controls = Array.from(document.querySelectorAll(selectors)).filter(visible).map(summarize).slice(0, 80);
    const modelRegex = /\b(?:models\/)?(?:gemini|gemma|deep-research|learnlm)-[a-z0-9][a-z0-9._-]*\b/gi;
    const modelMatches = [];
    const add = (value) => {
        for (const match of String(value || '').matchAll(modelRegex)) {
            const model = match[0].replace(/^models\//i, '').toLowerCase();
            if (!modelMatches.includes(model)) modelMatches.push(model);
        }
    };
    add(document.body?.innerText || '');
    for (const control of controls) add(Object.values(control).join(' '));
    return {
        url: window.location.href,
        title: document.title,
        bodyHead: (document.body?.innerText || '').slice(0, 1200),
        controls,
        modelMatches,
    };
}"""


def main() -> int:
    auth_file = os.environ["DIAG_AUTH_FILE"]
    out_dir = Path(os.environ.get("DIAG_ARTIFACT_DIR", "/tmp"))
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_models = [
        value.strip()
        for value in os.environ.get(
            "DIAG_MODELS",
            "gemini-3.5-flash,gemini-3-flash-preview,gemma-4-31b-it,gemma-4-26b-a4b-it,gemini-flash-latest,gemini-pro-latest",
        ).split(",")
        if value.strip()
    ]
    print(f"MODEL_DIAG_START auth_exists={Path(auth_file).exists()} candidates={candidate_models}")
    with Camoufox(**_browser_options()) as browser:
        context = browser.new_context(storage_state=auth_file, service_workers="block")
        page = context.new_page()
        try:
            _open_chat(page, 90_000)
            page.wait_for_timeout(2000)
            inspect_before = page.evaluate(INSPECT_JS)
            (out_dir / "model-picker-before.safe.json").write_text(json.dumps(inspect_before, ensure_ascii=False, indent=2), encoding="utf-8")
            page.screenshot(path=str(out_dir / "model-picker-before.png"), full_page=True)
            print(
                "MODEL_DIAG_BEFORE "
                + json.dumps(
                    {
                        "url": _safe_text(inspect_before.get("url"), 200),
                        "title": _safe_text(inspect_before.get("title"), 120),
                        "modelMatches": inspect_before.get("modelMatches"),
                        "bodyHead": _safe_text(inspect_before.get("bodyHead"), 500),
                        "controlTexts": [_safe_text(item.get("text"), 160) for item in inspect_before.get("controls", [])[:25]],
                    },
                    ensure_ascii=False,
                )
            )
            opened = page.evaluate(AI_STUDIO_OPEN_MODEL_PICKER_JS)
            page.wait_for_timeout(1500)
            inspect_opened = page.evaluate(INSPECT_JS)
            (out_dir / "model-picker-opened.safe.json").write_text(json.dumps(inspect_opened, ensure_ascii=False, indent=2), encoding="utf-8")
            page.screenshot(path=str(out_dir / "model-picker-opened.png"), full_page=True)
            print(
                "MODEL_DIAG_OPENED "
                + json.dumps(
                    {
                        "opened": opened,
                        "modelMatches": inspect_opened.get("modelMatches"),
                        "controlTexts": [_safe_text(item.get("text"), 160) for item in inspect_opened.get("controls", [])[:40]],
                    },
                    ensure_ascii=False,
                )
            )
            results = []
            for model in candidate_models:
                try:
                    selected = page.evaluate(AI_STUDIO_SELECT_TEXT_MODEL_JS, model)
                except Exception as exc:
                    selected = {"error": f"{type(exc).__name__}: {_safe_text(exc, 300)}"}
                results.append({"model": model, "selected": selected})
                page.wait_for_timeout(800)
            print("MODEL_DIAG_SELECT " + json.dumps(results, ensure_ascii=False))
        finally:
            page.close()
            context.close()
    print(f"MODEL_DIAG_DONE artifacts={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())