# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

<!--
Document your project's quality standards here.

Questions to answer:
- What patterns are forbidden?
- What linting rules do you enforce?
- What are your testing requirements?
- What code review standards apply?
-->

(To be filled by the team)

---

## Forbidden Patterns

<!-- Patterns that should never be used and why -->

(To be filled by the team)

---

## Required Patterns

<!-- Patterns that must always be used -->

(To be filled by the team)

---

## Testing Requirements

<!-- What level of testing is expected -->

(To be filled by the team)

---

## Code Review Checklist

<!-- What reviewers should check -->

(To be filled by the team)

---

## Scenario: Latest-Only Credential And Frontend Storage Formats

### 1. Scope / Trigger
- Trigger: Account credential persistence and frontend localStorage schema changes affect storage, API validation, and UI initialization.
- Scope: account registry files under `data/accounts`, `/accounts/import`, and browser localStorage keys used by `src/aistudio_api/static/app.js`.

### 2. Signatures
- API: `POST /accounts/import?name=<optional>&activate=<bool>` accepts either a current project backup package or one direct Playwright storage state object.
- Backup package signature: `{"format":"aistudio-api.credentials.backup","version":1,"accounts":[{"meta":{...},"auth":{...storageState}}]}`.
- Frontend keys: `aistudio.interfaceMode.v1` and `openai.localStudio.settings.v1` are the current browser storage keys.

### 3. Contracts
- `AccountStore` must not auto-migrate root `data/auth.json`; users re-add accounts through the current login/import flow.
- Backup account entries must use `auth`; legacy `storage_state` and `storageState` aliases are rejected.
- Direct Playwright storage states remain valid when they include non-expired Google cookies; `indexedDB` entries are preserved when present.
- Local Studio browser settings use the `providers` array plus camelCase fields such as `providerId`, `providerType`, `baseUrl`, `apiKey`, `interfaceMode`, and `imageModel`.
- Current HTTP/API payloads may still use snake_case fields such as `provider_type`, `provider_id`, and `image_model`; those are not localStorage compatibility shims.

### 4. Validation & Error Matrix
- Backup package missing `format` -> rejected as an invalid storage state or malformed import payload.
- Backup account missing `auth` -> `ValueError("credential backup account auth must be a storage state object")`, surfaced by the route as HTTP 400.
- Storage state without Google cookies -> `ValueError("credential storage state must include at least one Google cookie")`.
- Expired Google cookies -> `ValueError("credential storage state Google cookies are expired")`.
- `indexedDB` present but not an array -> `ValueError("credential storage state indexedDB entries must be arrays when present")`.

### 5. Good/Base/Bad Cases
- Good: Fresh login/import saves a Playwright storage state with cookies, origins, and optional `indexedDB` under `accounts[].auth`.
- Base: Empty account directory starts with no accounts even if a root `data/auth.json` exists.
- Bad: Re-importing old backup entries with `storage_state` or `storageState` fields.

### 6. Tests Required
- Unit: `test_account_store_does_not_auto_migrate_legacy_root_auth_file` asserts root `data/auth.json` is ignored.
- Unit: `test_import_credentials_rejects_legacy_backup_auth_field_aliases` asserts `storage_state` / `storageState` no longer import.
- Static frontend: assert old localStorage keys/schema markers such as `aistudio.apiSelection.v1` and `providerProfiles` do not appear.
- Real smoke when changing account/UI storage: run API and frontend UI smoke with a temporary empty accounts dir; real Google account validation may be skipped only when explicitly allowed.

### 7. Wrong vs Correct

#### Wrong
```json
{"format":"aistudio-api.credentials.backup","version":1,"accounts":[{"meta":{"name":"old"},"storage_state":{"cookies":[]}}]}
```

#### Correct
```json
{"format":"aistudio-api.credentials.backup","version":1,"accounts":[{"meta":{"name":"current"},"auth":{"cookies":[{"name":"sid","value":"...","domain":".google.com","path":"/"}],"origins":[]}}]}
```

---

## Scenario: Account Health Check And Delete Lifecycle

