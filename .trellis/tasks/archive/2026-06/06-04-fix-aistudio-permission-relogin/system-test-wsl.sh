#!/usr/bin/env bash
set -euo pipefail
set +x

SRC="/mnt/c/Users/bamboo/Desktop/nexus-studio"
TASK_DIR="$SRC/.trellis/tasks/06-04-fix-aistudio-permission-relogin"
RUN_ROOT="/home/bamboo/nexus-studio-permission-relogin-$(date +%Y%m%d-%H%M%S)"
export RUN_ROOT
mkdir -p "$RUN_ROOT"
echo "SYSTEM_TEST_START run_root=$RUN_ROOT"

echo "COPY_REPO_START"
rsync -a --delete \
  --exclude .git \
  --exclude .venv \
  --exclude venv \
  --exclude data \
  --exclude tmp \
  "$SRC/" "$RUN_ROOT/repo/"
echo "COPY_REPO_OK"

if [ -d /home/bamboo/nexus-studio/data/accounts ]; then
  REAL_ACCOUNTS_DIR=/home/bamboo/nexus-studio/data/accounts
elif [ -d /home/bamboo/aistudio-api/data/accounts ]; then
  REAL_ACCOUNTS_DIR=/home/bamboo/aistudio-api/data/accounts
else
  echo "SYSTEM_TEST_FAIL missing_real_accounts_dir"
  echo "run_root=$RUN_ROOT"
  exit 2
fi

mkdir -p "$RUN_ROOT/data"
rsync -a --delete "$REAL_ACCOUNTS_DIR/" "$RUN_ROOT/data/accounts/"
echo "COPY_ACCOUNTS_OK source=$REAL_ACCOUNTS_DIR"

cd "$RUN_ROOT/repo"

python3 -m venv venv
. venv/bin/activate
echo "PYTHON_ENV_READY"
echo "INSTALL_REPO_START"
python -m pip install -q -e .
echo "INSTALL_REPO_OK"
echo "PLAYWRIGHT_INSTALL_CHECK"
if python - <<'PY'
from pathlib import Path
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
        chromium_path = Path(playwright.chromium.executable_path)
        print(f"PLAYWRIGHT_CHROMIUM_PATH path={chromium_path} exists={chromium_path.exists()}")
        raise SystemExit(0 if chromium_path.exists() else 1)
PY
then
    echo "PLAYWRIGHT_INSTALL_SKIP cached=1"
else
    PLAYWRIGHT_INSTALL_LOG="$RUN_ROOT/playwright-install.log"
    echo "PLAYWRIGHT_INSTALL_START log=$PLAYWRIGHT_INSTALL_LOG"
    if timeout --kill-after=20s 900s python -m playwright install chromium > "$PLAYWRIGHT_INSTALL_LOG" 2>&1; then
        cat "$PLAYWRIGHT_INSTALL_LOG"
        echo "PLAYWRIGHT_INSTALL_OK"
    else
        install_status=$?
        echo "PLAYWRIGHT_INSTALL_FAIL code=$install_status log=$PLAYWRIGHT_INSTALL_LOG"
        tail -n 120 "$PLAYWRIGHT_INSTALL_LOG" || true
        exit "$install_status"
    fi
fi

python - <<'PY'
import json
import os
from pathlib import Path

accounts_dir = Path(os.environ["RUN_ROOT"]) / "data" / "accounts"
registry_path = accounts_dir / "registry.json"
if not registry_path.exists():
    raise SystemExit("SYSTEM_TEST_FAIL missing_registry")
registry = json.loads(registry_path.read_text(encoding="utf-8"))
accounts = registry.get("accounts") if isinstance(registry, dict) else None
if not isinstance(accounts, dict) or len(accounts) < 2:
    raise SystemExit("SYSTEM_TEST_FAIL need_at_least_two_accounts")
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
            except Exception:
                pass
registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"ACCOUNT_COPY_READY count={len(accounts)}")
PY

port_is_busy() {
  ss -ltn | awk '{print $4}' | grep -Eq "(^|:)${1}$"
}

PORT=18280
while port_is_busy "$PORT"; do PORT=$((PORT + 1)); done
CAMOUFOX_PORT=$((PORT + 1000))
while port_is_busy "$CAMOUFOX_PORT"; do CAMOUFOX_PORT=$((CAMOUFOX_PORT + 1)); done
LOGIN_CAMOUFOX_PORT=$((CAMOUFOX_PORT + 1))
while port_is_busy "$LOGIN_CAMOUFOX_PORT"; do LOGIN_CAMOUFOX_PORT=$((LOGIN_CAMOUFOX_PORT + 1)); done

