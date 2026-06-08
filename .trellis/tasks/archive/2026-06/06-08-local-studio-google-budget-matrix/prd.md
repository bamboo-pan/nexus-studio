# Fix Local Studio Google Performance Budget and System Matrix

## Goal

Resolve the remaining real-system-test blockers: Local Studio Google same-account visible latency must satisfy the official AI Studio comparison budget, and the system-test plan coverage matrix must no longer remain incomplete when the required P0/P1 rows are backed by hard assertions.

## What I already know

* Latest real diagnostic from the previous task reached real credential gates, network preflight, native worker preflight, warmup completion, API provider checks, official AI Studio direct baseline, and an expanded headed UI subset.
* Official AI Studio baseline for `gemini-3.5-flash` collected three visible HTTP 200 samples and reported median first-token/completion around `4442/4442 ms`.
* Same-account Local Studio Google samples returned correct HTTP 200/exact visible text but median first-token/completion around `30339/30343 ms`, exceeding budgets `8884/11663 ms`.
* The near-identical Local Studio first-token and completion timings suggest the UI/harness may only observe text once completion lands, or that the request path waits around a 30s boundary before emitting visible content.
* `SYSTEM_TEST_PLAN.md` requires a plan-script alignment gate; previous runner correctly emitted `SYSTEM_TEST_INCOMPLETE` while P0/P1 UI/performance/Provider Manager matrix rows remained missing or incomplete.

## Requirements

* Diagnose and fix the Local Studio Google text path so warmed same-account Local Studio UI samples meet the official comparison budget in a real WSL run.
* Preserve the existing official baseline oracle: same account, same or equivalent model, real visible official UI samples, and median-based budget calculation.
* Ensure Local Studio UI timing captures first visible assistant output separately from completion and does not hide streaming/pending latency behind a single completion update.
* Complete or materially extend the system-test coverage matrix mapping so each required P0/P1 plan row has a hard pass/fail/not-applicable-with-evidence assertion in the runner output.
* Keep secret boundaries intact: no raw cookies, tokens, storage state, raw screenshots with secrets, or request payload artifacts committed.

## Acceptance Criteria

* [ ] Unit tests relevant to touched code pass.
* [ ] Real WSL API and visible UI system test runs from a clean temporary copy with real Google and OpenAI-compatible credentials.
* [ ] Real system-test output no longer fails the Local Studio Google performance comparison for the same-account warmed text path, or records a newly discovered external blocker with concrete official/local/server/network evidence.
* [ ] Plan-script alignment artifact covers the required `SYSTEM_TEST_PLAN.md` P0/P1 rows with executable statuses and does not remain incomplete because of missing matrix rows.
* [ ] Task files under `.trellis/tasks/06-08-local-studio-google-budget-matrix/` are committed with the code changes.

## Out of Scope

* Relaxing the performance budget without a root-cause fix.
* Replacing real visible UI evidence with headless-only or API-only diagnostics for P0/P1 UI paths.
* Changing real credential locations or committing any credential-derived artifacts.

## Technical Notes

* Previous blocker report: `.trellis/tasks/archive/2026-06/06-07-system-test-bug-fixes/report.md`.
* System-test plan: `SYSTEM_TEST_PLAN.md`.
* Repo memory notes: native UI worker pool is required for account-backed Google text; complete system-test pass requires plan-script alignment, not just a basic headed smoke.