### 1. Scope / Trigger
- Trigger: Account management changes that touch `AccountStore`, `AccountService`, `/accounts/*` routes, frontend account controls, startup warmup readiness, or WSL account smoke tests.
- Scope: account registry files under `data/accounts`, `auth.json`/`meta.json`, `/accounts/{id}/test`, `/accounts/{id}` deletion, `/health` warmup status, and `#accounts` UI actions.

### 2. Signatures
- API health check: `POST /accounts/{account_id}/test` returns `AccountHealthResponse` with HTTP 200 when the health-check request is handled, even when `ok` is false.
- API delete: `DELETE /accounts/{account_id}` returns `{"ok": true}` for a deleted account and HTTP 404 with `{"detail":{"type":"not_found",...}}` when the account does not exist.
- Readiness oracle: `GET /health` returns `warmup.status` plus target/completed/failed account lists.
- Frontend account view: `#accounts` calls `testAccount(a)` and `deleteAccount(a)` from `src/aistudio_api/static/app.js`.

### 3. Contracts
- `/accounts/{id}/test` is a local credential-shape check. Success means the storage state has non-expired Google cookies and AI Studio origin browser storage; it does not prove that the account can generate with any selected model.
- Real text generation readiness must come from successful account warmup in `GET /health` or an actual API/UI generation request for the selected model.
- A storage state with Google cookies but no `https://aistudio.google.com` origin storage is not GenerateContent-ready and must be treated as isolated until the user re-logs in or imports a Playwright storage state captured after AI Studio fully loads.
- Deleting an account must remove the account directory, remove the registry entry, keep `active_account_id` either null or pointing at an existing account, and invalidate any account client pool entry for that account.
- Account deletion tests and smoke runs must operate on a temporary copy of real account directories or synthetic smoke accounts. They must never delete from the source credential directory.
- Frontend success wording for `/accounts/{id}/test` must not imply real generation permission; it should distinguish credential check success from warmup/real request success.

### 4. Validation & Error Matrix
- Missing `auth.json` -> health check returns `ok=false`, status `missing_auth`, and the account is isolated.
- Expired Google cookies -> health check returns `ok=false`, status `expired`, and the account is isolated.
- Google cookies present but AI Studio origin storage missing -> health check returns `ok=false`, status `isolated`, with the AI Studio browser storage re-login/import message.
- Valid credential shape -> health check returns `ok=true`, status `healthy`, but generation permission remains delegated to warmup or real generation.
- Existing account deletion -> HTTP 200, directory removed, registry updated, client pool invalidated.
- Missing account deletion -> HTTP 404, no ASGI 500.
- Server log contains `Exception in ASGI application`, `NameError`, or `500 Internal Server Error` during account deletion smoke -> test fails.

### 5. Good/Base/Bad Cases
- Good: `DELETE /accounts/acc_smoke_delete_api` against a temporary accounts copy returns 200, removes that directory, and a second delete returns 404.
- Good: UI account deletion removes the row, emits no console errors, and no network response is 5xx.
- Base: `/accounts/{id}/test` returns HTTP 200 with `ok=false` for an isolated credential; UI shows the reason and does not treat it as ready for generation.
- Bad: Treating HTTP 200 from `/accounts/{id}/test` as evidence that `gemini-3.5-flash` generation will succeed.
- Bad: Running a delete-account system smoke against `/home/bamboo/nexus-studio/data/accounts` directly.

### 6. Tests Required
- Unit: `AccountStore.delete_account()` removes the directory and promotes or clears the active account safely.
- Unit/API: `DELETE /accounts/{id}` returns 200, invalidates `account_client_pool`, and leaves no account directory; repeat delete returns 404.
- Unit/static frontend: account health-check success text says credential check success and points generation permission to warmup/real requests.
- Real WSL API smoke: delete a temporary/synthetic account under a copied accounts directory and assert 200/404 controlled responses, registry consistency, and no server-log 500/ASGI exception.
- Real WSL UI smoke: open `#accounts`, run health check, delete a temporary/synthetic account row, assert row removal, no console errors, and no 5xx responses.
- Real readiness check: when generation smoke is blocked, record a safe preflight summary with account ids, boolean storage-shape fields, and no cookie/token/storage-state values.

