#!/usr/bin/env bash
set -euo pipefail
set +x

SRC="${SRC:-/mnt/c/Users/bamboo/Desktop/nexus-studio}"
TASK_DIR="${TASK_DIR:-$SRC/.trellis/tasks/06-07-system-test-bug-fixes}"
RUN_ROOT="/home/bamboo/nexus-studio-system-test-$(date +%Y%m%d-%H%M%S)"
export RUN_ROOT
SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC="${SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC:-0}"
export SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC
mkdir -p "$RUN_ROOT/artifacts"
echo "SYSTEM_TEST_START run_root=$RUN_ROOT"

if [ -z "${HTTPS_PROXY:-${https_proxy:-}}" ] && [ -r /etc/profile.d/wsl-proxy.sh ]; then
    . /etc/profile.d/wsl-proxy.sh
    echo "WSL_PROFILE_PROXY_LOADED source=/etc/profile.d/wsl-proxy.sh"
fi

cd "$SRC"
git rev-parse HEAD > "$RUN_ROOT/source-commit.txt"
git -c core.autocrlf=true status --porcelain > "$RUN_ROOT/source-status.txt"
git -c core.autocrlf=true status --short --branch > "$RUN_ROOT/source-status-branch.txt"
grep -Ev '^.. \.trellis/tasks/06-07-system-test-bug-fixes(/|$)' "$RUN_ROOT/source-status.txt" > "$RUN_ROOT/source-status-product.txt" || true
grep -E '^.. \.trellis/tasks/06-07-system-test-bug-fixes(/|$)' "$RUN_ROOT/source-status.txt" > "$RUN_ROOT/source-status-task-harness.txt" || true
if [ -s "$RUN_ROOT/source-status-task-harness.txt" ]; then
    echo "SOURCE_STATUS_TASK_HARNESS_DIRTY_ALLOWED_FOR_RUNNER"
    cat "$RUN_ROOT/source-status-task-harness.txt"
fi
if [ -s "$RUN_ROOT/source-status-product.txt" ]; then
    cat "$RUN_ROOT/source-status-product.txt"
    if [ "$SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC" != "1" ]; then
        echo "SYSTEM_TEST_FAIL source_worktree_dirty"
        exit 2
    fi
    echo "SYSTEM_TEST_DIAGNOSTIC source_worktree_dirty_continuing result_cannot_be_SYSTEM_TEST_PASS"
fi

echo "COPY_REPO_START"
rsync -a --delete \
  --exclude .venv \
  --exclude venv \
  --exclude data \
  --exclude tmp \
  "$SRC/" "$RUN_ROOT/repo/"
echo "COPY_REPO_OK"

cd "$RUN_ROOT/repo"
git rev-parse HEAD > "$RUN_ROOT/test-copy-commit.txt"
git -c core.autocrlf=true status --porcelain > "$RUN_ROOT/test-copy-status.txt"
grep -Ev '^.. \.trellis/tasks/06-07-system-test-bug-fixes(/|$)' "$RUN_ROOT/test-copy-status.txt" > "$RUN_ROOT/test-copy-status-product.txt" || true
grep -E '^.. \.trellis/tasks/06-07-system-test-bug-fixes(/|$)' "$RUN_ROOT/test-copy-status.txt" > "$RUN_ROOT/test-copy-status-task-harness.txt" || true
if [ -s "$RUN_ROOT/test-copy-status-task-harness.txt" ]; then
    echo "TEST_COPY_STATUS_TASK_HARNESS_DIRTY_ALLOWED_FOR_RUNNER"
    cat "$RUN_ROOT/test-copy-status-task-harness.txt"
fi
if [ -s "$RUN_ROOT/test-copy-status-product.txt" ]; then
    cat "$RUN_ROOT/test-copy-status-product.txt"
    if [ "$SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC" != "1" ]; then
        echo "SYSTEM_TEST_FAIL test_copy_worktree_dirty"
        exit 2
    fi
    echo "SYSTEM_TEST_DIAGNOSTIC test_copy_worktree_dirty_continuing result_cannot_be_SYSTEM_TEST_PASS"
fi

REAL_ACCOUNTS_DIR="/home/bamboo/nexus-studio/data/accounts"
if [ ! -d "$REAL_ACCOUNTS_DIR" ]; then
  echo "SYSTEM_TEST_FAIL missing_real_accounts_dir=$REAL_ACCOUNTS_DIR"
  exit 2
fi
mkdir -p "$RUN_ROOT/data"
rsync -a --delete "$REAL_ACCOUNTS_DIR/" "$RUN_ROOT/data/accounts/"
echo "COPY_ACCOUNTS_OK source=$REAL_ACCOUNTS_DIR"

OPENAI_COMPAT_KEY_FILE="/mnt/c/Users/bamboo/Documents/github/key.txt"
if [ ! -s "$OPENAI_COMPAT_KEY_FILE" ]; then
  echo "SYSTEM_TEST_FAIL missing_openai_compat_key_file=$OPENAI_COMPAT_KEY_FILE"
  exit 2
fi
export OPENAI_COMPAT_KEY_FILE

echo "PYTHON_ENV_CREATE_START"
python3 -m venv venv
. venv/bin/activate
echo "PYTHON_ENV_READY python=$(python -c 'import sys; print(sys.executable)')"
python -m pip install -q -e .
echo "INSTALL_REPO_OK mode=editable"

if python - <<'PY'
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    path = Path(playwright.chromium.executable_path)
    print(f"PLAYWRIGHT_CHROMIUM_PATH path={path} exists={path.exists()}")
    raise SystemExit(0 if path.exists() else 1)
PY
then
    echo "PLAYWRIGHT_INSTALL_SKIP cached=1"
else
    python -m playwright install chromium >/dev/null
    echo "PLAYWRIGHT_INSTALL_OK browser=chromium"
fi

python - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ["RUN_ROOT"])
accounts_dir = root / "data" / "accounts"
account_dirs = [path.name for path in accounts_dir.iterdir() if path.is_dir()]

def read_openai_compat_credentials() -> tuple[str, str]:
    credential_file = Path(os.environ["OPENAI_COMPAT_KEY_FILE"])
    lines = [line.strip() for line in credential_file.read_text(encoding="utf-8").splitlines() if line.strip()]
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
    return (base_url or "https://api.openai.com/v1").rstrip("/"), token

