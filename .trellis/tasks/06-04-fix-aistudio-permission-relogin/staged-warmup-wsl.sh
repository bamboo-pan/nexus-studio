#!/usr/bin/env bash
set -euo pipefail
set +x

RUN_ROOT="${RUN_ROOT:-/home/bamboo/nexus-studio-permission-relogin-20260605-192331}"
REPO="$RUN_ROOT/repo"
export RUN_ROOT

if [ ! -d "$REPO" ]; then
  echo "STAGED_WARMUP_FAIL missing_repo repo=$REPO"
  exit 2
fi
if [ ! -x "$REPO/venv/bin/python" ]; then
  echo "STAGED_WARMUP_FAIL missing_venv repo=$REPO"
  exit 2
fi

port_is_busy() {
  ss -ltn | awk '{print $4}' | grep -Eq "(^|:)${1}$"
}

PORT="${AISTUDIO_PORT:-18280}"
while port_is_busy "$PORT"; do PORT=$((PORT + 1)); done
CAMOUFOX_PORT="${AISTUDIO_CAMOUFOX_PORT:-$((PORT + 1000))}"
while port_is_busy "$CAMOUFOX_PORT"; do CAMOUFOX_PORT=$((CAMOUFOX_PORT + 1)); done
LOGIN_CAMOUFOX_PORT=$((CAMOUFOX_PORT + 1))
while port_is_busy "$LOGIN_CAMOUFOX_PORT"; do LOGIN_CAMOUFOX_PORT=$((LOGIN_CAMOUFOX_PORT + 1)); done

export AISTUDIO_PORT="$PORT"
export AISTUDIO_CAMOUFOX_PORT="$CAMOUFOX_PORT"
export AISTUDIO_LOGIN_CAMOUFOX_PORT="$LOGIN_CAMOUFOX_PORT"
export AISTUDIO_ACCOUNTS_DIR="${AISTUDIO_ACCOUNTS_DIR:-$RUN_ROOT/data/accounts}"
export AISTUDIO_LOCAL_STUDIO_DIR="$RUN_ROOT/data/local-studio"
export AISTUDIO_REQUEST_LOGS_DIR="$RUN_ROOT/data/request-logs"
export AISTUDIO_GENERATED_IMAGES_DIR="$RUN_ROOT/data/generated-images"
export AISTUDIO_IMAGE_SESSIONS_DIR="$RUN_ROOT/data/image-sessions"
export AISTUDIO_PROVIDER_MANAGER_DIR="$RUN_ROOT/data/provider-manager"
export AISTUDIO_DUMP_RAW_RESPONSE="0"
export AISTUDIO_CAMOUFOX_GEOIP="0"
export AISTUDIO_ACCOUNT_WARMUP_LIMIT="${AISTUDIO_ACCOUNT_WARMUP_LIMIT:-1}"
export AISTUDIO_ACCOUNT_MAX_RETRIES="3"
export AISTUDIO_ACCOUNT_COOLDOWN_SECONDS="1"
export AISTUDIO_TIMEOUT_CAPTURE="90"
export AISTUDIO_TIMEOUT_REPLAY="180"
export AISTUDIO_TIMEOUT_STREAM="180"
export AISTUDIO_AUTHUSER_CANDIDATES="${AISTUDIO_AUTHUSER_CANDIDATES:-2,0,1}"
export SYSTEM_TEST_MODEL="${AISTUDIO_SYSTEM_TEST_MODEL:-gemini-3.5-flash}"
export AISTUDIO_WARMUP_TEXT_MODEL="${AISTUDIO_SYSTEM_TEST_WARMUP_MODEL:-$SYSTEM_TEST_MODEL}"
export AISTUDIO_DEFAULT_TEXT_MODEL="$SYSTEM_TEST_MODEL"

PROXY_CANDIDATES=()
add_proxy_candidate() {
  local candidate="${1:-}"
  [ -n "$candidate" ] || return 0
  local existing
  for existing in "${PROXY_CANDIDATES[@]}"; do
    [ "$existing" = "$candidate" ] && return 0
  done
  PROXY_CANDIDATES+=("$candidate")
}

add_proxy_candidate "${AISTUDIO_PROXY_SERVER:-}"
add_proxy_candidate "http://192.168.128.1:7890"
WSL_GATEWAY="$(ip route show default 2>/dev/null | awk '{print $3; exit}' || true)"
if [ -n "$WSL_GATEWAY" ]; then
  add_proxy_candidate "http://$WSL_GATEWAY:7890"
fi
add_proxy_candidate "http://127.0.0.1:7890"

