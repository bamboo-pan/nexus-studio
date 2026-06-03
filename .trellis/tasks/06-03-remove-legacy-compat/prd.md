# Remove legacy compatibility versions

## Goal

Remove historical state-format compatibility paths and keep only the latest credential and frontend storage formats. The user will delete old WSL accounts and add them again, so this task should not preserve old auth files or old browser/localStorage migration behavior.

## What I already know

* The current account backup package format is `aistudio-api.credentials.backup` with `version` 1.
* Fresh account storage state must preserve Playwright `indexedDB` data when present.
* The previous recommendation was to re-login because old WSL account states likely do not contain the newer IndexedDB authorization data.
* The user explicitly allows removing old accounts and re-adding them.
* Real account validation can be skipped for this task.

## Requirements

* Remove automatic migration from legacy `data/auth.json` into the multi-account registry.
* Remove legacy credential import aliases and accept only the latest backup account `auth` field plus direct Playwright storage state.
* Remove frontend reads/writes that keep old localStorage field or provider schema compatibility.
* Keep current public API compatibility surfaces such as OpenAI-compatible, Responses, Gemini, and Claude interfaces.
* Update unit/static tests to assert the latest-only behavior.

## Acceptance Criteria

* [ ] Creating `AccountStore` no longer imports a root `data/auth.json` automatically.
* [ ] Credential backup import rejects old `storage_state` / `storageState` account fields and accepts only `auth`.
* [ ] Frontend static tests no longer expect legacy `aistudio.apiSelection.v1` or old Local Studio provider profile fallback handling.
* [ ] Focused unit tests pass without using real Google accounts.

## Verification Plan

* Run focused unit tests covering account credentials, account health/selection, and static frontend capability contracts.
* Skip real WSL account/API/UI smoke validation by user instruction because accounts will be deleted and re-added.

## Out of Scope

* Deleting real account files under `data/accounts`.
* Real AI Studio GenerateContent / warmup validation with live credentials.
* Removing current OpenAI/Gemini/Claude compatibility APIs.

## Technical Notes

* Relevant backend implementation: `src/aistudio_api/infrastructure/account/account_store.py`.
* Relevant frontend implementation: `src/aistudio_api/static/app.js`.
* Relevant tests: `tests/unit/test_account_credentials.py`, `tests/unit/test_static_frontend_capabilities.py`.