openai_base_url, openai_token = read_openai_compat_credentials()
summary = {
    "accounts_dir": str(accounts_dir),
    "account_count": len(account_dirs),
    "openai_key_file": os.environ["OPENAI_COMPAT_KEY_FILE"],
    "openai_key_file_nonempty": Path(os.environ["OPENAI_COMPAT_KEY_FILE"]).is_file() and Path(os.environ["OPENAI_COMPAT_KEY_FILE"]).stat().st_size > 0,
    "openai_base_url": openai_base_url,
    "openai_token_nonempty": bool(openai_token),
}
(root / "artifacts" / "real-credentials-gate.safe.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
if summary["account_count"] < 1:
    raise SystemExit("SYSTEM_TEST_FAIL real_google_accounts_missing")
if not summary["openai_key_file_nonempty"] or not summary["openai_token_nonempty"]:
    raise SystemExit("SYSTEM_TEST_FAIL real_openai_key_missing")
print(f"REAL_CREDENTIALS_GATE_OK google_accounts={summary['account_count']} openai_key_file={summary['openai_key_file']}")
PY

REPO_SRC="$RUN_ROOT/repo/src"
export PYTHONPATH="$REPO_SRC"
echo "SOURCE_TREE_PYTHONPATH_OK src=$REPO_SRC"
python - <<'PY'
import importlib.util
from pathlib import Path
repo_src = (Path.cwd() / "src").resolve()
spec = importlib.util.find_spec("aistudio_api")
origin = Path(spec.origin).resolve() if spec and spec.origin else None
if origin is None or repo_src not in origin.parents:
    raise SystemExit(f"SYSTEM_TEST_FAIL source_import_origin={origin}")
import camoufox  # noqa: F401
print(f"SOURCE_IMPORT_OK origin={origin}")
PY

port_is_busy() {
  ss -ltn | awk '{print $4}' | grep -Eq "(^|:)${1}$"
}

PORT=18480
while port_is_busy "$PORT"; do PORT=$((PORT + 1)); done
CAMOUFOX_PORT=$((PORT + 1000))
while port_is_busy "$CAMOUFOX_PORT"; do CAMOUFOX_PORT=$((CAMOUFOX_PORT + 1)); done

export AISTUDIO_PORT="$PORT"
export AISTUDIO_CAMOUFOX_PORT="$CAMOUFOX_PORT"
export AISTUDIO_ACCOUNTS_DIR="$RUN_ROOT/data/accounts"
export AISTUDIO_LOCAL_STUDIO_DIR="$RUN_ROOT/data/local-studio"
export AISTUDIO_REQUEST_LOGS_DIR="$RUN_ROOT/data/request-logs"
export AISTUDIO_GENERATED_IMAGES_DIR="$RUN_ROOT/data/generated-images"
export AISTUDIO_IMAGE_SESSIONS_DIR="$RUN_ROOT/data/image-sessions"
export AISTUDIO_PROVIDER_MANAGER_DIR="$RUN_ROOT/data/provider-manager"
export AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT="${AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT:-3}"
export AISTUDIO_ACCOUNT_WARMUP_LIMIT="${AISTUDIO_ACCOUNT_WARMUP_LIMIT:-2}"
export AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS="${AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS:-300}"
export AISTUDIO_CAMOUFOX_HEADLESS="${AISTUDIO_CAMOUFOX_HEADLESS:-1}"
export AISTUDIO_CAMOUFOX_GEOIP="${AISTUDIO_CAMOUFOX_GEOIP:-0}"
export SYSTEM_TEST_MODEL="${SYSTEM_TEST_MODEL:-gemini-3.5-flash}"
export AISTUDIO_WARMUP_TEXT_MODEL="${AISTUDIO_WARMUP_TEXT_MODEL:-$SYSTEM_TEST_MODEL}"
export OPENAI_COMPAT_TEXT_MODEL="${OPENAI_COMPAT_TEXT_MODEL:-gpt-4.1-mini}"

python - <<'PY'
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

root = Path(os.environ["RUN_ROOT"])
probe = {"urllib": {}, "camoufox": {}, "proxy_env": {}}

def read_openai_compat_credentials() -> tuple[str, str]:
    credential_file = Path(os.environ["OPENAI_COMPAT_KEY_FILE"])
    lines = [line.strip() for line in credential_file.read_text(encoding="utf-8").splitlines() if line.strip()]
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
    return (base_url or "https://api.openai.com/v1").rstrip("/"), token

openai_base_url, openai_token = read_openai_compat_credentials()

def safe_proxy_value(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return "<set>"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", "", "", ""))

def safe_prefix(value: str) -> str:
    text = str(value or "")
    if openai_token:
        text = text.replace(openai_token, "[REDACTED]")
    return text[:500]

for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
    value = os.environ.get(key)
    if value:
        probe["proxy_env"][key] = safe_proxy_value(value)

for name, url in {"google": "https://www.google.com/", "aistudio": "https://aistudio.google.com/"}.items():
    try:
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=30) as response:
            probe["urllib"][name] = {"ok": True, "status": int(response.status), "url": response.geturl()}
    except Exception as exc:
        probe["urllib"][name] = {"ok": False, "type": type(exc).__name__, "error": str(exc)[:500]}

def openai_httpx_preflight() -> dict[str, object]:
    attempts: list[dict[str, object]] = []
    headers = {"Authorization": f"Bearer {openai_token}"}
    for attempt in range(1, 4):
        for trust_env in (True, False):
            try:
                with httpx.Client(timeout=30, trust_env=trust_env) as client:
                    response = client.get(f"{openai_base_url}/models", headers=headers)
                item = {
                    "attempt": attempt,
                    "trust_env": trust_env,
                    "ok": response.status_code < 400,
                    "status": response.status_code,
                    "url": str(response.url),
                    "error": safe_prefix(response.text) if response.status_code >= 400 else "",
                }
                attempts.append(item)
                if response.status_code < 400:
                    return {"ok": True, "status": response.status_code, "trust_env": trust_env, "attempts": attempts}
                if response.status_code in {401, 403}:
                    return {"ok": False, "status": response.status_code, "trust_env": trust_env, "attempts": attempts, "error": item["error"]}
            except Exception as exc:
                attempts.append({"attempt": attempt, "trust_env": trust_env, "ok": False, "type": type(exc).__name__, "error": safe_prefix(str(exc))})
    return {"ok": False, "attempts": attempts}

probe["httpx"] = {"openai": openai_httpx_preflight()}

try:
    from camoufox.sync_api import Camoufox
    from aistudio_api.infrastructure.gateway.native_ui_sender import _browser_options
    with Camoufox(**_browser_options()) as browser:
        context = browser.new_context(service_workers="block")
        page = context.new_page()
        try:
            page.goto("https://aistudio.google.com/", wait_until="commit", timeout=45_000)
            probe["camoufox"] = {"ok": True, "url": str(page.url)}
        except Exception as exc:
            probe["camoufox"] = {"ok": False, "type": type(exc).__name__, "error": str(exc)[:700], "url": str(getattr(page, "url", ""))}
        finally:
            context.close()
except Exception as exc:
    probe["camoufox"] = {"ok": False, "type": type(exc).__name__, "error": str(exc)[:700]}

serialized = json.dumps(probe, ensure_ascii=False, indent=2)
if openai_token and openai_token in serialized:
    raise SystemExit("SYSTEM_TEST_FAIL network_preflight_secret_leak")
(root / "artifacts" / "network-preflight.safe.json").write_text(serialized, encoding="utf-8")
if not (probe["urllib"].get("aistudio", {}).get("ok") and probe.get("httpx", {}).get("openai", {}).get("ok") and probe["camoufox"].get("ok")):
    print("SYSTEM_TEST_FAIL network_preflight_unavailable")
    print(json.dumps(probe, ensure_ascii=False))
    sys.exit(2)
print("NETWORK_PREFLIGHT_OK")
PY

echo "NATIVE_WORKER_PREFLIGHT_START workers=$AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT model=$SYSTEM_TEST_MODEL"
python - <<'PY'
import os
import sys
import time
from pathlib import Path
repo = Path.cwd()
sys.path.insert(0, str(repo / "src"))
from aistudio_api.infrastructure.gateway.native_ui_worker_pool import NativeUiWorker
worker_env = os.environ.copy()
worker_env.pop("PYTHONPATH", None)
worker = NativeUiWorker(index=2, env=worker_env)
process = worker._ensure_started()
time.sleep(2)
if process.poll() is not None:
    stderr = worker._stderr_summary()
    worker.close()
    raise SystemExit(f"SYSTEM_TEST_FAIL native_worker_preflight_exit code={process.returncode} stderr={stderr}")
worker.close()
print("NATIVE_WORKER_PREFLIGHT_OK")
PY

python main.py server --port "$PORT" --camoufox-port "$CAMOUFOX_PORT" > "$RUN_ROOT/artifacts/server.log" 2>&1 &
SERVER_PID=$!
echo "SERVER_STARTED port=$PORT camoufox_port=$CAMOUFOX_PORT pid=$SERVER_PID"

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

python - <<'PY'
import json
import os
import sys
import time
from pathlib import Path
from urllib import request

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
base = f"http://127.0.0.1:{port}"
deadline = time.time() + 900
last = None
while time.time() < deadline:
    try:
        with request.urlopen(base + "/health", timeout=5) as response:
            last = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        last = {"error": str(exc)}
        time.sleep(1)
        continue
    warmup = last.get("warmup") if isinstance(last, dict) else {}
    status = warmup.get("status") if isinstance(warmup, dict) else None
    if status == "complete":
        (root / "artifacts" / "warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        print("WARMUP_COMPLETE")
        break
    if status in {"failed", "partial", "cancelled"}:
        (root / "artifacts" / "warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"SYSTEM_TEST_FAIL warmup_status={status}")
        sys.exit(3)
    time.sleep(2)
else:
    (root / "artifacts" / "warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SYSTEM_TEST_FAIL warmup_timeout")
    sys.exit(4)
print("HEALTH_WARMUP_OK")
PY

python - <<'PY'
import json
import os
import sys
from pathlib import Path
from urllib import error, request

import httpx

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
base = f"http://127.0.0.1:{port}"

def read_openai_compat_credentials() -> tuple[str, str]:
    credential_file = Path(os.environ["OPENAI_COMPAT_KEY_FILE"])
    lines = [line.strip() for line in credential_file.read_text(encoding="utf-8").splitlines() if line.strip()]
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
    return (base_url or "https://api.openai.com/v1").rstrip("/"), token

openai_base_url, openai_token = read_openai_compat_credentials()
openai_model = os.environ["OPENAI_COMPAT_TEXT_MODEL"]
google_model = os.environ["SYSTEM_TEST_MODEL"]
api_results = []
control_plane_results = {}

def http_json(method: str, path: str, payload=None, timeout: int = 120, expect_error: bool = False):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(base + path, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else None
            except Exception:
                data = {"raw": raw[:500]}
            return response.status, data, raw
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        if expect_error:
            try:
                data = json.loads(raw)
            except Exception:
                data = {"raw": raw[:500]}
            return exc.code, data, raw
        raise

def post_local_studio(payload, timeout=360, expect_error=False):
    status, data, raw = http_json("POST", "/api/local-studio/chat", payload, timeout=timeout, expect_error=expect_error)
    api_results.append({
        "provider_type": payload.get("provider_type"),
        "interface_mode": payload.get("interface_mode"),
        "model": payload.get("model"),
        "search": bool((payload.get("options") or {}).get("search")),
        "stream": bool((payload.get("options") or {}).get("stream")),
        "status": status,
        "error_prefix": raw[:500] if status >= 400 else "",
    })
    return status, data, raw

def require_status(method: str, path: str, *, payload=None, timeout: int = 120, expected: set[int] | None = None, label: str):
    expected = expected or {200}
    status, data, raw = http_json(method, path, payload, timeout=timeout, expect_error=True)
    if status not in expected:
        raise AssertionError(f"{label} returned {status}: {raw[:300]}")
    control_plane_results[label] = {"status": status, "ok": True}
    return status, data, raw

status, health, raw = require_status("GET", "/health", label="system_health")
if not isinstance(health, dict) or health.get("status") not in {"ok", "healthy", "degraded"}:
    raise AssertionError(f"unexpected /health payload: {raw[:300]}")
control_plane_results["system_health"]["warmup_status"] = (health.get("warmup") or {}).get("status") if isinstance(health.get("warmup"), dict) else None

status, stats, raw = require_status("GET", "/stats", label="system_stats")
if not isinstance(stats, dict):
    raise AssertionError(f"unexpected /stats payload: {raw[:300]}")

status, config_payload, raw = require_status("GET", "/config", label="system_config")
if not isinstance(config_payload, dict) or not config_payload.get("groups"):
    raise AssertionError(f"unexpected /config payload: {raw[:300]}")

status, local_health, raw = require_status("GET", "/api/local-studio/health", label="local_studio_health")
if not isinstance(local_health, dict) or local_health.get("ok") is not True:
    raise AssertionError(f"unexpected /api/local-studio/health payload: {raw[:300]}")

status, accounts_payload, raw = require_status("GET", "/accounts", label="accounts_list")
account_data = accounts_payload if isinstance(accounts_payload, list) else accounts_payload.get("data") if isinstance(accounts_payload, dict) else []
if not account_data:
    raise AssertionError("/accounts did not return copied real accounts")
control_plane_results["accounts_list"]["account_count"] = len(account_data)

synthetic_storage_state = {
    "cookies": [
        {
            "name": "SID",
            "value": "synthetic-system-test-cookie",
            "domain": ".google.com",
            "path": "/",
            "expires": 4102444800,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ],
    "origins": [],
}
status, import_payload, raw = require_status(
    "POST",
    "/accounts/import?activate=false&name=System%20Test%20Synthetic%20Delete",
    payload=synthetic_storage_state,
    label="accounts_synthetic_import",
)
imported = import_payload.get("imported") if isinstance(import_payload, dict) else None
synthetic_id = str((imported or [{}])[0].get("id") or "") if isinstance(imported, list) and imported else ""
if not synthetic_id:
    raise AssertionError(f"synthetic import did not return account id: {raw[:300]}")
accounts_dir = Path(os.environ["AISTUDIO_ACCOUNTS_DIR"])
synthetic_copy_dir = accounts_dir / synthetic_id
if not synthetic_copy_dir.is_dir():
    raise AssertionError(f"synthetic account was not created in copied accounts dir: {synthetic_copy_dir}")
status, delete_payload, raw = require_status("DELETE", f"/accounts/{synthetic_id}", label="accounts_synthetic_delete")
if synthetic_copy_dir.exists():
    raise AssertionError("synthetic account still exists after delete")
real_accounts_dir = Path("/home/bamboo/nexus-studio/data/accounts")
if (real_accounts_dir / synthetic_id).exists():
    raise AssertionError("synthetic account leaked into source real accounts dir")
control_plane_results["accounts_synthetic_delete"].update({"synthetic_id": synthetic_id, "source_real_accounts_untouched": True})

status, pm_health, raw = require_status("GET", "/api/provider-manager/health", label="provider_manager_health")
if not isinstance(pm_health, dict) or pm_health.get("ok") is not True:
    raise AssertionError(f"unexpected Provider Manager health: {raw[:300]}")
status, pm_providers, raw = require_status("GET", "/api/provider-manager/providers", label="provider_manager_list")
providers = pm_providers.get("data") if isinstance(pm_providers, dict) else []
if not any(provider.get("id") == "google-ai-studio" and provider.get("built_in") for provider in providers if isinstance(provider, dict)):
    raise AssertionError("built-in Google AI Studio provider missing from Provider Manager list")

pm_provider_id = f"system-test-api-provider-{os.getpid()}"
pm_model_id = "system-test-api-model"
pm_payload = {
    "id": pm_provider_id,
    "name": "System Test API Provider",
    "type": "openai-compatible",
    "enabled": True,
    "base_url": "https://provider-manager-api.invalid/v1",
    "timeout": 37,
    "token": "pm-api-token-not-secret",
    "model_catalog": [
        {
            "external_model_id": pm_model_id,
            "display_name": "System Test API Model",
            "aliases": ["api-fast", "api-default"],
            "defaults": {"text": True},
            "capabilities": {"text_output": True, "streaming": True},
            "modalities": ["text", "streaming"],
            "source": "manual",
        }
    ],
    "health": {"status": "ready", "message": "system test"},
}
status, pm_created, raw = require_status("POST", "/api/provider-manager/providers", payload=pm_payload, label="provider_manager_create")
status, pm_get, raw = require_status("GET", f"/api/provider-manager/providers/{pm_provider_id}", label="provider_manager_get")
if pm_get.get("token") or "pm-api-token-not-secret" in json.dumps(pm_get, ensure_ascii=False):
    raise AssertionError("Provider Manager public get leaked token")
status, pm_catalog, raw = require_status("GET", f"/api/provider-manager/model-catalog?provider_id={pm_provider_id}", label="provider_manager_model_catalog")
models = pm_catalog.get("data") if isinstance(pm_catalog, dict) else []
if not any(model.get("external_model_id") == pm_model_id for model in models if isinstance(model, dict)):
    raise AssertionError("Provider Manager model catalog did not include manual model")
status, pm_disabled, raw = require_status("POST", f"/api/provider-manager/providers/{pm_provider_id}/enabled", payload={"enabled": False}, label="provider_manager_disable")
if pm_disabled.get("enabled") is not False:
    raise AssertionError("Provider Manager disable did not set enabled=false")
status, pm_audit, raw = require_status("GET", "/api/provider-manager/audit?limit=50", label="provider_manager_audit")
audit_events = pm_audit.get("data") if isinstance(pm_audit, dict) else []
if not any(event.get("target_id") == pm_provider_id for event in audit_events if isinstance(event, dict)):
    raise AssertionError("Provider Manager audit did not include system-test provider events")
status, pm_delete, raw = require_status("DELETE", f"/api/provider-manager/providers/{pm_provider_id}", label="provider_manager_delete")
if pm_delete.get("ok") is not True:
    raise AssertionError("Provider Manager delete did not return ok=true")
status, pm_builtin_forbidden, raw = require_status(
    "DELETE",
    "/api/provider-manager/providers/google-ai-studio",
    label="provider_manager_builtin_delete_forbidden",
    expected={403},
)
control_plane_results["provider_manager_create"].update({"provider_id": pm_provider_id, "model_id": pm_model_id, "token_publicly_redacted": True})

status, reqlog_status, raw = http_json("PUT", "/request-logs/status", {"enabled": True}, timeout=30)
assert status == 200 and reqlog_status.get("enabled") is True

status, google_models, raw = http_json("POST", "/api/local-studio/models", {"provider_type": "google-ai-studio", "interface_mode": "responses", "timeout": 300}, timeout=180)
assert status == 200 and google_models.get("data"), "Google model list is empty"
assert google_model in {item.get("id") for item in google_models.get("data", [])}, f"target Google model missing: {google_model}"

status, openai_models, raw = http_json("POST", "/api/local-studio/models", {"provider_type": "openai", "base_url": openai_base_url, "api_key": openai_token, "interface_mode": "responses", "timeout": 180}, timeout=180)
assert status == 200 and openai_models.get("data"), "OpenAI-compatible model list is empty"
model_ids = [str(item.get("id") or "") for item in openai_models.get("data", []) if isinstance(item, dict) and item.get("id")]

def openai_model_candidates(model_ids: list[str], requested_model: str) -> list[str]:
    preferred = [
        requested_model,
        "gpt-4.1-mini",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4o",
        "gpt-5-mini",
        "gpt-5",
        "gpt-5.2-chat-latest",
        "gpt-5.2",
        "codex-auto-review",
    ]
    non_chat_markers = ("audio", "realtime", "tts", "transcribe", "embedding", "image")
    candidates: list[str] = []
    for model_id in preferred + model_ids:
        if not model_id or model_id not in model_ids or model_id in candidates:
            continue
        if model_id not in preferred and any(marker in model_id.lower() for marker in non_chat_markers):
            continue
        candidates.append(model_id)
    return candidates

def verify_openai_responses_model(candidates: list[str]) -> tuple[str, list[dict[str, object]]]:
    results: list[dict[str, object]] = []
    headers = {"Authorization": f"Bearer {openai_token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=60) as client:
        for candidate in candidates:
            payload = {
                "model": candidate,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with exactly: nexus-openai-model-probe-ok"}]}],
            }
            try:
                response = client.post(f"{openai_base_url}/responses", headers=headers, json=payload)
                error_prefix = response.text[:500] if response.status_code >= 400 else ""
                if openai_token:
                    error_prefix = error_prefix.replace(openai_token, "[REDACTED]")
                results.append({"model": candidate, "status": response.status_code, "ok": response.status_code < 400, "error_prefix": error_prefix})
                if response.status_code < 400:
                    return candidate, results
            except Exception as exc:
                results.append({"model": candidate, "ok": False, "type": type(exc).__name__, "error_prefix": str(exc)[:500]})
    return "", results

openai_model, model_probe_results = verify_openai_responses_model(openai_model_candidates(model_ids, openai_model))
model_probe_safe = {"openai_model_used": openai_model, "results": model_probe_results}
model_probe_serialized = json.dumps(model_probe_safe, ensure_ascii=False, indent=2)
if openai_token and openai_token in model_probe_serialized:
    raise SystemExit("SYSTEM_TEST_FAIL openai_model_probe_secret_leak")
(root / "artifacts" / "openai-compatible-model-probe.safe.json").write_text(model_probe_serialized, encoding="utf-8")
if not openai_model:
    raise SystemExit("SYSTEM_TEST_FAIL openai_compatible_no_usable_responses_model")
(root / "artifacts" / "openai-compatible-model.safe.txt").write_text(openai_model, encoding="utf-8")
os.environ["OPENAI_COMPAT_TEXT_MODEL"] = openai_model

status, data, raw = post_local_studio({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
    "model": google_model,
    "message": "Reply with exactly: nexus-google-api-ok",
    "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off"},
})
assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]

status, data, raw = post_local_studio({
    "provider_type": "openai",
    "base_url": openai_base_url,
    "api_key": openai_token,
    "interface_mode": "responses",
    "timeout": 180,
    "model": openai_model,
    "message": "Reply with exactly: nexus-openai-api-ok",
    "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off"},
})
assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]

status, data, raw = post_local_studio({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
    "model": google_model,
    "message": "Search today's AI technology news and summarize in one short sentence.",
    "options": {"stream": True, "search": True, "image_tool_enabled": False, "reasoning_effort": "off"},
}, expect_error=True)
if status >= 500:
    raise AssertionError(f"Google search returned server error: {status} {raw[:300]}")
if "Please enable tool_config.include_server_side_tool_invocations" in raw:
    raise AssertionError("Google search/image tool_config include_server_side_tool_invocations regression appeared")

status, data, raw = post_local_studio({
    "provider_type": "openai",
    "base_url": openai_base_url,
    "api_key": openai_token,
    "interface_mode": "responses",
    "timeout": 180,
    "model": openai_model,
    "message": "Search today's OpenAI news and summarize in one short sentence.",
    "options": {"stream": True, "search": True, "image_tool_enabled": False, "reasoning_effort": "off"},
}, expect_error=True)
if status >= 500:
    raise AssertionError(f"OpenAI-compatible search returned server error: {status} {raw[:300]}")
if "Unsupported tool type: web_search_preview" in raw:
    raise AssertionError("OpenAI-compatible search used web_search_preview")

safe = {"api_results": api_results, "control_plane_results": control_plane_results, "openai_model_used": openai_model, "google_model_used": google_model}
serialized = json.dumps(safe, ensure_ascii=False, indent=2)
if openai_token in serialized:
    raise SystemExit("SYSTEM_TEST_FAIL api_results_secret_leak")
(root / "artifacts" / "api-results.json").write_text(serialized, encoding="utf-8")
print(f"API_REAL_PROVIDER_OK google_model={google_model} openai_model={openai_model}")
PY

if [ -s "$RUN_ROOT/artifacts/openai-compatible-model.safe.txt" ]; then
    OPENAI_COMPAT_TEXT_MODEL="$(cat "$RUN_ROOT/artifacts/openai-compatible-model.safe.txt")"
    export OPENAI_COMPAT_TEXT_MODEL
fi

HOST_ARTIFACT_WIN="$(wslpath -w "$RUN_ROOT/artifacts")"
HOST_ACCOUNTS_WIN="$(wslpath -w "$RUN_ROOT/data/accounts")"
HOST_REPO_SRC_WIN="$(wslpath -w "$RUN_ROOT/repo/src")"
HOST_SCRIPT_WIN="$(wslpath -w "$TASK_DIR/host-ui-smoke.py")"
HOST_PYTHON_PATH="${HOST_PYTHON_PATH:-/mnt/c/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe}"
HOST_UI_SMOKE_HEADLESS="${HOST_UI_SMOKE_HEADLESS:-0}"
HOST_UI_SMOKE_SLOW_MO_MS="${HOST_UI_SMOKE_SLOW_MO_MS:-200}"
HOST_UI_SMOKE_HOLD_SECONDS="${HOST_UI_SMOKE_HOLD_SECONDS:-30}"
if [ ! -s "$HOST_PYTHON_PATH" ]; then
    echo "SYSTEM_TEST_FAIL missing_host_python=$HOST_PYTHON_PATH"
    exit 2
fi
HOST_PYTHON_WIN="$(wslpath -w "$HOST_PYTHON_PATH")"
OPENAI_KEY_WIN="C:\\Users\\bamboo\\Documents\\github\\key.txt"
echo "HOST_UI_SMOKE_START artifacts=$HOST_ARTIFACT_WIN"
ps_quote() {
    printf "%s" "$1" | sed "s/'/''/g"
}
HOST_ARTIFACT_PS="$(ps_quote "$HOST_ARTIFACT_WIN")"
HOST_ACCOUNTS_PS="$(ps_quote "$HOST_ACCOUNTS_WIN")"
HOST_REPO_SRC_PS="$(ps_quote "$HOST_REPO_SRC_WIN")"
HOST_SCRIPT_PS="$(ps_quote "$HOST_SCRIPT_WIN")"
HOST_PYTHON_PS="$(ps_quote "$HOST_PYTHON_WIN")"
OPENAI_KEY_PS="$(ps_quote "$OPENAI_KEY_WIN")"
SYSTEM_TEST_MODEL_PS="$(ps_quote "$SYSTEM_TEST_MODEL")"
OPENAI_COMPAT_TEXT_MODEL_PS="$(ps_quote "$OPENAI_COMPAT_TEXT_MODEL")"
HOST_UI_SMOKE_HEADLESS_PS="$(ps_quote "$HOST_UI_SMOKE_HEADLESS")"
HOST_UI_SMOKE_SLOW_MO_MS_PS="$(ps_quote "$HOST_UI_SMOKE_SLOW_MO_MS")"
HOST_UI_SMOKE_HOLD_SECONDS_PS="$(ps_quote "$HOST_UI_SMOKE_HOLD_SECONDS")"
set +e
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command \
    "\$env:AISTUDIO_PORT='$PORT'; \$env:SYSTEM_TEST_MODEL='$SYSTEM_TEST_MODEL_PS'; \$env:OPENAI_COMPAT_TEXT_MODEL='$OPENAI_COMPAT_TEXT_MODEL_PS'; \$env:OPENAI_COMPAT_KEY_FILE='$OPENAI_KEY_PS'; \$env:HOST_ARTIFACT_DIR='$HOST_ARTIFACT_PS'; \$env:HOST_ACCOUNTS_DIR='$HOST_ACCOUNTS_PS'; \$env:HOST_REPO_SRC_DIR='$HOST_REPO_SRC_PS'; \$env:HOST_UI_SMOKE_HEADLESS='$HOST_UI_SMOKE_HEADLESS_PS'; \$env:HOST_UI_SMOKE_SLOW_MO_MS='$HOST_UI_SMOKE_SLOW_MO_MS_PS'; \$env:HOST_UI_SMOKE_HOLD_SECONDS='$HOST_UI_SMOKE_HOLD_SECONDS_PS'; & '$HOST_PYTHON_PS' '$HOST_SCRIPT_PS'"
HOST_UI_SMOKE_EXIT_CODE=$?
set -e
export HOST_UI_SMOKE_EXIT_CODE
echo "HOST_UI_SMOKE_EXIT code=$HOST_UI_SMOKE_EXIT_CODE"

python - <<'PY'
import json
import os
import re
import sys
from pathlib import Path
from urllib import request
from urllib.parse import urlsplit

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
base = f"http://127.0.0.1:{port}"

def read_openai_compat_credentials() -> tuple[str, str]:
    credential_file = Path(os.environ["OPENAI_COMPAT_KEY_FILE"])
    lines = [line.strip() for line in credential_file.read_text(encoding="utf-8").splitlines() if line.strip()]
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
    return (base_url or "https://api.openai.com/v1").rstrip("/"), token

openai_base_url, openai_token = read_openai_compat_credentials()
openai_host = urlsplit(openai_base_url).netloc
with request.urlopen(base + "/request-logs?limit=1000", timeout=30) as response:
    groups_payload = json.loads(response.read().decode("utf-8"))
failures = []
summary = {
    "group_total": groups_payload.get("total"),
    "entry_total": groups_payload.get("entry_total"),
    "openai_search_tool_types": [],
    "google_search_tool_types": [],
    "openai_authorization_redacted": False,
    "secret_leaked": False,
    "required_phases_seen": {},
}
for group in groups_payload.get("data") or []:
    chain_id = group.get("id") or group.get("chain_id")
    if not chain_id:
        continue
    with request.urlopen(base + f"/request-logs/groups/{chain_id}", timeout=30) as response:
        detail = json.loads(response.read().decode("utf-8"))
    dumped = json.dumps(detail, ensure_ascii=False)
    if openai_token and openai_token in dumped:
        summary["secret_leaked"] = True
    phases = {entry.get("phase") for entry in detail.get("entries") or []}
    if any(entry.get("kind", "").startswith("local_studio") for entry in detail.get("entries") or []):
        for phase in ("client_request", "upstream_request", "upstream_response", "client_response"):
            summary["required_phases_seen"][phase] = summary["required_phases_seen"].get(phase, False) or phase in phases
    for entry in detail.get("entries") or []:
        if entry.get("phase") != "upstream_request":
            continue
        body = entry.get("body_json") if isinstance(entry.get("body_json"), dict) else {}
        headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
        tools = body.get("tools") if isinstance(body, dict) and isinstance(body.get("tools"), list) else []
        tool_types = [tool.get("type") for tool in tools if isinstance(tool, dict) and tool.get("type")]
        url = str(entry.get("url") or "")
        provider_kind = "openai" if openai_host and openai_host in url else "google" if "127.0.0.1" in url and "/v1/responses" in url else ""
        if provider_kind == "openai":
            if headers.get("Authorization") == "Bearer ***":
                summary["openai_authorization_redacted"] = True
            if tool_types:
                summary["openai_search_tool_types"].extend(tool_types)
        elif provider_kind == "google" and tool_types:
            summary["google_search_tool_types"].extend(tool_types)
if summary["secret_leaked"]:
    failures.append("secret_leaked")
if "web_search_preview" in summary["openai_search_tool_types"]:
    failures.append("openai_used_web_search_preview")
if summary["openai_search_tool_types"] and "web_search" not in summary["openai_search_tool_types"]:
    failures.append("openai_web_search_missing")
if summary["google_search_tool_types"] and "web_search_preview" not in summary["google_search_tool_types"]:
    failures.append("google_web_search_preview_missing")
if not summary["google_search_tool_types"]:
    failures.append("google_search_tool_log_missing")
if not summary["openai_authorization_redacted"]:
    failures.append("openai_authorization_redaction_missing")
for phase in ("client_request", "upstream_request", "upstream_response", "client_response"):
    if not summary["required_phases_seen"].get(phase):
        failures.append(f"phase_missing={phase}")
log_text = (root / "artifacts" / "server.log").read_text(encoding="utf-8", errors="replace")
for marker in ("httpx.ResponseNotRead", "ExceptionGroup", "Exception in ASGI application"):
    if marker in log_text:
        failures.append(f"server_log_marker={marker}")
summary["failures"] = failures
(root / "artifacts" / "architecture-contract-results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
if failures:
    print("SYSTEM_TEST_FAIL architecture_or_request_log_oracle_failed")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(7)
print("REQUEST_LOG_AND_ARCHITECTURE_ORACLE_OK")
PY

python - <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["RUN_ROOT"])
artifacts = root / "artifacts"

def read_json(name):
    path = artifacts / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

mcp_ui = read_json("mcp-visible-ui-results.json") or {}
host_performance = read_json("host-performance-comparison-results.json") or {}
architecture = read_json("architecture-contract-results.json") or {}
api_results = read_json("api-results.json") or {}
control_plane = api_results.get("control_plane_results") or {}

covered_actions = set(mcp_ui.get("actions") or [])
covered_items = set(mcp_ui.get("covered_plan_items") or [])
known_missing = list(mcp_ui.get("known_missing_plan_items") or [])
required_control_plane_labels = [
    "system_health",
    "system_stats",
    "system_config",
    "local_studio_health",
    "accounts_list",
    "accounts_synthetic_import",
    "accounts_synthetic_delete",
    "provider_manager_health",
    "provider_manager_list",
    "provider_manager_create",
    "provider_manager_get",
    "provider_manager_model_catalog",
    "provider_manager_disable",
    "provider_manager_audit",
    "provider_manager_delete",
    "provider_manager_builtin_delete_forbidden",
]
control_plane_passed = all((control_plane.get(label) or {}).get("ok") for label in required_control_plane_labels)
source_product_status_clean = (root / "source-status-product.txt").read_text(encoding="utf-8") == ""
test_copy_product_status_clean = (root / "test-copy-status-product.txt").read_text(encoding="utf-8") == ""
dirty_diagnostic_mode = os.environ.get("SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC") == "1"
host_ui_smoke_exit_code = int(os.environ.get("HOST_UI_SMOKE_EXIT_CODE") or 0)
host_ui_artifact_written = bool(mcp_ui)
official_ai_studio = mcp_ui.get("official_ai_studio_result") or {}
performance_passed = host_performance.get("result") == "pass" and len((official_ai_studio.get("samples") or [])) >= 3
pass_blockers = []
if not source_product_status_clean:
    pass_blockers.append("source_product_worktree_dirty")
if not test_copy_product_status_clean:
    pass_blockers.append("test_copy_product_worktree_dirty")
if dirty_diagnostic_mode:
    pass_blockers.append("dirty_source_diagnostic_mode")
if host_ui_smoke_exit_code != 0:
    pass_blockers.append(f"host_ui_smoke_exit={host_ui_smoke_exit_code}")
if not host_ui_artifact_written:
    pass_blockers.append("host_ui_artifact_missing")

visible_subset_status = "pass" if host_ui_artifact_written else "fail"

covered_ui_cases = [
    {"id": "BOOT-02-complete-navigation", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "#chat, #studio, #providers, #images, #requests, #config and #accounts were opened by headed Chromium"},
    {"id": "G-LS-01-basic-google-responses-text", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "Google AI Studio Responses model loaded and one basic text message returned visible assistant text"},
    {"id": "O-LS-01-openai-compatible-provider-create-edit-delete", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "OpenAI-compatible Local Studio provider was created with real credentials, edited, used, and deleted through visible controls"},
    {"id": "O-LS-02-basic-openai-compatible-responses-text", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "OpenAI-compatible Responses model returned visible assistant text"},
    {"id": "LS-UI-repeated-prompt-fresh-request-log", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "The same visible prompt was sent twice and request-log group count increased by two"},
    {"id": "LOG-01-LOG-02-request-log-detail-copy-export-delete", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "Visible request-log detail, lifecycle phases, copy, active export, selected export, and delete were exercised"},
    {"id": "ACC-01-account-health-check", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "A real account row health-check button was clicked and /accounts/{id}/test returned successfully"},
    {"id": "PM-basic-custom-provider-crud", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "Provider Manager custom OpenAI-compatible provider create, edit, disable and delete ran through visible controls"},
    {"id": "LS-UI-runtime-stream-search-toggles", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "Visible Local Studio Stream and Web search controls were toggled and asserted for Google and OpenAI-compatible providers"},
    {"id": "LS-UI-image-tool-controls", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "Visible Local Studio image-tool controls were toggled and native selects were exercised"},
    {"id": "LS-UI-attachment-preview-remove", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "A visible attachment was selected, previewed, then removed before send"},
    {"id": "O-LS-invalid-provider-error-and-recovery", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "An invalid OpenAI-compatible provider produced a visible controlled error and the real provider recovered with a visible answer"},
    {"id": "LS-UI-conversation-create-rename-restore-rerun-delete-bulk-delete", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "Visible conversation create, rename, reload/restore, rerun, delete and bulk-delete flows were exercised"},
    {"id": "PM-visible-built-in-boundary-token-alias-default-audit-crud", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "Provider Manager visible built-in boundary, token visibility, manual alias/default text, audit delete event and CRUD were exercised"},
    {"id": "BASE-CHAT-01-playground-basic-gemini-chat", "tested_by": "mcp_visible", "status": visible_subset_status, "evidence": "Playground loaded Gemini models and returned a visible assistant answer"},
    {"id": "BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01-visible-official-ui", "tested_by": "mcp_visible", "status": "pass" if performance_passed else "fail", "evidence": "Official AI Studio visible page opened with copied real account storage state, selected the target model through visible controls, captured 3 official samples, and compared Local Studio latency budget", "performance_result": host_performance.get("result")},
    {"id": "PERF-01-official-ai-studio-local-comparison", "tested_by": "mcp_visible", "status": "pass" if performance_passed else "fail", "evidence": "Official AI Studio direct UI and Local Studio UI each produced 3 visible samples and budget medians were evaluated", "performance_result": host_performance.get("result")},
    {"id": "SYS-API-health-stats-config-local-studio-health", "tested_by": "api_real", "status": "pass" if control_plane_passed else "fail", "evidence": "GET /health, /stats, /config and /api/local-studio/health were asserted in the WSL real server", "control_plane_labels": ["system_health", "system_stats", "system_config", "local_studio_health"]},
    {"id": "ACC-API-synthetic-import-delete-copy-only", "tested_by": "api_real", "status": "pass" if control_plane_passed else "fail", "evidence": "A synthetic account was imported and deleted under the copied WSL accounts directory; the source real accounts directory was checked untouched", "control_plane_labels": ["accounts_synthetic_import", "accounts_synthetic_delete"]},
    {"id": "PM-API-health-list-catalog-audit-crud-boundary", "tested_by": "api_real", "status": "pass" if control_plane_passed else "fail", "evidence": "Provider Manager API health/list/custom provider CRUD/model catalog/audit/built-in delete forbidden were asserted", "control_plane_labels": ["provider_manager_health", "provider_manager_list", "provider_manager_create", "provider_manager_get", "provider_manager_model_catalog", "provider_manager_disable", "provider_manager_audit", "provider_manager_delete", "provider_manager_builtin_delete_forbidden"]},
]
ui_cases = list(covered_ui_cases)

missing_ui_cases = [
    ("G-LS-02-through-G-LS-11-google-ui-matrix", "Google UI matrix for repeat/search/image/reasoning/interfaces was not covered"),
    ("O-LS-02-through-O-LS-10-openai-ui-matrix", "OpenAI-compatible UI matrix for stream/search/image/reasoning/interfaces/error paths was not covered"),
    ("LS-UI-remaining-matrix", "Full attachment-send upstream matrix, timeout, cache isolation and every state-machine branch were not fully covered"),
    ("BASE-CHAT-remaining-through-BASE-ACC-02", "Base module regressions beyond basic #chat, #requests and #accounts smoke were not fully covered"),
    ("ACC-remaining-switch-delete", "Account switch/delete/import/export safety paths were not fully covered"),
    ("PM-ROLL-00-and-applicable-PM-gates", "Provider Manager rollout phase gate and discovery health assertions were not fully covered"),
    ("BUG-GEMINI-IMAGE-TOOL-01", "Google Responses search+image tool bug path was not covered in visible UI"),
    ("BUG-OPENAI-RESPONSES-REASONING-01", "OpenAI-compatible reasoning high + summary stream/non-stream UI path was not covered"),
]
if not performance_passed:
    missing_ui_cases.append(("BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01-visible-official-ui", "Official AI Studio visible model-selection evidence or local performance budget did not pass"))
for case_id, reason in missing_ui_cases:
    ui_cases.append({"id": case_id, "tested_by": "not_covered", "status": "fail", "reason": reason})

ui_results = {
    "result": "incomplete",
    "reason": "Current host-ui-smoke is an expanded headed visible UI subset, not the full SYSTEM_TEST_PLAN.md P0/P1 UI matrix.",
    "mcp_visible_browser": mcp_ui.get("browser", {}),
    "coverage_scope": mcp_ui.get("coverage_scope"),
    "full_system_plan_coverage": bool(mcp_ui.get("full_system_plan_coverage")),
    "host_ui_smoke_exit_code": host_ui_smoke_exit_code,
    "host_ui_artifact_written": host_ui_artifact_written,
    "actions": sorted(covered_actions),
    "covered_plan_items": sorted(covered_items),
    "known_missing_plan_items": known_missing,
    "cases": ui_cases,
}

performance_results = host_performance if host_performance else {
    "result": "incomplete",
    "reason": "Official AI Studio direct visible UI baseline was not measured, so Google text latency budget cannot be declared passed.",
    "required_by": "SYSTEM_TEST_PLAN.md official/local performance comparison",
    "local_samples_from_basic_smoke": [item for item in mcp_ui.get("provider_results", []) if item.get("label") == "google-ai-studio"],
    "official_samples": [],
}

provider_manager_results = {
    "result": "incomplete",
    "reason": "Visible #providers navigation, visible custom-provider CRUD, and API health/list/catalog/audit/boundary checks were exercised, but rollout phase gate and discovery health assertions were not fully executed.",
    "observed_provider_page_navigation": "navigate #providers" in covered_actions,
    "basic_custom_provider_crud": mcp_ui.get("provider_manager_result") or {},
    "api_control_plane_passed": control_plane_passed,
    "api_control_plane_results": control_plane,
}

plan_alignment = {
    "result": "incomplete",
    "reason": "Plan-script alignment audit found required P0/P1 UI/performance/provider-manager coverage that is not mapped to hard assertions.",
    "host_visible_ui_smoke_passed": host_ui_smoke_exit_code == 0,
    "host_visible_ui_artifact_written": host_ui_artifact_written,
    "host_ui_smoke_exit_code": host_ui_smoke_exit_code,
    "api_and_request_log_subset_passed": not architecture.get("failures") and bool(api_results.get("api_results")),
    "api_control_plane_subset_passed": control_plane_passed,
    "source_product_status_clean": source_product_status_clean,
    "test_copy_product_status_clean": test_copy_product_status_clean,
    "dirty_source_diagnostic_mode": dirty_diagnostic_mode,
    "pass_blockers": pass_blockers,
    "newly_covered_required_coverage": [case["id"] for case in covered_ui_cases],
    "missing_required_coverage": [case[0] for case in missing_ui_cases] + (["performance-comparison-results official baseline"] if not performance_passed else []) + [
        "complete ui-results.json P0/P1 matrix",
        "provider-manager-phase-gate-results complete phase evidence",
    ],
}

(artifacts / "ui-results.json").write_text(json.dumps(ui_results, ensure_ascii=False, indent=2), encoding="utf-8")
(artifacts / "performance-comparison-results.json").write_text(json.dumps(performance_results, ensure_ascii=False, indent=2), encoding="utf-8")
(artifacts / "provider-manager-phase-gate-results.json").write_text(json.dumps(provider_manager_results, ensure_ascii=False, indent=2), encoding="utf-8")
(artifacts / "plan-script-alignment-results.json").write_text(json.dumps(plan_alignment, ensure_ascii=False, indent=2), encoding="utf-8")

summary = {
    "run_root": str(root),
    "source_commit": (root / "source-commit.txt").read_text(encoding="utf-8").strip(),
    "test_copy_commit": (root / "test-copy-commit.txt").read_text(encoding="utf-8").strip(),
    "source_status_clean": (root / "source-status.txt").read_text(encoding="utf-8") == "",
    "test_copy_status_clean": (root / "test-copy-status.txt").read_text(encoding="utf-8") == "",
    "source_product_status_clean": source_product_status_clean,
    "test_copy_product_status_clean": test_copy_product_status_clean,
    "dirty_source_diagnostic_mode": dirty_diagnostic_mode,
    "pass_blockers": pass_blockers,
    "port": os.environ["AISTUDIO_PORT"],
    "camoufox_port": os.environ["AISTUDIO_CAMOUFOX_PORT"],
    "real_credentials_gate": "pass",
    "system_test_result": "SYSTEM_TEST_INCOMPLETE",
    "host_visible_ui_smoke_result": "pass" if host_ui_smoke_exit_code == 0 else "fail",
    "api_control_plane_result": "pass" if control_plane_passed else "fail",
    "incomplete_reason": plan_alignment["reason"],
}
(artifacts / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(artifacts / "summary.md").write_text("\n".join([
    "# System Test Summary",
    "",
    "- Result: SYSTEM_TEST_INCOMPLETE",
    f"- Run root: {root}",
    f"- Port: {os.environ['AISTUDIO_PORT']}",
    "- Real credentials gate: pass",
    f"- Host visible UI smoke: {'pass' if host_ui_smoke_exit_code == 0 else 'fail'} (exit {host_ui_smoke_exit_code})",
    f"- API/control-plane subset: {'pass' if control_plane_passed else 'fail'}",
    f"- Product source clean: {source_product_status_clean}",
    f"- Dirty-source diagnostic mode: {dirty_diagnostic_mode}",
    f"- Pass blockers: {', '.join(pass_blockers) if pass_blockers else 'none from worktree state'}",
    "- Full SYSTEM_TEST_PLAN.md P0/P1 UI/performance/provider-manager coverage: incomplete",
]), encoding="utf-8")

print("SYSTEM_TEST_INCOMPLETE plan_script_alignment_failed")
print(json.dumps(plan_alignment, ensure_ascii=False))
sys.exit(8)
PY