### 7. Wrong vs Correct

#### Wrong
```python
# HTTP 200 only means the health-check endpoint handled the request.
response = client.post(f"/accounts/{account_id}/test")
assert response.status_code == 200
mark_generation_ready(account_id)
```

#### Correct
```python
response = client.post(f"/accounts/{account_id}/test")
body = response.json()
assert response.status_code == 200
assert body["ok"] is True

health = client.get("/health").json()
assert health["warmup"]["status"] == "complete"
```

#### Wrong
```bash
# Do not delete from the source credential directory during smoke tests.
export AISTUDIO_ACCOUNTS_DIR=/home/bamboo/nexus-studio/data/accounts
curl -X DELETE "http://127.0.0.1:8080/accounts/$ACCOUNT_ID"
```

#### Correct
```bash
rsync -a /home/bamboo/nexus-studio/data/accounts/ "$RUN_ROOT/data/accounts/"
export AISTUDIO_ACCOUNTS_DIR="$RUN_ROOT/data/accounts"
curl -X DELETE "http://127.0.0.1:$PORT/accounts/$SMOKE_ACCOUNT_ID"
```

---

## Scenario: AI Studio Native Generation Permission Boundary

### 1. Scope / Trigger
- Trigger: AI Studio text generation, warmup, capture, or replay changes that touch `BrowserSession`, `StreamingGateway`, request rewriting, account health, or real WSL system tests.
- Scope: account-backed text generation for `/v1/chat/completions`, native AI Studio UI sends, request-log oracles, and Camoufox/Playwright process boundaries.

### 2. Signatures
- Worker command: `python -m aistudio_api.infrastructure.gateway.native_ui_sender --worker`.
- One-shot diagnostic command: `python -m aistudio_api.infrastructure.gateway.native_ui_sender`.
- One-shot stdin JSON: `{"auth_file": str, "model": str, "prompt": str, "timeout_ms": int}`.
- Worker stdin JSONL request: `{"id": str, "payload": {"auth_file": str, "model": str, "prompt": str, "timeout_ms": int}}`.
- Worker stdout JSONL success: `{"id": str, "ok": true, "status": int, "body_b64": str, "body_size": int, "wire_model": str, "url_path": str}`.
- Worker stdout JSONL failure: `{"id": str, "ok": false, "error": str}`.
- Runtime env: `AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT` controls the per-account pool size, default `3`, minimum `1`.

