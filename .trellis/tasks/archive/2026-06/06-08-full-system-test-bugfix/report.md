# Full System Test Bugfix Report

## Runs

| Run | Mode | Result | Run root |
| --- | --- | --- | --- |
| Initial | clean WSL copy | `SYSTEM_TEST_INCOMPLETE concrete_required_case_failures` | `/home/bamboo/nexus-studio-system-test-20260608-113803` |
| Diagnostic after warm-pool preservation | dirty-source diagnostic | `SYSTEM_TEST_INCOMPLETE`; target performance improved but account alignment still failed | `/home/bamboo/nexus-studio-system-test-20260608-115800` |
| Diagnostic after native worker warmup recovery | dirty-source diagnostic | `HOST_UI_SMOKE_OK`; target rows passed; dirty-source gates still blocked `SYSTEM_TEST_PASS` | `/home/bamboo/nexus-studio-system-test-20260608-121431` |
| Formal after commit `e8a8442` | clean WSL copy | `SYSTEM_TEST_INCOMPLETE`; source/test-copy clean, host UI stopped on transient OpenAI-compatible TLS EOF during recovery check | `/home/bamboo/nexus-studio-system-test-20260608-135313` |
| Formal after commit `ac0ec25` | clean WSL copy | `SYSTEM_TEST_INCOMPLETE concrete_required_case_failures`; API, visible UI, request-log, architecture, source clean, and test-copy clean gates passed | `/home/bamboo/nexus-studio-system-test-20260608-140643` |
| Continuation diagnostics | dirty-source diagnostics | Expanded API/UI matrix evidence and product fixes were exercised, but these interrupted diagnostics did not produce a final `summary.md` | `/home/bamboo/nexus-studio-system-test-20260608-163152` through `/home/bamboo/nexus-studio-system-test-20260608-201224` |
| Quota-blocked diagnostic | dirty-source diagnostic | Warmup completed and official/performance evidence passed, but the run stopped before final summary/UI artifact after official Google AI Studio daily quota exhaustion | `/home/bamboo/nexus-studio-system-test-20260608-203835` |
| Throttled quota-restored continuation | dirty-source diagnostic, `SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS=30` | API Google requests were paced and passed, but Host UI stopped on the first Local Studio Google performance sample with official `You exceeded your current quota ... rate-limits` text; no further Google rerun was attempted | `/home/bamboo/nexus-studio-system-test-20260608-221554` |
| Low-quota resume setup attempt | dirty-source diagnostic resume | Reused API artifacts from `20260608-221554`, but stopped before live service/UI calls because the same source run's official baseline artifact was not a passing 3-sample baseline | `/home/bamboo/nexus-studio-system-test-20260609-074327` |
| Low-quota two-source resume | dirty-source diagnostic resume, `SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS=60` | Reused API artifacts from `20260608-221554` and official baseline artifacts from `20260608-203835`, completed fresh warmup and current request-log setup, then stopped on the first Local Studio Google performance sample with official quota text; no further Google rerun was attempted | `/home/bamboo/nexus-studio-system-test-20260609-074940` |
| Low-quota partial-UI resume | dirty-source diagnostic resume, `SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS=60` | Warmup completed and Host UI reached Provider Manager; `mcp-visible-ui-results.json` was written as a partial checkpoint at `checkpoint_after_provider_manager`, then the run stopped on `google-ai-studio-performance-1` after an ambiguous PerUserQuota `404` response was captured as the generation stream result | `/home/bamboo/nexus-studio-system-test-20260610-011140` |
| Warmup quota retry after native PerUserQuota filter | dirty-source diagnostic resume, `SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS=60` | The retry stopped before Host UI because startup account-browser warmup hit official Gemini current-quota/rate-limits text on both copied accounts; the runner now classifies this warmup-stage condition as `SYSTEM_TEST_BLOCKED external_google_quota_exhausted` | `/home/bamboo/nexus-studio-system-test-20260610-012925` |
| Low-quota resume after cooldown | dirty-source diagnostic resume, `SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS=60` | Reused API artifacts from `20260608-221554` and official baseline artifacts from `20260608-203835`, then stopped during startup account-browser warmup with `SYSTEM_TEST_BLOCKED external_google_quota_exhausted`; this run verified the warmup-stage quota blocker artifact and summary path | `/home/bamboo/nexus-studio-system-test-20260610-072442` |
| Full matrix resume before proxy bridge | dirty-source diagnostic resume with API result reuse | Reused the clean API evidence root and completed the visible UI matrix; `BASE-IMG-02` still failed because the shell proxy was not yet bridged into Playwright/Camoufox replay, producing upstream `HTTP 0 / ENETUNREACH` after bounded image retry/fallback | `/home/bamboo/nexus-studio-system-test-20260611-105028` |
| Proxy-bridged image permission recovery run | dirty-source diagnostic resume with API result reuse and bridged `AISTUDIO_PROXY_SERVER` | Proxy bridge removed the ENETUNREACH path and exposed Google image-edit permission/availability handling; this run still failed `BASE-IMG-02`, `G-LS-01`, `PERF-01`, `BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01`, and dirty-source gates | `/home/bamboo/nexus-studio-system-test-20260611-224414` |
| Complete matrix after image/performance recovery | dirty-source diagnostic resume with API result reuse | Completed the 80/80 mapped matrix; `BASE-IMG-02`, `PERF-01`, `G-LS-01`, and native-model-selection regression rows passed, leaving only `G-LS-07` as a functional failure beside dirty-source diagnostic gates | `/home/bamboo/nexus-studio-system-test-20260611-232555` |
| Latest quota-blocked follow-up | dirty-source diagnostic resume with API result reuse | Bounded retry moved `G-LS-07` to passing before the run stopped later with `SYSTEM_TEST_BLOCKED external_google_quota_exhausted` at `google-claude-search-repeat` | `/home/bamboo/nexus-studio-system-test-20260612-001338` |

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