export AISTUDIO_PORT="$PORT"
export AISTUDIO_CAMOUFOX_PORT="$CAMOUFOX_PORT"
export AISTUDIO_LOGIN_CAMOUFOX_PORT="$LOGIN_CAMOUFOX_PORT"
export AISTUDIO_ACCOUNTS_DIR="$RUN_ROOT/data/accounts"
export AISTUDIO_LOCAL_STUDIO_DIR="$RUN_ROOT/data/local-studio"
export AISTUDIO_REQUEST_LOGS_DIR="$RUN_ROOT/data/request-logs"
export AISTUDIO_GENERATED_IMAGES_DIR="$RUN_ROOT/data/generated-images"
export AISTUDIO_IMAGE_SESSIONS_DIR="$RUN_ROOT/data/image-sessions"
export AISTUDIO_PROVIDER_MANAGER_DIR="$RUN_ROOT/data/provider-manager"
export AISTUDIO_DUMP_RAW_RESPONSE="0"
SYSTEM_TEST_MODEL="${AISTUDIO_SYSTEM_TEST_MODEL:-gemini-3.5-flash}"
export SYSTEM_TEST_MODEL
export AISTUDIO_WARMUP_TEXT_MODEL="${AISTUDIO_SYSTEM_TEST_WARMUP_MODEL:-$SYSTEM_TEST_MODEL}"
export AISTUDIO_DEFAULT_TEXT_MODEL="$SYSTEM_TEST_MODEL"
export AISTUDIO_ACCOUNT_WARMUP_LIMIT="2"
export AISTUDIO_ACCOUNT_MAX_RETRIES="3"
export AISTUDIO_ACCOUNT_COOLDOWN_SECONDS="1"
export AISTUDIO_TIMEOUT_CAPTURE="90"
export AISTUDIO_TIMEOUT_REPLAY="180"
export AISTUDIO_TIMEOUT_STREAM="180"
export AISTUDIO_CAMOUFOX_GEOIP="0"
export AISTUDIO_AUTHUSER_CANDIDATES="${AISTUDIO_AUTHUSER_CANDIDATES:-2,0,1}"

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
add_proxy_candidate "${https_proxy:-}"
add_proxy_candidate "${HTTPS_PROXY:-}"
add_proxy_candidate "${http_proxy:-}"
add_proxy_candidate "${HTTP_PROXY:-}"
WSL_GATEWAY="$(ip route show default 2>/dev/null | awk '{print $3; exit}' || true)"
if [ -n "$WSL_GATEWAY" ]; then
    add_proxy_candidate "http://$WSL_GATEWAY:7890"
    add_proxy_candidate "socks5h://$WSL_GATEWAY:7890"
fi
add_proxy_candidate "http://127.0.0.1:7890"
add_proxy_candidate "socks5h://127.0.0.1:7890"

BROWSER_PROXY=""
for proxy_candidate in "${PROXY_CANDIDATES[@]}"; do
    echo "NETWORK_PRECHECK_TRY proxy=$proxy_candidate"
    if curl --http1.1 -I -L --max-time 20 --connect-timeout 6 --proxy "$proxy_candidate" https://aistudio.google.com/ >/dev/null 2>&1; then
        BROWSER_PROXY="$proxy_candidate"
        break
    fi
done

if [ -n "$BROWSER_PROXY" ]; then
    export AISTUDIO_PROXY_SERVER="$BROWSER_PROXY"
    echo "NETWORK_PRECHECK_OK proxy=$BROWSER_PROXY"
else
    echo "NETWORK_PRECHECK_TRY proxy=direct"
    curl --http1.1 -I -L --max-time 20 --connect-timeout 6 https://aistudio.google.com/ >/dev/null
    unset AISTUDIO_PROXY_SERVER
    echo "NETWORK_PRECHECK_OK proxy=direct"
fi

python - <<'PY'
import json
from pathlib import Path
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox

from aistudio_api.config import camoufox_proxy_identity_options, settings
from aistudio_api.infrastructure.account.account_store import AccountStore

