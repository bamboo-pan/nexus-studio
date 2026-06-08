#!/usr/bin/env bash
set -euo pipefail
set +x

SRC="${SRC:-/mnt/c/Users/bamboo/Desktop/nexus-studio}"
TASK_DIR="${TASK_DIR:-$SRC/.trellis/tasks/06-08-local-studio-google-budget-matrix}"
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
grep -Ev '^.. \.trellis/tasks/06-08-local-studio-google-budget-matrix(/|$)' "$RUN_ROOT/source-status.txt" > "$RUN_ROOT/source-status-product.txt" || true
grep -E '^.. \.trellis/tasks/06-08-local-studio-google-budget-matrix(/|$)' "$RUN_ROOT/source-status.txt" > "$RUN_ROOT/source-status-task-harness.txt" || true
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
grep -Ev '^.. \.trellis/tasks/06-08-local-studio-google-budget-matrix(/|$)' "$RUN_ROOT/test-copy-status.txt" > "$RUN_ROOT/test-copy-status-product.txt" || true
grep -E '^.. \.trellis/tasks/06-08-local-studio-google-budget-matrix(/|$)' "$RUN_ROOT/test-copy-status.txt" > "$RUN_ROOT/test-copy-status-task-harness.txt" || true
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

def post_local_studio_with_retries(payload, *, timeout=360, attempts=2, label="local-studio"):
    last = None
    for attempt in range(1, attempts + 1):
        status, data, raw = post_local_studio(payload, timeout=timeout, expect_error=True)
        last = (status, data, raw)
        api_results[-1]["attempt"] = attempt
        api_results[-1]["label"] = label
        if status < 500:
            return status, data, raw
    assert last is not None
    return last

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

status, request_log_status, raw = require_status("GET", "/request-logs/status", label="request_logs_status")
if not isinstance(request_log_status, dict) or "enabled" not in request_log_status:
    raise AssertionError(f"unexpected /request-logs/status payload: {raw[:300]}")

status, v1_models, raw = require_status("GET", "/v1/models", label="openai_compatible_models_list")
v1_model_data = v1_models.get("data") if isinstance(v1_models, dict) else []
if not isinstance(v1_model_data, list) or not v1_model_data:
    raise AssertionError(f"unexpected /v1/models payload: {raw[:300]}")

status, v1beta_models, raw = require_status("GET", "/v1beta/models", label="gemini_models_list")
v1beta_model_data = v1beta_models.get("models") if isinstance(v1beta_models, dict) else []
if not isinstance(v1beta_model_data, list) or not v1beta_model_data:
    raise AssertionError(f"unexpected /v1beta/models payload: {raw[:300]}")

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
control_plane_results["request_logs_enable"] = {"status": status, "ok": True, "enabled": True}

status, google_models, raw = http_json("POST", "/api/local-studio/models", {"provider_type": "google-ai-studio", "interface_mode": "responses", "timeout": 300}, timeout=180)
assert status == 200 and google_models.get("data"), "Google model list is empty"
assert google_model in {item.get("id") for item in google_models.get("data", [])}, f"target Google model missing: {google_model}"
control_plane_results["local_studio_google_models"] = {"status": status, "ok": True, "target_model": google_model, "model_count": len(google_models.get("data", []))}

status, openai_models, raw = http_json("POST", "/api/local-studio/models", {"provider_type": "openai", "base_url": openai_base_url, "api_key": openai_token, "interface_mode": "responses", "timeout": 180}, timeout=180)
assert status == 200 and openai_models.get("data"), "OpenAI-compatible model list is empty"
model_ids = [str(item.get("id") or "") for item in openai_models.get("data", []) if isinstance(item, dict) and item.get("id")]
control_plane_results["local_studio_openai_models"] = {"status": status, "ok": True, "model_count": len(model_ids)}

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
                text = response.text
                expected = "nexus-openai-model-probe-ok"
                matched = response.status_code < 400 and expected in text
                error_prefix = text[:500] if not matched else ""
                if openai_token:
                    error_prefix = error_prefix.replace(openai_token, "[REDACTED]")
                results.append({"model": candidate, "status": response.status_code, "ok": matched, "sentinel_matched": matched, "error_prefix": error_prefix})
                if matched:
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

status, data, raw = post_local_studio_with_retries({
    "provider_type": "openai",
    "base_url": openai_base_url,
    "api_key": openai_token,
    "interface_mode": "responses",
    "timeout": 180,
    "model": openai_model,
    "message": "Reply with exactly: nexus-openai-api-ok",
    "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off"},
}, timeout=180, attempts=3, label="openai-compatible-basic")
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