Continuation changes made after the clean formal run:

- The WSL runner and headed host UI smoke now collect more concrete API/UI evidence for Local Studio, Provider Manager, reasoning, image, request-log, and interface-mode matrix rows instead of broad placeholder buckets.
- The runner probes and exports a real OpenAI-compatible reasoning model using a sentinel-output oracle, and host UI reasoning assertions use bounded real-evidence retries for transient upstream gaps.
- Local Studio streaming completion events now include the request body so reasoning/tool/search evidence can be asserted from saved conversation events.
- Conversation CRUD UI coverage uses the Google provider path instead of depending on the more failure-prone external OpenAI-compatible provider.
- Startup warmup now treats native UI send-button click timeout/disabled state as transient/recoverable while still keeping permission and auth failures hard.
- Native worker warmup now tracks successful distinct worker indexes through `send_with_metadata` so readiness evidence covers the configured worker pool instead of repeatedly proving only one hot worker.
- Account selection now waits for a short `rate_limited` account only when it is the remaining viable account, but keeps hard-isolated accounts excluded.
- Official Google daily/current quota exhaustion is now classified as `quota_exhausted` with a long configurable cooldown instead of the ordinary short `rate_limited` cooldown. This state is persisted, excluded from cooldown waiting, and rendered explicitly in the account UI.
- The WSL runner and headed host UI smoke now pace quota-consuming real Google generation requests with `SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS` (default `30` seconds). The throttle covers API Google Local Studio calls, official AI Studio baseline samples, Local Studio Google UI sends, conversation reruns, and Playground Google sends without reducing required samples or assertions.
- The WSL runner now bridges `HTTPS_PROXY`/`HTTP_PROXY` into `AISTUDIO_PROXY_SERVER` when the dedicated browser/service proxy is not preset, so WSL shell checks, Playwright `APIRequestContext`, and Camoufox replay use the same reachable proxy path.
- The host UI smoke now classifies official Google quota exhaustion as a dedicated external blocker: it writes `google-quota-blocker.safe.json`, exits with a quota-blocker code, and the WSL summary reports `SYSTEM_TEST_BLOCKED external_google_quota_exhausted` instead of merging quota exhaustion into ordinary matrix failures.
- The WSL runner now supports explicit low-quota resume mode. `SYSTEM_TEST_RESUME_FROM_RUN_ROOT` can reuse prior safe API/model artifacts, `SYSTEM_TEST_RESUME_OFFICIAL_BASELINE_FROM_RUN_ROOT` can reuse a separate passing official AI Studio 3-sample baseline, and the run writes `resume-evidence.safe.json` plus current-run setup evidence instead of pretending reused evidence was freshly generated.
- Quota-blocker handling now preserves the Windows host UI smoke exit code through PowerShell and skips the downstream request-log/architecture oracle once `google-quota-blocker.safe.json` exists, so an external Google quota blocker is not misreported as an ordinary request-log assertion failure.
- OpenAI Responses compatibility now maps `reasoning.effort` into the internal Local Studio thinking setting, so Responses-style reasoning requests exercise the same native/backend path as Chat Completions reasoning requests.
- The headed host UI smoke writes partial result artifacts at durable checkpoints and uses the same result writer on the final success path, preserving visible evidence if a later matrix phase is blocked.
- Native AI Studio session replay and the clean-process native UI sender now ignore ambiguous PerUserQuota `404` response bodies and continue waiting for the actual `GenerateContent` response instead of treating the helper/quota response as the generation stream.
- Startup warmup quota handling now writes a safe quota-blocker artifact when official Gemini quota text appears during account-browser warmup, allowing the WSL runner to report `SYSTEM_TEST_BLOCKED external_google_quota_exhausted` instead of an ordinary warmup failure.
- The visible #images smoke now handles user-visible image edit failures as a bounded recovery path: it reads the `.image-error` card, clicks the visible `重试` control, records each attempt, switches to a fallback Gemini image model after repeated transient upstream failures, recovers aligned account state when needed, and fails fast with evidence instead of waiting indefinitely for a nonexistent image.
- The visible #images smoke now also recovers aligned account state after Google image edit permission/availability failures and verifies the fallback model path against the visible Base Images UI instead of treating account health as sufficient image-edit authorization.
- Local Studio interface-matrix sends now reuse transient retry helpers for first sends, repeat sends, and Google image stream/non-stream cases, so one transient `HTTP 504`/network/no-image-data gap does not abort the remaining UI matrix without retry evidence.
- Local Studio performance alignment now treats the rotator success-counter delta as diagnostic when visible same-account samples and latency budgets pass, avoiding false failures when the active account already matches the sampled account.

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