### 3. Contracts
- Account-backed text-only API sends must decode the AI Studio wire body into `(model, prompt)` and send through the per-account `NativeUiWorkerPool` before any browser/context raw replay.
- Each stored account owns an isolated `AIStudioClient`; the client's `BrowserSession` owns one native UI worker pool for that account's auth file. Switching auth or closing the client must terminate the old pool.
- The worker pool must start up to `AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT` independent child processes. A worker child may reuse its own Camoufox browser/context and warmed page across requests, but it must remain outside the long-lived gateway hook/template browser process.
- Reusing a warmed native worker page must still navigate to a fresh AI Studio `new_chat` URL for every send. It may skip only the extra home-page prime on an already-warmed page; it must close and recreate the page after request failure, auth/context change, or worker close.
- `NativeUiWorkerPool.send()` and normal API `send_with_metadata()` calls must prefer the most recently successful worker so serial same-account requests stay on a hot worker/page. Startup warmup probes must pass `prefer_recent_worker=False` with `max_attempts=1` so every configured worker is probed and warmed in round-robin order.
- Worker release order is part of the contract: in normal hot-worker mode, successful sends return to the hot end and failed/restarted sends return to the cold end; in round-robin probe mode, every leased worker returns to the tail so the next probe can cover the next worker.
- Manual account activation and forced account rotation switch the shared browser auth state but must not invalidate the selected account's isolated `AccountClientPool` entry when that account's auth file is unchanged. Preserving the per-account `AIStudioClient` is required to preserve the startup-warmed native worker pool. Credential-changing paths such as login completion, credential import, and account deletion still must invalidate the affected account client.
- A worker child must create/use a context with `storage_state=auth_file` and `service_workers="block"`, navigate through configured AI Studio authuser chat routes, select the requested text model, fill the exact prompt, click Run/Send, and return the matched `GenerateContent` response body.
- Official AI Studio target-model navigation should open `/prompts/new_chat?model=<model>` when a target model is known, but URL initialization is not a sufficient oracle. The current Run settings/current model label must be read back and matched to the requested model before any generation sample is accepted.
- Account-backed async send paths must use a dedicated native worker executor sized to the worker count; they must not hold the main `_botguard_lock` or the single-thread hook/template Camoufox executor for the full native UI request.
- Response matching must require both the target wire model and a prompt marker from the requested prompt; unrelated `GenerateContent` or `CountTokens` responses are not acceptable oracles.
- Same-process native UI contexts may be used only when there is no account auth file, such as unit fakes or explicit unauthenticated diagnostics.
- Raw browser/context replay is a last fallback only when the native UI path cannot parse/send the request. A returned native UI HTTP status is authoritative and must not be overwritten by raw replay.
- Do not use `route.continue_(post_data=...)` to rewrite a production AI Studio generation request body.
- Environment inherited by the worker includes `AISTUDIO_PROXY_SERVER`, `AISTUDIO_CAMOUFOX_GEOIP`, `AISTUDIO_CAMOUFOX_HEADLESS`, and `AISTUDIO_AUTHUSER_CANDIDATES`.
- Worker subprocesses started from a source-tree server must prepend the repository `src` directory to `PYTHONPATH`; a successful editable install or parent-process `sys.path` mutation is not evidence that child workers can import `aistudio_api`.
- Real WSL native UI system tests must prove both Python HTTPS reachability to `https://aistudio.google.com/` and Camoufox `page.goto(..., wait_until="commit")` before starting the service. Direct network timeout, proxy CONNECT/TLS breakage, `NS_ERROR_NET_INTERRUPT`, or an `about:blank` browser page is an environment preflight failure, not a native worker readiness success.
- Startup/warmup probe timeout may be longer than normal request timeout, but account-native user requests must use the request timeout budget (`AISTUDIO_TIMEOUT_STREAM` / `AISTUDIO_TIMEOUT_REPLAY`) so account retry errors surface before Local Studio's outer HTTP client timeout masks them.
- Account startup warmup probes must use the same `NativeUiWorkerPool` boundary as account-native API sends. The probe must prewarm every configured worker context with `retry_statuses=(401, 403)` and only a validated real `GenerateContent` success from each worker can mark the pool ready. A fresh native/Camoufox context may return a recoverable first `403`; record it, retry in the same context once, and require a subsequent matched `200` before treating the worker as ready.

### 4. Validation & Error Matrix
- Missing `auth_file` path -> worker returns failure and parent falls back only if a non-UI replay path is explicitly allowed by the caller.
- Wire body cannot decode to a text-only prompt -> native UI send is unavailable; browser replay may be attempted as a compatibility fallback.
- Text request includes inline/file parts -> native UI send rejects with `native UI replay fallback only supports text-only requests`.
- Native UI matched response status `401`, `403`, or `429` -> propagate that upstream status and body to normal error classification; do not retry raw replay first.
- Startup warmup probe sees first-request `401` or `403` from one native UI worker -> retry that same worker/context once; if it still returns a retryable status, fail warmup with the last upstream status/body rather than marking the account ready.
- Worker returns request failure while staying alive, such as a cold AI Studio page not yet exposing the target model picker -> parent retries another pool worker without restarting the process; request-failure retries should cover the configured worker count.
- Worker returns `AI Studio text model not selected in native UI sender` with `current_text_model_not_found` or a model picker opened as `text_category` -> parent restarts that worker before returning it to the pool and retries another worker. Startup warmup must treat this as recoverable UI state while there is remaining worker coverage budget, not as account permission failure.
- Worker request failure says `TargetClosedError` or `Target page, context or browser has been closed` -> parent restarts that worker before returning it to the pool, because the Playwright context/page boundary is no longer trustworthy.
- Worker exits, times out, emits malformed JSON, or returns malformed base64 body -> parent restarts that worker and retries; if still failing, surface the worker failure to the normal fallback/error path.
- Worker pool size changes or auth file changes -> close the old pool and create a new pool before the next account-backed native send.
- Startup warmup uses the default recent-worker preference -> the same worker can be probed repeatedly while other configured workers stay cold; readiness and performance evidence are invalid.
- Serial Local Studio Google performance samples lease different idle workers after the first success -> the pool is rotating cold pages through a performance path and may fail the official-vs-local latency budget.
- Request-log oracle sees `models/gemini-3-flash-preview` for a `gemini-3.5-flash` request -> system test must fail.
- WSL Python HTTPS or Camoufox navigation cannot reach AI Studio -> write a safe `network-preflight.safe.json` artifact and fail the system test before service startup; do not continue to `/health`, model listing, API chat, or host UI assertions.

