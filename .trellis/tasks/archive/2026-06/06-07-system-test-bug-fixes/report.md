# System Test Bug Fix Report

## Current Result

- Result: `SYSTEM_TEST_INCOMPLETE`.
- Latest real diagnostic run root: `/home/bamboo/nexus-studio-system-test-20260608-010023`.
- Diagnostic mode was required because the source and temporary copy both contained current task/product changes. The runner therefore correctly recorded `result_cannot_be_SYSTEM_TEST_PASS` even where individual API/UI checks passed.
- Real credential gate passed with copied Google account credentials from `/home/bamboo/nexus-studio/data/accounts` and the real OpenAI-compatible credential file from `C:\Users\bamboo\Documents\github\key.txt`.
- The earlier official AI Studio direct `403` blocker has been resolved. The host official baseline now opens the official page with `?model=gemini-3.5-flash`, reads back the visible current model as `Gemini 3.5 Flash`, uses a Camoufox/native-style browser context, and collected three visible official `GenerateContent` samples with HTTP 200.
- The remaining product-facing hard blocker is the Local Studio Google performance comparison: official median first token/completion was `4442 ms`, while same-account Local Studio median first token/completion was `30339/30343 ms`, exceeding the current budgets of `8884/11663 ms`.
- The run still does not satisfy full `SYSTEM_TEST_PLAN.md` coverage. The plan-script alignment gate intentionally remains `incomplete` for missing P0/P1 UI matrix coverage.

## Latest Safe Evidence

- Environment: the runner created a fresh WSL temporary copy, fresh venv, editable install, and isolated data directories under `/home/bamboo/nexus-studio-system-test-20260608-010023`.
- Preflight: `NETWORK_PREFLIGHT_OK` and `NATIVE_WORKER_PREFLIGHT_OK` passed before service startup.
- Startup readiness: global `/health` reached `warmup.status == "complete"` before API and UI phases ran.
- API provider checks passed with real credentials:
  - Google AI Studio Responses, `search=false`, `stream=false`, model `gemini-3.5-flash`, HTTP 200.
  - OpenAI-compatible Responses, `search=false`, `stream=false`, model `codex-auto-review`, HTTP 200.
  - Google AI Studio Responses, `search=true`, `stream=true`, HTTP 200.
  - OpenAI-compatible Responses, `search=true`, `stream=true`, HTTP 200.
- Control-plane API checks passed, including system health/stats/config, Local Studio health, accounts list, synthetic account import/delete on the copied accounts dir, Provider Manager health/list/create/get/catalog/disable/audit/delete, and built-in Google provider delete forbidden with HTTP 403.
- Host visible UI ran in Windows headed Chromium with `headless=false`, `slow_mo_ms=200`, and `hold_seconds=30`.
- Visible UI artifact coverage remains an expanded subset: it covers navigation, Google/OpenAI provider paths, Provider Manager CRUD/boundary checks, request logs, account health, conversation CRUD, attachment preview/remove, and Playground basic Gemini chat; `full_system_plan_coverage=false` remains correct.
- OpenAI-compatible model loading recovered from transient upstream 502s with bounded retry. The latest run had no unexpected console errors.

## Official Baseline Resolution

- Artifact: `host-official-aistudio-results.json` under `/home/bamboo/nexus-studio-system-test-20260608-010023/artifacts`.
- Result: `pass`.
- Browser context: `headed_camoufox_visible`, engine `camoufox`.
- Account: `acc_5b6e9fdd`, effective official `authuser=0`.
- Model: target `gemini-3.5-flash`; visible current model readback matched `Gemini 3.5 Flash`.
- Official direct samples: status list `[200, 200, 200]`; sample first-visible/completion times were approximately `5644 ms`, `4438 ms`, and `4442 ms`.
- The `authuser=2` candidate was rejected because the official site redirected it to `/u/0/prompts/new_chat`; this is now recorded as an authuser mismatch instead of silently treating the redirected page as a successful `u/2` page.
- The official baseline records warmup/discarded attempts separately. In the latest run, the first official warmup saw the known transient 403 body preview, and one subsequent HTTP 200 sample did not display exact text in the official UI; the harness kept both as evidence and then collected three passing visible samples.

## Remaining Blockers

