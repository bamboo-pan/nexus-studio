#!/usr/bin/env bash
set -euo pipefail
set +x

SRC="/mnt/c/Users/bamboo/Desktop/nexus-studio"
TASK_DIR="$SRC/.trellis/tasks/06-06-fix-native-ui-worker-wsl-system-test"
RUN_ROOT="/home/bamboo/nexus-studio-native-worker-import-$(date +%Y%m%d-%H%M%S)"
export RUN_ROOT
mkdir -p "$RUN_ROOT/artifacts"
echo "SYSTEM_TEST_START run_root=$RUN_ROOT"

if [ -z "${HTTPS_PROXY:-${https_proxy:-}}" ] && [ -r /etc/profile.d/wsl-proxy.sh ]; then
    . /etc/profile.d/wsl-proxy.sh
    echo "WSL_PROFILE_PROXY_LOADED source=/etc/profile.d/wsl-proxy.sh"
fi

echo "COPY_REPO_START"
rsync -a --delete \
  --exclude .git \
  --exclude .venv \
  --exclude venv \
  --exclude data \
  --exclude tmp \
  "$SRC/" "$RUN_ROOT/repo/"
echo "COPY_REPO_OK"

REAL_ACCOUNTS_DIR="/home/bamboo/nexus-studio/data/accounts"
if [ ! -d "$REAL_ACCOUNTS_DIR" ]; then
  echo "SYSTEM_TEST_FAIL missing_real_accounts_dir=$REAL_ACCOUNTS_DIR"
  exit 2
fi
mkdir -p "$RUN_ROOT/data"
rsync -a --delete "$REAL_ACCOUNTS_DIR/" "$RUN_ROOT/data/accounts/"
echo "COPY_ACCOUNTS_OK source=$REAL_ACCOUNTS_DIR"

cd "$RUN_ROOT/repo"
python3 -m venv venv
. venv/bin/activate
echo "PYTHON_ENV_READY python=$(python -c 'import sys; print(sys.executable)')"
if python -m pip install -q -r requirements.txt; then
    echo "INSTALL_DEPS_OK mode=requirements_no_editable"
else
    DEP_VENV="${AISTUDIO_SYSTEM_TEST_DEP_VENV:-/home/bamboo/nexus-studio/.venv}"
    if [ ! -x "$DEP_VENV/bin/python" ]; then
        echo "SYSTEM_TEST_FAIL dependency_install_failed_and_dep_venv_missing=$DEP_VENV"
        exit 2
    fi
    PY_VERSION="$(python - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"
    DEP_PY_VERSION="$($DEP_VENV/bin/python - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"
    if [ "$PY_VERSION" != "$DEP_PY_VERSION" ]; then
        echo "SYSTEM_TEST_FAIL dependency_install_failed_and_dep_venv_python_mismatch current=$PY_VERSION dep=$DEP_PY_VERSION"
        exit 2
    fi
    if $DEP_VENV/bin/python - <<'PY'
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("aistudio_api") is None else 1)
PY
    then
        :
    else
        echo "SYSTEM_TEST_FAIL dependency_venv_contains_aistudio_api=$DEP_VENV"
        exit 2
    fi
    DEP_SITE="$($DEP_VENV/bin/python - <<'PY'
import site
for path in site.getsitepackages():
        if path.endswith("site-packages"):
                print(path)
                break
PY
)"
    if [ -z "$DEP_SITE" ] || [ ! -d "$DEP_SITE" ]; then
        echo "SYSTEM_TEST_FAIL dependency_site_packages_missing=$DEP_SITE"
        exit 2
    fi
    export AISTUDIO_SYSTEM_TEST_DEPENDENCY_PYTHONPATH="$DEP_SITE"
    export PYTHONPATH="$RUN_ROOT/repo/src:$AISTUDIO_SYSTEM_TEST_DEPENDENCY_PYTHONPATH"
    echo "INSTALL_DEPS_OK mode=dependency_site_fallback_no_editable dep_site=$DEP_SITE"
fi

REPO_SRC="$RUN_ROOT/repo/src"
if [ -n "${AISTUDIO_SYSTEM_TEST_DEPENDENCY_PYTHONPATH:-}" ]; then
    export PYTHONPATH="$REPO_SRC:$AISTUDIO_SYSTEM_TEST_DEPENDENCY_PYTHONPATH"