### 5. Good/Base/Bad Cases
- Good: repeated `gemini-3.5-flash` API requests use the same per-account worker pool, matched native UI responses return `200`, and request logs show `models/gemini-3.5-flash` with no auth/rate status.
- Good: serial Local Studio Google performance samples lease the same recently successful worker and reuse its warmed page, while concurrent same-account calls can still lease additional workers.
- Good: two same-account text requests may lease two different workers when global/API concurrency allows it.
- Good: official direct baseline opens `https://aistudio.google.com/u/0/prompts/new_chat?model=gemini-3.5-flash`, reads back `Gemini 3.5 Flash` from the visible current model control, and accepts only samples whose request body wire model is `models/gemini-3.5-flash` and whose assistant text appears in the page.
- Good: account startup warmup gets a transient `403` from a fresh worker context, retries that same worker/context, validates a real `200` `GenerateContent` response, repeats this for each configured worker, and marks the account ready only after all configured worker contexts are prewarmed.
- Good: account startup warmup sees one worker stuck on an AI Studio text-category/model-readback page, restarts that worker, continues probing the remaining workers with `max_attempts=1`, and marks the account ready only after the configured number of successful worker probes.
- Base: Account startup warmup probes the configured warmup text model through that account's `NativeUiWorkerPool`; successful native worker `GenerateContent` is the account-backed text readiness gate. Legacy template capture may still run for raw replay fallback compatibility, but it must not mark an otherwise worker-ready account as failed.
- Base: every native worker returns `403`; the last upstream body is surfaced as an `AuthError`, the account warmup fails, and no raw replay fallback is used to mask the permission failure.
- Bad: official baseline clicks or sees a model picker card and records `selected=true` while the right-side current model still shows a different model.
- Bad: Same-process isolated context sends the API prompt after template capture and receives AI Studio `403`, even though the same account/model/prompt succeeds in a standalone clean Camoufox process.
- Bad: every request starts a fresh helper process, losing worker reuse and adding cold-start latency.
- Bad: every account-backed native request opens and closes a fresh AI Studio page after warmup, so Local Studio first visible text is dominated by page/model-selection startup while the official baseline reuses a visible page.
- Bad: FIFO worker-pool leasing sends three serial performance samples to workers 0, 1, and 2, forcing each sample to pay cold page/model-selection cost even though a hot worker exists.

