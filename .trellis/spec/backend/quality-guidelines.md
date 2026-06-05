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

## Scenario: AI Studio Native Generation Permission Boundary

### 1. Scope / Trigger
- Trigger: AI Studio text generation, warmup, capture, or replay changes that touch `BrowserSession`, `StreamingGateway`, request rewriting, account health, or real WSL system tests.
- Scope: account-backed text generation for `/v1/chat/completions`, native AI Studio UI sends, request-log oracles, and Camoufox/Playwright process boundaries.

### 2. Signatures
- Internal helper command: `python -m aistudio_api.infrastructure.gateway.native_ui_sender`.
- Helper stdin JSON: `{"auth_file": str, "model": str, "prompt": str, "timeout_ms": int}`.
- Helper stdout JSON success: `{"ok": true, "status": int, "body_b64": str, "body_size": int, "wire_model": str, "url_path": str}`.
- Helper stdout JSON failure: `{"ok": false, "error": str}` with a non-zero exit code.

### 3. Contracts
- Account-backed text-only API sends must decode the AI Studio wire body into `(model, prompt)` and send through the native UI helper subprocess before any browser/context raw replay.
- The helper must launch a fresh Camoufox process, create a context with `storage_state=auth_file` and `service_workers="block"`, navigate through configured AI Studio authuser chat routes, select the requested text model, fill the exact prompt, click Run/Send, and return the matched `GenerateContent` response body.
- Response matching must require both the target wire model and a prompt marker from the requested prompt; unrelated `GenerateContent` or `CountTokens` responses are not acceptable oracles.
- Same-process native UI contexts may be used only when there is no account auth file, such as unit fakes or explicit unauthenticated diagnostics.
- Raw browser/context replay is a last fallback only when the native UI path cannot parse/send the request. A returned native UI HTTP status is authoritative and must not be overwritten by raw replay.
- Do not use `route.continue_(post_data=...)` to rewrite a production AI Studio generation request body.
- Environment inherited by the helper includes `AISTUDIO_PROXY_SERVER`, `AISTUDIO_CAMOUFOX_GEOIP`, `AISTUDIO_CAMOUFOX_HEADLESS`, and `AISTUDIO_AUTHUSER_CANDIDATES`.

### 4. Validation & Error Matrix
- Missing `auth_file` path -> helper returns failure and parent falls back only if a non-UI replay path is explicitly allowed by the caller.
- Wire body cannot decode to a text-only prompt -> native UI send is unavailable; browser replay may be attempted as a compatibility fallback.
- Text request includes inline/file parts -> native UI send rejects with `native UI replay fallback only supports text-only requests`.
- Native UI matched response status `401`, `403`, or `429` -> propagate that upstream status and body to normal error classification; do not retry raw replay first.
- Helper produces no JSON or malformed base64 body -> parent raises a helper-result error instead of silently returning an empty response.
- Request-log oracle sees `models/gemini-3-flash-preview` for a `gemini-3.5-flash` request -> system test must fail.

### 5. Good/Base/Bad Cases
- Good: `gemini-3.5-flash` API request uses a clean helper process, matched native UI response returns `200`, and request logs show `models/gemini-3.5-flash` with no auth/rate status.
- Base: Warmup probes native `GenerateContent` with the configured warmup model before template capture, then captures a template for the same requested text model.
- Bad: Same-process isolated context sends the API prompt after template capture and receives AI Studio `403`, even though the same account/model/prompt succeeds in a standalone clean Camoufox process.

### 6. Tests Required
- Unit: account-backed `_send_native_generate_content_body_sync` invokes `python -m aistudio_api.infrastructure.gateway.native_ui_sender`, passes auth/model/prompt/timeout over stdin, and decodes `body_b64`.
- Unit: no-auth/fake sessions still use the in-process clean context path without installing transport init scripts.
- Unit: invalid/non-wire bodies still exercise browser fetch/context request fallback.
- Full unit suite: run `pytest tests/unit -q` after changing gateway replay, warmup, capture, or model rewrite behavior.
- Real WSL system test: run `.trellis/tasks/06-04-fix-aistudio-permission-relogin/system-test-wsl.sh` and require `WARMUP_COMPLETE`, `API_STREAM_OK`, `UI_STREAM_OK`, `REQUEST_LOG_ORACLE_OK`, and `SYSTEM_TEST_PASS`.

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
# Account-backed text generation crosses a process boundary before raw replay.
model, prompt = self._native_text_replay_payload_from_body(body)
status, raw = self._send_native_generate_content_subprocess_sync(
	model=model,
	prompt=prompt,
	timeout_ms=timeout_ms,
)
return status, raw
```