The first formal non-diagnostic rerun after commit `e8a8442` confirmed the clean-source gates were no longer blockers, but host UI smoke stopped before writing its full artifact because the OpenAI-compatible recovery sample hit a real upstream TLS EOF:

```text
AssertionError: expected Local Studio success for openai-compatible-recovery-after-invalid-provider, got error=ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol
```

This failure was outside the Google/native-worker target path and occurred only after the controlled invalid-provider error had already been verified. The host UI smoke now retries that specific recovery sample for transient upstream connect/read/protocol errors while keeping persistent recovery failures hard.

The final formal non-diagnostic run at `/home/bamboo/nexus-studio-system-test-20260608-140643` verified the fixed path from a clean source tree and clean test copy:

- `HOST_UI_SMOKE_OK` with final visible UI artifact `mcp-visible-ui-results.json` written.
- API/request-log subset, API control-plane subset, provider-manager phase gate, and request-log/architecture oracle all passed.
- `source_product_status_clean=true`, `test_copy_product_status_clean=true`, `dirty_source_diagnostic_mode=false`, and `pass_blockers=[]`.
- `G-LS-01`, `G-LS-03`, `BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01`, and `PERF-01` remained in `passing_required_cases`.

Latest local non-quota-consuming verification after the continuation fixes:

```text
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest tests/unit/test_account_health_and_selection.py tests/unit/test_gateway_session_readiness.py tests/unit/test_native_ui_worker_pool.py tests/unit/test_local_studio.py -q
183 passed

C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m py_compile src/aistudio_api/application/api_service.py src/aistudio_api/application/account_rotator.py src/aistudio_api/infrastructure/account/account_store.py src/aistudio_api/config.py .trellis/tasks/06-08-full-system-test-bugfix/host-ui-smoke.py
passed

bash -n .trellis/tasks/06-08-full-system-test-bugfix/system-test-wsl.sh
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest .trellis/tasks/06-08-full-system-test-bugfix/test_matrix_harness_contract.py -q
2 passed

C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest tests/unit/test_static_frontend_capabilities.py -q
18 passed

git diff --check
passed
```

The throttled real WSL continuation at `/home/bamboo/nexus-studio-system-test-20260608-221554` used `SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS=30` and showed the remaining blocker is external quota state, not a burst-frequency harness path:

- API Google Local Studio cases logged throttle waits and completed before Host UI.
- Host UI failed on `google-ai-studio-performance-1` with official Google text `You exceeded your current quota ... rate-limits`.
- `server.log` recorded the same official quota text during warmup and later logged `Account was quota_exhausted; cooling down for 21600s`, proving the product quota classifier was exercised.
- The pre-classifier summary for this run was `SYSTEM_TEST_INCOMPLETE` only because the harness still treated quota exhaustion as an ordinary host UI failure. The harness now emits `SYSTEM_TEST_BLOCKED external_google_quota_exhausted` for this condition and stops quota-consuming Google UI sends.

