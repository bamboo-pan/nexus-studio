# Error Handling

> How errors are handled in this project.

---

## Overview

<!--
Document your project's error handling conventions here.

Questions to answer:
- What error types do you define?
- How are errors propagated?
- How are errors logged?
- How are errors returned to clients?
-->

(To be filled by the team)

---

## Error Types

<!-- Custom error classes/types -->

(To be filled by the team)

---

## Error Handling Patterns

<!-- Try-catch patterns, error propagation -->

(To be filled by the team)

---

## API Error Responses

<!-- Standard error response format -->

### Scenario: Browser Gateway Auth Errors During Account Rotation

#### 1. Scope / Trigger

- Trigger: Changing OpenAI-compatible/Gemini-compatible chat/image handlers, account rotation, account pool leasing, browser gateway replay, or upstream error classification.
- Scope: `handle_chat`, `handle_image_generation`, `_request_account_context`, `AccountRotator`, `AccountClientPool`, `AIStudioClient`, and route error responses.

#### 2. Signatures

- Chat route: `POST /v1/chat/completions`.
- Local Studio route: `POST /api/local-studio/chat`, which calls the OpenAI-compatible/Responses/Gemini routes according to `interface_mode`.
- Upstream auth exception: `AuthError(message)`.
- API auth response shape: HTTP 401 with `detail/error.type == "authentication_error"`.
- No-account response shape: HTTP 503 with `detail/error.type == "service_unavailable"` only when there is no prior upstream auth root cause.

#### 3. Contracts

- Preserve root-cause upstream auth errors across retry/rotation attempts. If the first account fails with `AuthError` and a retry cannot lease another account, return the original authentication failure, not a generic 500 or bare account exhaustion.
- `HTTPException` raised by account selection, validation, or route-level guards must be re-raised unless there is an explicit root-cause mapping such as prior `AuthError` plus retry account exhaustion.
- Secret-bearing request headers, cookies, tokens, and storage-state content must never appear in error response messages or health reasons.
- Rotation stats may record the failed account, but user-facing responses should describe the category (`authentication_error`, `rate_limit_exceeded`, `service_unavailable`, `upstream_error`) rather than internal lease mechanics.

#### 4. Validation & Error Matrix

| Condition | Required handling |
| --- | --- |
| Upstream replay returns 401/403 | Raise `AuthError`; route returns HTTP 401 `authentication_error`. |
| First account raises `AuthError`, retry has no available account | Return the original HTTP 401 `authentication_error`. |
| No account can be leased before any upstream call | Return HTTP 503 `service_unavailable`. |
| Upstream returns quota/rate limit | Return HTTP 429 `rate_limit_exceeded` after configured rotation retries are exhausted. |
| Unexpected non-AIStudio exception | Log with traceback and return HTTP 500 `server_error`. |

#### 5. Good/Base/Bad Cases

- Good: A stale AI Studio auth state returns `authentication_error` with a re-login/import guidance path and never claims that the server crashed.
- Base: A cold service with no configured accounts can still return `service_unavailable`.
- Bad: Catching `HTTPException(503)` from `_request_account_context` in the broad `Exception` block and wrapping it as HTTP 500 `server_error`.

#### 6. Tests Required

- Unit: pooled chat request preserves `AuthError` when retry account acquisition fails.
- Unit: rate-limited accounts are excluded and released without leaking in-flight leases.
- Real WSL API: use copied real accounts and assert old/broken auth state produces `authentication_error`, not `server_error`.
- Real WSL UI: Local Studio must recover `busy=false / can_send=true` after success or diagnosed auth failure.

#### 7. Wrong vs Correct

Wrong:

```python
except Exception as exc:
	raise HTTPException(500, detail={"message": str(exc), "type": "server_error"})
```

Correct:

```python
except HTTPException as exc:
	if isinstance(last_error, AuthError) and exc.status_code == 503:
		raise _upstream_exception(last_error) from exc
	raise
except Exception as exc:
	raise HTTPException(500, detail={"message": str(exc), "type": "server_error"})
```

---

## Common Mistakes

<!-- Error handling mistakes your team has made -->

(To be filled by the team)