### 6. Tests Required
- Unit: account-backed `_send_native_generate_content_body_sync` creates/reuses `NativeUiWorkerPool`, passes auth/model/prompt/timeout, and decodes returned bytes.
- Unit: startup `probe_native_worker_generate_content` calls `NativeUiWorkerPool.send_with_metadata(..., max_attempts=1, retry_statuses=(401, 403))` once per configured worker and still validates the returned status/model before readiness.
- Unit: native sender and session model-selection tests must simulate a clicked target card while the current model readback remains mismatched; selection must fail or continue retrying until current model readback matches.
- Unit: native sender worker tests must prove same-auth requests reuse the warmed page, warmed-page sends still call fresh `new_chat` navigation with `prime_home=False`, and a failed request closes the page so the next request recreates it.
- Unit: native worker pool tests must prove serial sends prefer the most recently successful worker, round-robin/probe mode can cover every worker, retryable statuses retry the same worker once before moving on, and the startup warmup probe passes `prefer_recent_worker=False`.
- Unit: native worker pool and session readiness tests must prove `current_text_model_not_found`/`text_category` model-selection failures restart the bad worker and allow startup warmup to continue probing other workers without masking hard 401/403 permission failures.
- Unit: native worker pool reuses workers, leases multiple workers under concurrent calls, retries request-level UI failures without restart across configured workers, restarts and retries after process/protocol failure, and closes workers on pool close.
- Unit: account-backed `send_hooked_request` and `send_streaming_request` use the dedicated native worker executor before the main browser replay path.
- Unit: switching auth closes the old native worker pool before creating a new one.
- Unit: no-auth/fake sessions still use the in-process clean context path without installing transport init scripts.
- Unit: invalid/non-wire bodies still exercise browser fetch/context request fallback.
- Unit: config route exposes `AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT` with default `3` and rejects invalid values.
- Unit: request-level account-native send timeouts remain bounded by the configured request timeout and do not inherit `AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS`.
- Full unit suite: run `pytest tests/unit -q` after changing gateway replay, warmup, capture, or model rewrite behavior.
- Real WSL system test: run the current task's `system-test-wsl.sh`; require source-tree import evidence, `NETWORK_PREFLIGHT_OK`, `NATIVE_WORKER_PREFLIGHT_OK`, native worker matched `GenerateContent` log evidence, Windows host Playwright UI artifacts, and `SYSTEM_TEST_PASS`. If `network_preflight_unavailable` appears, the result is an explicit environment blocker rather than a passed or assumed-available native worker test.

### 7. Wrong vs Correct

#### Wrong
```python
# Same process/context after hook/template capture can return AI Studio 403.
context = self._new_context_sync(install_init_scripts=False)
status, raw = send_prompt_through_native_ui(context, model, prompt)
if status == 403:
	status, raw = self._browser_fetch_generate_content_sync(body=body, url=url, headers=headers, timeout_ms=timeout_ms)
```

#### Correct
```python
# Account-backed text generation crosses a process boundary and reuses the per-account pool.
model, prompt = self._native_text_replay_payload_from_body(body)
status, raw = self._send_native_generate_content_worker_pool_sync(
	model=model,
	prompt=prompt,
	timeout_ms=timeout_ms,
	retry_statuses=(401, 403),
)
return status, raw
```

---

## Scenario: Real System Test Environment And Visible UI Oracles

### 1. Scope / Trigger
- Trigger: Changes that touch account-backed generation, gateway routing/replay, browser automation, Local Studio UI, request logs, provider/model selection, or system-test scripts.
- Scope: `SYSTEM_TEST_PLAN.md`, WSL real-system smoke scripts, MCP/Playwright UI verification, native AI Studio UI worker warmup, and Local Studio user-facing workflows.

### 2. Signatures
- Required report artifacts: `artifacts/mcp-visible-ui-results.json`, `artifacts/performance-comparison-results.json`, `artifacts/api-results.json`, `artifacts/ui-results.json`, `artifacts/server.log`.
- Required environment evidence files: `source-commit.txt`, `source-status.txt`, `test-copy-commit.txt`, `test-copy-status.txt`.
- Required data-directory env keys: `AISTUDIO_LOCAL_STUDIO_DIR`, `AISTUDIO_REQUEST_LOGS_DIR`, `AISTUDIO_GENERATED_IMAGES_DIR`, `AISTUDIO_IMAGE_SESSIONS_DIR`, `AISTUDIO_PROVIDER_MANAGER_DIR`.

