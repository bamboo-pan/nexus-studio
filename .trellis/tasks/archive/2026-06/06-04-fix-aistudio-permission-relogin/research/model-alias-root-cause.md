# AI Studio permission relogin root cause

## Symptom

The failing request was submitted as `gemini-3.5-flash`, but the captured/replayed AI Studio wire request used `models/gemini-3-flash-preview`. The upstream response then returned `[,[7,"The caller does not have permission"]]`.

## Root causes

1. The gateway had an unsafe AI Studio wire alias that rewrote `gemini-3.5-flash` to `gemini-3-flash-preview`. The registry already exposes both models as separate text models, so silently sending the preview model changed the user's requested upstream contract.
2. The original failing path was `stream=True`. For SSE responses, upstream `AuthError` can happen after FastAPI has already returned `StreamingResponse`; the outer account retry handler cannot catch errors raised inside the response generator. Account fallback therefore has to live inside the SSE generator and can only switch accounts before any irreversible content chunk is emitted.

## Fix

- Removed the unsafe model alias so `gemini-3.5-flash` is encoded as `models/gemini-3.5-flash` on the AI Studio wire.
- Updated request rewriter regression tests so capture and replay preserve `gemini-3.5-flash` instead of preview.
- Added one pre-content account replacement attempt inside both OpenAI-compatible and Gemini-native SSE builders. The failed account is recorded as an error, a replacement account is leased excluding the failed account, and the old lease is released only after replacement acquisition.

## Verification oracle

Request logs can contain raw/captured headers and browser cookies. System-test evidence must not export or print raw request-log entries. Safe evidence is limited to:

- final upstream AI Studio body model, especially `body_json[0]`
- status codes
- short response-body prefixes with no headers, cookies, storage state, or credentials
- account counts and health statuses without emails or auth material

The task system test must fail if any upstream request for the test chain uses `models/gemini-3-flash-preview`. If the upstream body is `models/gemini-3.5-flash` but AI Studio still returns 401/403/429, that is a real upstream account/model authorization result rather than the old alias bug.

## Focused unit tests run

- `tests/unit/test_request_rewriter.py`
- `tests/unit/test_gateway_replay_request_contract.py`
- `tests/unit/test_model_capabilities.py`
- `tests/unit/test_account_health_and_selection.py`
- `tests/unit/test_streaming_stability.py`
- `tests/unit/test_gemini_native_routes.py`
- `tests/unit/test_openai_compatibility.py`

## Follow-up: account test and delete-account gaps

The user reported that account health checks returned HTTP 200 while startup logged both accounts as isolated. The reason is that `POST /accounts/{id}/test` is an HTTP endpoint that returns an `AccountHealthResponse`; HTTP 200 means the health-check request was handled, not that the account can generate with a selected model. Its positive result only validates local credential shape: Google cookies are present, unexpired, and AI Studio origin browser storage exists. Real generation readiness must be proved by startup/account warmup (`GET /health` warmup status) or an actual API/UI generation request for the selected model.

The delete-account 500 was a separate uncovered regression: `AccountStore.delete_account()` called `shutil.rmtree()` without importing `shutil`. Existing unit tests covered account import/export/health/update paths, but not store deletion or `DELETE /accounts/{id}` through FastAPI, and the previous real UI smoke opened the accounts page without deleting a copied account.

Added regression coverage:

- Unit: `AccountStore.delete_account()` removes the account directory and promotes the next active account.
- Unit: `DELETE /accounts/{id}` returns `{"ok": true}`, removes the account directory, and invalidates the account client pool.
- Static frontend: the account test toast says credential check success, with generation permission delegated to warmup/real requests.
- System plan: `BASE-ACC-02` and `API-ACC-01` require account deletion in a WSL temporary account directory copy and treat `/accounts/{id}/test` as insufficient for generation readiness.
- Real WSL smoke result recorded in `system-test-results/account-delete.safe.json`: temporary smoke accounts were created in a copied account directory, one was tested/deleted through HTTP API and one through the browser UI, with no console errors, no 5xx responses, no ASGI exception, and no `NameError`/`shutil` server log signature.
- Real account readiness preflight recorded in `system-test-results/real-account-preflight.safe.json`: both stored real accounts still have unexpired Google cookies but lack `https://aistudio.google.com` origin storage, so real generation smoke is blocked until re-login or importing a Playwright storage state captured after AI Studio fully loads.
