#!/usr/bin/env bash
set -euo pipefail
set +x

SRC="/mnt/c/Users/bamboo/Desktop/nexus-studio"
BASE_SCRIPT="$SRC/.trellis/tasks/06-04-fix-aistudio-permission-relogin/system-test-wsl.sh"
TASK_DIR="$SRC/.trellis/tasks/06-06-native-ui-worker-pool"
GENERATED_SCRIPT="/tmp/nexus-native-ui-worker-pool-system-test-$$.sh"

if [ ! -f "$BASE_SCRIPT" ]; then
  echo "SYSTEM_TEST_FAIL missing_base_script=$BASE_SCRIPT"
  exit 2
fi

mkdir -p "$TASK_DIR/system-test-results"
export AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT="${AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT:-3}"

python3 - "$BASE_SCRIPT" "$GENERATED_SCRIPT" <<'PY_GENERATE_NATIVE_WORKER_POOL_SYSTEM_TEST'
from __future__ import annotations

import sys
from pathlib import Path

base_script = Path(sys.argv[1])
generated_script = Path(sys.argv[2])
text = base_script.read_text(encoding="utf-8")

text = text.replace(
    'TASK_DIR="$SRC/.trellis/tasks/06-04-fix-aistudio-permission-relogin"',
    'TASK_DIR="$SRC/.trellis/tasks/06-06-native-ui-worker-pool"',
)
text = text.replace(
    'RUN_ROOT="/home/bamboo/nexus-studio-permission-relogin-$(date +%Y%m%d-%H%M%S)"',
    'RUN_ROOT="/home/bamboo/nexus-studio-native-worker-pool-$(date +%Y%m%d-%H%M%S)"',
)
text = text.replace(
    'export AISTUDIO_AUTHUSER_CANDIDATES="${AISTUDIO_AUTHUSER_CANDIDATES:-2,0,1}"',
    'export AISTUDIO_AUTHUSER_CANDIDATES="${AISTUDIO_AUTHUSER_CANDIDATES:-2,0,1}"\n'
    'export AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT="${AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT:-3}"\n'
    'echo "NATIVE_UI_WORKER_POOL_CONFIG workers=$AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT"',
)

ui_block_start = '''python - <<'PY'
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright
'''
ui_block_wrapped_start = '''echo "UI_STREAM_START"
UI_PHASE_LOG="$RUN_ROOT/ui-phase.safe.log"
set +e
python - <<'PY' > "$UI_PHASE_LOG" 2>&1
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright
'''
if ui_block_start not in text:
    raise SystemExit("SYSTEM_TEST_FAIL ui_block_start_missing")
text = text.replace(ui_block_start, ui_block_wrapped_start, 1)

ui_block_end = '''asyncio.run(main())
PY

python - <<'PY'
import json
import os
import sys
from pathlib import Path
from urllib import request
'''
ui_block_wrapped_end = '''asyncio.run(main())
PY
ui_phase_status=$?
set -e
cat "$UI_PHASE_LOG"
if [ "$ui_phase_status" -ne 0 ]; then
    echo "SYSTEM_TEST_FAIL ui_phase_exit code=$ui_phase_status log=$UI_PHASE_LOG"
    exit "$ui_phase_status"
fi

python - <<'PY'
import json
import os
import sys
from pathlib import Path
from urllib import request
'''
if ui_block_end not in text:
    raise SystemExit("SYSTEM_TEST_FAIL ui_block_end_missing")
text = text.replace(ui_block_end, ui_block_wrapped_end, 1)