status, data, raw = post_local_studio_with_retries({
    "provider_type": "openai",
    "base_url": openai_base_url,
    "api_key": openai_token,
    "interface_mode": "responses",
    "timeout": 180,
    "model": openai_model,
    "message": "Search today's OpenAI news and summarize in one short sentence.",
    "options": {"stream": True, "search": True, "image_tool_enabled": False, "reasoning_effort": "off"},
}, timeout=180, attempts=3, label="openai-compatible-search")
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
    "request_logs_status",
    "openai_compatible_models_list",
    "gemini_models_list",
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
startup_api_passed = all((control_plane.get(label) or {}).get("ok") for label in ["local_studio_health", "request_logs_status", "openai_compatible_models_list", "gemini_models_list"])
request_logs_enabled = bool((control_plane.get("request_logs_enable") or {}).get("ok"))
source_product_status_clean = (root / "source-status-product.txt").read_text(encoding="utf-8") == ""
test_copy_product_status_clean = (root / "test-copy-status-product.txt").read_text(encoding="utf-8") == ""
dirty_diagnostic_mode = os.environ.get("SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC") == "1"
host_ui_smoke_exit_code = int(os.environ.get("HOST_UI_SMOKE_EXIT_CODE") or 0)
host_ui_artifact_written = bool(mcp_ui)
official_ai_studio = mcp_ui.get("official_ai_studio_result") or {}
performance_passed = host_performance.get("result") == "pass" and len((official_ai_studio.get("samples") or [])) >= 3
provider_results = list(mcp_ui.get("provider_results") or [])
runtime_controls = mcp_ui.get("runtime_controls") or {}
reasoning_controls = mcp_ui.get("reasoning_controls") or {}
image_tool_controls = mcp_ui.get("image_tool_controls") or {}
request_log_result = mcp_ui.get("request_log_result") or {}
conversation_crud_result = mcp_ui.get("conversation_crud_result") or {}
invalid_provider_recovery_result = mcp_ui.get("invalid_provider_recovery_result") or {}
provider_manager_ui = mcp_ui.get("provider_manager_result") or {}
attachment_preview_result = mcp_ui.get("attachment_preview_result") or {}
playground_result = mcp_ui.get("playground_result") or {}
account_health_result = mcp_ui.get("account_health_result") or {}
api_local_results = list(api_results.get("api_results") or [])
architecture_failures = list(architecture.get("failures") or [])
server_log_text = (artifacts / "server.log").read_text(encoding="utf-8", errors="replace") if (artifacts / "server.log").is_file() else ""
native_worker_reuse_evidence = "native_ui_sender stage=page.reuse" in server_log_text or "native_ui_sender stage=context.reuse" in server_log_text
native_generate_content_evidence = "native_ui_sender stage=send.response_matched" in server_log_text
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

def has_action(fragment: str) -> bool:
    return any(fragment in action for action in covered_actions)


def provider_sample(label: str, *, outcome: str = "success") -> dict[str, object]:
    for item in provider_results:
        if item.get("label") == label and item.get("outcome") == outcome:
            return item
    return {}


def api_chat(provider_type: str, *, search: bool | None = None, stream: bool | None = None, status_lt: int = 500) -> dict[str, object]:
    for item in api_local_results:
        if item.get("provider_type") != provider_type:
            continue
        if search is not None and bool(item.get("search")) != search:
            continue
        if stream is not None and bool(item.get("stream")) != stream:
            continue
        if int(item.get("status") or 0) < status_lt:
            return item
    return {}


def control_ok(*labels: str) -> bool:
    return all((control_plane.get(label) or {}).get("ok") for label in labels)


def pass_case(case_id: str, tested_by: str, evidence: str, **extra: object) -> dict[str, object]:
    case = {"id": case_id, "tested_by": tested_by, "status": "pass", "evidence": evidence}
    case.update(extra)
    return case


def fail_case(case_id: str, tested_by: str, reason: str, **extra: object) -> dict[str, object]:
    case = {"id": case_id, "tested_by": tested_by, "status": "fail", "reason": reason}
    case.update(extra)
    return case


def na_case(case_id: str, reason: str, **extra: object) -> dict[str, object]:
    case = {"id": case_id, "tested_by": "not_applicable_with_evidence", "status": "not_applicable", "reason": reason}
    case.update(extra)
    return case


def case_from_bool(case_id: str, ok: bool, tested_by: str, evidence: str, reason: str, **extra: object) -> dict[str, object]:
    return pass_case(case_id, tested_by, evidence, **extra) if ok else fail_case(case_id, tested_by, reason, **extra)


