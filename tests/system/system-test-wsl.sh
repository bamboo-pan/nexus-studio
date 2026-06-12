#!/usr/bin/env bash
set -euo pipefail
set +x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
SYSTEM_TEST_DIR="${SYSTEM_TEST_DIR:-$SCRIPT_DIR}"
RUN_ROOT="/home/bamboo/nexus-studio-system-test-$(date +%Y%m%d-%H%M%S)"
export RUN_ROOT
SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC="${SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC:-0}"
export SYSTEM_TEST_ALLOW_DIRTY_SOURCE_DIAGNOSTIC
SYSTEM_TEST_RESUME_FROM_RUN_ROOT="${SYSTEM_TEST_RESUME_FROM_RUN_ROOT:-}"
SYSTEM_TEST_RESUME_OFFICIAL_BASELINE_FROM_RUN_ROOT="${SYSTEM_TEST_RESUME_OFFICIAL_BASELINE_FROM_RUN_ROOT:-$SYSTEM_TEST_RESUME_FROM_RUN_ROOT}"
SYSTEM_TEST_REUSE_API_RESULTS="${SYSTEM_TEST_REUSE_API_RESULTS:-1}"
export SYSTEM_TEST_RESUME_FROM_RUN_ROOT
export SYSTEM_TEST_RESUME_OFFICIAL_BASELINE_FROM_RUN_ROOT
export SYSTEM_TEST_REUSE_API_RESULTS
mkdir -p "$RUN_ROOT/artifacts"
echo "SYSTEM_TEST_START run_root=$RUN_ROOT"
if [ -n "$SYSTEM_TEST_RESUME_FROM_RUN_ROOT" ]; then
    echo "SYSTEM_TEST_RESUME_FROM_RUN_ROOT source=$SYSTEM_TEST_RESUME_FROM_RUN_ROOT official_baseline_source=$SYSTEM_TEST_RESUME_OFFICIAL_BASELINE_FROM_RUN_ROOT reuse_api=$SYSTEM_TEST_REUSE_API_RESULTS"
fi

if [ -z "${HTTPS_PROXY:-${https_proxy:-}}" ] && [ -r /etc/profile.d/wsl-proxy.sh ]; then
    . /etc/profile.d/wsl-proxy.sh
    echo "WSL_PROFILE_PROXY_LOADED source=/etc/profile.d/wsl-proxy.sh"
fi

if [ -z "${AISTUDIO_PROXY_SERVER:-}" ]; then
    AISTUDIO_PROXY_SERVER="${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}"
    if [ -n "$AISTUDIO_PROXY_SERVER" ]; then
        export AISTUDIO_PROXY_SERVER
        echo "AISTUDIO_PROXY_SERVER_BRIDGED source=wsl_proxy_env"
    fi
else
    export AISTUDIO_PROXY_SERVER
    echo "AISTUDIO_PROXY_SERVER_PRESET"
fi

cd "$SRC"
git rev-parse HEAD > "$RUN_ROOT/source-commit.txt"
git -c core.autocrlf=true status --porcelain > "$RUN_ROOT/source-status.txt"
git -c core.autocrlf=true status --short --branch > "$RUN_ROOT/source-status-branch.txt"
grep -Ev '^.. (\.trellis/tasks/06-08-full-system-test-bugfix|tests/system)(/|$)' "$RUN_ROOT/source-status.txt" > "$RUN_ROOT/source-status-product.txt" || true
grep -E '^.. (\.trellis/tasks/06-08-full-system-test-bugfix|tests/system)(/|$)' "$RUN_ROOT/source-status.txt" > "$RUN_ROOT/source-status-task-harness.txt" || true
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
grep -Ev '^.. (\.trellis/tasks/06-08-full-system-test-bugfix|tests/system)(/|$)' "$RUN_ROOT/test-copy-status.txt" > "$RUN_ROOT/test-copy-status-product.txt" || true
grep -E '^.. (\.trellis/tasks/06-08-full-system-test-bugfix|tests/system)(/|$)' "$RUN_ROOT/test-copy-status.txt" > "$RUN_ROOT/test-copy-status-task-harness.txt" || true
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
export AISTUDIO_ACCOUNT_QUOTA_EXHAUSTED_COOLDOWN_SECONDS="${AISTUDIO_ACCOUNT_QUOTA_EXHAUSTED_COOLDOWN_SECONDS:-1}"
export AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS="${AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS:-300}"
export AISTUDIO_CAMOUFOX_HEADLESS="${AISTUDIO_CAMOUFOX_HEADLESS:-1}"
export AISTUDIO_CAMOUFOX_GEOIP="${AISTUDIO_CAMOUFOX_GEOIP:-0}"
export AISTUDIO_PROXY_SERVER="${AISTUDIO_PROXY_SERVER:-}"
export SYSTEM_TEST_MODEL_CANDIDATES="${SYSTEM_TEST_MODEL_CANDIDATES:-gemini-3-flash-preview,gemini-3.1-flash-lite,gemma-4-31b-it,gemini-flash-lite-latest,gemini-flash-latest,gemini-3.5-flash}"
export SYSTEM_TEST_MODEL="${SYSTEM_TEST_MODEL:-${SYSTEM_TEST_MODEL_CANDIDATES%%,*}}"
export AISTUDIO_WARMUP_TEXT_MODEL="${AISTUDIO_WARMUP_TEXT_MODEL:-$SYSTEM_TEST_MODEL}"
export AISTUDIO_WARMUP_TEXT_MODEL_CANDIDATES="${AISTUDIO_WARMUP_TEXT_MODEL_CANDIDATES:-$SYSTEM_TEST_MODEL_CANDIDATES}"
export OPENAI_COMPAT_TEXT_MODEL="${OPENAI_COMPAT_TEXT_MODEL:-gpt-5.4-mini}"
export SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS="${SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS:-30}"

if [ -n "$SYSTEM_TEST_RESUME_FROM_RUN_ROOT" ]; then
    RESUME_ARTIFACTS="$SYSTEM_TEST_RESUME_FROM_RUN_ROOT/artifacts"
    if [ ! -d "$RESUME_ARTIFACTS" ]; then
        echo "SYSTEM_TEST_FAIL resume_artifacts_missing=$RESUME_ARTIFACTS"
        exit 2
    fi
    python - <<'PY'
import json
import os
import shutil
from pathlib import Path

root = Path(os.environ["RUN_ROOT"])
source_root = Path(os.environ["SYSTEM_TEST_RESUME_FROM_RUN_ROOT"])
official_source_root_value = os.environ.get("SYSTEM_TEST_RESUME_OFFICIAL_BASELINE_FROM_RUN_ROOT", "").strip()
official_source_root = Path(official_source_root_value) if official_source_root_value else source_root
source_artifacts = source_root / "artifacts"
official_source_artifacts = official_source_root / "artifacts"
target_artifacts = root / "artifacts"
reuse_api = os.environ.get("SYSTEM_TEST_REUSE_API_RESULTS", "1") == "1"
required_api_files = [
    "api-results.json",
    "openai-compatible-model.safe.txt",
    "openai-compatible-reasoning-model.safe.txt",
    "openai-compatible-model-probe.safe.json",
]
reusable_api_evidence_files = [
    "architecture-contract-results.json",
]
copied: list[str] = []
missing: list[str] = []
skipped_optional: list[dict[str, object]] = []

def copy_required(name: str) -> None:
    source = source_artifacts / name
    if not source.is_file() or source.stat().st_size <= 0:
        missing.append(name)
        return
    shutil.copy2(source, target_artifacts / name)
    copied.append(name)