else
    export PYTHONPATH="$REPO_SRC"
fi
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
export AISTUDIO_ACCOUNT_WARMUP_LIMIT="${AISTUDIO_ACCOUNT_WARMUP_LIMIT:-0}"
export AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS="${AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS:-300}"
export AISTUDIO_CAMOUFOX_HEADLESS="${AISTUDIO_CAMOUFOX_HEADLESS:-1}"
export AISTUDIO_CAMOUFOX_GEOIP="${AISTUDIO_CAMOUFOX_GEOIP:-0}"
export SYSTEM_TEST_MODEL="${SYSTEM_TEST_MODEL:-gemini-3.5-flash}"
export AISTUDIO_SYSTEM_TEST_REQUIRE_WARMUP="${AISTUDIO_SYSTEM_TEST_REQUIRE_WARMUP:-0}"

python - <<'PY'
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

root = Path(os.environ["RUN_ROOT"])
probe = {"urllib": {}, "camoufox": {}, "proxy_env": {}}

def safe_proxy_value(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return "<set>"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", "", "", ""))

for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
    value = os.environ.get(key)
    if value:
        probe["proxy_env"][key] = safe_proxy_value(value)

for name, url in {
    "google": "https://www.google.com/",
    "aistudio": "https://aistudio.google.com/",
}.items():
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            probe["urllib"][name] = {"ok": True, "status": int(response.status), "url": response.geturl()}
    except Exception as exc:
        probe["urllib"][name] = {"ok": False, "type": type(exc).__name__, "error": str(exc)[:500]}

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

(root / "artifacts" / "network-preflight.safe.json").write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8")
if not (probe["urllib"].get("aistudio", {}).get("ok") and probe["camoufox"].get("ok")):
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
dependency_pythonpath = os.environ.get("AISTUDIO_SYSTEM_TEST_DEPENDENCY_PYTHONPATH", "")
if dependency_pythonpath:
    worker_env["PYTHONPATH"] = dependency_pythonpath
else:
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
from urllib import error, request

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
base = f"http://127.0.0.1:{port}"
require_warmup = os.environ.get("AISTUDIO_SYSTEM_TEST_REQUIRE_WARMUP", "0").lower() not in {"0", "false", "no", "off"}

def http_json(method: str, path: str, payload=None, timeout: int = 10):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(base + path, data=body, method=method, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None

last = None
deadline = time.time() + 900
next_progress = 0.0
while time.time() < deadline:
    try:
        last = http_json("GET", "/health", timeout=5)
    except Exception as exc:
        last = {"error": str(exc)}
        time.sleep(1)
        continue
    warmup = last.get("warmup") if isinstance(last, dict) else {}
    status = warmup.get("status") if isinstance(warmup, dict) else None
    if time.time() >= next_progress:
        print(f"WARMUP_WAIT status={status} completed={len(warmup.get('completed_accounts') or [])} failed={len(warmup.get('failed_accounts') or [])}")
        next_progress = time.time() + 30
    if not require_warmup and status == "idle":
        (root / "artifacts" / "warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        print("WARMUP_SKIPPED status=idle")
        break
    if status in {"complete", "partial"}:
        (root / "artifacts" / "warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"WARMUP_READY status={status}")
        break
    if status in {"failed", "cancelled"}:
        (root / "artifacts" / "warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"SYSTEM_TEST_FAIL warmup_status={status}")
        sys.exit(3)
    time.sleep(2)
else:
    (root / "artifacts" / "warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SYSTEM_TEST_FAIL warmup_timeout")
    sys.exit(4)

models_payload = {
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
}
models = http_json("POST", "/api/local-studio/models", models_payload, timeout=120)
(root / "artifacts" / "models.safe.json").write_text(json.dumps({"count": len(models.get("data") or []), "first": (models.get("data") or [{}])[:3]}, ensure_ascii=False, indent=2), encoding="utf-8")
if not any(item.get("id") == os.environ["SYSTEM_TEST_MODEL"] for item in models.get("data") or []):
    print("SYSTEM_TEST_FAIL target_model_missing")
    sys.exit(5)

status = http_json("PUT", "/request-logs/status", {"enabled": True}, timeout=30)
(root / "artifacts" / "request-log-status.safe.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
print("API_PREFLIGHT_OK")
PY

python - <<'PY'
import json
import os
import sys
from pathlib import Path
from urllib import error, request

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
base = f"http://127.0.0.1:{port}"
payload = {
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
    "model": os.environ["SYSTEM_TEST_MODEL"],
    "message": "Reply with exactly: nexus-native-worker-api-ok",
    "options": {"stream": True, "search": False, "image_tool_enabled": False, "reasoning_effort": "off"},
}
req = request.Request(base + "/api/local-studio/chat", data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"})
content = ""
error_text = ""
status_code = None
try:
    with request.urlopen(req, timeout=360) as response:
        status_code = response.status
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            if parsed.get("type") == "error" or parsed.get("error"):
                error_text = json.dumps(parsed, ensure_ascii=False)[:500]
            if parsed.get("type") == "local_studio.delta":
                content += str(parsed.get("content") or "")
except error.HTTPError as exc:
    status_code = exc.code
    error_text = exc.read(500).decode("utf-8", errors="replace")
except Exception as exc:
    error_text = str(exc)[:500]

result = {"status_code": status_code, "content_prefix": content[:500], "error": error_text}
(root / "artifacts" / "api-local-studio.safe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
if status_code != 200 or error_text or not content.strip():
    print("SYSTEM_TEST_FAIL api_local_studio_failed")
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(6)
print("API_LOCAL_STUDIO_OK")
PY

HOST_ARTIFACT_WIN="$(wslpath -w "$RUN_ROOT/artifacts")"
HOST_SCRIPT_WIN="$(wslpath -w "$TASK_DIR/host-ui-smoke.py")"
HOST_PYTHON_WIN="$(wslpath -w "$SRC/.venv/Scripts/python.exe")"
echo "HOST_UI_SMOKE_START artifacts=$HOST_ARTIFACT_WIN"
ps_quote() {
    printf "%s" "$1" | sed "s/'/''/g"
}
HOST_ARTIFACT_PS="$(ps_quote "$HOST_ARTIFACT_WIN")"
HOST_SCRIPT_PS="$(ps_quote "$HOST_SCRIPT_WIN")"
HOST_PYTHON_PS="$(ps_quote "$HOST_PYTHON_WIN")"
SYSTEM_TEST_MODEL_PS="$(ps_quote "$SYSTEM_TEST_MODEL")"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command \
    "\$env:AISTUDIO_PORT='$PORT'; \$env:SYSTEM_TEST_MODEL='$SYSTEM_TEST_MODEL_PS'; \$env:HOST_ARTIFACT_DIR='$HOST_ARTIFACT_PS'; & '$HOST_PYTHON_PS' '$HOST_SCRIPT_PS'"

python - <<'PY'
import json
import os
import re
import sys
from pathlib import Path

root = Path(os.environ["RUN_ROOT"])
log_path = root / "artifacts" / "server.log"
text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
failures = []
if "ModuleNotFoundError: No module named 'aistudio_api'" in text:
    failures.append("module_not_found")
if "Error while finding module specification for 'aistudio_api.infrastructure.gateway.native_ui_sender'" in text:
    failures.append("module_spec_error")
ready = re.findall(r"AI Studio native UI worker pool ready: auth_hash=([0-9a-f]+) worker_count=(\d+)", text)
starts = re.findall(r"Started native UI worker index=(\d+) pid=(\d+)", text)
matches = re.findall(r"AI Studio native UI worker replay matched response: auth_hash=([0-9a-f]+) worker=(\d+) status=(\d+)", text)
if not ready:
    failures.append("pool_ready_missing")
if not starts:
    failures.append("worker_start_missing")
if not matches:
    failures.append("matched_response_missing")
summary = {
    "ready": [{"auth_hash": item[0], "worker_count": int(item[1])} for item in ready],
    "starts": [{"worker": int(item[0]), "pid": int(item[1])} for item in starts],
    "matches": [{"auth_hash": item[0], "worker": int(item[1]), "status": int(item[2])} for item in matches],
    "failures": failures,
}
(root / "artifacts" / "native-worker-log-oracle.safe.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
if failures:
    print("SYSTEM_TEST_FAIL native_worker_log_oracle_failed")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(7)
print("NATIVE_WORKER_LOG_ORACLE_OK")
PY

echo "SYSTEM_TEST_PASS run_root=$RUN_ROOT port=$PORT camoufox_port=$CAMOUFOX_PORT"