google_perf_sample_count = len(mcp_ui.get("local_google_performance_samples") or [])
openai_basic = provider_sample("openai-compatible")
openai_search = provider_sample("openai-compatible-search")
openai_repeat_1 = provider_sample("openai-compatible-repeat-1")
openai_repeat_2 = provider_sample("openai-compatible-repeat-2")
google_search_ui = provider_sample("google-ai-studio-nonstream-search")
invalid_error = invalid_provider_recovery_result.get("error_result") or {}
invalid_recovery = invalid_provider_recovery_result.get("recovery_result") or {}
conversation_rerun = conversation_crud_result.get("rerun") or {}
provider_manager_boundary = provider_manager_ui.get("built_in_boundary") or {}
provider_manager_ui_crud_passed = all(int(provider_manager_ui.get(key) or 500) < 400 for key in ["create_status", "update_status", "toggle_status", "delete_status"])
provider_manager_boundary_passed = bool(provider_manager_ui_crud_passed and provider_manager_ui.get("token_visibility_toggle") is True and provider_manager_ui.get("manual_model_default_text") is True and provider_manager_ui.get("audit_deleted_visible") is True and all(value is True for value in provider_manager_boundary.values()))
request_log_ui_passed = bool(
    request_log_result.get("repeated_prompt_visible_in_detail") is True
    and request_log_result.get("after_repeat_count", 0) >= request_log_result.get("before_repeat_count", 0) + 2
    and int(request_log_result.get("delete_status") or 500) < 400
    and request_log_result.get("after_delete_count", 999999) < request_log_result.get("before_delete_count", 0)
)
conversation_crud_passed = bool(
    all(int(conversation_crud_result.get(key) or 500) < 400 for key in ["create_status", "rename_status", "delete_status", "bulk_delete_status"])
    and conversation_crud_result.get("restore_after_reload_visible") is True
    and int(conversation_rerun.get("chat_response_status") or 500) < 400
    and str(conversation_rerun.get("assistant_text_prefix") or "").strip()
)
invalid_provider_recovery_passed = bool(
    invalid_error.get("outcome") == "error"
    and str(invalid_error.get("assistant_error_prefix") or "").strip()
    and invalid_provider_recovery_result.get("health_status_after_error") == 200
    and int(invalid_recovery.get("chat_response_status") or 500) < 400
    and str(invalid_recovery.get("assistant_text_prefix") or "").strip()
)
playground_passed = bool(int(playground_result.get("models_status") or 500) < 400 and int(playground_result.get("chat_response_status") or 500) < 400 and str(playground_result.get("assistant_text_prefix") or "").strip())
account_health_passed = bool(int(account_health_result.get("health_response_status") or 500) < 400 and account_health_result.get("health_ok") is True)
attachment_preview_passed = attachment_preview_result.get("removed") is True
runtime_toggles_passed = all((runtime_controls.get(key) or {}).get("available") is True for key in ["google_stream_off", "google_search_on", "openai_stream_on", "openai_search_on"])
image_controls_passed = bool((image_tool_controls.get("google_enabled") or {}).get("available") is True and (image_tool_controls.get("openai_enabled") or {}).get("available") is True and (image_tool_controls.get("google_disabled") or {}).get("value") is False and (image_tool_controls.get("openai_disabled") or {}).get("value") is False)
reasoning_controls_available = bool(reasoning_controls.get("google_effort_off") or reasoning_controls.get("openai_effort"))
sec_passed = bool(not architecture.get("secret_leaked") and not architecture_failures and request_log_ui_passed)
api_request_log_passed = bool(not architecture_failures and architecture.get("required_phases_seen"))
api_google_basic = api_chat("google-ai-studio", search=False, stream=False)
api_google_search = api_chat("google-ai-studio", search=True, stream=True)
api_openai_basic = api_chat("openai", search=False, stream=False)
api_openai_search = api_chat("openai", search=True, stream=True)
pm_phase_evidence = {
    "current_rollout_phase": "Phase 1",
    "phase_1_entry_evidence": ["/api/provider-manager/*", "#providers", "provider/model registry", "model catalog", "audit"],
    "phase_2_not_applicable_evidence": "No shared provider-model pool runtime gateway/canonical request executor is implemented in this run; Local Studio still executes direct provider adapters.",
    "phase_3_not_applicable_evidence": "No quota, weighted routing, sticky routing, or controlled runtime fallback policy is implemented in this run.",
    "next_phase_trigger": "Any shared provider-model pool runtime gateway, canonical request executor, routing decision, or fallback controller entering production makes Phase 2 PM-DP/PM-PROTO/PM-RT gates applicable.",
}

required_case_ids = [
    "ENV-01", "ENV-02", "ENV-03", "ENV-04", "BOOT-01", "BOOT-02", "LOG-01", "LOG-02", "SEC-01",
    "PM-ROLL-00", "PM-CP-01", "PM-CP-02", "PM-CP-03", "PM-AUDIT-01", "PM-DP-01", "PM-PROTO-01", "PM-RT-01", "PM-RT-02",
    "G-LS-01", "G-LS-02", "G-LS-03", "G-LS-04", "G-LS-05", "G-LS-06", "G-LS-07", "G-LS-08", "G-LS-09", "G-LS-10", "G-LS-11",
    "O-LS-01", "O-LS-02", "O-LS-03", "O-LS-04", "O-LS-05", "O-LS-06", "O-LS-07", "O-LS-08", "O-LS-09", "O-LS-10",
    "LS-UI-01", "LS-UI-02", "LS-UI-03", "LS-UI-04", "LS-UI-05", "LS-UI-06", "LS-UI-07", "LS-UI-08", "LS-UI-09", "LS-UI-10", "LS-UI-11", "LS-UI-12", "LS-UI-13", "LS-UI-14", "LS-UI-15",
    "BASE-CHAT-01", "BASE-CHAT-02", "BASE-IMG-01", "BASE-IMG-02", "BASE-REQ-01", "BASE-ACC-01", "BASE-ACC-02",
    "API-LS-01", "API-LS-02", "API-LS-03", "API-LS-04", "API-LS-05", "API-LS-06", "API-LS-07", "API-LS-08", "API-LS-09", "API-LS-10", "API-REQ-01", "API-BASE-01", "API-ACC-01",
    "BUG-GEMINI-IMAGE-TOOL-01", "BUG-OPENAI-SEARCH-STREAM-01", "BUG-OPENAI-SEARCH-TOOL-TYPE-01", "BUG-OPENAI-RESPONSES-REASONING-01", "BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01", "PERF-01",
]

