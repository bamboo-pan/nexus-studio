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
