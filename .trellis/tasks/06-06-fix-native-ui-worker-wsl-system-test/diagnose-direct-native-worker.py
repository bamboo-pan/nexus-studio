from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from aistudio_api.infrastructure.gateway.native_ui_worker_pool import NativeUiWorker


def main() -> int:
    auth_file = os.environ["DIAG_AUTH_FILE"]
    model = os.environ.get("SYSTEM_TEST_MODEL", "gemini-3.5-flash")
    timeout_ms = int(os.environ.get("DIAG_TIMEOUT_MS", "180000"))
    payload = {
        "auth_file": auth_file,
        "model": model,
        "prompt": "Reply with exactly: nexus-direct-worker-ok",
        "timeout_ms": timeout_ms,
    }
    worker_env = os.environ.copy()
    worker_env.pop("PYTHONPATH", None)
    worker = NativeUiWorker(index=0, env=worker_env)
    started = time.monotonic()
    try:
        result = worker.send(payload, timeout_seconds=max(60.0, timeout_ms / 1000.0 + 90.0))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        safe = {
            "elapsed_ms": elapsed_ms,
            "ok": result.get("ok"),
            "status": result.get("status"),
            "wire_model": result.get("wire_model"),
            "url_path": result.get("url_path"),
            "error": str(result.get("error") or "")[:500],
            "stderr": str(result.get("stderr") or "")[:1000],
            "body_size": result.get("body_size"),
        }
        print("DIRECT_WORKER_RESULT " + json.dumps(safe, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(
            "DIRECT_WORKER_EXCEPTION "
            + json.dumps(
                {"elapsed_ms": elapsed_ms, "type": type(exc).__name__, "error": str(exc)[:1200]},
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())