Post-classification local harness checks passed without making new real Google requests:

```text
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m py_compile .trellis/tasks/06-08-full-system-test-bugfix/host-ui-smoke.py
passed

bash -n .trellis/tasks/06-08-full-system-test-bugfix/system-test-wsl.sh
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest .trellis/tasks/06-08-full-system-test-bugfix/test_matrix_harness_contract.py -q
2 passed

git diff --check
passed
```

After the quota-restored continuation change, the harness-only checks passed again:

```text
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m py_compile .trellis/tasks/06-08-full-system-test-bugfix/host-ui-smoke.py
passed

bash -n .trellis/tasks/06-08-full-system-test-bugfix/system-test-wsl.sh
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest .trellis/tasks/06-08-full-system-test-bugfix/test_matrix_harness_contract.py -q
2 passed

git diff --check
passed
```

After adding low-quota resume support and correcting quota-blocker classification, harness-only checks passed again without making real Google requests:

```text
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m py_compile .trellis/tasks/06-08-full-system-test-bugfix/host-ui-smoke.py
bash -n .trellis/tasks/06-08-full-system-test-bugfix/system-test-wsl.sh
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest .trellis/tasks/06-08-full-system-test-bugfix/test_matrix_harness_contract.py -q
4 passed
```

The final non-quota validation pass after documenting the latest blocked resume also passed:

```text
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m py_compile .trellis/tasks/06-08-full-system-test-bugfix/host-ui-smoke.py
bash -n .trellis/tasks/06-08-full-system-test-bugfix/system-test-wsl.sh
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest .trellis/tasks/06-08-full-system-test-bugfix/test_matrix_harness_contract.py tests/unit/test_account_health_and_selection.py tests/unit/test_gateway_session_readiness.py tests/unit/test_native_ui_worker_pool.py tests/unit/test_local_studio.py tests/unit/test_static_frontend_capabilities.py -q
205 passed

git diff --check
passed
```

The latest local validation after the 20260610 PerUserQuota filter, warmup quota-blocker classification, and tracker refresh also passed without making real Google requests:

```text
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest tests/unit/test_api_responses.py tests/unit/test_native_ui_sender.py tests/unit/test_native_ui_worker_pool.py tests/unit/test_gateway_session_readiness.py .trellis/tasks/06-08-full-system-test-bugfix/test_matrix_harness_contract.py -q
133 passed

C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m py_compile src/aistudio_api/application/api_service.py src/aistudio_api/infrastructure/gateway/session.py src/aistudio_api/infrastructure/gateway/native_ui_sender.py .trellis/tasks/06-08-full-system-test-bugfix/host-ui-smoke.py .trellis/tasks/06-08-full-system-test-bugfix/build_matrix_coverage_excel.py
bash -n .trellis/tasks/06-08-full-system-test-bugfix/system-test-wsl.sh
git diff --check
passed
```

After the `/home/bamboo/nexus-studio-system-test-20260610-072442` warmup quota blocker, local validation still passed without making real Google requests:

```text
C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest tests/unit/test_api_responses.py tests/unit/test_native_ui_sender.py tests/unit/test_native_ui_worker_pool.py tests/unit/test_gateway_session_readiness.py .trellis/tasks/06-08-full-system-test-bugfix/test_matrix_harness_contract.py -q
133 passed, 1 deprecation warning

C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m py_compile .trellis/tasks/06-08-full-system-test-bugfix/build_matrix_coverage_excel.py .trellis/tasks/06-08-full-system-test-bugfix/host-ui-smoke.py
bash -n .trellis/tasks/06-08-full-system-test-bugfix/system-test-wsl.sh
git diff --check
passed; git reported only existing line-ending normalization warnings
```

The earlier quota-blocked WSL run at `/home/bamboo/nexus-studio-system-test-20260608-203835` produced safe evidence before stopping:

- `warmup-health.safe.json` reported `warmup.status=complete`, with both `acc_5b6e9fdd` and `acc_493e01f1` completed and no failed accounts.
- `host-official-aistudio-results.json` reported `result=pass` for `acc_493e01f1`, `authuser=0`, `model=gemini-3.5-flash`, with three `200` official samples.
- `host-performance-comparison-results.json` reported `result=pass`, official median completion `10065ms`, Local Studio median completion `7856ms`, and completion budget `20097.5ms`.
- `api-results.json` recorded `15` API cases with `14` HTTP 200 cases and one OpenAI-compatible transient TLS EOF `502` marked as a transient upstream retry.
- `server.log` later recorded `UsageLimitExceeded: 配额用完` with official Google text `You exceeded your current quota... rate-limits`. The user also confirmed the visible AI Studio daily quota notice.
- No final `summary.md` and no `mcp-visible-ui-results.json` were written for this run, so it is an external quota blocker, not a pass verdict.