accounts_dir = Path(__import__("os").environ["AISTUDIO_ACCOUNTS_DIR"])
store = AccountStore(accounts_dir=accounts_dir)
accounts = store.list_accounts()
needs_refresh = []
for account in accounts:
    auth_path = store.get_auth_path(account.id)
    if auth_path is None:
        continue
    state = json.loads(auth_path.read_text(encoding="utf-8"))
    try:
        store.validate_generate_ready_storage_state(state)
    except ValueError:
        needs_refresh.append(account.id)

if not needs_refresh:
    print("ACCOUNT_BROWSER_STORAGE_REFRESH skipped=ready")
    raise SystemExit(0)

browser_options = {
    "headless": True,
    "main_world_eval": True,
    "firefox_user_prefs": {
        "network.dns.disableIPv6": True,
        "network.http.http3.enable": False,
    },
}
if settings.proxy_server:
    browser_options["proxy"] = {"server": settings.proxy_server}
    browser_options.update(camoufox_proxy_identity_options())


def _state_summary(state):
    origins = state.get("origins") if isinstance(state, dict) else None
    aistudio_local_storage = 0
    aistudio_indexed_db = 0
    if isinstance(origins, list):
        for origin in origins:
            if not isinstance(origin, dict):
                continue
            if urlparse(str(origin.get("origin") or "")).hostname != "aistudio.google.com":
                continue
            local_storage = origin.get("localStorage")
            if isinstance(local_storage, list):
                aistudio_local_storage += len(local_storage)
            indexed_db = origin.get("indexedDB")
            if isinstance(indexed_db, list):
                aistudio_indexed_db += len(indexed_db)
    return len(origins or []), aistudio_local_storage, aistudio_indexed_db


with Camoufox(**browser_options) as browser:
    for account_id in needs_refresh:
        auth_path = accounts_dir / account_id / "auth.json"
        print(f"ACCOUNT_BROWSER_STORAGE_REFRESH_START account={account_id}")
        context = browser.new_context(storage_state=str(auth_path))
        page = context.new_page()
        refreshed = False
        failure = "missing_ai_studio_browser_storage"
        try:
            for navigation_attempt in range(1, 4):
                try:
                    page.goto("https://aistudio.google.com/", wait_until="domcontentloaded", timeout=90000)
                    break
                except Exception as exc:
                    failure = f"{type(exc).__name__}: {exc}"
                    if navigation_attempt >= 3:
                        raise
                    print(f"ACCOUNT_BROWSER_STORAGE_REFRESH_NAV_RETRY account={account_id} attempt={navigation_attempt}")
                    page.wait_for_timeout(2000)
            for attempt in range(1, 31):
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                current_url = page.url
                if "accounts.google.com" in current_url:
                    failure = "login_required"
                    break
                state = context.storage_state(indexed_db=True)
                origins, local_storage_count, indexed_db_count = _state_summary(state)
                try:
                    store.validate_generate_ready_storage_state(state)
                except ValueError:
                    if attempt in {1, 5, 10, 20, 30}:
                        print(
                            "ACCOUNT_BROWSER_STORAGE_REFRESH_WAIT "
                            f"account={account_id} attempt={attempt} origins={origins} "
                            f"aistudio_local_storage={local_storage_count} aistudio_indexed_db={indexed_db_count}"
                        )
                    page.wait_for_timeout(2000)
                    continue
                auth_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                print(
                    "ACCOUNT_BROWSER_STORAGE_REFRESH_OK "
                    f"account={account_id} origins={origins} "
                    f"aistudio_local_storage={local_storage_count} aistudio_indexed_db={indexed_db_count}"
                )
                refreshed = True
                break
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            context.close()
        if not refreshed:
            print(f"ACCOUNT_BROWSER_STORAGE_REFRESH_FAIL account={account_id} reason={failure}")
PY

python main.py server --port "$PORT" --camoufox-port "$CAMOUFOX_PORT" > "$RUN_ROOT/server.log" 2>&1 &
SERVER_PID=$!
echo "SERVER_STARTED port=$PORT camoufox_port=$CAMOUFOX_PORT"

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