### 3. Contracts
- Full real system tests must run from a fresh WSL temporary copy, not the developer workspace or a reused dev server.
- The temporary copy must preserve enough Git metadata to record commit and clean-status evidence.
- Writable test data directories must point under the current run root. Account edit/delete tests must use a copied account directory, never the source real credential directory.
- P0/P1 UI pass evidence must include a visible MCP browser-tool user path: navigation, clicks, input, model/provider selection, send, visible wait state, final visible result/error, snapshot or screenshot, console summary, and network summary.
- Host Playwright UI smoke that is intended to satisfy P0/P1 visible UI coverage must launch headed (`headless=false`) and record the browser mode in `mcp-visible-ui-results.json`; headless runs are diagnostic or bulk automation only.
- Basic headed smoke is not full plan coverage. If the script covers only a subset of `SYSTEM_TEST_PLAN.md`, it must record `coverage_scope`, `covered_plan_items`, and `known_missing_plan_items`, write a plan-script alignment artifact, and exit incomplete/fail instead of emitting `SYSTEM_TEST_PASS`.
- Plan-script alignment must separate matrix mapping completeness from case success. Every required `SYSTEM_TEST_PLAN.md` P0/P1 row must appear exactly once with `status=pass`, `status=fail`, or `status=not_applicable` plus evidence; broad buckets such as `complete_P0_P1_UI_matrix` or `G-LS-02-through-G-LS-11` are not valid row mappings.
- Visible UI assertions must match how the user-visible state is represented. Text nodes should use text assertions; form controls must assert `value`, `checked`, `selected`, or disabled state instead of relying on `has_text`, because input values are not visible text content.
- Direct DOM/Alpine mutation, `page.evaluate()` state injection, localStorage preloading, static DOM checks, or API-only success can be diagnostic aids only. They cannot replace user-path UI pass evidence.
- The served UI must not create default browser resource 404s such as `/favicon.ico`; strict console-error gates should catch these, and the product should serve or intentionally 204 the resource rather than ignore the console error.
- Google AI Studio account-backed text tests must compare Local Studio user-visible first-token and completion latency against direct official AI Studio web UI in the same network/account/model class.
- OpenAI-compatible real-system model probes must validate the sentinel response text, not only a 2xx `/responses` status, before selecting a model for later API/UI smoke steps.
- Local Studio upstream transport failures must surface a diagnostic message with the `httpx` exception type and fallback text when the exception string is empty; blank 502 responses or empty request-log response bodies are not acceptable evidence.
- Native GenerateContent success evidence may come from child sender stderr (`native_ui_sender stage=send.response_matched`) or parent worker-pool logs (`AI Studio native UI worker replay matched response` with status 200 and requested wire model); system-test oracles must accept both emitted forms.

### 4. Validation & Error Matrix
- Dirty source or test copy -> system test fails before service startup.
- Service command started from the developer workspace or connects to an old dev server -> system test fails.
- P0/P1 UI result has no MCP-visible user path evidence -> UI coverage is incomplete and system test fails.
- Host UI smoke result records `browser.headless == true` -> UI coverage is incomplete and system test fails, even when API calls and screenshots exist.
- Plan-script alignment artifact contains any required P0/P1 case with `status=fail`, `not_covered`, or `incomplete` -> system test result is `SYSTEM_TEST_INCOMPLETE` or fail, never `SYSTEM_TEST_PASS`.
- Plan-script alignment artifact has `unmapped_required_cases` or duplicate required IDs -> result is an alignment failure. If `unmapped_required_cases=[]` but some rows fail, the result is concrete required-case failure, not incomplete mapping.
- Browser console contains a resource 404 such as `/favicon.ico` -> fix the app/static route or asset; do not suppress the console error in the system-test verdict.
- UI runner waits for text stored only in an `<input>`, `<select>`, checkbox, or toggle value -> assertion is invalid; change the runner to assert the control state directly and rerun a clean real-system test.
- UI path succeeds only after internal state injection -> mark `diagnostic_pass_after_patch` or diagnostic-only, not `SYSTEM_TEST_PASS`.
- Official AI Studio direct UI is unreachable -> mark environment/model-selection blocker; do not declare Local Studio latency pass.
- Local Studio first-token or completion latency exceeds `SYSTEM_TEST_PLAN.md` budget -> performance failure even if final text is correct.
- OpenAI-compatible `/responses` probe returns HTTP 200 without the expected sentinel text -> do not select that model; continue probing candidates or fail with a safe model-probe artifact.
- Local Studio OpenAI-compatible upstream request raises an empty `httpx.HTTPError` -> return/log a diagnostic such as `ReadError: upstream request failed without an error message`, not an empty upstream-error message.

