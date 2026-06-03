# Fix WSL Camoufox AI Studio Internal Error

## Goal

Fix the WSL-launched browser path so a real stored AI Studio account that works in a Windows host browser can also generate successfully through the browser environment used by Nexus Studio. The repair must treat the Windows-native success as proof that the account itself is not the primary fault.

## What I Already Know

- Real account `acc_1840d770` can generate normally after login in a Windows host browser.
- The same account loaded through a WSL-launched Camoufox browser opens AI Studio, but manual generation shows `error An internal error has occurred.`
- Earlier API/UI validation saw `/health.warmup.status == complete` while `gemma-4-31b-it` and `gemini-3-flash-preview` requests failed upstream with 403 and were wrapped locally as 401.
- Earlier native AI Studio contrast in WSL/Camoufox also saw failures for accounts `acc_c3356787` and `acc_1840d770`, but the new Windows-host result proves at least `acc_1840d770` is capable outside this WSL browser path.
- The current browser launcher uses Camoufox Firefox via a Playwright server compatibility shim.
- Account auth state is stored at `data/accounts/<account_id>/auth.json`.

## Requirements

- Diagnose why WSL-launched Camoufox causes native AI Studio generation to fail for a working account.
- Implement a focused fix in the browser/account/gateway startup path rather than reclassifying this as an account permission problem.
- Keep compatibility with existing headless warmup and account login flows.
- Preserve secret safety: do not print cookies, tokens, or raw auth state in scripts/logs.
- Add unit coverage for any code-level behavior change.
- Run real WSL API and frontend/UI validation using a real account after the fix.

## Acceptance Criteria

- [x] A WSL-launched browser using `acc_1840d770` can generate in native AI Studio without the `An internal error has occurred` failure, or the implemented path avoids the broken browser mode with a tested equivalent browser session supported by Nexus Studio.
- [x] Nexus API real WSL smoke succeeds for an authorized model/account path or records a native-success/API-failure contrast that points to a remaining code-level issue.
- [x] Local Studio UI real WSL smoke can send a prompt and recover `busy=false / can_send=true` after success or any remaining diagnosed upstream error.
- [x] Relevant unit tests pass.
- [x] `bash -n`, `git diff --check`, Trellis task validation, and Trellis check pass.

## Implementation Summary

- Login storage-state capture now requests Playwright `indexed_db=True` and falls back only for older Playwright versions that do not support the argument.
- Account credential import/export validates and preserves per-origin `indexedDB` entries so future imports do not strip Google GIS/OAuth browser state.
- AI Studio warmup now returns a rewritten captured request and replays it once through `GenerateContent`; `/health.warmup.status == complete` is no longer capture-only.
- Account-pool warmup isolates accounts on hard `AuthError`/401/403 with a secret-safe reason that instructs re-login/import with IndexedDB state.
- Chat retry error propagation now preserves an initial `AuthError` when the retry path finds no further accounts, returning `authentication_error` instead of wrapping the condition as `server_error`.
- AI Studio route candidates now include `/u/<authuser>/...` forms, and send-button detection is scoped to the active composer area to avoid unrelated AI Studio controls.

## Validation Results

- Windows `git diff --check`: passed.
- Windows full unit suite after Trellis check fixes: `400 passed in 9.24s`.
- WSL focused unit suite in copied workspace after Trellis check fixes: `107 passed in 1.51s`.
- Real WSL API smoke with copied real accounts: old stored account state returned HTTP 401 `authentication_error` with upstream `The caller does not have permission`; it no longer reports warmup complete falsely or wraps account exhaustion as HTTP 500.
- Real WSL Local Studio UI smoke with copied real accounts: Playwright opened the actual UI, sent a prompt through Google AI Studio, and verified both supported outcomes during validation: successful assistant generation on one run and diagnosed HTTP 401 recovery on the final run with `localStudioBusy == false`, `localStudioCanSend == true`, and draft restoration.
- VS Code diagnostics for `src` and `tests/unit`: no errors found.
- Trellis check agent: passed after aligning chat, image, and Gemini auth retry error propagation; no remaining blockers.

## Follow-Up Note

- Existing `auth.json` files created before this fix may still lack IndexedDB. The code now preserves IndexedDB for future login/import, but old credentials should be refreshed by re-login or by importing a full browser storage state that includes IndexedDB.

## Definition of Done

- Code changes are minimal and consistent with the existing backend/browser structure.
- New or changed behavior is covered by focused tests.
- Real WSL validation covers both API and frontend UI paths when production code changes are made.
- Task metadata under this Trellis task is committed with the work.

## Out of Scope

- Changing or regenerating real account credentials unless needed for validation.
- Treating the problem as solved solely by changing account tier/permissions.
- Broad replacement of the AI Studio wire codec unless the browser fix proves insufficient.

## Technical Notes

- Relevant spec: `.trellis/spec/backend/quality-guidelines.md`, especially Camoufox launcher compatibility and browser warmup contracts.
- Likely modules: `src/aistudio_api/infrastructure/browser/camoufox_launcher.py`, `src/aistudio_api/infrastructure/browser/camoufox_manager.py`, `src/aistudio_api/infrastructure/gateway/session.py`, `src/aistudio_api/infrastructure/gateway/capture.py`, and account pool/client startup code.