run_native_generate_probe() {
    local output_path="$RUN_ROOT/native-generate-probe.safe.jsonl"
    echo "NATIVE_GENERATE_PROBE_START output=$output_path"
    if AISTUDIO_NATIVE_PROBE_MODEL="$SYSTEM_TEST_MODEL" \
        AISTUDIO_NATIVE_PROBE_AUTHUSERS="${AISTUDIO_AUTHUSER_CANDIDATES:-2,0,1}" \
        AISTUDIO_NATIVE_PROBE_DUMP_MODELS="${AISTUDIO_NATIVE_PROBE_DUMP_MODELS:-0}" \
        AISTUDIO_NATIVE_PROBE_SERVICE_WORKERS="${AISTUDIO_NATIVE_PROBE_SERVICE_WORKERS:-block}" \
        timeout --kill-after=20s 420s python "$RUN_ROOT/repo/.trellis/tasks/06-04-fix-aistudio-permission-relogin/native_generate_probe.py" > "$output_path" 2>&1; then
        cat "$output_path"
        echo "NATIVE_GENERATE_PROBE_OK"
    else
        local probe_status=$?
        cat "$output_path" || true
        echo "NATIVE_GENERATE_PROBE_EXIT code=$probe_status"
    fi
}

set +e
python - <<'PY'
import json
import os
import sys
import time
from pathlib import Path
from urllib import error, request

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
target_model = os.environ["SYSTEM_TEST_MODEL"]
base = f"http://127.0.0.1:{port}"