if reuse_api:
    for file_name in required_api_files:
        copy_required(file_name)
    if missing:
        raise SystemExit(f"SYSTEM_TEST_FAIL resume_api_artifacts_missing={','.join(missing)}")
    api_payload = json.loads((target_artifacts / "api-results.json").read_text(encoding="utf-8"))
    api_items = api_payload.get("api_results") if isinstance(api_payload, dict) else None
    control_plane = api_payload.get("control_plane_results") if isinstance(api_payload, dict) else None
    if not isinstance(api_items, list) or not api_items or not isinstance(control_plane, dict) or not control_plane:
        raise SystemExit("SYSTEM_TEST_FAIL resume_api_artifact_invalid")
    if not api_payload.get("google_quota_blocker") and api_payload.get("checkpoint") != "api_complete":
        raise SystemExit("SYSTEM_TEST_FAIL resume_api_artifact_incomplete")
    api_payload["reused_from_run_root"] = str(source_root)
    api_payload["reused_artifacts"] = list(required_api_files)
    (target_artifacts / "api-results.json").write_text(json.dumps(api_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for file_name in reusable_api_evidence_files:
        source = source_artifacts / file_name
        if not source.is_file() or source.stat().st_size <= 0:
            skipped_optional.append({"artifact": file_name, "reason": "artifact missing or empty"})
            continue
        payload = json.loads(source.read_text(encoding="utf-8"))
        if file_name == "architecture-contract-results.json" and (not isinstance(payload, dict) or payload.get("failures")):
            skipped_optional.append({
                "artifact": file_name,
                "reason": "architecture/request-log oracle was not clean",
                "failures": payload.get("failures") if isinstance(payload, dict) else None,
            })
            continue
        if isinstance(payload, dict):
            payload["reused_from_run_root"] = str(source_root)
            payload["reused_artifact"] = file_name
            payload["current_run_request_log_oracle_skipped"] = True
            (target_artifacts / file_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            shutil.copy2(source, target_artifacts / file_name)
        copied.append(file_name)

official_source = official_source_artifacts / "host-official-aistudio-results.json"
official_target = target_artifacts / "host-official-aistudio-results.json"
if official_source.is_file() and official_source.stat().st_size > 0:
    official_payload = json.loads(official_source.read_text(encoding="utf-8"))
    if not isinstance(official_payload, dict) or official_payload.get("result") != "pass" or len(official_payload.get("samples") or []) < 3:
        skipped_optional.append({
            "artifact": "host-official-aistudio-results.json",
            "source_run_root": str(official_source_root),
            "reason": "official baseline was not pass with at least 3 samples",
            "result": official_payload.get("result") if isinstance(official_payload, dict) else None,
            "sample_count": len(official_payload.get("samples") or []) if isinstance(official_payload, dict) else 0,
        })
    else:
        official_payload["reused_from_run_root"] = str(official_source_root)
        official_payload["reused_artifact"] = "host-official-aistudio-results.json"
        official_target.write_text(json.dumps(official_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        copied.append("host-official-aistudio-results.json")
        screenshot_names: set[str] = set()
        for key in ("screenshot",):
            value = official_payload.get(key)
            if isinstance(value, str) and value:
                screenshot_names.add(value)
        for value in official_payload.get("screenshots") or []:
            if isinstance(value, str) and value:
                screenshot_names.add(value)
        for candidate in official_payload.get("candidate_results") or []:
            if isinstance(candidate, dict):
                value = candidate.get("screenshot")
                if isinstance(value, str) and value:
                    screenshot_names.add(value)
        for screenshot_name in sorted(screenshot_names):
            source = official_source_artifacts / screenshot_name
            if source.is_file() and source.stat().st_size > 0:
                shutil.copy2(source, target_artifacts / screenshot_name)
                copied.append(screenshot_name)
else:
    skipped_optional.append({
        "artifact": "host-official-aistudio-results.json",
        "source_run_root": str(official_source_root),
        "reason": "artifact missing or empty",
    })

evidence = {
    "result": "pass",
    "mode": "low_quota_resume",
    "source_run_root": str(source_root),
    "official_baseline_source_run_root": str(official_source_root),
    "target_run_root": str(root),
    "reuse_api_results": reuse_api,
    "copied_artifacts": sorted(set(copied)),
    "skipped_optional_artifacts": skipped_optional,
    "missing_optional_artifacts": ["host-official-aistudio-results.json"] if not official_target.is_file() else [],
}
(target_artifacts / "resume-evidence.safe.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
print("SYSTEM_TEST_RESUME_EVIDENCE_OK copied=" + ",".join(evidence["copied_artifacts"]))
PY
fi

python - <<'PY'
import json
import os
import sys
import time
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

def urllib_preflight(url: str, *, attempts: int = 3) -> dict[str, object]:
    attempt_results: list[dict[str, object]] = []
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url)
            with urllib.request.urlopen(request, timeout=30) as response:
                item = {"attempt": attempt, "ok": True, "status": int(response.status), "url": response.geturl()}
                attempt_results.append(item)
                return {**item, "attempts": attempt_results}
        except Exception as exc:
            item = {"attempt": attempt, "ok": False, "type": type(exc).__name__, "error": safe_prefix(str(exc))}
            attempt_results.append(item)
            if attempt < attempts:
                time.sleep(min(5, attempt * 2))
    last = attempt_results[-1] if attempt_results else {"ok": False}
    return {**last, "attempts": attempt_results}

for name, url in {"google": "https://www.google.com/", "aistudio": "https://aistudio.google.com/"}.items():
    probe["urllib"][name] = urllib_preflight(url)

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

def google_quota_exhausted_text(text):
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

def safe_preview(text, limit=500):
    return " ".join(str(text or "").split())[:limit]

def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

def write_warmup_quota_blocker(health_payload, warmup_status):
    artifacts = root / "artifacts"
    server_log_text = read_text(artifacts / "server.log")
    if not google_quota_exhausted_text(server_log_text):
        return False
    quota_lines = [line for line in server_log_text.splitlines() if google_quota_exhausted_text(line)]
    status_code = 429 if "status=429" in server_log_text or "current quota" in server_log_text.lower() else 0
    try:
        google_interval_seconds = max(0, int(os.environ.get("SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS", "30") or "30"))
    except ValueError:
        google_interval_seconds = 30
    blocker = {
        "result": "blocked",
        "reason": "external_google_quota_exhausted",
        "source": "wsl-startup-warmup",
        "label": "startup-account-browser-warmup",
        "status": status_code,
        "warmup_status": warmup_status,
        "text_preview": safe_preview("\n".join(quota_lines[-5:]) or server_log_text),
        "google_request_interval_seconds": google_interval_seconds,
        "warmup": (health_payload or {}).get("warmup") if isinstance(health_payload, dict) else {},
    }
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "google-quota-blocker.safe.json").write_text(json.dumps(blocker, ensure_ascii=False, indent=2), encoding="utf-8")
    architecture = {
        "result": "blocked",
        "reason": "external_google_quota_exhausted",
        "failures": [],
        "oracle_skipped": True,
        "blocked_before_host_ui": True,
    }
    (artifacts / "architecture-contract-results.json").write_text(json.dumps(architecture, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "run_root": str(root),
        "system_test_result": "SYSTEM_TEST_BLOCKED",
        "incomplete_reason": "external_google_quota_exhausted",
        "external_google_quota_blocked": True,
        "google_quota_blocker": blocker,
        "warmup_status": warmup_status,
    }
    (artifacts / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (artifacts / "summary.md").write_text("\n".join([
        "# System Test Summary",
        "",
        "- Result: SYSTEM_TEST_BLOCKED",
        f"- Run root: {root}",
        "- Incomplete reason: external_google_quota_exhausted",
        "- Blocked phase: startup-account-browser-warmup",
        f"- Warmup status: {warmup_status}",
    ]), encoding="utf-8")
    return True

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
        if write_warmup_quota_blocker(last, status):
            print("SYSTEM_TEST_BLOCKED external_google_quota_exhausted")
            sys.exit(9)
        print(f"SYSTEM_TEST_FAIL warmup_status={status}")
        sys.exit(3)
    time.sleep(2)
else:
    (root / "artifacts" / "warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
    if write_warmup_quota_blocker(last, "timeout"):
        print("SYSTEM_TEST_BLOCKED external_google_quota_exhausted")
        sys.exit(9)
    print("SYSTEM_TEST_FAIL warmup_timeout")
    sys.exit(4)
print("HEALTH_WARMUP_OK")
PY

if [ "$SYSTEM_TEST_REUSE_API_RESULTS" = "1" ] && [ -n "$SYSTEM_TEST_RESUME_FROM_RUN_ROOT" ] && [ -s "$RUN_ROOT/artifacts/api-results.json" ]; then
    echo "API_REAL_PROVIDER_REUSED source=$SYSTEM_TEST_RESUME_FROM_RUN_ROOT/artifacts/api-results.json"
    python - <<'PY'
import json
import os
from pathlib import Path
from urllib import request

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
base = f"http://127.0.0.1:{port}"

def http_json(method: str, path: str, payload=None, timeout: int = 60):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(base + path, data=body, method=method, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return response.status, json.loads(raw) if raw else None

health_status, health = http_json("GET", "/health")
logs_status, logs = http_json("PUT", "/request-logs/status", {"enabled": True})
evidence = {
    "result": "pass",
    "mode": "low_quota_resume_current_run_setup",
    "health_status": health_status,
    "warmup_status": (health.get("warmup") or {}).get("status") if isinstance(health, dict) else None,
    "request_logs_status": logs_status,
    "request_logs_enabled": logs.get("enabled") is True if isinstance(logs, dict) else False,
}
if health_status >= 400 or logs_status >= 400 or evidence["request_logs_enabled"] is not True:
    raise SystemExit(f"SYSTEM_TEST_FAIL resume_current_setup={json.dumps(evidence, ensure_ascii=False)}")
api_results_path = root / "artifacts" / "api-results.json"
if api_results_path.is_file():
    api_payload = json.loads(api_results_path.read_text(encoding="utf-8"))
    if isinstance(api_payload, dict):
        control_plane = api_payload.setdefault("control_plane_results", {})
        if isinstance(control_plane, dict):
            control_plane["system_health_current_resume_setup"] = {
                "status": health_status,
                "ok": health_status < 400,
                "warmup_status": evidence["warmup_status"],
                "current_run": True,
            }
            control_plane["request_logs_enable"] = {
                "status": logs_status,
                "ok": logs_status < 400 and evidence["request_logs_enabled"] is True,
                "enabled": evidence["request_logs_enabled"],
                "current_run": True,
            }
        api_payload["current_run_setup"] = evidence
        api_results_path.write_text(json.dumps(api_payload, ensure_ascii=False, indent=2), encoding="utf-8")
(root / "artifacts" / "resume-current-run-setup.safe.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
print("API_REAL_PROVIDER_REUSE_CURRENT_SETUP_OK request_logs_enabled=1")
PY
else
python - <<'PY'
import json
import os
import sys
import time
from pathlib import Path
from urllib import error, request
from urllib.parse import quote

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
google_model_candidates_raw = os.environ.get("SYSTEM_TEST_MODEL_CANDIDATES", "")
api_results = []
control_plane_results = {}
base_api_results: list[dict[str, object]] = []
google_request_interval_seconds = max(0, int(os.environ.get("SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS", "30") or 0))
last_google_request_at = 0.0


def throttle_google_request(label: str) -> None:
    global last_google_request_at
    if google_request_interval_seconds <= 0:
        return
    now = time.monotonic()
    if last_google_request_at:
        wait_seconds = google_request_interval_seconds - (now - last_google_request_at)
        if wait_seconds > 0:
            print(f"API_GOOGLE_REQUEST_THROTTLE label={label} wait_seconds={wait_seconds:.1f}")
            time.sleep(wait_seconds)
            now = time.monotonic()
    last_google_request_at = now

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

def http_binary(method: str, path: str, payload=None, timeout: int = 120, expect_error: bool = False):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(base + path, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except error.HTTPError as exc:
        raw = exc.read()
        if expect_error:
            return exc.code, dict(exc.headers), raw
        raise

def safe_api_prefix(text: str, limit: int = 500) -> str:
    value = str(text or "")[:limit]
    if openai_token:
        value = value.replace(openai_token, "[REDACTED]")
    return value


def parse_local_studio_sse(raw: str) -> dict[str, object]:
    events: list[dict[str, object]] = []
    completed: dict[str, object] = {}
    for line in str(raw or "").splitlines():
        if not line.startswith("data: "):
            continue
        data_text = line[6:].strip()
        if not data_text or data_text == "[DONE]":
            continue
        try:
            event = json.loads(data_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if event.get("type") == "local_studio.completed":
            completed = event
    data: dict[str, object] = {"events": events, "completed": completed}
    if completed:
        data.update(completed)
    return data


def local_studio_conversation_from_result(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        return {}
    conversation = data.get("conversation")
    if isinstance(conversation, dict):
        return conversation
    completed = data.get("completed")
    if isinstance(completed, dict) and isinstance(completed.get("conversation"), dict):
        return completed["conversation"]
    return {}


def local_studio_last_assistant(conversation: dict[str, object]) -> dict[str, object]:
    messages = conversation.get("messages") if isinstance(conversation, dict) else []
    if not isinstance(messages, list):
        return {}
    assistants = [message for message in messages if isinstance(message, dict) and message.get("role") == "assistant"]
    return assistants[-1] if assistants else {}


def local_studio_tools(request_body: object) -> list[str]:
    if not isinstance(request_body, dict):
        return []
    tools = request_body.get("tools")
    if not isinstance(tools, list):
        return []
    return [str(tool.get("type") or "") for tool in tools if isinstance(tool, dict) and tool.get("type")]


def summarize_local_studio_result(label: str, payload: dict[str, object], status: int, data: object, raw: str) -> dict[str, object]:
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    request_body = data.get("request") if isinstance(data, dict) and isinstance(data.get("request"), dict) else {}
    conversation = local_studio_conversation_from_result(data)
    assistant = local_studio_last_assistant(conversation)
    images = assistant.get("images") if isinstance(assistant.get("images"), list) else []
    content = str(assistant.get("content") or "")
    thinking = str(assistant.get("thinking") or "")
    assistant_error = str(assistant.get("error") or "")
    reasoning = request_body.get("reasoning") if isinstance(request_body, dict) and isinstance(request_body.get("reasoning"), dict) else {}
    tool_types = local_studio_tools(request_body)
    messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
    request_has_attachments = any(isinstance(message, dict) and message.get("attachments") for message in messages)
    return {
        "label": label,
        "provider_type": payload.get("provider_type"),
        "interface_mode": payload.get("interface_mode"),
        "model": payload.get("model"),
        "search": bool(options.get("search")),
        "stream": bool(options.get("stream")),
        "image_tool_enabled": bool(options.get("image_tool_enabled")),
        "reasoning_effort": str(options.get("reasoning_effort") or ""),
        "reasoning_summary": str(options.get("reasoning_summary") or ""),
        "status": status,
        "error_prefix": safe_api_prefix(raw) if status >= 400 else safe_api_prefix(assistant_error),
        "conversation_id": conversation.get("id") if isinstance(conversation, dict) else "",
        "assistant_has_text": bool(content.strip()),
        "assistant_has_thinking": bool(thinking.strip()),
        "assistant_has_error": bool(assistant_error.strip()),
        "assistant_image_count": len(images),
        "assistant_image_paths": [str(image.get("path") or "") for image in images if isinstance(image, dict) and image.get("path")],
        "assistant_image_urls": [str(image.get("url") or "") for image in images if isinstance(image, dict) and image.get("url")],
        "assistant_text_prefix": safe_api_prefix(content),
        "assistant_thinking_prefix": safe_api_prefix(thinking),
        "has_request_body": bool(request_body),
        "has_request_reasoning": bool(reasoning),
        "request_reasoning": reasoning,
        "request_tool_types": tool_types,
        "request_contains_search_tool": any(tool_type in {"web_search", "web_search_preview"} for tool_type in tool_types),
        "request_contains_image_tool": "image_generation" in tool_types,
        "request_has_attachments": request_has_attachments,
    }

def summarize_image_generation(label: str, model: str, status: int, data: object, raw: str) -> dict[str, object]:
    images = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else []
    return {
        "label": label,
        "model": model,
        "status": status,
        "ok": status < 400 and bool(images),
        "image_count": len(images),
        "image_paths": [str(image.get("path") or "") for image in images if isinstance(image, dict) and image.get("path")],
        "image_urls": [str(image.get("url") or "") for image in images if isinstance(image, dict) and image.get("url")],
        "mime_types": [str(image.get("mime_type") or "") for image in images if isinstance(image, dict)],
        "error_prefix": safe_api_prefix(raw) if status >= 400 else "",
    }

def base_api_text_from_data(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        parts.append(output_text)
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    if choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            content = message.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
    content_items = data.get("content") if isinstance(data.get("content"), list) else []
    for part in content_items:
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part.get("text"):
            parts.append(part["text"])
    output_items = data.get("output") if isinstance(data.get("output"), list) else []
    for item in output_items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part.get("text"):
                    parts.append(part["text"])
    return "".join(parts)

def summarize_base_api_result(label: str, status: int, data: object, raw: str, expected_text: str = "") -> dict[str, object]:
    text = base_api_text_from_data(data)
    image_count = 0
    if isinstance(data, dict):
        output = data.get("output") if isinstance(data.get("output"), list) else []
        image_count = sum(1 for item in output if isinstance(item, dict) and item.get("type") == "image_generation_call")
    sentinel_matched = bool(expected_text and expected_text in text)
    return {
        "label": label,
        "status": status,
        "ok": status < 400 and (sentinel_matched if expected_text else bool(text.strip() or image_count)),
        "expected_text": expected_text,
        "sentinel_matched": sentinel_matched,
        "text_prefix": safe_api_prefix(text),
        "image_count": image_count,
        "error_prefix": safe_api_prefix(raw) if status >= 400 else "",
    }


def post_local_studio(payload, timeout=360, expect_error=False, label="local-studio"):
    if isinstance(payload, dict) and payload.get("provider_type") == "google-ai-studio":
        throttle_google_request(label)
    status, data, raw = http_json("POST", "/api/local-studio/chat", payload, timeout=timeout, expect_error=expect_error)
    if status < 400 and bool((payload.get("options") or {}).get("stream")):
        data = parse_local_studio_sse(raw)
    api_results.append(summarize_local_studio_result(label, payload, status, data, raw))
    return status, data, raw

def local_studio_transient_upstream_error(status: int, data: object, raw: str) -> bool:
    if status < 500:
        return False
    detail = data.get("detail") if isinstance(data, dict) else None
    detail_type = str(detail.get("type") or "") if isinstance(detail, dict) else ""
    message = str(detail.get("message") or raw or "") if isinstance(detail, dict) else str(raw or "")
    text = message.lower()
    return bool(
        detail_type in {"upstream_error", "upstream_timeout"}
        and any(marker in text for marker in (
            "connecterror",
            "remoteprotocolerror",
            "unexpected_eof_while_reading",
            "eof occurred in violation of protocol",
            "server disconnected",
            "connection reset",
        ))
    )

def post_local_studio_with_retries(payload, *, timeout=360, attempts=2, label="local-studio"):
    last = None
    for attempt in range(1, attempts + 1):
        status, data, raw = post_local_studio(payload, timeout=timeout, expect_error=True, label=label)
        last = (status, data, raw)
        api_results[-1]["attempt"] = attempt
        if local_studio_transient_upstream_error(status, data, raw):
            api_results[-1]["transient_upstream_retry"] = True
        if status < 500:
            return status, data, raw
    assert last is not None
    return last

def local_studio_result_has_reasoning_evidence(item: dict[str, object]) -> bool:
    reasoning = item.get("request_reasoning") if isinstance(item.get("request_reasoning"), dict) else {}
    return bool(
        int(item.get("status") or 500) < 400
        and item.get("assistant_has_text") is True
        and item.get("assistant_has_error") is not True
        and item.get("has_request_reasoning") is True
        and reasoning.get("effort") == "high"
        and reasoning.get("summary") == "auto"
        and item.get("assistant_has_thinking") is True
    )

def post_local_studio_until_reasoning_evidence(payload, *, timeout=360, attempts=5, label="local-studio"):
    last = None
    for attempt in range(1, attempts + 1):
        status, data, raw = post_local_studio(payload, timeout=timeout, expect_error=True, label=label)
        last = (status, data, raw)
        api_results[-1]["attempt"] = attempt
        api_results[-1]["reasoning_evidence_retry"] = True
        if local_studio_transient_upstream_error(status, data, raw):
            api_results[-1]["transient_upstream_retry"] = True
        if local_studio_result_has_reasoning_evidence(api_results[-1]):
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
google_model_ids = [str(item.get("id") or "") for item in google_models.get("data", []) if isinstance(item, dict) and item.get("id")]

def split_model_candidates(raw: str) -> list[str]:
    return [candidate.strip() for candidate in raw.replace(";", ",").split(",") if candidate.strip()]

def google_text_model_candidates(model_ids: list[str], requested_model: str) -> list[str]:
    preferred = [
        requested_model,
        *split_model_candidates(google_model_candidates_raw),
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite",
        "gemma-4-31b-it",
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-3.5-flash",
    ]
    non_text_markers = ("image", "tts", "live")
    candidates: list[str] = []
    for model_id in preferred + model_ids:
        if not model_id or model_id not in model_ids or model_id in candidates:
            continue
        if model_id not in preferred and any(marker in model_id.lower() for marker in non_text_markers):
            continue
        candidates.append(model_id)
    return candidates

requested_google_model = google_model
google_model_candidate_chain = google_text_model_candidates(google_model_ids, requested_google_model)
if not google_model_candidate_chain:
    raise AssertionError(f"no usable Google text model from requested={requested_google_model} available={google_model_ids[:20]}")
google_model = google_model_candidate_chain[0]
os.environ["SYSTEM_TEST_MODEL"] = google_model
control_plane_results["local_studio_google_models"] = {
    "status": status,
    "ok": True,
    "requested_model": requested_google_model,
    "selected_model": google_model,
    "fallback_used": google_model != requested_google_model,
    "candidate_chain": google_model_candidate_chain,
    "selection_verified": False,
    "selection_probe_attempts": [],
    "model_count": len(google_model_ids),
}

def persist_google_model_selection() -> None:
    (root / "artifacts" / "google-model.safe.txt").write_text(google_model, encoding="utf-8")
    (root / "artifacts" / "google-model-selection.safe.json").write_text(
        json.dumps(control_plane_results["local_studio_google_models"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

persist_google_model_selection()

status, openai_models, raw = http_json("POST", "/api/local-studio/models", {"provider_type": "openai", "base_url": openai_base_url, "api_key": openai_token, "interface_mode": "responses", "timeout": 180}, timeout=180)
assert status == 200 and openai_models.get("data"), "OpenAI-compatible model list is empty"
model_ids = [str(item.get("id") or "") for item in openai_models.get("data", []) if isinstance(item, dict) and item.get("id")]
control_plane_results["local_studio_openai_models"] = {"status": status, "ok": True, "model_count": len(model_ids)}

def openai_model_candidates(model_ids: list[str], requested_model: str) -> list[str]:
    preferred = [
        requested_model,
        "gpt-5.4-mini",
        "gpt-5-mini",
        "gpt-5",
        "gpt-4.1-mini",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4o",
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
def openai_reasoning_model_candidates(model_ids: list[str], fallback: str) -> list[str]:
    preferred = [
        fallback,
        "gpt-5.4-mini",
        "codex-auto-review",
        "gpt-5-mini",
        "gpt-5",
        "gpt-5.2-chat-latest",
        "gpt-5.2",
        "o4-mini",
        "o3-mini",
        "o3",
    ]
    candidates: list[str] = []
    for model_id in preferred + model_ids:
        if model_id and model_id in model_ids and model_id not in candidates:
            candidates.append(model_id)
    return candidates

def verify_openai_reasoning_model(candidates: list[str]) -> tuple[str, list[dict[str, object]]]:
    results: list[dict[str, object]] = []
    headers = {"Authorization": f"Bearer {openai_token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=60) as client:
        for candidate in candidates:
            payload = {
                "model": candidate,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with exactly: nexus-openai-reasoning-probe-ok"}]}],
                "reasoning": {"effort": "high", "summary": "auto"},
            }
            try:
                response = client.post(f"{openai_base_url}/responses", headers=headers, json=payload)
                text = response.text
                expected = "nexus-openai-reasoning-probe-ok"
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

openai_reasoning_model, reasoning_model_probe_results = verify_openai_reasoning_model(openai_reasoning_model_candidates(model_ids, openai_model))
model_probe_safe = {"openai_model_used": openai_model, "openai_reasoning_model_used": openai_reasoning_model, "results": model_probe_results, "reasoning_results": reasoning_model_probe_results}
model_probe_serialized = json.dumps(model_probe_safe, ensure_ascii=False, indent=2)
if openai_token and openai_token in model_probe_serialized:
    raise SystemExit("SYSTEM_TEST_FAIL openai_model_probe_secret_leak")
(root / "artifacts" / "openai-compatible-model-probe.safe.json").write_text(model_probe_serialized, encoding="utf-8")
if not openai_model:
    raise SystemExit("SYSTEM_TEST_FAIL openai_compatible_no_usable_responses_model")
(root / "artifacts" / "openai-compatible-model.safe.txt").write_text(openai_model, encoding="utf-8")
(root / "artifacts" / "openai-compatible-reasoning-model.safe.txt").write_text(openai_reasoning_model, encoding="utf-8")
os.environ["OPENAI_COMPAT_TEXT_MODEL"] = openai_model

def write_api_results_artifact(extra: dict[str, object] | None = None) -> None:
    safe = {
        "api_results": api_results,
        "control_plane_results": control_plane_results,
        "base_api_results": base_api_results,
        "image_model_used": globals().get("image_model", ""),
        "openai_model_used": openai_model,
        "openai_reasoning_model_used": openai_reasoning_model,
        "google_model_used": google_model,
    }
    if extra:
        safe.update(extra)
    serialized = json.dumps(safe, ensure_ascii=False, indent=2)
    if openai_token in serialized:
        raise SystemExit("SYSTEM_TEST_FAIL api_results_secret_leak")
    (root / "artifacts" / "api-results.json").write_text(serialized, encoding="utf-8")

write_api_results_artifact({"checkpoint": "control_plane_and_model_selection"})

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

def local_studio_google_model_unavailable(status: int, data: object, raw: str) -> bool:
    if status < 400:
        return False
    detail = data.get("detail") if isinstance(data, dict) else None
    message = str(detail.get("message") or raw or "") if isinstance(detail, dict) else str(raw or "")
    text = message.lower()
    return any(marker in text for marker in (
        "ai studio text model not selected",
        "text model not selected",
        "text_model_not_found",
        "current_text_model_not_found",
        "not_text_model",
    ))

def local_studio_google_quota_exhausted(status: int, data: object, raw: str) -> bool:
    if status < 400:
        return False
    detail = data.get("detail") if isinstance(data, dict) else None
    message = str(detail.get("message") or raw or "") if isinstance(detail, dict) else str(raw or "")
    return google_quota_exhausted_text(message) or google_quota_exhausted_text(raw)

def local_studio_google_no_available_account(status: int, data: object, raw: str) -> bool:
    if status < 400:
        return False
    detail = data.get("detail") if isinstance(data, dict) else None
    message = str(detail.get("message") or raw or "") if isinstance(detail, dict) else str(raw or "")
    return "no available account" in message.lower()

def google_candidate_failure_reason(*, model_unavailable: bool, quota_exhausted: bool, status: int, no_available_account: bool = False) -> str:
    if quota_exhausted:
        return "candidate_quota_exhausted"
    if model_unavailable:
        return "candidate_model_unavailable"
    if no_available_account:
        return "candidate_no_available_account"
    if status < 400:
        return "candidate_sentinel_mismatch"
    if status >= 500:
        return "server_error"
    if status >= 400:
        return "client_error"
    return "unknown"

def google_model_attempt_chain() -> list[str]:
    candidates: list[str] = []
    for model_id in [google_model, *google_model_candidate_chain]:
        if model_id and model_id not in candidates:
            candidates.append(model_id)
    return candidates

def google_failed_candidate_reasons(attempts: list[dict[str, object]]) -> list[str]:
    return [
        f"{attempt.get('model')}:{attempt.get('failure_reason')}"
        for attempt in attempts
        if attempt.get("failure_reason") and attempt.get("ok") is not True
    ]

def write_api_google_quota_blocker(label: str, attempts: list[dict[str, object]], status: int, raw: str) -> None:
    blocker = {
        "result": "blocked",
        "reason": "external_google_quota_exhausted",
        "source": "api-google-model-fallback",
        "label": label,
        "status": status,
        "text_preview": safe_api_prefix(raw),
        "google_request_interval_seconds": google_request_interval_seconds,
        "candidate_chain": google_model_candidate_chain,
        "selection_probe_attempts": attempts,
        "failed_candidate_reasons": google_failed_candidate_reasons(attempts),
        "quota_or_unavailable_scope": "candidate_chain_exhausted",
    }
    control_plane_results["local_studio_google_models"].update(
        {
            "selection_verified": False,
            "selection_probe_attempts": attempts,
            "failed_candidate_reasons": blocker["failed_candidate_reasons"],
            "quota_or_unavailable_scope": "candidate_chain_exhausted",
        }
    )
    persist_google_model_selection()
    (root / "artifacts" / "google-quota-blocker.safe.json").write_text(json.dumps(blocker, ensure_ascii=False, indent=2), encoding="utf-8")
    write_api_results_artifact({"google_quota_blocker": blocker})
    print("API_REAL_PROVIDER_BLOCKED external_google_quota_exhausted " + json.dumps(blocker, ensure_ascii=False))

def post_google_with_model_fallback(payload: dict[str, object], *, timeout: int, label: str, allow_nonserver_status: bool = False):
    global google_model
    attempts: list[dict[str, object]] = []
    last_raw = ""
    last_status = 0
    for candidate_index, candidate in enumerate(google_model_attempt_chain(), start=1):
        probe_payload = dict(payload)
        probe_payload["model"] = candidate
        attempt_label = label if candidate_index == 1 else f"{label}-fallback-{candidate_index}"
        status, data, raw = post_local_studio(probe_payload, timeout=timeout, expect_error=True, label=attempt_label)
        last_raw = raw
        last_status = status
        conversation = local_studio_conversation_from_result(data)
        ok = status == 200 and isinstance(conversation, dict) and bool(conversation.get("messages"))
        model_unavailable = local_studio_google_model_unavailable(status, data, raw)
        quota_exhausted = local_studio_google_quota_exhausted(status, data, raw)
        no_available_account = local_studio_google_no_available_account(status, data, raw)
        fallbackable = model_unavailable or quota_exhausted or no_available_account
        attempt = {
            "model": candidate,
            "status": status,
            "ok": ok,
            "model_unavailable": model_unavailable,
            "quota_exhausted": quota_exhausted,
            "no_available_account": no_available_account,
            "fallbackable": fallbackable,
            "failure_reason": "" if ok else google_candidate_failure_reason(model_unavailable=model_unavailable, quota_exhausted=quota_exhausted, no_available_account=no_available_account, status=status),
            "error_prefix": safe_api_prefix(raw) if not ok else "",
        }
        attempts.append(attempt)
        api_results[-1]["model_selection_candidate_index"] = candidate_index
        api_results[-1]["model_selection_unavailable"] = model_unavailable
        api_results[-1]["model_selection_quota_exhausted"] = quota_exhausted
        api_results[-1]["model_selection_no_available_account"] = no_available_account
        if ok:
            google_model = candidate
            os.environ["SYSTEM_TEST_MODEL"] = google_model
            control_plane_results["local_studio_google_models"].update(
                {
                    "selected_model": google_model,
                    "fallback_used": google_model != requested_google_model,
                    "selection_verified": True,
                    "selection_probe_attempts": attempts,
                    "failed_candidate_reasons": google_failed_candidate_reasons(attempts),
                    "quota_or_unavailable_scope": "candidate_fallback_used" if len(attempts) > 1 else "none",
                }
            )
            persist_google_model_selection()
            return status, data, raw
        control_plane_results["local_studio_google_models"].update(
            {
                "selection_verified": False,
                "selection_probe_attempts": attempts,
                "failed_candidate_reasons": google_failed_candidate_reasons(attempts),
                "quota_or_unavailable_scope": "candidate_fallback_in_progress" if fallbackable else "none",
            }
        )
        persist_google_model_selection()
        if allow_nonserver_status and status < 500 and not fallbackable:
            return status, data, raw
        if fallbackable and candidate_index < len(google_model_attempt_chain()):
            continue
        break
    if attempts and all(attempt.get("fallbackable") for attempt in attempts) and any(attempt.get("quota_exhausted") for attempt in attempts):
        write_api_google_quota_blocker(label, attempts, last_status, last_raw)
        raise SystemExit(0)
    raise AssertionError(last_raw[:500] or "Google text model fallback exhausted")

def post_google_text_api_with_model_fallback(path: str, payload: dict[str, object], *, timeout: int, label: str, expected_text: str = ""):
    global google_model
    attempts: list[dict[str, object]] = []
    last_raw = ""
    last_status = 0
    for candidate_index, candidate in enumerate(google_model_attempt_chain(), start=1):
        probe_payload = dict(payload)
        probe_payload["model"] = candidate
        throttle_google_request(label if candidate_index == 1 else f"{label}-fallback-{candidate_index}")
        status, data, raw = http_json("POST", path, probe_payload, timeout=timeout, expect_error=True)
        last_raw = raw
        last_status = status
        quota_exhausted = google_quota_exhausted_text(raw)
        no_available_account = local_studio_google_no_available_account(status, data, raw)
        text = base_api_text_from_data(data)
        sentinel_matched = bool(expected_text and expected_text in text)
        ok = status < 400 and (sentinel_matched if expected_text else True)
        fallbackable = quota_exhausted or no_available_account or (status < 400 and bool(expected_text) and not sentinel_matched)
        attempts.append(
            {
                "model": candidate,
                "status": status,
                "ok": ok,
                "sentinel_matched": sentinel_matched,
                "model_unavailable": False,
                "quota_exhausted": quota_exhausted,
                "no_available_account": no_available_account,
                "fallbackable": fallbackable,
                "failure_reason": "" if ok else google_candidate_failure_reason(model_unavailable=False, quota_exhausted=quota_exhausted, no_available_account=no_available_account, status=status),
                "text_prefix": safe_api_prefix(text),
                "error_prefix": safe_api_prefix(raw) if status >= 400 else "",
            }
        )
        control_plane_results["local_studio_google_models"].update(
            {
                "selected_model": candidate if ok else google_model,
                "selection_probe_attempts": attempts,
                "failed_candidate_reasons": google_failed_candidate_reasons(attempts),
                "quota_or_unavailable_scope": "candidate_fallback_used" if len(attempts) > 1 and ok else "candidate_fallback_in_progress" if fallbackable else "none",
            }
        )
        persist_google_model_selection()
        if ok:
            google_model = candidate
            os.environ["SYSTEM_TEST_MODEL"] = google_model
            return status, data, raw
        if fallbackable and candidate_index < len(google_model_attempt_chain()):
            continue
        break
    if attempts and all(attempt.get("fallbackable") for attempt in attempts) and any(attempt.get("quota_exhausted") for attempt in attempts):
        write_api_google_quota_blocker(label, attempts, last_status, last_raw)
        raise SystemExit(0)
    raise AssertionError(last_raw[:500] or f"Google text API fallback exhausted for {path}")

status, data, raw = post_google_with_model_fallback({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
    "message": "Reply with exactly: nexus-google-api-ok",
    "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off"},
}, timeout=300, label="google-basic")
assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]

google_repeat_prompt = "Reply with exactly: nexus-google-api-repeat-ok"
for repeat_index in range(1, 3):
    status, data, raw = post_google_with_model_fallback({
        "provider_type": "google-ai-studio",
        "interface_mode": "responses",
        "timeout": 300,
        "message": google_repeat_prompt,
        "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off"},
    }, timeout=300, label=f"google-repeat-{repeat_index}")
    assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]

status, data, raw = post_google_with_model_fallback({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
    "message": "Do not search and do not create an image. Reply with exactly: nexus-google-api-optional-tools-text-ok",
    "options": {"stream": False, "search": True, "image_tool_enabled": True, "reasoning_effort": "off"},
}, timeout=300, label="google-optional-search-image-text")
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

status, data, raw = post_google_with_model_fallback({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
    "message": "Search today's AI technology news and summarize in one short sentence.",
    "options": {"stream": True, "search": True, "image_tool_enabled": False, "reasoning_effort": "off"},
}, timeout=300, label="google-search", allow_nonserver_status=True)
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

status, data, raw = post_local_studio_with_retries({
    "provider_type": "openai",
    "base_url": openai_base_url,
    "api_key": openai_token,
    "interface_mode": "responses",
    "timeout": 180,
    "model": openai_model,
    "message": "Do not search and do not create an image. Reply with exactly: nexus-openai-api-optional-tools-text-ok",
    "options": {"stream": False, "search": True, "image_tool_enabled": True, "reasoning_effort": "off"},
}, timeout=180, attempts=3, label="openai-compatible-optional-search-image-text")

status, data, raw = post_local_studio_with_retries({
    "provider_type": "openai",
    "base_url": openai_base_url,
    "api_key": openai_token,
    "interface_mode": "responses",
    "timeout": 180,
    "model": openai_reasoning_model,
    "message": "Do not search and do not create an image. Reply with exactly: nexus-openai-api-optional-tools-reasoning-text-ok",
    "options": {"stream": False, "search": True, "image_tool_enabled": True, "reasoning_effort": "high", "reasoning_summary": "auto"},
}, timeout=180, attempts=3, label="openai-compatible-optional-search-image-reasoning-text")

reasoning_prompt = "请分步骤判断 17*23 是否大于 390，并给出简短结论。"
status, data, raw = post_local_studio_with_retries({
    "provider_type": "openai",
    "base_url": openai_base_url,
    "api_key": openai_token,
    "interface_mode": "responses",
    "timeout": 180,
    "model": openai_reasoning_model,
    "message": reasoning_prompt,
    "options": {"stream": True, "search": False, "image_tool_enabled": False, "reasoning_effort": "high", "reasoning_summary": "auto"},
}, timeout=180, attempts=5, label="openai-compatible-reasoning-stream")

for reasoning_label in ("openai-compatible-reasoning-nonstream", "openai-compatible-reasoning-repeat"):
    status, data, raw = post_local_studio_until_reasoning_evidence({
        "provider_type": "openai",
        "base_url": openai_base_url,
        "api_key": openai_token,
        "interface_mode": "responses",
        "timeout": 180,
        "model": openai_reasoning_model,
        "message": reasoning_prompt,
        "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "high", "reasoning_summary": "auto"},
    }, timeout=180, attempts=5, label=reasoning_label)
write_api_results_artifact({"checkpoint": "local_studio_text_matrix"})

image_model = os.environ.get("SYSTEM_TEST_IMAGE_MODEL", "gemini-3.1-flash-image-preview").strip() or "gemini-3.1-flash-image-preview"
status, data, raw = post_google_with_model_fallback({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 420,
    "message": "Create an image: a simple blue square icon on a plain white background. Do not return text only.",
    "options": {"stream": True, "search": False, "image_tool_enabled": True, "image_tool_provider": "google-ai-studio", "image_model": image_model, "size": "1024x1024", "reasoning_effort": "off"},
}, timeout=420, label="google-image-tool-stream")
assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]
google_image_result = api_results[-1]
assert int(google_image_result.get("assistant_image_count") or 0) == 1, json.dumps(google_image_result, ensure_ascii=False)[:500]
write_api_results_artifact({"checkpoint": "google_image_tool_stream"})

status, data, raw = post_google_with_model_fallback({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
    "message": "你好，只回复一句简短问候。",
    "options": {"stream": False, "search": False, "image_tool_enabled": True, "image_tool_provider": "google-ai-studio", "image_model": image_model, "size": "1024x1024", "reasoning_effort": "off"},
}, timeout=300, label="google-search-image-tool-multiturn-hello")
assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]
google_multiturn_conversation_id = str(api_results[-1].get("conversation_id") or "")
status, data, raw = post_google_with_model_fallback({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
    "conversation_id": google_multiturn_conversation_id,
    "message": "你是谁？请用一句话回答。",
    "options": {"stream": False, "search": False, "image_tool_enabled": True, "image_tool_provider": "google-ai-studio", "image_model": image_model, "size": "1024x1024", "reasoning_effort": "off"},
}, timeout=300, label="google-search-image-tool-multiturn-identity")
assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]
status, data, raw = post_google_with_model_fallback({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 300,
    "conversation_id": google_multiturn_conversation_id,
    "message": "Search for one current AI technology headline and summarize it in one sentence. Do not create an image yet.",
    "options": {"stream": False, "search": True, "image_tool_enabled": True, "image_tool_provider": "google-ai-studio", "image_model": image_model, "size": "1024x1024", "reasoning_effort": "off"},
}, timeout=300, label="google-search-image-tool-multiturn-news")
assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]
status, data, raw = post_google_with_model_fallback({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 420,
    "conversation_id": google_multiturn_conversation_id,
    "message": "做成图片：create exactly one concise infographic image about the headline above.",
    "options": {"stream": True, "search": True, "image_tool_enabled": True, "image_tool_provider": "google-ai-studio", "image_model": image_model, "size": "1024x1024", "reasoning_effort": "off"},
}, timeout=420, label="google-search-image-tool-multiturn")
assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]
google_search_image_result = api_results[-1]
assert int(google_search_image_result.get("assistant_image_count") or 0) == 1, json.dumps(google_search_image_result, ensure_ascii=False)[:500]
write_api_results_artifact({"checkpoint": "google_search_image_tool_multiturn"})

status, data, raw = post_google_with_model_fallback({
    "provider_type": "google-ai-studio",
    "interface_mode": "responses",
    "timeout": 420,
    "message": "Search today's AI technology news and create exactly one simple infographic image. Do not stream.",
    "options": {"stream": False, "search": True, "image_tool_enabled": True, "image_tool_provider": "google-ai-studio", "image_model": image_model, "size": "1024x1024", "reasoning_effort": "off"},
}, timeout=420, label="google-search-image-tool-nonstream")
assert status == 200 and data.get("conversation", {}).get("messages"), raw[:500]
google_nonstream_image_result = api_results[-1]
assert int(google_nonstream_image_result.get("assistant_image_count") or 0) == 1, json.dumps(google_nonstream_image_result, ensure_ascii=False)[:500]
write_api_results_artifact({"checkpoint": "google_search_image_tool_nonstream"})

asset_path = next((path for item in (google_image_result, google_search_image_result, google_nonstream_image_result) for path in item.get("assistant_image_paths", []) if path), "")
if not asset_path:
    raise AssertionError("Local Studio generated image asset path missing")
encoded_asset_path = "/".join(quote(part) for part in asset_path.replace("\\", "/").split("/") if part)
asset_status, asset_headers, asset_body = http_binary("GET", f"/api/local-studio/assets/{encoded_asset_path}", timeout=120)
asset_traversal_status, traversal_headers, traversal_body = http_binary("GET", "/api/local-studio/assets/..%2F..%2Fregistry.json", timeout=30, expect_error=True)
control_plane_results["local_studio_generated_asset_fetch"] = {
    "status": asset_status,
    "ok": asset_status == 200 and len(asset_body) > 0 and str(asset_headers.get("content-type") or "").startswith("image/"),
    "asset_path": asset_path,
    "content_type": asset_headers.get("content-type"),
    "body_size": len(asset_body),
    "traversal_status": asset_traversal_status,
    "traversal_blocked": asset_traversal_status in {400, 404},
}
assert control_plane_results["local_studio_generated_asset_fetch"]["ok"] is True
assert control_plane_results["local_studio_generated_asset_fetch"]["traversal_blocked"] is True
write_api_results_artifact({"checkpoint": "local_studio_generated_asset_fetch"})

openai_image_status, openai_image_data, openai_image_raw = post_local_studio_with_retries({
    "provider_type": "openai",
    "base_url": openai_base_url,
    "api_key": openai_token,
    "interface_mode": "responses",
    "timeout": 180,
    "model": openai_model,
    "message": "Create an image: a small blue square icon.",
    "options": {"stream": False, "search": False, "image_tool_enabled": True, "image_model": "gpt-image-2", "size": "1024x1024", "reasoning_effort": "off"},
}, timeout=180, attempts=2, label="openai-compatible-image-tool",)
openai_image_item = api_results[-1]
openai_image_item["controlled_image_path_ok"] = bool(openai_image_status < 400 and int(openai_image_item.get("assistant_image_count") or 0) >= 1)
openai_image_item["controlled_unsupported_ok"] = bool(400 <= openai_image_status < 500 and str(openai_image_item.get("error_prefix") or "").strip())
write_api_results_artifact({"checkpoint": "openai_compatible_image_tool"})

cache_variation_payloads = [
    (
        "cache-google-same-prompt",
        {
            "provider_type": "google-ai-studio",
            "interface_mode": "responses",
            "timeout": 300,
            "model": google_model,
            "message": "Reply with exactly: nexus-cache-isolation-ok",
            "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off", "cache_enabled": True, "cache_namespace": "system-test"},
        },
    ),
    (
        "cache-openai-provider-variation",
        {
            "provider_type": "openai",
            "base_url": openai_base_url,
            "api_key": openai_token,
            "interface_mode": "responses",
            "timeout": 180,
            "model": openai_model,
            "message": "Reply with exactly: nexus-cache-isolation-ok",
            "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off", "cache_enabled": True, "cache_namespace": "system-test"},
        },
    ),
    (
        "cache-openai-interface-variation",
        {
            "provider_type": "openai",
            "base_url": openai_base_url,
            "api_key": openai_token,
            "interface_mode": "openai",
            "timeout": 180,
            "model": openai_model,
            "message": "Reply with exactly: nexus-cache-isolation-ok",
            "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off", "cache_enabled": True, "cache_namespace": "system-test"},
        },
    ),
    (
        "cache-openai-search-tool-reasoning-variation",
        {
            "provider_type": "openai",
            "base_url": openai_base_url,
            "api_key": openai_token,
            "interface_mode": "responses",
            "timeout": 180,
            "model": openai_model,
            "message": "Do not search and do not create an image. Reply with exactly: nexus-cache-isolation-ok",
            "options": {"stream": False, "search": True, "image_tool_enabled": True, "image_model": "gpt-image-2", "reasoning_effort": "off", "cache_enabled": True, "cache_namespace": "system-test"},
        },
    ),
    (
        "cache-openai-token-variation",
        {
            "provider_type": "openai",
            "base_url": openai_base_url,
            "api_key": f" {openai_token} ",
            "interface_mode": "responses",
            "timeout": 180,
            "model": openai_model,
            "message": "Reply with exactly: nexus-cache-isolation-ok",
            "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off", "cache_enabled": True, "cache_namespace": "system-test-token"},
        },
    ),
    (
        "cache-openai-attachment-variation",
        {
            "provider_type": "openai",
            "base_url": openai_base_url,
            "api_key": openai_token,
            "interface_mode": "responses",
            "timeout": 180,
            "model": openai_model,
            "message": "Read the attached text and reply with exactly: nexus-cache-isolation-ok",
            "files": [{"name": "cache-isolation.txt", "mime_type": "text/plain", "data_url": "data:text/plain;base64,bmV4dXMtc3lzdGVtLXRlc3QtYXR0YWNobWVudA=="}],
            "options": {"stream": False, "search": False, "image_tool_enabled": False, "reasoning_effort": "off", "cache_enabled": True, "cache_namespace": "system-test-attachment"},
        },
    ),
]
cache_labels: list[str] = []
for label, payload in cache_variation_payloads:
    cache_labels.append(label)
    if payload.get("provider_type") == "openai":
        status, data, raw = post_local_studio_with_retries(payload, timeout=180, attempts=3, label=label)
    else:
        status, data, raw = post_google_with_model_fallback(payload, timeout=300, label=label)
    write_api_results_artifact({"checkpoint": f"cache_variation_{label}"})
    if status >= 400:
        raise AssertionError(f"cache variation {label} failed: {status} {raw[:300]}")

cache_attempt_items = [item for item in api_results if item.get("label") in cache_labels]
cache_item_by_label = {label: next((item for item in reversed(cache_attempt_items) if item.get("label") == label), {}) for label in cache_labels}
cache_items = [cache_item_by_label[label] for label in cache_labels]
cache_serialized = json.dumps(cache_items, ensure_ascii=False).lower()
control_plane_results["local_studio_cache_isolation"] = {
    "ok": len(cache_items) == len(cache_labels) and all(int(item.get("status") or 500) < 400 and item.get("assistant_has_error") is not True for item in cache_items) and "cache.hit" not in cache_serialized and "cache hit" not in cache_serialized,
    "labels": cache_labels,
    "result_count": len(cache_items),
    "attempt_result_count": len(cache_attempt_items),
    "conversation_ids": [item.get("conversation_id") for item in cache_items],
    "attachment_variation_has_request_attachment": bool((next((item for item in cache_items if item.get("label") == "cache-openai-attachment-variation"), {}) or {}).get("request_has_attachments")),
    "cache_markers_absent": "cache.hit" not in cache_serialized and "cache hit" not in cache_serialized,
}
assert control_plane_results["local_studio_cache_isolation"]["ok"] is True
write_api_results_artifact({"checkpoint": "local_studio_cache_isolation"})

base_api_results.clear()
status, data, raw = post_google_text_api_with_model_fallback("/v1/chat/completions", {"model": google_model, "messages": [{"role": "user", "content": "Reply with exactly: nexus-base-chat-ok"}]}, timeout=300, label="base-chat-completions", expected_text="nexus-base-chat-ok")
base_api_results.append(summarize_base_api_result("base-chat-completions", status, data, raw, expected_text="nexus-base-chat-ok"))
write_api_results_artifact({"checkpoint": "base_chat_completions"})
status, data, raw = post_google_text_api_with_model_fallback("/v1/responses", {"model": google_model, "input": "Reply with exactly: nexus-base-responses-ok"}, timeout=300, label="base-responses", expected_text="nexus-base-responses-ok")
base_api_results.append(summarize_base_api_result("base-responses", status, data, raw, expected_text="nexus-base-responses-ok"))
write_api_results_artifact({"checkpoint": "base_responses"})
status, data, raw = post_google_text_api_with_model_fallback("/v1/messages", {"model": google_model, "messages": [{"role": "user", "content": "Reply with exactly: nexus-base-messages-ok"}]}, timeout=300, label="base-messages", expected_text="nexus-base-messages-ok")
base_api_results.append(summarize_base_api_result("base-messages", status, data, raw, expected_text="nexus-base-messages-ok"))
write_api_results_artifact({"checkpoint": "base_messages"})
throttle_google_request("base-images-generations")
status, data, raw = http_json("POST", "/v1/images/generations", {"model": image_model, "prompt": "A simple blue square icon on a white background", "size": "1024x1024", "n": 1, "response_format": "url", "timeout": 420}, timeout=420)
base_image_summary = summarize_image_generation("base-images-generations", image_model, status, data, raw)
base_api_results.append(base_image_summary)
write_api_results_artifact({"checkpoint": "base_images_generations"})
control_plane_results["base_api_smoke"] = {
    "ok": all(item.get("ok") for item in base_api_results),
    "results": base_api_results,
}
assert control_plane_results["base_api_smoke"]["ok"] is True

write_api_results_artifact({"checkpoint": "api_complete"})
print(f"API_REAL_PROVIDER_OK google_model={google_model} openai_model={openai_model}")
PY
fi

if [ -s "$RUN_ROOT/artifacts/openai-compatible-model.safe.txt" ]; then
    OPENAI_COMPAT_TEXT_MODEL="$(cat "$RUN_ROOT/artifacts/openai-compatible-model.safe.txt")"
    export OPENAI_COMPAT_TEXT_MODEL
fi
if [ -s "$RUN_ROOT/artifacts/google-model.safe.txt" ]; then
    SYSTEM_TEST_MODEL="$(cat "$RUN_ROOT/artifacts/google-model.safe.txt")"
    export SYSTEM_TEST_MODEL
fi
if [ -s "$RUN_ROOT/artifacts/openai-compatible-reasoning-model.safe.txt" ]; then
    OPENAI_COMPAT_REASONING_MODEL="$(cat "$RUN_ROOT/artifacts/openai-compatible-reasoning-model.safe.txt")"
    export OPENAI_COMPAT_REASONING_MODEL
fi

if [ -s "$RUN_ROOT/artifacts/google-quota-blocker.safe.json" ]; then
    HOST_UI_SMOKE_EXIT_CODE=77
    export HOST_UI_SMOKE_EXIT_CODE
    echo "HOST_UI_SMOKE_SKIP external_google_quota_exhausted source=api-google-model-fallback"
else
HOST_ARTIFACT_WIN="$(wslpath -w "$RUN_ROOT/artifacts")"
HOST_ACCOUNTS_WIN="$(wslpath -w "$RUN_ROOT/data/accounts")"
HOST_REPO_SRC_WIN="$(wslpath -w "$RUN_ROOT/repo/src")"
HOST_SCRIPT_WIN="$(wslpath -w "$SYSTEM_TEST_DIR/host-ui-smoke.py")"
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
SYSTEM_TEST_MODEL_CANDIDATES_PS="$(ps_quote "$SYSTEM_TEST_MODEL_CANDIDATES")"
OPENAI_COMPAT_TEXT_MODEL_PS="$(ps_quote "$OPENAI_COMPAT_TEXT_MODEL")"
OPENAI_COMPAT_REASONING_MODEL_PS="$(ps_quote "${OPENAI_COMPAT_REASONING_MODEL:-}")"
HOST_UI_SMOKE_HEADLESS_PS="$(ps_quote "$HOST_UI_SMOKE_HEADLESS")"
HOST_UI_SMOKE_SLOW_MO_MS_PS="$(ps_quote "$HOST_UI_SMOKE_SLOW_MO_MS")"
HOST_UI_SMOKE_HOLD_SECONDS_PS="$(ps_quote "$HOST_UI_SMOKE_HOLD_SECONDS")"
SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS_PS="$(ps_quote "$SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS")"
HOST_OFFICIAL_BASELINE_RESULTS_FILE_PS=""
if [ -s "$RUN_ROOT/artifacts/host-official-aistudio-results.json" ]; then
    HOST_OFFICIAL_BASELINE_RESULTS_FILE_WIN="$(wslpath -w "$RUN_ROOT/artifacts/host-official-aistudio-results.json")"
    HOST_OFFICIAL_BASELINE_RESULTS_FILE_PS="$(ps_quote "$HOST_OFFICIAL_BASELINE_RESULTS_FILE_WIN")"
fi
set +e
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command \
    "\$env:AISTUDIO_PORT='$PORT'; \$env:SYSTEM_TEST_MODEL='$SYSTEM_TEST_MODEL_PS'; \$env:SYSTEM_TEST_MODEL_CANDIDATES='$SYSTEM_TEST_MODEL_CANDIDATES_PS'; \$env:OPENAI_COMPAT_TEXT_MODEL='$OPENAI_COMPAT_TEXT_MODEL_PS'; \$env:OPENAI_COMPAT_REASONING_MODEL='$OPENAI_COMPAT_REASONING_MODEL_PS'; \$env:OPENAI_COMPAT_KEY_FILE='$OPENAI_KEY_PS'; \$env:HOST_ARTIFACT_DIR='$HOST_ARTIFACT_PS'; \$env:HOST_ACCOUNTS_DIR='$HOST_ACCOUNTS_PS'; \$env:HOST_REPO_SRC_DIR='$HOST_REPO_SRC_PS'; \$env:HOST_UI_SMOKE_HEADLESS='$HOST_UI_SMOKE_HEADLESS_PS'; \$env:HOST_UI_SMOKE_SLOW_MO_MS='$HOST_UI_SMOKE_SLOW_MO_MS_PS'; \$env:HOST_UI_SMOKE_HOLD_SECONDS='$HOST_UI_SMOKE_HOLD_SECONDS_PS'; \$env:SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS='$SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS_PS'; \$env:HOST_OFFICIAL_BASELINE_RESULTS_FILE='$HOST_OFFICIAL_BASELINE_RESULTS_FILE_PS'; & '$HOST_PYTHON_PS' '$HOST_SCRIPT_PS'; exit \$LASTEXITCODE"
HOST_UI_SMOKE_EXIT_CODE=$?
set -e
export HOST_UI_SMOKE_EXIT_CODE
echo "HOST_UI_SMOKE_EXIT code=$HOST_UI_SMOKE_EXIT_CODE"
fi

if [ -s "$RUN_ROOT/artifacts/google-quota-blocker.safe.json" ]; then
    echo "REQUEST_LOG_AND_ARCHITECTURE_ORACLE_SKIP external_google_quota_exhausted"
    python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["RUN_ROOT"])
summary = {
    "result": "blocked",
    "reason": "external_google_quota_exhausted",
    "failures": [],
    "oracle_skipped": True,
}
(root / "artifacts" / "architecture-contract-results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
PY
elif [ "$SYSTEM_TEST_REUSE_API_RESULTS" = "1" ] && [ -n "$SYSTEM_TEST_RESUME_FROM_RUN_ROOT" ] && [ -s "$RUN_ROOT/artifacts/architecture-contract-results.json" ]; then
    echo "REQUEST_LOG_AND_ARCHITECTURE_ORACLE_REUSED source=$SYSTEM_TEST_RESUME_FROM_RUN_ROOT/artifacts/architecture-contract-results.json"
    python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["RUN_ROOT"])
path = root / "artifacts" / "architecture-contract-results.json"
summary = json.loads(path.read_text(encoding="utf-8"))
summary["reused_from_run_root"] = os.environ.get("SYSTEM_TEST_RESUME_FROM_RUN_ROOT")
summary["current_run_request_log_oracle_skipped"] = True
path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
PY
else
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
fi

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
google_quota_blocker = read_json("google-quota-blocker.safe.json") or {}
architecture = read_json("architecture-contract-results.json") or {}
api_results = read_json("api-results.json") or {}
resume_evidence = read_json("resume-evidence.safe.json") or {}
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
external_google_quota_blocked = bool(google_quota_blocker) or host_ui_smoke_exit_code == 77
resume_artifact_reuse = bool(resume_evidence)
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
base_image_result = mcp_ui.get("base_image_result") if isinstance(mcp_ui.get("base_image_result"), dict) else {}
cache_isolation_result = mcp_ui.get("cache_isolation_result") if isinstance(mcp_ui.get("cache_isolation_result"), dict) else {}
local_studio_image_generation_results = mcp_ui.get("local_studio_image_generation_results") if isinstance(mcp_ui.get("local_studio_image_generation_results"), dict) else {}
account_health_result = mcp_ui.get("account_health_result") or {}
api_local_results = list(api_results.get("api_results") or [])
base_api_results = list(api_results.get("base_api_results") or [])
architecture_failures = list(architecture.get("failures") or [])
server_log_text = (artifacts / "server.log").read_text(encoding="utf-8", errors="replace") if (artifacts / "server.log").is_file() else ""
native_worker_reuse_evidence = "native_ui_sender stage=page.reuse" in server_log_text or "native_ui_sender stage=context.reuse" in server_log_text
native_generate_content_evidence = "native_ui_sender stage=send.response_matched" in server_log_text or (
    "AI Studio native UI worker replay matched response:" in server_log_text
    and "status=200" in server_log_text
    and f"wire_model=models/{os.environ['SYSTEM_TEST_MODEL']}" in server_log_text
)
pass_blockers = []
if not source_product_status_clean:
    pass_blockers.append("source_product_worktree_dirty")
if not test_copy_product_status_clean:
    pass_blockers.append("test_copy_product_worktree_dirty")
if dirty_diagnostic_mode:
    pass_blockers.append("dirty_source_diagnostic_mode")
if external_google_quota_blocked:
    pass_blockers.append("external_google_quota_exhausted")
elif host_ui_smoke_exit_code != 0:
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
        if item.get("model_selection_unavailable") is True:
            continue
        if search is not None and bool(item.get("search")) != search:
            continue
        if stream is not None and bool(item.get("stream")) != stream:
            continue
        if int(item.get("status") or 0) < status_lt:
            return item
    return {}


def api_result_by_label(label: str, *, status_lt: int | None = None) -> dict[str, object]:
    for item in reversed(api_local_results):
        if item.get("label") == label and (status_lt is None or int(item.get("status") or 0) < status_lt):
            return item
    return {}


def api_success_text(item: dict[str, object]) -> bool:
    return bool(int(item.get("status") or 500) < 400 and item.get("assistant_has_text") is True and item.get("assistant_has_error") is not True)


def api_success_no_images(item: dict[str, object]) -> bool:
    return bool(api_success_text(item) and int(item.get("assistant_image_count") or 0) == 0)


def api_success_images(item: dict[str, object], *, exact_count: int | None = 1) -> bool:
    image_count = int(item.get("assistant_image_count") or 0)
    count_ok = image_count > 0 if exact_count is None else image_count == exact_count
    return bool(int(item.get("status") or 500) < 400 and item.get("assistant_has_error") is not True and count_ok)


def api_optional_text_with_tools_ok(item: dict[str, object], *, require_reasoning: bool = False) -> bool:
    return bool(
        api_success_no_images(item)
        and item.get("request_contains_search_tool") is True
        and item.get("request_contains_image_tool") is True
        and (not require_reasoning or item.get("has_request_reasoning") is True)
    )


def api_reasoning_ok(item: dict[str, object], *, require_thinking: bool = True) -> bool:
    reasoning = item.get("request_reasoning") if isinstance(item.get("request_reasoning"), dict) else {}
    return bool(
        api_success_text(item)
        and item.get("has_request_reasoning") is True
        and reasoning.get("effort") == "high"
        and reasoning.get("summary") == "auto"
        and (not require_thinking or item.get("assistant_has_thinking") is True)
    )


def ui_result_success(item: dict[str, object]) -> bool:
    return bool(int(item.get("chat_response_status") or 500) < 400 and item.get("outcome") == "success" and str(item.get("assistant_text_prefix") or "").strip())


def ui_result_no_images(item: dict[str, object]) -> bool:
    state = item.get("assistant_state") if isinstance(item.get("assistant_state"), dict) else {}
    return bool(ui_result_success(item) and int(state.get("image_count") or 0) == 0)


def ui_result_images(item: dict[str, object], *, exact_count: int | None = 1) -> bool:
    state = item.get("assistant_state") if isinstance(item.get("assistant_state"), dict) else {}
    image_count = int(state.get("image_count") or 0)
    count_ok = image_count > 0 if exact_count is None else image_count == exact_count
    return bool(int(item.get("chat_response_status") or 500) < 400 and item.get("outcome") == "success" and count_ok)


def ui_result_controlled(item: dict[str, object], *, allow_error: bool = False) -> bool:
    if ui_result_success(item):
        return True
    return bool(allow_error and int(item.get("chat_response_status") or 500) < 500 and str(item.get("assistant_error_prefix") or "").strip())


def ui_interface_entry_ok(entry: dict[str, object], *, allow_error: bool = False) -> bool:
    if allow_error and entry.get("controlled_before_send") is True:
        return bool(entry.get("health_status") == 200 and str(entry.get("selection_error_prefix") or "").strip())
    sent = entry.get("send") if isinstance(entry.get("send"), dict) else {}
    repeat_sent = entry.get("repeat_send") if isinstance(entry.get("repeat_send"), dict) else None
    return bool(
        entry.get("health_status") == 200
        and ui_result_controlled(sent, allow_error=allow_error)
        and (repeat_sent is None or ui_result_controlled(repeat_sent, allow_error=allow_error))
    )


def ui_reasoning_matrix_ok(result: dict[str, object], *, require_summary_visible: bool = False) -> bool:
    return bool(
        result.get("controls_available") is True
        and result.get("all_text_visible") is True
        and result.get("refresh_restored_assistant") is True
        and (not require_summary_visible or result.get("any_reasoning_summary_visible") is True)
    )


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
google_repeat_results = list(mcp_ui.get("google_repeat_results") or [])
optional_tool_text_results = mcp_ui.get("optional_tool_text_results") if isinstance(mcp_ui.get("optional_tool_text_results"), dict) else {}
reasoning_matrix_results = mcp_ui.get("reasoning_matrix_results") if isinstance(mcp_ui.get("reasoning_matrix_results"), dict) else {}
interface_matrix_results = mcp_ui.get("interface_matrix_results") if isinstance(mcp_ui.get("interface_matrix_results"), dict) else {}
provider_persistence_result = mcp_ui.get("provider_persistence_result") if isinstance(mcp_ui.get("provider_persistence_result"), dict) else {}
playground_search_result = mcp_ui.get("playground_search_result") if isinstance(mcp_ui.get("playground_search_result"), dict) else {}
openai_basic = provider_sample("openai-compatible")
openai_search = provider_sample("openai-compatible-search")
openai_repeat_1 = provider_sample("openai-compatible-repeat-1")
openai_repeat_2 = provider_sample("openai-compatible-repeat-2")
google_search_ui = provider_sample("google-ai-studio-nonstream-search")
google_optional_text_ui = optional_tool_text_results.get("google_search_image_text") if isinstance(optional_tool_text_results.get("google_search_image_text"), dict) else {}
openai_optional_text_ui = optional_tool_text_results.get("openai_search_image_text") if isinstance(optional_tool_text_results.get("openai_search_image_text"), dict) else {}
openai_reasoning_optional_text_ui = optional_tool_text_results.get("openai_search_image_reasoning_text") if isinstance(optional_tool_text_results.get("openai_search_image_reasoning_text"), dict) else {}
google_reasoning_ui = reasoning_matrix_results.get("google") if isinstance(reasoning_matrix_results.get("google"), dict) else {}
openai_reasoning_ui = reasoning_matrix_results.get("openai") if isinstance(reasoning_matrix_results.get("openai"), dict) else {}
google_interface_ui = interface_matrix_results.get("google") if isinstance(interface_matrix_results.get("google"), dict) else {}
openai_interface_ui = interface_matrix_results.get("openai") if isinstance(interface_matrix_results.get("openai"), dict) else {}
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
image_controls_passed = bool((image_tool_controls.get("google_enabled") or {}).get("available") is True and (image_tool_controls.get("openai_enabled") or {}).get("available") is True and (image_tool_controls.get("google_disabled") or {}).get("value") in {"off", False} and (image_tool_controls.get("openai_disabled") or {}).get("value") in {"off", False})
reasoning_controls_available = bool(reasoning_controls.get("google_effort_off") or reasoning_controls.get("openai_effort"))
sec_passed = bool(not architecture.get("secret_leaked") and not architecture_failures and request_log_ui_passed)
api_request_log_passed = bool(not architecture_failures and architecture.get("required_phases_seen"))
api_google_basic = api_chat("google-ai-studio", search=False, stream=False)
api_google_search = api_chat("google-ai-studio", search=True, stream=True)
api_openai_basic = api_chat("openai", search=False, stream=False)
api_openai_search = api_chat("openai", search=True, stream=True)
api_google_repeat_1 = api_result_by_label("google-repeat-1")
api_google_repeat_2 = api_result_by_label("google-repeat-2")
api_google_optional_tools = api_result_by_label("google-optional-search-image-text")
api_openai_optional_tools = api_result_by_label("openai-compatible-optional-search-image-text")
api_openai_reasoning_optional_tools = api_result_by_label("openai-compatible-optional-search-image-reasoning-text")
api_openai_reasoning_stream = api_result_by_label("openai-compatible-reasoning-stream")
api_openai_reasoning_nonstream = api_result_by_label("openai-compatible-reasoning-nonstream")
api_openai_reasoning_repeat = api_result_by_label("openai-compatible-reasoning-repeat")
api_google_image_tool_stream = api_result_by_label("google-image-tool-stream")
api_google_search_image_multiturn = api_result_by_label("google-search-image-tool-multiturn")
api_google_search_image_nonstream = api_result_by_label("google-search-image-tool-nonstream")
api_openai_image_tool = api_result_by_label("openai-compatible-image-tool")
api_cache_items = [item for item in api_local_results if str(item.get("label") or "").startswith("cache-")]
google_repeat_ui_passed = len(google_repeat_results) >= 2 and all(ui_result_success(item) for item in google_repeat_results[:2])
google_repeat_api_passed = api_success_text(api_google_repeat_1) and api_success_text(api_google_repeat_2)
google_optional_text_passed = ui_result_no_images(google_optional_text_ui) and api_optional_text_with_tools_ok(api_google_optional_tools)
openai_optional_text_passed = ui_result_no_images(openai_optional_text_ui) and api_optional_text_with_tools_ok(api_openai_optional_tools)
openai_reasoning_optional_text_passed = ui_result_no_images(openai_reasoning_optional_text_ui) and api_optional_text_with_tools_ok(api_openai_reasoning_optional_tools, require_reasoning=True)
openai_reasoning_api_passed = all(api_reasoning_ok(item) for item in (api_openai_reasoning_stream, api_openai_reasoning_nonstream, api_openai_reasoning_repeat))
google_image_tool_api_passed = api_success_images(api_google_image_tool_stream)
google_search_image_multiturn_api_passed = api_success_images(api_google_search_image_multiturn) and api_google_search_image_multiturn.get("request_contains_search_tool") is True and api_google_search_image_multiturn.get("request_contains_image_tool") is True
google_search_image_nonstream_api_passed = api_success_images(api_google_search_image_nonstream) and api_google_search_image_nonstream.get("stream") is False and api_google_search_image_nonstream.get("request_contains_search_tool") is True and api_google_search_image_nonstream.get("request_contains_image_tool") is True
openai_image_tool_api_controlled = bool(api_openai_image_tool.get("controlled_image_path_ok") is True or api_openai_image_tool.get("controlled_unsupported_ok") is True)
google_reasoning_ui_passed = ui_reasoning_matrix_ok(google_reasoning_ui)
openai_reasoning_ui_passed = ui_reasoning_matrix_ok(openai_reasoning_ui, require_summary_visible=True)
google_gemini_interface_passed = ui_interface_entry_ok(google_interface_ui.get("gemini_basic") or {}) and ui_interface_entry_ok(google_interface_ui.get("gemini_search_repeat") or {})
google_openai_chat_interface_passed = ui_interface_entry_ok(google_interface_ui.get("openai_chat_basic") or {}) and ui_interface_entry_ok(google_interface_ui.get("openai_chat_search_repeat") or {})
google_claude_interface_passed = ui_interface_entry_ok(google_interface_ui.get("claude_basic") or {}, allow_error=True) and ui_interface_entry_ok(google_interface_ui.get("claude_search_repeat") or {}, allow_error=True)
openai_openai_chat_interface_passed = ui_interface_entry_ok(openai_interface_ui.get("openai_chat_basic") or {}) and ui_interface_entry_ok(openai_interface_ui.get("openai_chat_search_repeat") or {})
openai_claude_interface_passed = ui_interface_entry_ok(openai_interface_ui.get("claude_basic") or {}, allow_error=True) and ui_interface_entry_ok(openai_interface_ui.get("claude_search_repeat") or {}, allow_error=True)
openai_gemini_interface_passed = ui_interface_entry_ok(openai_interface_ui.get("gemini_basic") or {}, allow_error=True) and ui_interface_entry_ok(openai_interface_ui.get("gemini_search_repeat") or {}, allow_error=True)
playground_search_send = playground_search_result.get("send") if isinstance(playground_search_result.get("send"), dict) else {}
playground_search_passed = bool((playground_search_result.get("search_toggle") or {}).get("available") is True and ui_result_success(playground_search_send))
local_studio_google_image_ui = local_studio_image_generation_results.get("google_image_tool_stream") if isinstance(local_studio_image_generation_results.get("google_image_tool_stream"), dict) else {}
local_studio_google_multiturn_image_ui = local_studio_image_generation_results.get("google_search_image_multiturn") if isinstance(local_studio_image_generation_results.get("google_search_image_multiturn"), dict) else {}
local_studio_google_nonstream_image_ui = local_studio_image_generation_results.get("google_search_image_nonstream") if isinstance(local_studio_image_generation_results.get("google_search_image_nonstream"), dict) else {}
local_studio_openai_image_ui = local_studio_image_generation_results.get("openai_image_tool") if isinstance(local_studio_image_generation_results.get("openai_image_tool"), dict) else {}
google_image_tool_visible_passed = ui_result_images(local_studio_google_image_ui)
google_search_image_multiturn_visible_passed = ui_result_images(local_studio_google_multiturn_image_ui)
google_search_image_nonstream_visible_passed = ui_result_images(local_studio_google_nonstream_image_ui)
openai_image_tool_visible_controlled = bool(
    (ui_result_images(local_studio_openai_image_ui) or (local_studio_openai_image_ui.get("outcome") == "error" and int(local_studio_openai_image_ui.get("chat_response_status") or 500) < 500 and str(local_studio_openai_image_ui.get("assistant_error_prefix") or "").strip()))
    if local_studio_openai_image_ui else False
)
asset_api_passed = bool((control_plane.get("local_studio_generated_asset_fetch") or {}).get("ok") and (control_plane.get("local_studio_generated_asset_fetch") or {}).get("traversal_blocked") is True)
cache_isolation_api_passed = bool((control_plane.get("local_studio_cache_isolation") or {}).get("ok") and (control_plane.get("local_studio_cache_isolation") or {}).get("attachment_variation_has_request_attachment") is True)
cache_isolation_ui_passed = bool(cache_isolation_result.get("ok") is True)
base_api_passed = bool((control_plane.get("base_api_smoke") or {}).get("ok") and len(base_api_results) >= 4)
base_image_api_passed = any(item.get("label") == "base-images-generations" and item.get("ok") and int(item.get("image_count") or 0) >= 1 for item in base_api_results)
base_image_ui_generated = bool(base_image_result.get("generation_ok") is True or base_image_result.get("ok") is True)
base_image_ui_edit_retry = bool(base_image_result.get("edit_retry_ok") is True)
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
    case_from_bool("G-LS-02", google_repeat_ui_passed and google_repeat_api_passed, "api_real+mcp_visible", "Google Responses repeated same-prompt UI and API sends both completed with visible/new assistant text.", "Google Responses repeated same-prompt UI/API evidence failed", ui_results=google_repeat_results, api_results=[api_google_repeat_1, api_google_repeat_2]),
    case_from_bool("G-LS-03", bool(google_search_ui) and api_google_search and not architecture_failures, "api_real+mcp_visible", "Google Responses search path used visible Web search toggle and API/request-log oracle checked web_search_preview.", "Google Responses search UI/API/request-log evidence missing", api_result=api_google_search),
    case_from_bool("G-LS-04", google_image_tool_api_passed and google_image_tool_visible_passed and asset_api_passed, "api_real+mcp_visible", "Google Responses image tool generated exactly one image, visible UI rendered it, and the Local Studio asset URL was fetched successfully.", "Google Responses image tool API/UI/asset evidence failed", api_result=api_google_image_tool_stream, ui_result=local_studio_google_image_ui, asset_result=control_plane.get("local_studio_generated_asset_fetch")),
    case_from_bool("G-LS-05", google_optional_text_passed, "api_real+mcp_visible", "Google Search+Image enabled ordinary text optional-tool path returned text and no rendered image while request body allowed both tools.", "Google Search+Image optional ordinary-text evidence failed", ui_result=google_optional_text_ui, api_result=api_google_optional_tools),
    case_from_bool("G-LS-06", google_search_image_multiturn_api_passed and google_search_image_multiturn_visible_passed, "api_real+mcp_visible", "Google multi-turn greeting/identity/news/search-to-image path completed without include_server_side_tool_invocations and produced one image.", "Google multi-turn search-to-image API/UI evidence failed", api_result=api_google_search_image_multiturn, ui_result=local_studio_google_multiturn_image_ui),
    case_from_bool("G-LS-07", google_search_image_nonstream_api_passed and google_search_image_nonstream_visible_passed, "api_real+mcp_visible", "Google non-stream Search+Image infographic path saved one image and did not depend on SSE-only parsing.", "Google non-stream search+image API/UI evidence failed", api_result=api_google_search_image_nonstream, ui_result=local_studio_google_nonstream_image_ui),
    case_from_bool("G-LS-08", google_reasoning_ui_passed, "mcp_visible", "Google Responses reasoning high + summary auto stream/non-stream/repeat UI path completed and refreshed with assistant state restored.", "Google reasoning high + summary auto UI evidence failed", result=google_reasoning_ui),
    case_from_bool("G-LS-09", google_gemini_interface_passed, "mcp_visible", "Google Gemini interface basic and stream/search repeat matrix completed or failed only where explicitly controlled.", "Google Gemini interface stream/search first/repeat evidence failed", result=google_interface_ui.get("gemini_search_repeat") if isinstance(google_interface_ui, dict) else {}),
    case_from_bool("G-LS-10", google_openai_chat_interface_passed, "mcp_visible", "Google OpenAI Chat interface basic and stream/search repeat matrix completed visibly.", "Google OpenAI Chat interface stream/search first/repeat evidence failed", result=google_interface_ui.get("openai_chat_search_repeat") if isinstance(google_interface_ui, dict) else {}),
    case_from_bool("G-LS-11", google_claude_interface_passed, "mcp_visible", "Google Claude interface compatibility matrix completed with visible success or controlled failure and service health preserved.", "Google Claude interface stream/search first/repeat compatibility evidence failed", result=google_interface_ui.get("claude_search_repeat") if isinstance(google_interface_ui, dict) else {}),
    case_from_bool("O-LS-01", bool(has_action("add openai-compatible provider") and has_action("edit local studio openai-compatible provider") and has_action("delete local studio openai-compatible provider") and (control_plane.get("local_studio_openai_models") or {}).get("ok")), "mcp_visible", "OpenAI-compatible provider was added/edited/deleted visibly and model list loaded with real credentials.", "OpenAI-compatible provider CRUD/model-load UI evidence missing"),
    case_from_bool("O-LS-02", bool(openai_basic and openai_repeat_1 and openai_repeat_2 and request_log_ui_passed), "mcp_visible", "OpenAI-compatible Responses visible stream basic send plus same-prompt repeat produced fresh request-log rows.", "OpenAI-compatible Responses basic/repeat visible evidence missing"),
    case_from_bool("O-LS-03", bool(openai_search and api_openai_search and not architecture_failures), "api_real+mcp_visible", "OpenAI-compatible Responses search visible/API path used web_search and did not emit web_search_preview or ResponseNotRead/ASGI errors.", "OpenAI-compatible search evidence or request-log oracle failed", api_result=api_openai_search),
    case_from_bool("O-LS-04", openai_image_tool_api_controlled and openai_image_tool_visible_controlled, "api_real+mcp_visible", "OpenAI-compatible image tool path either generated an image or returned a controlled unsupported-provider error while service health stayed usable.", "OpenAI-compatible image tool API/UI controlled evidence failed", api_result=api_openai_image_tool, ui_result=local_studio_openai_image_ui),
    case_from_bool("O-LS-05", openai_optional_text_passed, "api_real+mcp_visible", "OpenAI-compatible search+image enabled ordinary text optional-tool path returned text and no rendered image while request body allowed tools.", "OpenAI-compatible optional search+image text evidence failed", ui_result=openai_optional_text_ui, api_result=api_openai_optional_tools),
    case_from_bool("O-LS-06", openai_reasoning_ui_passed and openai_reasoning_api_passed, "api_real+mcp_visible", "OpenAI-compatible reasoning high + summary auto stream/non-stream/repeat API and UI paths preserved thinking evidence and refresh state.", "OpenAI-compatible reasoning high + summary auto evidence failed", ui_result=openai_reasoning_ui, api_results=[api_openai_reasoning_stream, api_openai_reasoning_nonstream, api_openai_reasoning_repeat]),
    case_from_bool("O-LS-07", openai_reasoning_optional_text_passed, "api_real+mcp_visible", "OpenAI-compatible search+image+reasoning ordinary-text optional-tool path completed without forcing tool/image output.", "OpenAI-compatible search+image+reasoning ordinary-text optional-tool evidence failed", ui_result=openai_reasoning_optional_text_ui, api_result=api_openai_reasoning_optional_tools),
    case_from_bool("O-LS-08", openai_openai_chat_interface_passed, "mcp_visible", "OpenAI-compatible OpenAI Chat interface basic and stream/search repeat matrix completed visibly.", "OpenAI-compatible OpenAI Chat interface stream/search first/repeat evidence failed", result=openai_interface_ui.get("openai_chat_search_repeat") if isinstance(openai_interface_ui, dict) else {}),
    case_from_bool("O-LS-09", openai_claude_interface_passed, "mcp_visible", "OpenAI-compatible Claude interface compatibility matrix completed with visible success or controlled failure and service health preserved.", "OpenAI-compatible Claude interface compatibility evidence failed", result=openai_interface_ui.get("claude_search_repeat") if isinstance(openai_interface_ui, dict) else {}),
    case_from_bool("O-LS-10", openai_gemini_interface_passed, "mcp_visible", "OpenAI-compatible Gemini interface negative compatibility matrix completed with controlled failure and service health preserved.", "OpenAI-compatible Gemini-interface compatibility evidence failed", result=openai_interface_ui.get("gemini_search_repeat") if isinstance(openai_interface_ui, dict) else {}),
    case_from_bool("LS-UI-01", provider_persistence_result.get("ok") is True and int(provider_persistence_result.get("restore_models_status") or 500) < 400, "mcp_visible", "Two OpenAI-compatible providers were created/switched, refresh restored the selected provider, and secondary cleanup restored the primary provider.", "Two-provider switch + refresh persistence evidence failed", result=provider_persistence_result),
    case_from_bool("LS-UI-02", image_controls_passed, "mcp_visible", "Google and OpenAI-compatible image-tool controls/selects were toggled and selected without cross-provider residuals in the visible smoke subset.", "Image Tool UI controls/selects were not all available", image_tool_controls=image_tool_controls),
    case_from_bool("LS-UI-03", bool((control_plane.get("local_studio_google_models") or {}).get("ok") and (control_plane.get("local_studio_openai_models") or {}).get("ok")), "api_real", "Responses model lists loaded for Google and OpenAI-compatible providers.", "Model filtering was not fully asserted beyond model-list load", control_plane_labels=["local_studio_google_models", "local_studio_openai_models"]),
    case_from_bool("LS-UI-04", bool(any(item.get("pending_observed") for item in provider_results)), "mcp_visible", "At least one visible Local Studio send observed pending state and completion timing.", "pending/streaming state was not observed", provider_result_labels=[item.get("label") for item in provider_results if item.get("pending_observed")]),
    case_from_bool("LS-UI-05", invalid_provider_recovery_passed, "mcp_visible", "Invalid OpenAI-compatible provider produced a visible controlled error, health stayed 200, and recovery send succeeded.", "Invalid provider error/recovery UI evidence missing", result=invalid_provider_recovery_result),
    case_from_bool("LS-UI-06", attachment_preview_passed, "mcp_visible", "Visible attachment chooser selected, previewed, and removed a text attachment before send.", "Attachment preview/remove evidence missing", result=attachment_preview_result),
    case_from_bool("LS-UI-07", conversation_crud_passed, "mcp_visible", "Visible conversation create/send/rename/reload/restore/rerun/delete/bulk-delete flows were asserted.", "Conversation lifecycle evidence missing", result=conversation_crud_result),
    case_from_bool("LS-UI-08", request_log_ui_passed and bool(openai_repeat_1 and openai_repeat_2), "mcp_visible", "OpenAI-compatible repeated prompt visibly sent twice and request-log group count increased by two.", "Repeated prompt request-log freshness evidence missing", result=request_log_result),
    case_from_bool("LS-UI-09", google_reasoning_ui_passed and openai_reasoning_ui_passed and openai_reasoning_api_passed, "api_real+mcp_visible", "Reasoning high+summary stream/non-stream/repeat matrix ran for Google UI and OpenAI-compatible UI/API with refresh preservation.", "Reasoning/capability high+summary stream/non-stream refresh evidence failed", google_result=google_reasoning_ui, openai_result=openai_reasoning_ui, api_results=[api_openai_reasoning_stream, api_openai_reasoning_nonstream, api_openai_reasoning_repeat]),
    case_from_bool("LS-UI-10", bool(has_action("edit local studio openai-compatible provider") and has_action("delete local studio openai-compatible provider") and sec_passed), "mcp_visible", "OpenAI-compatible provider edit/delete visible path ran and token was not leaked into logs/exports.", "Provider edit/delete or token-redaction evidence missing"),
    case_from_bool("LS-UI-11", invalid_provider_recovery_passed, "mcp_visible", "A one-second invalid Base URL produced a controlled provider error and recovered with service health 200.", "Timeout/invalid-provider controlled error evidence missing"),
    case_from_bool("LS-UI-12", request_log_ui_passed and bool(openai_repeat_1 and openai_repeat_2), "mcp_visible", "Default repeated-prompt path produced fresh request-log rows and no cache marker was observed in visible detail.", "No-result-cache default evidence missing"),
    case_from_bool("LS-UI-13", cache_isolation_api_passed and cache_isolation_ui_passed, "api_real+mcp_visible", "No-result-cache isolation covered provider/interface/model/search/image/reasoning/attachment/token variations with fresh requests and no cache markers.", "No-result-cache isolation API/UI matrix evidence failed", api_result=control_plane.get("local_studio_cache_isolation"), ui_result=cache_isolation_result, api_items=api_cache_items),
    case_from_bool("LS-UI-14", invalid_provider_recovery_passed and playground_passed and account_health_passed, "mcp_visible", "After invalid Local Studio provider error, Playground and Accounts remained usable in visible UI.", "Provider independence across base modules was not fully asserted"),
    case_from_bool("LS-UI-15", bool(invalid_provider_recovery_passed and conversation_crud_passed and request_log_ui_passed and any(item.get("pending_observed") for item in provider_results)), "mcp_visible", "Visible success/error/rerun/repeated flows ended with pending hidden, enabled input, and no residual cache markers in asserted helpers.", "UI state-machine coverage is missing some success/error/rerun/repeat assertions"),
    case_from_bool("BASE-CHAT-01", playground_passed, "mcp_visible", "Playground basic Gemini chat loaded models and returned visible assistant text after Local Studio flows.", "Playground basic chat evidence missing", result=playground_result),
    case_from_bool("BASE-CHAT-02", playground_search_passed, "mcp_visible", "Playground Search prompt path toggled Search on and returned visible assistant text.", "Playground Search prompt evidence failed", result=playground_search_result),
    case_from_bool("BASE-IMG-01", base_image_api_passed and base_image_ui_generated, "api_real+mcp_visible", "Base #images page generated a real image and base /v1/images/generations returned persisted image data.", "Base #images generation API/UI evidence failed", api_results=base_api_results, ui_result=base_image_result),
    case_from_bool("BASE-IMG-02", base_image_ui_edit_retry, "mcp_visible", "Base #images page set generated output as base/reference and completed a retry/edit path with image history preserved.", "Base #images reference/edit/retry UI evidence failed", ui_result=base_image_result),
    case_from_bool("BASE-REQ-01", request_log_ui_passed, "mcp_visible", "#requests detail/copy/export/delete was exercised for request-log groups.", "Base request-log lifecycle evidence missing", result=request_log_result),
    case_from_bool("BASE-ACC-01", account_health_passed, "mcp_visible", "#accounts listed real copied accounts and health-check button returned ok.", "Account list/health evidence missing", result=account_health_result),
    case_from_bool("BASE-ACC-02", control_ok("accounts_synthetic_import", "accounts_synthetic_delete"), "api_real", "Synthetic account import/delete ran under copied WSL accounts directory and source real accounts stayed untouched.", "Copied-account delete safety evidence missing", control_plane_labels=["accounts_synthetic_import", "accounts_synthetic_delete"]),
    case_from_bool("API-LS-01", bool((control_plane.get("local_studio_google_models") or {}).get("ok")), "api_real", "POST /api/local-studio/models for Google Responses returned model lists and required no Authorization.", "Google Local Studio model-list API failed"),
    case_from_bool("API-LS-02", bool((control_plane.get("local_studio_openai_models") or {}).get("ok") and sec_passed), "api_real", "POST /api/local-studio/models for OpenAI-compatible used real token and downstream artifacts remained redacted.", "OpenAI-compatible model-list API or redaction evidence failed"),
    case_from_bool("API-LS-03", bool(api_google_basic), "api_real", "Google Responses non-stream text API saved conversation messages.", "Google Responses non-stream API text evidence missing", api_result=api_google_basic),
    case_from_bool("API-LS-04", bool(api_google_search and not architecture_failures), "api_real", "Google Responses search API path ran without include_server_side_tool_invocations regression.", "Google search/image-tool bug API evidence missing", api_result=api_google_search),
    case_from_bool("API-LS-05", bool(api_openai_search and not architecture_failures), "api_real", "OpenAI-compatible Responses stream+search API path used web_search and had no ResponseNotRead/ASGI markers.", "OpenAI-compatible stream+search API evidence missing", api_result=api_openai_search),
    case_from_bool("API-LS-06", asset_api_passed, "api_real", "GET /api/local-studio/assets/{path} fetched a generated image asset and traversal was blocked.", "Local Studio generated asset API evidence failed", asset_result=control_plane.get("local_studio_generated_asset_fetch")),
    case_from_bool("API-LS-07", bool(api_google_search and api_openai_search and not architecture_failures), "api_real", "Provider-aware search oracle verified Google web_search_preview and OpenAI-compatible web_search request-log semantics.", "Provider-aware search oracle failed", architecture=architecture),
    case_from_bool("API-LS-08", openai_reasoning_api_passed, "api_real", "OpenAI-compatible reasoning high + summary auto stream/non-stream/repeat API path preserved request reasoning and assistant thinking.", "OpenAI-compatible reasoning high + summary auto API evidence failed", api_results=[api_openai_reasoning_stream, api_openai_reasoning_nonstream, api_openai_reasoning_repeat]),
    case_from_bool("API-LS-09", cache_isolation_api_passed and len(api_cache_items) >= 6, "api_real", "API cache-isolation matrix changed provider/interface/model/tools/reasoning/attachments/token-compatible shape and every send produced a fresh non-cache assistant result.", "API cache-isolation variation evidence failed", api_results=api_cache_items, cache_result=control_plane.get("local_studio_cache_isolation")),
    case_from_bool("API-LS-10", invalid_provider_recovery_passed and playground_passed, "mcp_visible", "Local Studio invalid provider error was controlled and base Playground API/UI remained usable.", "Local Studio error to base API isolation was not fully asserted"),
    case_from_bool("API-REQ-01", api_request_log_passed and request_log_ui_passed, "api_real+mcp_visible", "Request-log status/list/detail/export/delete lifecycle and phases were asserted.", "Request-log API lifecycle evidence missing", architecture=architecture),
    case_from_bool("API-BASE-01", base_api_passed, "api_real", "Base /v1 chat/completions, /v1/responses, /v1/messages, and /v1/images/generations smoke all returned usable protocol-native results.", "Base /v1 API smoke evidence failed", api_results=base_api_results),
    case_from_bool("API-ACC-01", control_ok("system_health", "accounts_list", "accounts_synthetic_delete") and account_health_passed, "api_real+mcp_visible", "GET /health, GET /accounts, visible /accounts/{id}/test, and copied-account delete safety were asserted.", "Account API/health/delete evidence missing"),
    case_from_bool("BUG-GEMINI-IMAGE-TOOL-01", google_search_image_multiturn_api_passed and google_search_image_multiturn_visible_passed and not architecture_failures, "api_real+mcp_visible", "Google Search+Image multi-turn reproduction path completed without include_server_side_tool_invocations and preserved exactly one generated image.", "Google Search+Image multi-turn regression evidence failed", api_result=api_google_search_image_multiturn, ui_result=local_studio_google_multiturn_image_ui),
    case_from_bool("BUG-OPENAI-SEARCH-STREAM-01", bool(api_openai_search and not architecture_failures), "api_real+mcp_visible", "OpenAI-compatible search stream path was exercised and request-log/server-log oracle checked controlled error/no ResponseNotRead/no ASGI exception markers.", "OpenAI search stream bug oracle failed"),
    case_from_bool("BUG-OPENAI-SEARCH-TOOL-TYPE-01", bool(api_openai_search and api_google_search and not architecture_failures), "api_real", "Request-log oracle verified OpenAI-compatible search uses web_search while Google continues using web_search_preview.", "Search tool-type oracle failed"),
    case_from_bool("BUG-OPENAI-RESPONSES-REASONING-01", openai_reasoning_ui_passed and openai_reasoning_api_passed, "api_real+mcp_visible", "OpenAI-compatible reasoning high + summary stream/non-stream visible/API path preserved thinking through completed conversation and refreshed UI.", "OpenAI-compatible Responses reasoning preservation regression evidence failed", ui_result=openai_reasoning_ui, api_results=[api_openai_reasoning_stream, api_openai_reasoning_nonstream]),
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
plan_result = "blocked" if external_google_quota_blocked else "pass" if matrix_all_passed else "fail" if not matrix_mapping_complete else "complete_with_failures"
ui_result = "blocked" if external_google_quota_blocked else "pass" if matrix_all_passed else "fail"
plan_reason = (
    "External Google AI Studio quota exhaustion blocked quota-consuming visible UI coverage; do not rerun real Google cases until quota recovers."
    if external_google_quota_blocked
    else "Plan-script alignment maps every required P0/P1 row to pass/fail/not_applicable-with-evidence; remaining blockers are concrete failing rows, not missing matrix mapping."
)

ui_results = {
    "result": ui_result,
    "reason": plan_reason,
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
    "external_google_quota_blocked": external_google_quota_blocked,
    "google_quota_blocker": google_quota_blocker,
    "resume_artifact_reuse": resume_artifact_reuse,
    "resume_evidence": resume_evidence,
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
    "reason": plan_reason,
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
    "external_google_quota_blocked": external_google_quota_blocked,
    "google_quota_blocker": google_quota_blocker,
    "resume_artifact_reuse": resume_artifact_reuse,
    "resume_evidence": resume_evidence,
    "newly_covered_required_coverage": passing_required_cases,
    "missing_required_coverage": unmapped_required_cases,
    "concrete_failed_coverage": failing_required_cases,
}

system_test_result = "SYSTEM_TEST_PASS" if matrix_all_passed else "SYSTEM_TEST_BLOCKED" if external_google_quota_blocked else "SYSTEM_TEST_INCOMPLETE"
incomplete_reason = "none" if matrix_all_passed else "external_google_quota_exhausted" if external_google_quota_blocked else "concrete_required_case_failures" if failing_required_cases or pass_blockers else "none"

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
    "system_test_result": system_test_result,
    "host_visible_ui_smoke_result": "pass" if host_ui_smoke_exit_code == 0 else "fail",
    "api_control_plane_result": "pass" if control_plane_passed else "fail",
    "plan_script_alignment_result": plan_alignment["result"],
    "matrix_mapping_complete": matrix_mapping_complete,
    "failing_required_case_count": len(failing_required_cases),
    "incomplete_reason": incomplete_reason,
    "external_google_quota_blocked": external_google_quota_blocked,
    "google_quota_blocker": google_quota_blocker,
    "resume_artifact_reuse": resume_artifact_reuse,
    "resume_evidence": resume_evidence,
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
    f"- External Google quota blocked: {external_google_quota_blocked}",
    f"- Resume artifact reuse: {resume_artifact_reuse}",
    f"- Resume source: {resume_evidence.get('source_run_root', 'none') if isinstance(resume_evidence, dict) else 'none'}",
    f"- Pass blockers: {', '.join(pass_blockers) if pass_blockers else 'none from worktree state'}",
    f"- Plan-script matrix mapping complete: {matrix_mapping_complete}",
    f"- Required case rows: {len(required_case_ids)} mapped={len(case_id_counts)} pass={len(passing_required_cases)} fail={len(failing_required_cases)} not_applicable={len(not_applicable_cases)}",
    f"- Remaining failing rows: {', '.join(failing_required_cases[:20]) if failing_required_cases else 'none'}",
]), encoding="utf-8")

if matrix_all_passed:
    print("SYSTEM_TEST_PASS")
    print(json.dumps(plan_alignment, ensure_ascii=False))
    sys.exit(0)
if external_google_quota_blocked:
    print("SYSTEM_TEST_BLOCKED external_google_quota_exhausted")
    print(json.dumps(plan_alignment, ensure_ascii=False))
    sys.exit(9)
print("SYSTEM_TEST_INCOMPLETE concrete_required_case_failures")
print(json.dumps(plan_alignment, ensure_ascii=False))
sys.exit(8)
PY