The 20260609 low-quota WSL resume at `/home/bamboo/nexus-studio-system-test-20260609-074940` reduced quota use but still hit the same external Google limit:

- `resume-evidence.safe.json` recorded API/model artifacts reused from `/home/bamboo/nexus-studio-system-test-20260608-221554` and a passing official AI Studio baseline reused from `/home/bamboo/nexus-studio-system-test-20260608-203835`.
- The run created a fresh WSL test copy, completed `NETWORK_PREFLIGHT_OK`, `NATIVE_WORKER_PREFLIGHT_OK`, and `WARMUP_COMPLETE`, then enabled request logs on the current service without replaying API Google cases.
- Host UI smoke stopped on `google-ai-studio-performance-1` with official text `You exceeded your current quota ... ai.google.dev/gemini-api/docs/rate-limits` even with `SYSTEM_TEST_GOOGLE_REQUEST_INTERVAL_SECONDS=60`.
- The runner initially continued into the request-log/architecture oracle after the host blocker, which produced misleading failures such as missing search-tool logs. The harness now skips that oracle when `google-quota-blocker.safe.json` exists and preserves the host quota-blocker exit code for a `SYSTEM_TEST_BLOCKED external_google_quota_exhausted` summary.

The 20260610 low-quota partial-UI resume at `/home/bamboo/nexus-studio-system-test-20260610-011140` preserved current visible evidence before stopping:

- `mcp-visible-ui-results.json` was written as a partial artifact with `partial=true` and `partial_reason=checkpoint_after_provider_manager`.
- The preserved artifact includes Provider Manager and navigation evidence from the current run, but it does not credit the later failed Google generation phase.
- The run exposed an ambiguous PerUserQuota `404` body on a `GenerateContent` response path. The native sender/session handlers now ignore that helper/quota body and continue waiting for the actual generation response.

The 20260610 warmup retry at `/home/bamboo/nexus-studio-system-test-20260610-012925` stopped earlier, during startup account-browser warmup:

- `warmup-health.safe.json` reported `warmup.status=failed`, both copied accounts in `failed_accounts`, and no completed accounts.
- `server.log` contained official Gemini current-quota/rate-limits text for both accounts.
- The runner was updated after this diagnosis to write `google-quota-blocker.safe.json` and report `SYSTEM_TEST_BLOCKED external_google_quota_exhausted` for this warmup-stage quota condition.

The latest low-quota WSL resume at `/home/bamboo/nexus-studio-system-test-20260610-072442` was run after the cooldown window and verified the new warmup blocker path:

- `resume-evidence.safe.json` recorded API/model artifacts reused from `/home/bamboo/nexus-studio-system-test-20260608-221554` and a passing official AI Studio baseline reused from `/home/bamboo/nexus-studio-system-test-20260608-203835`.
- The run copied real credentials, completed `NETWORK_PREFLIGHT_OK` and `NATIVE_WORKER_PREFLIGHT_OK`, then started the server on port `18480`.
- Startup account-browser warmup hit official Gemini current-quota/rate-limits text for both copied accounts and stopped before Host UI.
- `google-quota-blocker.safe.json` was written with `label=startup-account-browser-warmup`, `status=429`, `warmup_status=failed`, and `google_request_interval_seconds=60`.
- `summary.json` and `summary.md` reported `SYSTEM_TEST_BLOCKED` with `incomplete_reason=external_google_quota_exhausted`.
- The root workbook `系统测试矩阵覆盖跟踪.xlsx` was regenerated so its summary points to `startup-account-browser-warmup`, the latest resume root `20260610-072442`, and the partial UI evidence root `20260610-011140`.

The `/home/bamboo/nexus-studio-system-test-20260611-105028` full matrix resume proved the visible image error path no longer hangs, but also showed the browser/service proxy was not reaching the Google image-edit endpoint:

- `ui-results.json` reported `matrix_mapping_complete=true`, `required_case_count=80`, and `mapped_case_count=80`.
- The remaining required failures were `ENV-01`, `ENV-04`, `BOOT-02`, and `BASE-IMG-02`; the first three were dirty-source diagnostic pass gates, not additional product regressions.
- `BASE-IMG-01` passed: the #images page generated a real persisted image at `/generated-images/20260611/7aee7d78e28e4094949da46642beeadf.jpg`.
- `BASE-IMG-02` failed after bounded recovery, not because the script stalled: the script clicked visible retry, tried 3 edit attempts on `gemini-3.1-flash-image-preview`, switched model, then tried 2 more attempts on `gemini-3-pro-image-preview`.
- Every edit attempt returned `502` with `HTTP 0: APIRequestContext.post: connect ENETUNREACH ... https://alkalimakersuite-pa.clients6.google.com/$rpc/...`. Later verification showed explicit proxy traffic could reach that host, so the root cause was missing `AISTUDIO_PROXY_SERVER` propagation into Playwright/Camoufox replay, not an unrecoverable WSL network route.

The `/home/bamboo/nexus-studio-system-test-20260611-232555` complete matrix resume verified the proxy bridge, image permission recovery, fallback model handling, and performance-account correction:

- `plan-script-alignment-results.json` reported `required_case_count=80`, `mapped_case_count=80`, and `result=complete_with_failures`.
- `BASE-IMG-02` passed: the Base Images UI set a generated output as base/reference and completed a retry/edit path with image history preserved.
- `PERF-01` passed: official AI Studio and Local Studio each produced three same-account visible samples and the median latency budget passed.
- `G-LS-01` and `BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01` passed again under the bridged proxy run.
- The remaining failures were `ENV-01`, `ENV-04`, `BOOT-02`, and `G-LS-07`; the first three are dirty-source diagnostic gates.

The latest follow-up at `/home/bamboo/nexus-studio-system-test-20260612-001338` verified the Google non-stream image retry before exhausting external quota:

- `summary.json` reported `SYSTEM_TEST_BLOCKED` with `incomplete_reason=external_google_quota_exhausted`.
- `google_quota_blocker.label=google-claude-search-repeat`, `reason=external_google_quota_exhausted`, and `google_request_interval_seconds=30`.
- `G-LS-07`, `BASE-IMG-02`, and `PERF-01` were all listed in `passing_required_cases` before the quota blocker stopped later Google Local Studio coverage.

## Current Full-System Status

The final completed clean formal run still emitted `SYSTEM_TEST_INCOMPLETE concrete_required_case_failures` because the executable matrix had concrete failing rows outside the original Google/native-worker bug fix. The mapping was complete (`80` required rows mapped, `47` passing, `29` failing, `4` not applicable), and there were no dirty-source pass blockers.

```text
G-LS-02, G-LS-04, G-LS-05, G-LS-06, G-LS-07, G-LS-08, G-LS-09, G-LS-10, G-LS-11,
O-LS-04, O-LS-05, O-LS-06, O-LS-07, O-LS-08, O-LS-09, O-LS-10,
LS-UI-01, LS-UI-02, LS-UI-09, LS-UI-13,
BASE-CHAT-02, BASE-IMG-01, BASE-IMG-02,
API-LS-06, API-LS-08, API-LS-09, API-BASE-01,
BUG-GEMINI-IMAGE-TOOL-01, BUG-OPENAI-RESPONSES-REASONING-01
```

These rows remain the historical clean formal matrix gaps and were not relaxed.

Continuation work expanded the executable matrix and fixed additional product/harness issues, including low-quota artifact reuse, partial UI artifacts, native PerUserQuota response filtering, warmup-stage quota-blocker reporting, WSL-to-browser proxy bridging, visible #images edit retry/model fallback, account recovery after image retries, Local Studio performance account-alignment diagnostics, and Local Studio interface/image transient retry. The latest complete real matrix run no longer stops on script-level waiting, quota classification, proxy propagation, image-edit permission recovery, or performance-account false failures. The latest follow-up moved `G-LS-07` to passing before stopping later on external Google quota exhaustion, so the current non-local blocker is `SYSTEM_TEST_BLOCKED external_google_quota_exhausted`, not `BASE-IMG-02`.

## Next Work

The Google/native-worker performance and warmup regression is fixed and verified by the clean formal run. The continuation fixes are locally verified and documented in backend code-spec, report, and reusable tracker workbook. To reach `SYSTEM_TEST_PASS`, rerun from a clean source/test copy after committing or otherwise clearing diagnostic dirty-source gates, and wait for Google quota recovery before rerunning quota-consuming Google UI coverage; if official quota text reappears, keep it classified as `SYSTEM_TEST_BLOCKED external_google_quota_exhausted`.