def write_json(name, payload):
    (root / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def http_json(method, path, payload=None, timeout=60):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(base + path, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    with request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None

deadline = time.time() + 1500
started = time.time()
next_progress = 0.0
last = None
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
        completed = warmup.get("completed_accounts") if isinstance(warmup, dict) else []
        failed = warmup.get("failed_accounts") if isinstance(warmup, dict) else []
        targets = warmup.get("target_accounts") if isinstance(warmup, dict) else []
        print(
            "WARMUP_WAIT "
            f"elapsed={int(time.time() - started)}s "
            f"status={status} "
            f"completed={len(completed or [])}/{len(targets or [])} "
            f"failed={len(failed or [])}"
        )
        next_progress = time.time() + 30
    if status == "complete":
        write_json("warmup-health.safe.json", last)
        print("WARMUP_COMPLETE")
        break
    if status == "partial":
        completed = warmup.get("completed_accounts") if isinstance(warmup, dict) else []
        write_json("warmup-health.safe.json", last)
        if completed:
            print(f"WARMUP_PARTIAL_CONTINUE completed={len(completed)}")
            break
        print("SYSTEM_TEST_FAIL warmup_status=partial completed=0")
        sys.exit(3)
    if status in {"failed", "cancelled"}:
        write_json("warmup-health.safe.json", last)
        print(f"SYSTEM_TEST_FAIL warmup_status={status}")
        sys.exit(3)
    time.sleep(2)
else:
    write_json("warmup-health.safe.json", last)
    print("SYSTEM_TEST_FAIL warmup_timeout")
    sys.exit(4)

accounts = http_json("GET", "/accounts", timeout=30)
safe_accounts = [
    {
        "id": item.get("id"),
        "tier": item.get("tier"),
        "health_status": item.get("health_status"),
        "is_isolated": item.get("is_isolated"),
    }
    for item in accounts
]
write_json("accounts.safe.json", {"count": len(safe_accounts), "accounts": safe_accounts})
if len(safe_accounts) < 2:
    print("SYSTEM_TEST_FAIL need_two_accounts_after_startup")
    sys.exit(5)

status = http_json("PUT", "/request-logs/status", {"enabled": True}, timeout=30)
write_json("request-log-status.safe.json", status)
PY
warmup_probe_status=$?
set -e
if [ "$warmup_probe_status" -ne 0 ]; then
    run_native_generate_probe
    exit "$warmup_probe_status"
fi

python - <<'PY'
import json
import os
import sys
from pathlib import Path
from urllib import error, request

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
target_model = os.environ["SYSTEM_TEST_MODEL"]
base = f"http://127.0.0.1:{port}"

payload = {
    "model": os.environ["SYSTEM_TEST_MODEL"],
    "stream": True,
    "messages": [
        {"role": "user", "content": "Reply with exactly: nexus-permission-api-ok"}
    ],
    "temperature": 0,
    "max_tokens": 32,
}

req = request.Request(
    base + "/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={"Content-Type": "application/json"},
)
events = []
content = ""
error_event = None
status_code = None
raw_prefix = ""
try:
    with request.urlopen(req, timeout=300) as response:
        status_code = response.status
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if len(raw_prefix) < 800:
                raw_prefix += line[: max(0, 800 - len(raw_prefix))]
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                events.append({"done": True})
                break
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            if parsed.get("error"):
                error_event = parsed["error"]
            choices = parsed.get("choices") or []
            for choice in choices:
                delta = choice.get("delta") or {}
                content += str(delta.get("content") or "")
except error.HTTPError as exc:
    status_code = exc.code
    raw_prefix = exc.read(800).decode("utf-8", errors="replace")
except Exception as exc:
    raw_prefix = str(exc)[:800]

result = {
    "status_code": status_code,
    "content_prefix": content[:400],
    "error_event": error_event,
    "raw_prefix": raw_prefix[:400],
    "done_seen": bool(events and events[-1].get("done")),
}
(root / "api-stream.safe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
if status_code != 200 or error_event or not content.strip():
    print("SYSTEM_TEST_FAIL api_stream_failed")
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(6)
print("API_STREAM_OK")
PY

python - <<'PY'
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

async def main():
    root = Path(os.environ["RUN_ROOT"])
    port = os.environ["AISTUDIO_PORT"]
    model = os.environ["SYSTEM_TEST_MODEL"]
    base = f"http://127.0.0.1:{port}"
    console_errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        page.on(
            "console",
            lambda msg: console_errors.append(f"{msg.type}:{msg.text}") if msg.type == "error" else None,
        )
        await page.goto(base + "/static/index.html#studio", wait_until="networkidle")
        await page.wait_for_selector("#studio-page.active", timeout=20000)
        await page.evaluate(
            """(model) => {
                localStorage.setItem('openai.localStudio.settings.v1', JSON.stringify({
                    providers: [{
                        id: 'google-ai-studio',
                        type: 'google-ai-studio',
                        providerType: 'google-ai-studio',
                        name: 'Google AI Studio',
                        baseUrl: '',
                        apiKey: '',
                        timeout: 300,
                        interfaceMode: 'responses'
                    }],
                    providerId: 'google-ai-studio',
                    providerType: 'google-ai-studio',
                    interfaceMode: 'responses',
                    model,
                    imageModel: '',
                    stream: 'on',
                    reasoningEffort: 'off',
                    reasoningSummary: 'auto',
                    search: 'off',
                    imageToolEnabled: false,
                    imageSize: '1024x1024',
                    imageCustomSize: '',
                    imageQuality: 'auto',
                    imageBackground: 'auto',
                    imageFormat: 'png',
                    imageCompression: 100
                }));
            }""",
            model,
        )
        await page.reload(wait_until="networkidle")
        await page.wait_for_selector("#studio-page.active", timeout=20000)
        await page.wait_for_selector("#studio-page textarea[placeholder*='Local Studio']", timeout=20000)
        await page.evaluate(
            """(model) => {
                const root = document.body.__x?.$data || Alpine.$data(document.body);
                root.localStudioProviderId = 'google-ai-studio';
                root.localStudioProviderType = 'google-ai-studio';
                root.localStudioInterfaceMode = 'responses';
                root.localStudioModel = model;
                root.localStudioStream = 'on';
                root.localStudioReasoningEffort = 'off';
                root.localStudioSearch = 'off';
                root.localStudioImageToolEnabled = false;
                root.saveLocalStudioSettings();
            }""",
            model,
        )
        textarea = page.locator("#studio-page textarea[placeholder*='Local Studio']")
        await textarea.fill("Reply with exactly: nexus-permission-ui-ok")
        send_button = page.locator("#studio-page .local-studio-compose-row button.send")
        await send_button.click()
        await page.wait_for_function(
            """() => {
                const root = document.body.__x?.$data || Alpine.$data(document.body);
                return !root.localStudioBusy && root.localStudioActiveMessages.length >= 2;
            }""",
            timeout=300000,
        )
        state = await page.evaluate(
            """() => {
                const root = document.body.__x?.$data || Alpine.$data(document.body);
                const messages = root.localStudioActiveMessages.map(message => ({
                    role: message.role,
                    content: String(message.content || '').slice(0, 400),
                    error: String(message.error || '').slice(0, 400)
                }));
                return {
                    view: root.view,
                    model: root.localStudioModel,
                    stream: root.localStudioStream,
                    busy: root.localStudioBusy,
                    error: String(root.localStudioError || '').slice(0, 400),
                    messages
                };
            }"""
        )
        await page.goto(base + "/static/index.html#requests", wait_until="networkidle")
        await page.wait_for_selector(".request-page.active", timeout=20000)
        await page.goto(base + "/static/index.html#accounts", wait_until="networkidle")
        await page.wait_for_selector(".page-wrap.active .account-table-panel", timeout=20000)
        await browser.close()
    (root / "ui-console-errors.safe.txt").write_text("\n".join(console_errors), encoding="utf-8")
    (root / "ui-local-studio.safe.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    assistant_messages = [message for message in state.get("messages", []) if message.get("role") == "assistant"]
    if console_errors or state.get("error") or not assistant_messages or any(message.get("error") for message in assistant_messages):
        print("SYSTEM_TEST_FAIL ui_stream_failed")
        print(json.dumps(state, ensure_ascii=False))
        sys.exit(7)
    print("UI_STREAM_OK")

asyncio.run(main())
PY

python - <<'PY'
import json
import os
import sys
from pathlib import Path
from urllib import request

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
target_model = os.environ["SYSTEM_TEST_MODEL"]
base = f"http://127.0.0.1:{port}"

with request.urlopen(base + "/request-logs?limit=1000", timeout=30) as response:
    groups_payload = json.loads(response.read().decode("utf-8"))

summary = {
    "group_total": groups_payload.get("total"),
    "entry_total": groups_payload.get("entry_total"),
    "upstream_models": [],
    "status_codes": [],
    "preview_model_seen": False,
    "target_model_seen": False,
    "error_prefixes": [],
}

for group in groups_payload.get("data") or []:
    chain_id = group.get("id") or group.get("chain_id")
    if not chain_id:
        continue
    with request.urlopen(base + f"/request-logs/groups/{chain_id}", timeout=30) as response:
        detail = json.loads(response.read().decode("utf-8"))
    group_has_target_client_request = any(
        entry.get("phase") == "client_request" and entry.get("model") == target_model
        for entry in detail.get("entries") or []
    )
    if not group_has_target_client_request:
        continue
    for entry in detail.get("entries") or []:
        status_code = entry.get("status_code")
        if status_code is not None:
            summary["status_codes"].append(int(status_code))
        if entry.get("phase") != "upstream_request":
            continue
        body_json = entry.get("body_json")
        wire_model = body_json[0] if isinstance(body_json, list) and body_json else None
        summary["upstream_models"].append(
            {
                "kind": entry.get("kind"),
                "model": entry.get("model"),
                "wire_model": wire_model,
                "status_code": status_code,
                "response_prefix": str(entry.get("response_body_raw") or "")[:160],
            }
        )
        if wire_model == "models/gemini-3-flash-preview":
            summary["preview_model_seen"] = True
        if wire_model == f"models/{target_model.removeprefix('models/')}":
            summary["target_model_seen"] = True
        response_prefix = str(entry.get("response_body_raw") or "")[:160]
        if response_prefix:
            summary["error_prefixes"].append(response_prefix)

(root / "request-log-summary.safe.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
if target_model.removeprefix("models/") != "gemini-3-flash-preview" and summary["preview_model_seen"]:
    print("SYSTEM_TEST_FAIL preview_wire_model_seen")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(8)
if not summary["target_model_seen"]:
    print("SYSTEM_TEST_FAIL target_wire_model_not_seen")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(9)
bad_statuses = [code for code in summary["status_codes"] if code in (401, 403, 429)]
if bad_statuses:
    print("SYSTEM_TEST_FAIL upstream_auth_or_rate_error")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(10)
print("REQUEST_LOG_ORACLE_OK")
PY

mkdir -p "$TASK_DIR/system-test-results"
cp "$RUN_ROOT/warmup-health.safe.json" "$TASK_DIR/system-test-results/warmup-health.safe.json"
cp "$RUN_ROOT/accounts.safe.json" "$TASK_DIR/system-test-results/accounts.safe.json"
cp "$RUN_ROOT/api-stream.safe.json" "$TASK_DIR/system-test-results/api-stream.safe.json"
cp "$RUN_ROOT/ui-local-studio.safe.json" "$TASK_DIR/system-test-results/ui-local-studio.safe.json"
cp "$RUN_ROOT/request-log-summary.safe.json" "$TASK_DIR/system-test-results/request-log-summary.safe.json"
cp "$RUN_ROOT/ui-console-errors.safe.txt" "$TASK_DIR/system-test-results/ui-console-errors.safe.txt"

echo "SYSTEM_TEST_PASS run_root=$RUN_ROOT port=$PORT camoufox_port=$CAMOUFOX_PORT"