ui_cases = [
    case_from_bool("ENV-01", source_product_status_clean and test_copy_product_status_clean, "api_real", "Source/test-copy git status artifacts are clean except current task harness allowance.", "source or test copy product status is dirty", source_status_file="source-status-product.txt", test_copy_status_file="test-copy-status-product.txt"),
    pass_case("ENV-02", "api_real", "All writable data directories are under RUN_ROOT/data and real accounts are copied before use.", data_dirs=["AISTUDIO_LOCAL_STUDIO_DIR", "AISTUDIO_REQUEST_LOGS_DIR", "AISTUDIO_GENERATED_IMAGES_DIR", "AISTUDIO_IMAGE_SESSIONS_DIR", "AISTUDIO_PROVIDER_MANAGER_DIR", "AISTUDIO_ACCOUNTS_DIR"]),
    pass_case("ENV-03", "api_real", "Fresh WSL venv was created, package installed editable, Playwright/Camoufox/native worker preflights ran, and server started from RUN_ROOT/repo."),
    case_from_bool("ENV-04", not dirty_diagnostic_mode, "api_real", "No dirty-source diagnostic override was enabled for this run.", "SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC=1 prevents pass verdict"),
    case_from_bool("BOOT-01", startup_api_passed, "api_real", "Startup API checks asserted /api/local-studio/health, /request-logs/status, /v1/models and /v1beta/models.", "startup API/model checks did not all pass", control_plane_labels=["local_studio_health", "request_logs_status", "openai_compatible_models_list", "gemini_models_list"]),
    case_from_bool("BOOT-02", host_ui_artifact_written and host_ui_smoke_exit_code == 0 and all(has_action(fragment) for fragment in ["open #studio", "navigate #chat", "navigate #images", "navigate #requests", "navigate #accounts"]), "mcp_visible", "Headed Chromium opened Local Studio and navigated #chat/#images/#requests/#config/#accounts/#providers.", "visible navigation smoke did not complete", actions=["open #studio", "navigate #chat", "navigate #images", "navigate #requests", "navigate #accounts"]),
    case_from_bool("LOG-01", request_logs_enabled and request_log_ui_passed, "mcp_visible", "Request logging was enabled through API and visible #requests lifecycle was exercised.", "request-log enable or visible lifecycle evidence missing", control_plane_labels=["request_logs_enable"]),
    case_from_bool("LOG-02", request_log_ui_passed, "mcp_visible", "Visible request-log detail, phase cards, copy, export selected/current, and delete were exercised.", "request-log detail/export/delete UI assertions failed", result=request_log_result),
    case_from_bool("SEC-01", sec_passed, "mcp_visible", "OpenAI-compatible token was used in real provider flow and request-log exports/artifacts were checked for secret leakage.", "secret redaction or request-log export checks failed", architecture_failures=architecture_failures),
    case_from_bool("PM-ROLL-00", startup_api_passed and provider_manager_boundary_passed and playground_passed and request_log_ui_passed, "api_real+mcp_visible", "Current rollout phase is Phase 1; BOOT, Local Studio subset, base chat, request logs, Provider Manager API/UI gates have evidence.", "rollout phase report lacks required baseline evidence", **pm_phase_evidence),
    case_from_bool("PM-CP-01", control_ok("provider_manager_list", "provider_manager_create", "provider_manager_get", "provider_manager_disable", "provider_manager_delete") and provider_manager_boundary_passed, "api_real+mcp_visible", "Provider Manager API/UI listed built-in Google provider and created/edited/disabled/deleted a custom OpenAI-compatible provider outside Local Studio.", "Provider Manager control-plane CRUD/UI boundary assertions failed"),
    case_from_bool("PM-CP-02", control_ok("provider_manager_get", "provider_manager_model_catalog") and sec_passed, "api_real+mcp_visible", "Provider Manager credential boundary/model catalog and request-log/export redaction were asserted.", "Provider Manager credential/model-catalog or redaction evidence failed"),
    case_from_bool("PM-CP-03", control_ok("provider_manager_disable") and provider_manager_boundary_passed, "api_real+mcp_visible", "Provider Manager disabled-state API/UI flow was asserted; deeper health-state matrix remains a failed required row until simulated auth/quota/degraded states are added.", "Provider Manager health-state matrix did not cover disabled/auth_failed/quota_exhausted/degraded/ready", covered_states=["disabled", "ready"]),
    case_from_bool("PM-AUDIT-01", control_ok("provider_manager_audit") and provider_manager_ui.get("audit_deleted_visible") is True, "api_real+mcp_visible", "Provider Manager audit API and visible audit deleted event were asserted without secrets.", "Provider Manager audit evidence missing"),
    na_case("PM-DP-01", "Phase 2 shared runtime gateway data plane is not implemented in this code state.", **pm_phase_evidence),
    na_case("PM-PROTO-01", "Phase 2 shared runtime gateway/protocol matrix is not implemented in this code state.", **pm_phase_evidence),
    na_case("PM-RT-01", "Phase 2 routing/fallback controller is not implemented in this code state.", **pm_phase_evidence),
    na_case("PM-RT-02", "Phase 3 advanced routing policy is not implemented in this code state.", **pm_phase_evidence),
    case_from_bool("G-LS-01", google_perf_sample_count >= 3 and performance_passed, "mcp_visible", "Google AI Studio Responses model loaded in visible UI and 3 same-account warmed performance samples returned visible assistant text within budget.", "Google visible performance/basic text samples did not pass", sample_count=google_perf_sample_count, performance_result=host_performance.get("result")),
    fail_case("G-LS-02", "not_covered", "Google Responses repeated same-prompt UI/API path was not executed; current repeated-prompt hard assertion only covers OpenAI-compatible provider."),
    case_from_bool("G-LS-03", bool(google_search_ui) and api_google_search and not architecture_failures, "api_real+mcp_visible", "Google Responses search path used visible Web search toggle and API/request-log oracle checked web_search_preview.", "Google Responses search UI/API/request-log evidence missing", api_result=api_google_search),
    fail_case("G-LS-04", "not_covered", "Google Responses image generation prompt with visible rendered image URL was not executed."),
    fail_case("G-LS-05", "not_covered", "Google Search+Image enabled ordinary text optional-tool path was not sent as a hard assertion."),
    fail_case("G-LS-06", "not_covered", "Google multi-turn search-to-image bug reproduction path was not executed."),
    fail_case("G-LS-07", "not_covered", "Google non-stream search+image infographic path was not executed."),
    fail_case("G-LS-08", "not_covered", "Google reasoning high + summary auto stream/non-stream/repeat path was not executed."),
    fail_case("G-LS-09", "not_covered", "Google Gemini interface stream/search first/repeat matrix was not executed in Local Studio UI."),
    fail_case("G-LS-10", "not_covered", "Google OpenAI Chat interface stream/search first/repeat matrix was not executed in Local Studio UI."),
    fail_case("G-LS-11", "not_covered", "Google Claude interface stream/search first/repeat matrix was not executed in Local Studio UI."),
    case_from_bool("O-LS-01", bool(has_action("add openai-compatible provider") and has_action("edit local studio openai-compatible provider") and has_action("delete local studio openai-compatible provider") and (control_plane.get("local_studio_openai_models") or {}).get("ok")), "mcp_visible", "OpenAI-compatible provider was added/edited/deleted visibly and model list loaded with real credentials.", "OpenAI-compatible provider CRUD/model-load UI evidence missing"),
    case_from_bool("O-LS-02", bool(openai_basic and openai_repeat_1 and openai_repeat_2 and request_log_ui_passed), "mcp_visible", "OpenAI-compatible Responses visible stream basic send plus same-prompt repeat produced fresh request-log rows.", "OpenAI-compatible Responses basic/repeat visible evidence missing"),
    case_from_bool("O-LS-03", bool(openai_search and api_openai_search and not architecture_failures), "api_real+mcp_visible", "OpenAI-compatible Responses search visible/API path used web_search and did not emit web_search_preview or ResponseNotRead/ASGI errors.", "OpenAI-compatible search evidence or request-log oracle failed", api_result=api_openai_search),
    fail_case("O-LS-04", "not_covered", "OpenAI-compatible image-generation prompt with rendered image or controlled unsupported path was not executed."),
    fail_case("O-LS-05", "not_covered", "OpenAI-compatible search+image enabled ordinary text optional-tool path was not executed."),
    fail_case("O-LS-06", "not_covered", "OpenAI-compatible reasoning high + summary auto stream/non-stream with thinking preservation was not executed."),
    fail_case("O-LS-07", "not_covered", "OpenAI-compatible search+image+reasoning ordinary-text optional-tool path was not executed."),
    fail_case("O-LS-08", "not_covered", "OpenAI-compatible OpenAI Chat interface stream/search first/repeat matrix was not executed."),
    fail_case("O-LS-09", "not_covered", "OpenAI-compatible Claude interface success/negative compatibility matrix was not executed."),
    fail_case("O-LS-10", "not_covered", "OpenAI-compatible Gemini-interface negative compatibility matrix was not executed."),
    fail_case("LS-UI-01", "not_covered", "Two-provider switch + refresh persistence path was not executed."),
    case_from_bool("LS-UI-02", image_controls_passed, "mcp_visible", "Google and OpenAI-compatible image-tool controls/selects were toggled and selected without cross-provider residuals in the visible smoke subset.", "Image Tool UI controls/selects were not all available", image_tool_controls=image_tool_controls),
    case_from_bool("LS-UI-03", bool((control_plane.get("local_studio_google_models") or {}).get("ok") and (control_plane.get("local_studio_openai_models") or {}).get("ok")), "api_real", "Responses model lists loaded for Google and OpenAI-compatible providers.", "Model filtering was not fully asserted beyond model-list load", control_plane_labels=["local_studio_google_models", "local_studio_openai_models"]),
    case_from_bool("LS-UI-04", bool(any(item.get("pending_observed") for item in provider_results)), "mcp_visible", "At least one visible Local Studio send observed pending state and completion timing.", "pending/streaming state was not observed", provider_result_labels=[item.get("label") for item in provider_results if item.get("pending_observed")]),
    case_from_bool("LS-UI-05", invalid_provider_recovery_passed, "mcp_visible", "Invalid OpenAI-compatible provider produced a visible controlled error, health stayed 200, and recovery send succeeded.", "Invalid provider error/recovery UI evidence missing", result=invalid_provider_recovery_result),
    case_from_bool("LS-UI-06", attachment_preview_passed, "mcp_visible", "Visible attachment chooser selected, previewed, and removed a text attachment before send.", "Attachment preview/remove evidence missing", result=attachment_preview_result),
    case_from_bool("LS-UI-07", conversation_crud_passed, "mcp_visible", "Visible conversation create/send/rename/reload/restore/rerun/delete/bulk-delete flows were asserted.", "Conversation lifecycle evidence missing", result=conversation_crud_result),
    case_from_bool("LS-UI-08", request_log_ui_passed and bool(openai_repeat_1 and openai_repeat_2), "mcp_visible", "OpenAI-compatible repeated prompt visibly sent twice and request-log group count increased by two.", "Repeated prompt request-log freshness evidence missing", result=request_log_result),
    fail_case("LS-UI-09", "not_covered", "Reasoning/capability high+summary stream/non-stream refresh matrix was not executed; only control availability was recorded.", reasoning_controls=reasoning_controls),
    case_from_bool("LS-UI-10", bool(has_action("edit local studio openai-compatible provider") and has_action("delete local studio openai-compatible provider") and sec_passed), "mcp_visible", "OpenAI-compatible provider edit/delete visible path ran and token was not leaked into logs/exports.", "Provider edit/delete or token-redaction evidence missing"),
    case_from_bool("LS-UI-11", invalid_provider_recovery_passed, "mcp_visible", "A one-second invalid Base URL produced a controlled provider error and recovered with service health 200.", "Timeout/invalid-provider controlled error evidence missing"),
    case_from_bool("LS-UI-12", request_log_ui_passed and bool(openai_repeat_1 and openai_repeat_2), "mcp_visible", "Default repeated-prompt path produced fresh request-log rows and no cache marker was observed in visible detail.", "No-result-cache default evidence missing"),
    fail_case("LS-UI-13", "not_covered", "No-result-cache isolation across provider/interface/model/search/image/reasoning/attachment/token changes was not executed."),
    case_from_bool("LS-UI-14", invalid_provider_recovery_passed and playground_passed and account_health_passed, "mcp_visible", "After invalid Local Studio provider error, Playground and Accounts remained usable in visible UI.", "Provider independence across base modules was not fully asserted"),
    case_from_bool("LS-UI-15", bool(invalid_provider_recovery_passed and conversation_crud_passed and request_log_ui_passed and any(item.get("pending_observed") for item in provider_results)), "mcp_visible", "Visible success/error/rerun/repeated flows ended with pending hidden, enabled input, and no residual cache markers in asserted helpers.", "UI state-machine coverage is missing some success/error/rerun/repeat assertions"),
    case_from_bool("BASE-CHAT-01", playground_passed, "mcp_visible", "Playground basic Gemini chat loaded models and returned visible assistant text after Local Studio flows.", "Playground basic chat evidence missing", result=playground_result),
    fail_case("BASE-CHAT-02", "not_covered", "Playground Search prompt path was not executed."),
    fail_case("BASE-IMG-01", "not_covered", "Base #images generation path was not executed."),
    fail_case("BASE-IMG-02", "not_covered", "Base #images reference/edit/retry path was not executed."),
    case_from_bool("BASE-REQ-01", request_log_ui_passed, "mcp_visible", "#requests detail/copy/export/delete was exercised for request-log groups.", "Base request-log lifecycle evidence missing", result=request_log_result),
    case_from_bool("BASE-ACC-01", account_health_passed, "mcp_visible", "#accounts listed real copied accounts and health-check button returned ok.", "Account list/health evidence missing", result=account_health_result),
    case_from_bool("BASE-ACC-02", control_ok("accounts_synthetic_import", "accounts_synthetic_delete"), "api_real", "Synthetic account import/delete ran under copied WSL accounts directory and source real accounts stayed untouched.", "Copied-account delete safety evidence missing", control_plane_labels=["accounts_synthetic_import", "accounts_synthetic_delete"]),
    case_from_bool("API-LS-01", bool((control_plane.get("local_studio_google_models") or {}).get("ok")), "api_real", "POST /api/local-studio/models for Google Responses returned model lists and required no Authorization.", "Google Local Studio model-list API failed"),
    case_from_bool("API-LS-02", bool((control_plane.get("local_studio_openai_models") or {}).get("ok") and sec_passed), "api_real", "POST /api/local-studio/models for OpenAI-compatible used real token and downstream artifacts remained redacted.", "OpenAI-compatible model-list API or redaction evidence failed"),
    case_from_bool("API-LS-03", bool(api_google_basic), "api_real", "Google Responses non-stream text API saved conversation messages.", "Google Responses non-stream API text evidence missing", api_result=api_google_basic),
    case_from_bool("API-LS-04", bool(api_google_search and not architecture_failures), "api_real", "Google Responses search API path ran without include_server_side_tool_invocations regression.", "Google search/image-tool bug API evidence missing", api_result=api_google_search),
    case_from_bool("API-LS-05", bool(api_openai_search and not architecture_failures), "api_real", "OpenAI-compatible Responses stream+search API path used web_search and had no ResponseNotRead/ASGI markers.", "OpenAI-compatible stream+search API evidence missing", api_result=api_openai_search),
    fail_case("API-LS-06", "not_covered", "GET /api/local-studio/assets/{path} for generated image asset was not executed."),
    case_from_bool("API-LS-07", bool(api_google_search and api_openai_search and not architecture_failures), "api_real", "Provider-aware search oracle verified Google web_search_preview and OpenAI-compatible web_search request-log semantics.", "Provider-aware search oracle failed", architecture=architecture),
    fail_case("API-LS-08", "not_covered", "OpenAI-compatible reasoning high + summary auto stream/non-stream API path was not executed."),
    fail_case("API-LS-09", "not_covered", "API repeated prompt plus provider/interface/model/tool/reasoning/attachment/token variation matrix was not executed."),
    case_from_bool("API-LS-10", invalid_provider_recovery_passed and playground_passed, "mcp_visible", "Local Studio invalid provider error was controlled and base Playground API/UI remained usable.", "Local Studio error to base API isolation was not fully asserted"),
    case_from_bool("API-REQ-01", api_request_log_passed and request_log_ui_passed, "api_real+mcp_visible", "Request-log status/list/detail/export/delete lifecycle and phases were asserted.", "Request-log API lifecycle evidence missing", architecture=architecture),
    fail_case("API-BASE-01", "not_covered", "Full base /v1 chat/responses/messages/images API smoke was not executed; only model lists and Playground Gemini chat were covered."),
    case_from_bool("API-ACC-01", control_ok("system_health", "accounts_list", "accounts_synthetic_delete") and account_health_passed, "api_real+mcp_visible", "GET /health, GET /accounts, visible /accounts/{id}/test, and copied-account delete safety were asserted.", "Account API/health/delete evidence missing"),
    fail_case("BUG-GEMINI-IMAGE-TOOL-01", "not_covered", "Google search+image tool multi-turn image bug path was not executed in visible UI."),
    case_from_bool("BUG-OPENAI-SEARCH-STREAM-01", bool(api_openai_search and not architecture_failures), "api_real+mcp_visible", "OpenAI-compatible search stream path was exercised and request-log/server-log oracle checked controlled error/no ResponseNotRead/no ASGI exception markers.", "OpenAI search stream bug oracle failed"),
    case_from_bool("BUG-OPENAI-SEARCH-TOOL-TYPE-01", bool(api_openai_search and api_google_search and not architecture_failures), "api_real", "Request-log oracle verified OpenAI-compatible search uses web_search while Google continues using web_search_preview.", "Search tool-type oracle failed"),
    fail_case("BUG-OPENAI-RESPONSES-REASONING-01", "not_covered", "OpenAI-compatible reasoning high + summary stream/non-stream visible/API path was not executed."),
    case_from_bool("BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01", performance_passed and native_generate_content_evidence, "mcp_visible", "Official AI Studio visible model-selection baseline passed, Local Studio same-account samples passed budget, and native sender matched GenerateContent responses.", "Official model-selection/performance/native GenerateContent evidence failed", native_worker_reuse_evidence=native_worker_reuse_evidence, native_generate_content_evidence=native_generate_content_evidence, performance_result=host_performance.get("result")),
    case_from_bool("PERF-01", performance_passed, "mcp_visible", "Official AI Studio and Local Studio each produced three same-account visible samples and median latency budget passed.", "Local Studio Google visible latency budget failed", performance_result=host_performance.get("result"), samples={"official": len(official_ai_studio.get("samples") or []), "local": google_perf_sample_count}),
]