injection = r'''
python - <<'PY'
import concurrent.futures
import json
import os
import sys
from pathlib import Path
from urllib import error, request

root = Path(os.environ["RUN_ROOT"])
port = os.environ["AISTUDIO_PORT"]
base = f"http://127.0.0.1:{port}"
prompt = "Reply with exactly: nexus-worker-pool-api-ok"


def send_probe(sequence: int) -> dict:
    payload = {
        "model": os.environ["SYSTEM_TEST_MODEL"],
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 32,
    }
    req = request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=300) as response:
            raw = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
            choice = (parsed.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            return {
                "sequence": sequence,
                "status_code": response.status,
                "content_prefix": str(message.get("content") or "")[:400],
                "error": None,
            }
    except error.HTTPError as exc:
        return {
            "sequence": sequence,
            "status_code": exc.code,
            "content_prefix": "",
            "error": exc.read(400).decode("utf-8", errors="replace"),
        }
    except Exception as exc:
        return {"sequence": sequence, "status_code": None, "content_prefix": "", "error": str(exc)[:400]}


print("WORKER_POOL_API_PROBE_START")
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    concurrent_results = list(executor.map(send_probe, [1, 2]))
results = concurrent_results + [send_probe(3)]
(root / "worker-pool-api.safe.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
failures = [item for item in results if item.get("status_code") != 200 or item.get("error") or not item.get("content_prefix")]
if failures:
    print("SYSTEM_TEST_FAIL worker_pool_api_probe_failed")
    print(json.dumps(results, ensure_ascii=False))
    sys.exit(11)
print("WORKER_POOL_API_PROBE_OK")
PY

python - <<'PY'
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

root = Path(os.environ["RUN_ROOT"])
log_path = root / "server.log"
configured_workers = int(os.environ.get("AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT", "3"))

ready_re = re.compile(r"AI Studio native UI worker pool ready: auth_hash=([0-9a-f]+) worker_count=(\d+)")
match_re = re.compile(r"AI Studio native UI worker replay matched response: auth_hash=([0-9a-f]+) worker=(\d+) status=(\d+)")
start_re = re.compile(r"Started native UI worker index=(\d+) pid=(\d+)")

text = ""
for _ in range(60):
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    if "AI Studio native UI worker replay matched response" in text and "AI Studio native UI worker pool ready" in text:
        break
    time.sleep(0.5)

ready = [(auth_hash, int(count)) for auth_hash, count in ready_re.findall(text)]
matches = [(auth_hash, int(worker), int(status)) for auth_hash, worker, status in match_re.findall(text)]
starts = [(int(worker), int(pid)) for worker, pid in start_re.findall(text)]
restart_lines = [line for line in text.splitlines() if "Restarted native UI worker" in line]
fallback_lines = [line for line in text.splitlines() if "native UI worker replay unavailable" in line]

matches_by_hash = defaultdict(list)
for auth_hash, worker, status in matches:
    matches_by_hash[auth_hash].append({"worker": worker, "status": status})

summary = {
    "configured_workers": configured_workers,
    "pool_ready": [{"auth_hash": auth_hash, "worker_count": count} for auth_hash, count in ready],
    "start_count": len(starts),
    "started_workers": sorted({worker for worker, _ in starts}),
    "matched_count": len(matches),
    "matches_by_hash": dict(matches_by_hash),
    "restart_count": len(restart_lines),
    "fallback_count": len(fallback_lines),
}
(root / "worker-pool-oracle.safe.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

if not ready:
    print("SYSTEM_TEST_FAIL worker_pool_not_ready")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(12)
if any(count != configured_workers for _, count in ready):
    print("SYSTEM_TEST_FAIL worker_pool_config_mismatch")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(13)
if not matches:
    print("SYSTEM_TEST_FAIL worker_pool_no_matches")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(14)
bad_statuses = [status for _, _, status in matches if status in (401, 403, 429)]
if bad_statuses:
    print("SYSTEM_TEST_FAIL worker_pool_auth_or_rate_error")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(15)
if restart_lines:
    print("SYSTEM_TEST_FAIL worker_pool_restarted")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(16)
if starts and max(worker for worker, _ in starts) >= configured_workers:
    print("SYSTEM_TEST_FAIL worker_index_exceeds_config")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(17)
if len(starts) > len(ready) * configured_workers:
    print("SYSTEM_TEST_FAIL too_many_worker_processes")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(18)
reused_hashes = [auth_hash for auth_hash, items in matches_by_hash.items() if len(items) >= 3]
concurrent_hashes = [auth_hash for auth_hash, items in matches_by_hash.items() if len({item["worker"] for item in items}) >= 2]
if not reused_hashes:
    print("SYSTEM_TEST_FAIL worker_pool_reuse_not_seen")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(19)
if not concurrent_hashes:
    print("SYSTEM_TEST_FAIL worker_pool_concurrency_not_seen")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(20)
if fallback_lines:
    print("SYSTEM_TEST_FAIL worker_pool_fallback_seen")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(21)

print("WORKER_POOL_ORACLE_OK")
PY
'''

marker = 'print("REQUEST_LOG_ORACLE_OK")\nPY\n\nmkdir -p "$TASK_DIR/system-test-results"'
if marker not in text:
    raise SystemExit("SYSTEM_TEST_FAIL injection_marker_missing")
text = text.replace(marker, 'print("REQUEST_LOG_ORACLE_OK")\nPY\n\n' + injection + '\nmkdir -p "$TASK_DIR/system-test-results"')
text = text.replace(
    'cp "$RUN_ROOT/request-log-summary.safe.json" "$TASK_DIR/system-test-results/request-log-summary.safe.json"\n'
    'cp "$RUN_ROOT/ui-console-errors.safe.txt" "$TASK_DIR/system-test-results/ui-console-errors.safe.txt"',
    'cp "$RUN_ROOT/request-log-summary.safe.json" "$TASK_DIR/system-test-results/request-log-summary.safe.json"\n'
    'cp "$RUN_ROOT/worker-pool-api.safe.json" "$TASK_DIR/system-test-results/worker-pool-api.safe.json"\n'
    'cp "$RUN_ROOT/worker-pool-oracle.safe.json" "$TASK_DIR/system-test-results/worker-pool-oracle.safe.json"\n'
    '[ -f "$RUN_ROOT/ui-phase.safe.log" ] && cp "$RUN_ROOT/ui-phase.safe.log" "$TASK_DIR/system-test-results/ui-phase.safe.log"\n'
    'cp "$RUN_ROOT/ui-console-errors.safe.txt" "$TASK_DIR/system-test-results/ui-console-errors.safe.txt"',
)

generated_script.write_text(text, encoding="utf-8")
generated_script.chmod(0o700)
PY_GENERATE_NATIVE_WORKER_POOL_SYSTEM_TEST

echo "SYSTEM_TEST_WRAPPER generated=$GENERATED_SCRIPT workers=$AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT"
bash "$GENERATED_SCRIPT"