- `performance_comparison_result=fail`: Local Studio Google same-account samples succeeded with HTTP 200 and exact visible text, but median visible latency exceeded the official direct baseline budget.
  - Official median first token/completion: `4442/4442 ms`.
  - Local Studio median first token/completion: `30339/30343 ms`.
  - Budget: `8884/11663 ms`.
  - Account alignment succeeded: account `acc_5b6e9fdd`, success delta `3`, expected at least `3`.
- `SYSTEM_TEST_PLAN.md` coverage remains incomplete. Missing or incomplete coverage includes:
  - Complete P0/P1 UI matrix for both Google AI Studio and OpenAI-compatible providers.
  - Full search, image, and reasoning generation matrix with hard UI/request-log oracles.
  - Attachment send upstream matrix, not just preview/remove.
  - Image generation real visible output matrix.
  - Accounts switch/delete UI paths on a temporary copied account directory.
  - Provider Manager rollout phase gate and discovery/health completeness beyond the current CRUD/boundary subset.
- The runner correctly exits non-zero instead of printing `SYSTEM_TEST_PASS` while these gaps or blockers remain.

## Fixes Covered In This Session

- Shared official model selection now includes a current-model readback oracle. A target card in the picker is no longer enough; the right-side Run settings/current model must match the target model.
- Official and native UI chat opening now supports `?model=<target>`, verified against the real official page. This prevents defaulting to `Gemini 3 Flash Preview` when the test target is `Gemini 3.5 Flash`.
- Native sender, `BrowserSession`, and host official baseline now reject model-selection false positives and record the current model label/candidates.
- Host official baseline now enumerates copied accounts and `authuser` candidates, records redirects/mismatches, and uses Camoufox/native-style browser context for official direct comparison.
- Official baseline now records warmup and discarded samples separately and requires three passing visible HTTP 200 samples before performance comparison.
- Native UI worker pool now retries recoverable 401/403 once on the same worker/context before rotating workers.
- Account warmup now prewarms each native UI worker context, reducing the chance that user-visible requests pay the first-request 403 recovery cost.
- Host UI model loading now uses bounded retry for transient upstream 502 model-list responses and only treats unrecovered errors as unexpected console failures.
- Unit coverage was added/updated for native sender model readback, session readiness, native worker same-context retry, and worker-pool warmup behavior.

## Verification

- `C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest tests/unit -q`: `491 passed in 30.52s`.
- `C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest tests/unit/test_native_ui_worker_pool.py tests/unit/test_native_ui_sender.py tests/unit/test_gateway_session_readiness.py tests/unit/test_capture_service.py -q`: `121 passed in 23.27s`.
- `C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m py_compile src/aistudio_api/infrastructure/gateway/session.py src/aistudio_api/infrastructure/gateway/native_ui_worker_pool.py .trellis/tasks/06-07-system-test-bug-fixes/host-ui-smoke.py tests/unit/test_gateway_session_readiness.py tests/unit/test_native_ui_worker_pool.py`: passed.
- `git diff --check -- src/aistudio_api/infrastructure/gateway/session.py src/aistudio_api/infrastructure/gateway/native_ui_worker_pool.py .trellis/tasks/06-07-system-test-bug-fixes/host-ui-smoke.py tests/unit/test_gateway_session_readiness.py tests/unit/test_native_ui_worker_pool.py`: passed.
- Real WSL diagnostic `/home/bamboo/nexus-studio-system-test-20260608-010023`: real credential gate, network preflight, native worker preflight, warmup, API provider checks, official direct baseline, and expanded headed UI subset ran. Official baseline passed; Local Studio performance budget failed; plan coverage remains incomplete.

## Spec Update Judgment

- Trellis Phase 3.3 spec-update gate was performed after quality verification.
- Spec updates were required because the task discovered executable contracts for official AI Studio model selection, first-context `401`/`403` native UI recovery, and same-model official-vs-Local Studio visible performance evidence.
- Updated `.trellis/spec/backend/quality-guidelines.md`; no additional spec files were needed after the full unit suite passed.

## Secret Boundary

- Raw request logs, cookies, storage states, tokens, screenshots, generated images, and server logs are not included in this task report.
- Only safe summaries, artifact file names, status codes, model ids, account-id hashes already emitted by the app, and redacted official error previews are recorded here.