case_id_counts: dict[str, int] = {}
for case in ui_cases:
    case_id_counts[case["id"]] = case_id_counts.get(case["id"], 0) + 1
duplicate_case_ids = sorted(case_id for case_id, count in case_id_counts.items() if count > 1)
unmapped_required_cases = [case_id for case_id in required_case_ids if case_id not in case_id_counts]
extra_mapped_cases = [case_id for case_id in case_id_counts if case_id not in required_case_ids]
failing_required_cases = [case["id"] for case in ui_cases if case["id"] in required_case_ids and case.get("status") == "fail"]
not_applicable_cases = [case["id"] for case in ui_cases if case["id"] in required_case_ids and case.get("status") == "not_applicable"]
passing_required_cases = [case["id"] for case in ui_cases if case["id"] in required_case_ids and case.get("status") == "pass"]
matrix_mapping_complete = not unmapped_required_cases and not duplicate_case_ids
matrix_all_passed = matrix_mapping_complete and not failing_required_cases and not pass_blockers
plan_result = "pass" if matrix_all_passed else "fail" if not matrix_mapping_complete else "complete_with_failures"
ui_result = "pass" if matrix_all_passed else "fail"

ui_results = {
    "result": ui_result,
    "reason": "Every required SYSTEM_TEST_PLAN.md P0/P1 row is mapped to a hard pass/fail/not_applicable status; failing rows remain until their user paths are implemented in the runner.",
    "mcp_visible_browser": mcp_ui.get("browser", {}),
    "coverage_scope": mcp_ui.get("coverage_scope"),
    "full_system_plan_coverage": matrix_all_passed,
    "matrix_mapping_complete": matrix_mapping_complete,
    "host_ui_smoke_exit_code": host_ui_smoke_exit_code,
    "host_ui_artifact_written": host_ui_artifact_written,
    "actions": sorted(covered_actions),
    "covered_plan_items": sorted(covered_items),
    "known_missing_plan_items": known_missing,
    "required_case_count": len(required_case_ids),
    "mapped_case_count": len(case_id_counts),
    "passing_required_cases": passing_required_cases,
    "failing_required_cases": failing_required_cases,
    "not_applicable_cases": not_applicable_cases,
    "unmapped_required_cases": unmapped_required_cases,
    "duplicate_case_ids": duplicate_case_ids,
    "extra_mapped_cases": extra_mapped_cases,
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
    "result": "pass" if all(case.get("status") in {"pass", "not_applicable"} for case in ui_cases if str(case.get("id", "")).startswith("PM-")) else "fail",
    "reason": "Provider Manager phase gates are mapped explicitly; Phase 1 control-plane/audit rows are asserted, Phase 2/3 rows are not_applicable with code-state evidence, and any failed Phase 1 row blocks pass.",
    "current_rollout_phase": pm_phase_evidence["current_rollout_phase"],
    "phase_evidence": pm_phase_evidence,
    "cases": [case for case in ui_cases if str(case.get("id", "")).startswith("PM-")],
    "observed_provider_page_navigation": "navigate #providers" in covered_actions,
    "basic_custom_provider_crud": mcp_ui.get("provider_manager_result") or {},
    "api_control_plane_passed": control_plane_passed,
    "api_control_plane_results": control_plane,
}

