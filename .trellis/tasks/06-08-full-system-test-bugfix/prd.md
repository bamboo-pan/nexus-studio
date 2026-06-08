# Full System Test Bugfix

## Goal

Run the repository's full real system-test workflow from a clean WSL copy with real Google AI Studio and OpenAI-compatible credentials, then fix product or test-harness bugs found by the run until the current task has the strongest attainable verified result.

## What I already know

* User requested a complete system test and bug fixes.
* `SYSTEM_TEST_PLAN.md` is the highest-priority test oracle and requires clean WSL copies, real credentials, API-level checks, and visible browser UI checks.
* Previous system-test work produced a reusable runner with clean-copy gates, real credential gates, network/native worker preflight, host Playwright UI evidence, architecture contract artifacts, Provider Manager gates, and plan-script alignment.
* The latest known blocker from previous work was Local Studio Google visible latency/streaming behavior and incomplete coverage rows that must remain explicit failures until executable assertions are added.
* The repository's remote default branch is `main`, not `master`; the task branch is `feature/full-system-test-bugfix`.

## Requirements

* Run the current task's WSL real system-test runner from a clean temporary copy under `/home/bamboo`.
* Use real Google account credentials from `/home/bamboo/nexus-studio/data/accounts` and the real OpenAI-compatible key file at `/mnt/c/Users/bamboo/Documents/github/key.txt` without printing secrets.
* Preserve API, request-log, native worker, Provider Manager, and visible UI artifacts under the WSL run root.
* Treat `SYSTEM_TEST_INCOMPLETE` and any concrete failing required row as a failure to investigate, not as a pass.
* Fix discovered product bugs at the root cause when feasible, keeping changes scoped and consistent with existing code.
* After fixes, rerun focused unit tests and the real WSL system test from a clean copy.

## Acceptance Criteria

* [ ] Initial real WSL system test has been executed and summarized with a run root.
* [ ] Any discovered bug has a root-cause fix or is recorded as an explicit environment/coverage blocker with evidence.
* [ ] Relevant unit tests pass after code changes.
* [ ] Final real WSL system test has been rerun after fixes from a clean temporary copy.
* [ ] Task artifacts include `system-test-wsl.sh`, `host-ui-smoke.py`, plan-alignment contract test, PRD, research notes, and final report.

## Out of Scope

* Lowering system-test budgets or weakening `SYSTEM_TEST_PLAN.md` pass criteria.
* Substituting mock providers, fake credentials, headless-only UI checks, or development workspace runs for required real-system evidence.
* Completing every missing P0/P1 matrix row by prose only; uncovered rows must remain explicit failures unless implemented as real assertions.

## Technical Notes

* Relevant spec: `.trellis/spec/backend/quality-guidelines.md`, especially native worker and real system-test oracle sections.
* Runner source: `.trellis/tasks/archive/2026-06/06-08-local-studio-google-budget-matrix/system-test-wsl.sh`.
* Host UI smoke source: `.trellis/tasks/archive/2026-06/06-08-local-studio-google-budget-matrix/host-ui-smoke.py`.
* Previous blocker summary recorded in `research/system-test-scope.md`.