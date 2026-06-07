from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox

from aistudio_api.infrastructure.gateway.native_ui_sender import _browser_options


URLS = (
    "https://www.google.com/generate_204",
    "https://aistudio.google.com/",
    "https://aistudio.google.com/u/2/prompts/new_chat",
    "https://aistudio.google.com/u/0/prompts/new_chat",
    "https://aistudio.google.com/prompts/new_chat",
)


def _path(url: str) -> str:
    parsed = urlparse(url or "")
    return f"{parsed.hostname or '<unknown>'}{parsed.path or '/'}"


def _text(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def main() -> int:
    auth_file = os.environ.get("DIAG_AUTH_FILE")
    options = _browser_options()
    print(
        "DIAG_START "
        f"headless={options.get('headless')} "
        f"proxy={'yes' if options.get('proxy') else 'no'} "
        f"auth={'yes' if auth_file else 'no'}"
    )
    with Camoufox(**options) as browser:
        context_kwargs = {"service_workers": "block"}
        if auth_file:
            auth_path = Path(auth_file)
            print(f"DIAG_AUTH exists={auth_path.exists()} path_suffix={auth_path.parent.name}/{auth_path.name}")
            context_kwargs["storage_state"] = str(auth_path)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        try:
            for url in URLS:
                started = time.monotonic()
                try:
                    response = page.goto(url, wait_until="commit", timeout=15_000)
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    status = getattr(response, "status", None) if response is not None else None
                    current = getattr(page, "url", "")
                    print(f"DIAG_GOTO_OK target={_path(url)} elapsed_ms={elapsed_ms} status={status} current={_path(current)}")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except Exception as exc:
                        print(f"DIAG_DOM_TIMEOUT current={_path(getattr(page, 'url', ''))} error={type(exc).__name__}:{_text(exc, 160)}")
                    try:
                        title = page.title()
                    except Exception as exc:
                        title = f"title_error={type(exc).__name__}:{_text(exc, 120)}"
                    try:
                        body = page.evaluate("() => document.body?.innerText?.slice(0, 240) || ''")
                    except Exception as exc:
                        body = f"body_error={type(exc).__name__}:{_text(exc, 120)}"
                    print(f"DIAG_PAGE title={_text(title, 120)} body={_text(body, 240)}")
                except Exception as exc:
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    print(f"DIAG_GOTO_FAIL target={_path(url)} elapsed_ms={elapsed_ms} error={type(exc).__name__}:{_text(exc, 240)}")
        finally:
            page.close()
            context.close()
    print("DIAG_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())