### 5. Good/Base/Bad Cases
- Good: A WSL run creates `/home/bamboo/nexus-studio-system-test-*`, installs a fresh venv, starts the service from that copy, uses MCP browser tools to perform Local Studio user actions, records request-log group ids, and emits `SYSTEM_TEST_PASS` only after all P0 gates pass.
- Base: A headless Playwright script supplements MCP-visible UI coverage with bulk assertions, but the report still identifies the corresponding visible MCP path.
- Base: A basic headed Local Studio smoke passes API/provider checks and writes useful evidence, but also writes a coverage-gap artifact and exits `SYSTEM_TEST_INCOMPLETE` until the complete plan matrix is covered.
- Bad: The only Local Studio UI proof is `playwright.chromium.launch(headless=True)` plus screenshots; users cannot see the browser path and it cannot satisfy the visible UI gate.
- Bad: A basic headed UI smoke sends one Google message and one OpenAI-compatible message, then reports `SYSTEM_TEST_PASS` while `SYSTEM_TEST_PLAN.md` P0/P1 rows remain unmapped.
- Bad: A runner reports `missing_required_coverage=["complete ui-results.json P0/P1 matrix"]` without enumerating each required plan row and its evidence.
- Bad: A headed browser loads the app and completes chat but the console contains a favicon 404; do not mark the UI gate passed until the static resource behavior is fixed.
- Bad: A script sets Alpine fields with `page.evaluate()`, clicks send, and claims this proves the provider/model picker user path works.
- Bad: A local temporary code patch makes a smoke pass, but the clean checkout is never rerun.

### 6. Tests Required
- Documentation/system-plan updates: run `git diff --check` and markdown diagnostics for changed docs.
- System-test harness updates: add hard-fail assertions for every expected/oracle field recorded in result JSON.
- System-test harness updates: add or update plan-script alignment artifacts whenever `SYSTEM_TEST_PLAN.md` coverage changes; every required row must map to pass/fail/not-applicable-with-evidence before any pass verdict.
- System-test harness updates: include a contract test or equivalent static check that the runner has an explicit required-case registry, emits `unmapped_required_cases`, and does not use broad missing matrix buckets.
- System-test harness updates: OpenAI-compatible model selection must use a sentinel-output oracle and record the model-probe results in a safe artifact; transient 5xx from the external compatible service may be retried but must remain visible in `api-results.json`.
- Host UI smoke updates: assert `browser.headless` is false for P0/P1 visible UI coverage and keep strict console-error checks enabled.
- Host UI smoke updates: when validating form-driven UI such as provider/model editors, assert rendered control values with Playwright value/checked/selected assertions and keep a screenshot or result artifact for that UI state.
- Account/gateway/UI code updates: run unit tests plus WSL clean-copy API and MCP-visible UI real-system tests as required by `SYSTEM_TEST_PLAN.md`.
- Performance-sensitive Google text changes: record official AI Studio direct UI and Local Studio UI first-token/completion samples in `performance-comparison-results.json`.

### 7. Wrong vs Correct

#### Wrong
```python
await page.evaluate("""
() => {
	const root = document.querySelector('[x-data]')._x_dataStack[0]
	root.localStudioProviderId = 'google-ai-studio'
	root.localStudioModel = 'gemini-3.5-flash'
}
""")
mark_ui_passed()
```

#### Correct
```text
Use MCP browser tools to open the WSL Local Studio URL, click the visible provider selector,
choose Google AI Studio, open the visible model selector, choose the requested model, send
the prompt, record the first visible assistant token, completion state, screenshot/snapshot,
console/network summary, and request-log group id.
```
