# Add native UI worker pool

## Goal

Optimize Google AI Studio account-backed text generation by replacing per-request native UI helper launches with a reusable per-account pool of independent native UI worker processes. The implementation must keep the clean-process permission boundary that fixed relogin 403s, reduce steady-state latency, support per-account concurrency, update architecture and system-test documentation, and pass full real WSL system testing.

## What I Already Know

- Existing account-backed text requests decode the AI Studio wire body into `(model, prompt)` and call `python -m aistudio_api.infrastructure.gateway.native_ui_sender` once per request.
- Same-process native UI contexts can still inherit hook/template state and return AI Studio 403, while a standalone clean Camoufox process succeeds with the same account/model/prompt.
- `BrowserSession` uses a single-thread main Camoufox executor for warmup, hook/template capture, replay fallback, and current streaming send work.
- Each stored account gets an isolated `AIStudioClient` via `AccountClientPool`, so a worker pool owned by `BrowserSession` naturally becomes per-account.
- Runtime API concurrency is already controlled by `AISTUDIO_MAX_CONCURRENCY`, default `3`.
- Real system tests must run in WSL and include both API-level and frontend UI usage.

## Requirements

- Add a configurable worker count for native UI workers per account, default `3`.
- Reuse native UI worker processes across requests for the same account auth file.
- Preserve independent process isolation from the long-lived hook/template Camoufox process.
- Allow multiple workers for one account to process requests concurrently when upstream request concurrency allows it.
- Restart or replace a worker when it exits, times out, returns malformed output, or reports a process/protocol failure.
- Keep no-auth/fake unit-test path using the existing in-process native UI fallback.
- Keep browser/context raw replay as a compatibility fallback only when native UI cannot parse/send the request, not after an authoritative native UI HTTP response.
- Update `ARCHITECTURE.md`, `SYSTEM_TEST_PLAN.md`, and backend Trellis spec to describe the worker-pool architecture and verification requirements.

## Acceptance Criteria

- [x] Account-backed text-only sends use a per-account persistent native UI worker pool instead of launching a helper for every request.
- [x] `AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT` is exposed in runtime config and defaults to `3`.
- [x] Unit tests prove worker reuse, worker restart on failure, config exposure, and unchanged no-auth fallback behavior.
- [x] Architecture and system-test plan describe the clean worker process pool, default size, concurrency relationship, and real-test oracles.
- [x] Full unit suite passes.
- [x] Full real WSL system test passes with warmup, API stream, UI stream, request-log oracle, and worker-pool reuse evidence.

## Out of Scope

- Replacing the main hook/template capture browser with workers.
- Adding a shared cross-account global worker scheduler.
- Supporting native UI worker replay for image/file/multimodal AI Studio wire bodies in this task.
- Changing Provider Manager rollout phase or implementing the future shared provider-model pool.

## Technical Notes

- `src/aistudio_api/infrastructure/gateway/session.py` owns the decision to use native UI vs fallback replay.
- `src/aistudio_api/infrastructure/gateway/native_ui_sender.py` owns clean-process Camoufox UI send behavior and can be extended with a long-lived JSONL worker mode.
- A worker should receive JSON request lines and emit JSON result lines over stdout, while logs/noise from browser startup must not break the parent parser.
- `AccountClientPool` already closes each account client, so `AIStudioClient.close()` / `BrowserSession.close()` should terminate the owned worker pool.
- The main `BrowserSession` executor is single-threaded; account-backed worker-pool sends must avoid taking `_botguard_lock` for the whole native UI request if same-account concurrency is expected.

## Spec Update Gate

- Updated `.trellis/spec/backend/quality-guidelines.md` with the native UI worker pool command/protocol, env contract, fallback boundary, worker retry semantics, and required unit/system oracles.
- Updated `ARCHITECTURE.md` with the per-account clean-process worker pool design, default worker count, concurrency relationship, warmup behavior, and authoritative native UI response handling.
- Updated `SYSTEM_TEST_PLAN.md` with worker-pool reuse/concurrency oracles and the WSL swap/OOM precondition learned during real browser-heavy testing.
- No additional code-spec files are needed for this task; the backend quality guideline is the applicable executable contract location.