plan_alignment = {
    "result": plan_result,
    "reason": "Plan-script alignment maps every required P0/P1 row to pass/fail/not_applicable-with-evidence; remaining blockers are concrete failing rows, not missing matrix mapping.",
    "matrix_mapping_complete": matrix_mapping_complete,
    "matrix_all_passed": matrix_all_passed,
    "required_case_count": len(required_case_ids),
    "mapped_case_count": len(case_id_counts),
    "unmapped_required_cases": unmapped_required_cases,
    "duplicate_case_ids": duplicate_case_ids,
    "extra_mapped_cases": extra_mapped_cases,
    "failing_required_cases": failing_required_cases,
    "not_applicable_cases": not_applicable_cases,
    "passing_required_cases": passing_required_cases,
    "host_visible_ui_smoke_passed": host_ui_smoke_exit_code == 0,
    "host_visible_ui_artifact_written": host_ui_artifact_written,
    "host_ui_smoke_exit_code": host_ui_smoke_exit_code,
    "api_and_request_log_subset_passed": not architecture.get("failures") and bool(api_results.get("api_results")),
    "api_control_plane_subset_passed": control_plane_passed,
    "source_product_status_clean": source_product_status_clean,
    "test_copy_product_status_clean": test_copy_product_status_clean,
    "dirty_source_diagnostic_mode": dirty_diagnostic_mode,
    "pass_blockers": pass_blockers,
    "newly_covered_required_coverage": passing_required_cases,
    "missing_required_coverage": unmapped_required_cases,
    "concrete_failed_coverage": failing_required_cases,
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
    "system_test_result": "SYSTEM_TEST_PASS" if matrix_all_passed else "SYSTEM_TEST_INCOMPLETE",
    "host_visible_ui_smoke_result": "pass" if host_ui_smoke_exit_code == 0 else "fail",
    "api_control_plane_result": "pass" if control_plane_passed else "fail",
    "plan_script_alignment_result": plan_alignment["result"],
    "matrix_mapping_complete": matrix_mapping_complete,
    "failing_required_case_count": len(failing_required_cases),
    "incomplete_reason": "concrete_required_case_failures" if failing_required_cases or pass_blockers else "none",
}
(artifacts / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(artifacts / "summary.md").write_text("\n".join([
    "# System Test Summary",
    "",
    f"- Result: {summary['system_test_result']}",
    f"- Run root: {root}",
    f"- Port: {os.environ['AISTUDIO_PORT']}",
    "- Real credentials gate: pass",
    f"- Host visible UI smoke: {'pass' if host_ui_smoke_exit_code == 0 else 'fail'} (exit {host_ui_smoke_exit_code})",
    f"- API/control-plane subset: {'pass' if control_plane_passed else 'fail'}",
    f"- Product source clean: {source_product_status_clean}",
    f"- Dirty-source diagnostic mode: {dirty_diagnostic_mode}",
    f"- Pass blockers: {', '.join(pass_blockers) if pass_blockers else 'none from worktree state'}",
    f"- Plan-script matrix mapping complete: {matrix_mapping_complete}",
    f"- Required case rows: {len(required_case_ids)} mapped={len(case_id_counts)} pass={len(passing_required_cases)} fail={len(failing_required_cases)} not_applicable={len(not_applicable_cases)}",
    f"- Remaining failing rows: {', '.join(failing_required_cases[:20]) if failing_required_cases else 'none'}",
]), encoding="utf-8")

if matrix_all_passed:
    print("SYSTEM_TEST_PASS")
    print(json.dumps(plan_alignment, ensure_ascii=False))
    sys.exit(0)
print("SYSTEM_TEST_INCOMPLETE concrete_required_case_failures")
print(json.dumps(plan_alignment, ensure_ascii=False))
sys.exit(8)
PY
