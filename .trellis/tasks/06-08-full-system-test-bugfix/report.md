# Full System Test Bugfix Report

## Runs

| Run | Mode | Result | Run root |
| --- | --- | --- | --- |
| Initial | clean WSL copy | `SYSTEM_TEST_INCOMPLETE concrete_required_case_failures` | `/home/bamboo/nexus-studio-system-test-20260608-113803` |
| Diagnostic after warm-pool preservation | dirty-source diagnostic | `SYSTEM_TEST_INCOMPLETE`; target performance improved but account alignment still failed | `/home/bamboo/nexus-studio-system-test-20260608-115800` |
| Diagnostic after native worker warmup recovery | dirty-source diagnostic | `HOST_UI_SMOKE_OK`; target rows passed; dirty-source gates still blocked `SYSTEM_TEST_PASS` | `/home/bamboo/nexus-studio-system-test-20260608-121431` |

## Initial Failure

The first real WSL system test reached the real-credential and server gates, including `NETWORK_PREFLIGHT_OK`, `NATIVE_WORKER_PREFLIGHT_OK`, `WARMUP_COMPLETE`, `API_REAL_PROVIDER_OK`, and `REQUEST_LOG_AND_ARCHITECTURE_ORACLE_OK`, but failed visible UI performance coverage.

Key failure signatures:

- `HOST_UI_SMOKE_FAIL performance_comparison_result=fail`.
- Official AI Studio median completion was about `3906ms`; Local Studio median completion was about `27978ms` against an about `10859ms` budget.
- One Local Studio Google sample took about `67832ms`.
- Server logs showed a startup-warmed account native worker pool being discarded by later account activation, and a native worker model-selection failure lasting about 45 seconds with `current_text_model_not_found` plus `type=text_category`.

## Root Causes Fixed

1. `/accounts/{account_id}/activate` and `/rotation/next` closed the same account's warmed `AccountClientPool` entry even when the account credentials had not changed. This destroyed the native worker pool warmed during startup and forced Local Studio performance samples back onto cold workers.
2. Native UI worker request failures caused by a stale/incorrect AI Studio model-selection page state, specifically `AI Studio text model not selected in native UI sender` with `current_text_model_not_found` or `text_category`, were treated as ordinary request failures. During startup warmup, a single bad worker page state could fail and isolate an otherwise usable account before the rest of the worker pool was probed.

## Changes

- Manual account activation and forced rotation now preserve existing per-account isolated clients and their native worker pools. Credential-changing paths still invalidate the pool: login completion, credential import, and account delete.
- Native worker pool request-error classification now restarts workers on model-selection readback failures from the native sender.
- Account-native startup warmup continues probing additional workers after recoverable model-selection UI failures while preserving the `max_attempts=1` per-probe coverage contract and keeping 401/403 permission failures hard.
- Regression tests cover warmed pool preservation, worker restart on current-model readback failure, and warmup continuation after recoverable native model-selection failure.

## Verification

Focused local unit tests passed:

```text
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest tests/unit/test_native_ui_worker_pool.py tests/unit/test_gateway_session_readiness.py::test_probe_native_worker_generate_content_retries_permission_statuses tests/unit/test_gateway_session_readiness.py::test_probe_native_worker_generate_content_continues_after_recoverable_model_selection_failure tests/unit/test_account_auth_activation.py tests/unit/test_capture_service.py::test_client_account_text_warmup_uses_native_worker_pool_without_template_capture tests/unit/test_capture_service.py::test_client_account_text_warmup_keeps_native_worker_forbidden_as_auth_failure -q
23 passed
```

The final diagnostic WSL run at `/home/bamboo/nexus-studio-system-test-20260608-121431` verified the target bug class:

- `HOST_UI_SMOKE_OK`.
- `G-LS-01`, `BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01`, and `PERF-01` moved to `passing_required_cases`.
- `performance-comparison-results.json` reported `result=pass`, official median completion `4000ms`, Local Studio median completion `7827ms`, and completion budget `11000ms`.
- API/request-log subset and API control-plane subset both passed.

## Remaining Failures

The final diagnostic run still emitted `SYSTEM_TEST_INCOMPLETE` because dirty-source diagnostic mode cannot produce `SYSTEM_TEST_PASS`, and the current executable matrix still has concrete failing rows outside this bug fix:

```text
ENV-01, ENV-04, G-LS-02, G-LS-04, G-LS-05, G-LS-06, G-LS-07, G-LS-08, G-LS-09, G-LS-10, G-LS-11,
O-LS-04, O-LS-05, O-LS-06, O-LS-07, O-LS-08, O-LS-09, O-LS-10,
LS-UI-01, LS-UI-02, LS-UI-09, LS-UI-13,
BASE-CHAT-02, BASE-IMG-01, BASE-IMG-02,
API-LS-06, API-LS-08, API-LS-09, API-BASE-01,
BUG-GEMINI-IMAGE-TOOL-01, BUG-OPENAI-RESPONSES-REASONING-01
```

These rows remain explicit plan coverage/product blockers and were not relaxed.

## Next Gate

After committing the task changes, rerun `.trellis/tasks/06-08-full-system-test-bugfix/system-test-wsl.sh` without diagnostic mode from a clean source tree. The expected result is no dirty-source blockers, target rows still passing, and any remaining `SYSTEM_TEST_INCOMPLETE` limited to the unresolved matrix rows above.