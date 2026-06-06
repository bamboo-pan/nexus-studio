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
- The worker pool must start up to `AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT` independent child processes. A worker child may reuse its own Camoufox browser/context across requests, but it must remain outside the long-lived gateway hook/template browser process.
- A worker child must create/use a context with `storage_state=auth_file` and `service_workers="block"`, navigate through configured AI Studio authuser chat routes, select the requested text model, fill the exact prompt, click Run/Send, and return the matched `GenerateContent` response body.
- Account-backed async send paths must use a dedicated native worker executor sized to the worker count; they must not hold the main `_botguard_lock` or the single-thread hook/template Camoufox executor for the full native UI request.
- Response matching must require both the target wire model and a prompt marker from the requested prompt; unrelated `GenerateContent` or `CountTokens` responses are not acceptable oracles.
- Same-process native UI contexts may be used only when there is no account auth file, such as unit fakes or explicit unauthenticated diagnostics.
- Raw browser/context replay is a last fallback only when the native UI path cannot parse/send the request. A returned native UI HTTP status is authoritative and must not be overwritten by raw replay.
- Do not use `route.continue_(post_data=...)` to rewrite a production AI Studio generation request body.
- Environment inherited by the worker includes `AISTUDIO_PROXY_SERVER`, `AISTUDIO_CAMOUFOX_GEOIP`, `AISTUDIO_CAMOUFOX_HEADLESS`, and `AISTUDIO_AUTHUSER_CANDIDATES`.

### 4. Validation & Error Matrix
- Missing `auth_file` path -> worker returns failure and parent falls back only if a non-UI replay path is explicitly allowed by the caller.
- Wire body cannot decode to a text-only prompt -> native UI send is unavailable; browser replay may be attempted as a compatibility fallback.
- Text request includes inline/file parts -> native UI send rejects with `native UI replay fallback only supports text-only requests`.
- Native UI matched response status `401`, `403`, or `429` -> propagate that upstream status and body to normal error classification; do not retry raw replay first.
- Worker returns request failure while staying alive, such as a cold AI Studio page not yet exposing the target model picker -> parent retries another pool worker without restarting the process; request-failure retries should cover the configured worker count.
- Worker exits, times out, emits malformed JSON, or returns malformed base64 body -> parent restarts that worker and retries; if still failing, surface the worker failure to the normal fallback/error path.
- Worker pool size changes or auth file changes -> close the old pool and create a new pool before the next account-backed native send.
- Request-log oracle sees `models/gemini-3-flash-preview` for a `gemini-3.5-flash` request -> system test must fail.

### 5. Good/Base/Bad Cases
- Good: repeated `gemini-3.5-flash` API requests use the same per-account worker pool, matched native UI responses return `200`, and request logs show `models/gemini-3.5-flash` with no auth/rate status.
- Good: two same-account text requests may lease two different workers when global/API concurrency allows it.
- Base: Account startup warmup probes the configured warmup text model through that account's `NativeUiWorkerPool`; successful native worker `GenerateContent` is the account-backed text readiness gate. Legacy template capture may still run for raw replay fallback compatibility, but it must not mark an otherwise worker-ready account as failed.
- Bad: Same-process isolated context sends the API prompt after template capture and receives AI Studio `403`, even though the same account/model/prompt succeeds in a standalone clean Camoufox process.
- Bad: every request starts a fresh helper process, losing worker reuse and adding cold-start latency.

### 6. Tests Required
- Unit: account-backed `_send_native_generate_content_body_sync` creates/reuses `NativeUiWorkerPool`, passes auth/model/prompt/timeout, and decodes returned bytes.
- Unit: native worker pool reuses workers, leases multiple workers under concurrent calls, retries request-level UI failures without restart across configured workers, restarts and retries after process/protocol failure, and closes workers on pool close.
- Unit: account-backed `send_hooked_request` and `send_streaming_request` use the dedicated native worker executor before the main browser replay path.
- Unit: switching auth closes the old native worker pool before creating a new one.
- Unit: no-auth/fake sessions still use the in-process clean context path without installing transport init scripts.
- Unit: invalid/non-wire bodies still exercise browser fetch/context request fallback.
- Unit: config route exposes `AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT` with default `3` and rejects invalid values.
- Full unit suite: run `pytest tests/unit -q` after changing gateway replay, warmup, capture, or model rewrite behavior.
- Real WSL system test: run the current task's `system-test-wsl.sh` and require `WARMUP_COMPLETE`, `API_STREAM_OK`, `UI_STREAM_OK`, `REQUEST_LOG_ORACLE_OK`, `WORKER_POOL_ORACLE_OK`, and `SYSTEM_TEST_PASS`.

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
)
return status, raw
```
