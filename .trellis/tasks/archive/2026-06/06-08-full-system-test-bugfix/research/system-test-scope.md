# System Test Scope

## Relevant plan requirements

* Full P0/P1 evidence must start from a clean WSL temporary copy and must not use the development workspace or development data directories as writable state.
* Google AI Studio and OpenAI-compatible paths must use real credentials and must prove both API-level and visible UI-level behavior.
* Request logs must include full lifecycle phases and redaction evidence.
* Google account-backed text readiness must come from `GET /health` warmup status using native UI worker `GenerateContent` probes.
* Visible UI evidence must include headed browser actions, screenshots or snapshots, console/network summaries, and final visible result or error state.
* Plan-script alignment must map every required P0/P1 row exactly once to pass/fail/not_applicable with evidence.

## Previous known blockers

* Previous Local Studio Google performance samples completed successfully but exceeded the official AI Studio latency budget, with first-visible-token and completion times nearly identical.
* That signature points to streaming deltas not becoming visible until completion, or the UI harness only detecting final rendered text.
* Basic smoke coverage is useful but cannot emit `SYSTEM_TEST_PASS` while required matrix rows remain failed or uncovered.

## Current task strategy

* Reuse the latest complete runner and host UI smoke so the initial run exercises the same hard gates.
* Fix concrete failures found by the run; do not relax plan criteria.
* Keep uncovered matrix rows explicit until executable coverage is added.