BROWSER_PROXY=""
for proxy_candidate in "${PROXY_CANDIDATES[@]}"; do
  echo "STAGED_NETWORK_TRY proxy=$proxy_candidate"
  if curl --http1.1 -I -L --max-time 20 --connect-timeout 6 --proxy "$proxy_candidate" https://aistudio.google.com/ >/dev/null 2>&1; then
    BROWSER_PROXY="$proxy_candidate"
    break
  fi
done
if [ -n "$BROWSER_PROXY" ]; then
  export AISTUDIO_PROXY_SERVER="$BROWSER_PROXY"
  echo "STAGED_NETWORK_OK proxy=$BROWSER_PROXY"
else
  echo "STAGED_NETWORK_TRY proxy=direct"
  curl --http1.1 -I -L --max-time 20 --connect-timeout 6 https://aistudio.google.com/ >/dev/null
  unset AISTUDIO_PROXY_SERVER
  echo "STAGED_NETWORK_OK proxy=direct"
fi

"$REPO/venv/bin/python" - <<'PY'
import json
import os
from pathlib import Path

accounts_dir = Path(os.environ["AISTUDIO_ACCOUNTS_DIR"])
registry_path = accounts_dir / "registry.json"
registry = json.loads(registry_path.read_text(encoding="utf-8"))
accounts = registry.get("accounts") if isinstance(registry, dict) else None
if not isinstance(accounts, dict):
    raise SystemExit("STAGED_WARMUP_FAIL missing_accounts_registry")
for account_id, meta in accounts.items():
    if isinstance(meta, dict):
        meta["health_status"] = "unknown"
        meta["health_reason"] = None
        meta["last_health_check"] = None
        meta["isolated_until"] = None
        meta_path = accounts_dir / str(account_id) / "meta.json"
        if meta_path.exists():
            try:
                meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta_payload = None
            if isinstance(meta_payload, dict):
                meta_payload.update(
                    {
                        "health_status": "unknown",
                        "health_reason": None,
                        "last_health_check": None,
                        "isolated_until": None,
                    }
                )
                meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8")
registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"STAGED_ACCOUNTS_RESET count={len(accounts)}")
PY

LOG_PATH="$RUN_ROOT/server-stage-warmup.log"
rm -f "$LOG_PATH"
cd "$REPO"
"$REPO/venv/bin/python" main.py server --port "$PORT" --camoufox-port "$CAMOUFOX_PORT" > "$LOG_PATH" 2>&1 &
SERVER_PID=$!
echo "STAGED_SERVER_STARTED port=$PORT camoufox_port=$CAMOUFOX_PORT log=$LOG_PATH"

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

"$REPO/venv/bin/python" - <<'PY'
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
started = time.time()
next_progress = 0.0
last = None

def http_json(path, timeout=5):
    with request.urlopen(base + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

while time.time() < deadline:
    try:
        last = http_json("/health")
    except Exception as exc:
        last = {"error": str(exc)}
        time.sleep(1)
        continue
    warmup = last.get("warmup") if isinstance(last, dict) else {}
    status = warmup.get("status") if isinstance(warmup, dict) else None
    if time.time() >= next_progress:
        completed = warmup.get("completed_accounts") if isinstance(warmup, dict) else []
        failed = warmup.get("failed_accounts") if isinstance(warmup, dict) else []
        targets = warmup.get("target_accounts") if isinstance(warmup, dict) else []
        print(
            "STAGED_WARMUP_WAIT "
            f"elapsed={int(time.time() - started)}s status={status} "
            f"completed={len(completed or [])}/{len(targets or [])} failed={len(failed or [])}",
            flush=True,
        )
        next_progress = time.time() + 30
    if status == "complete":
        (root / "staged-warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        print("STAGED_WARMUP_COMPLETE")
        sys.exit(0)
    if status == "partial":
        completed = warmup.get("completed_accounts") if isinstance(warmup, dict) else []
        (root / "staged-warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        if completed:
            print(f"STAGED_WARMUP_PARTIAL_CONTINUE completed={len(completed)}")
            sys.exit(0)
        print("STAGED_WARMUP_FAIL partial_without_completed")
        sys.exit(3)
    if status in {"failed", "cancelled"}:
        (root / "staged-warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"STAGED_WARMUP_FAIL status={status}")
        sys.exit(3)
    time.sleep(2)

(root / "staged-warmup-health.safe.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
print("STAGED_WARMUP_FAIL timeout")
sys.exit(4)
PY

echo "STAGED_SERVER_LOG_TAIL"
tail -n 80 "$LOG_PATH" || true