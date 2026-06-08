# Previous System Test Blockers

## Source

Previous task report: `.trellis/tasks/archive/2026-06/06-07-system-test-bug-fixes/report.md`.

## Local Studio Google performance blocker

* Official AI Studio direct visible baseline passed for `gemini-3.5-flash` with three HTTP 200 visible samples.
* Official median first token/completion: `4442/4442 ms`.
* Same-account Local Studio UI samples returned HTTP 200 and exact visible text, but median first token/completion: `30339/30343 ms`.
* Budget was `8884/11663 ms` from `max(official * 2, official + 3000)` for first token and `official * 1.5 + 5000` for completion.
* Local first-token and completion timings being almost identical points toward either Local Studio not rendering stream deltas until completion, or the harness measuring only final visible text after a delayed complete event.

## System plan matrix blocker

* `SYSTEM_TEST_PLAN.md` requires a plan-script alignment gate.
* Basic headed smoke coverage remains useful evidence but must emit `SYSTEM_TEST_INCOMPLETE` until all required P0/P1 UI, performance, and Provider Manager rows are mapped to hard assertions.
* Remaining gaps listed in the previous report include complete P0/P1 UI matrix, search/image/reasoning generation matrix, attachment send upstream matrix, image generation visible output matrix, account switch/delete UI paths, and Provider Manager phase/discovery/health completeness.

## Implementation implications

* Do not relax budgets to make the test pass.
* Diagnose Local Studio streaming/user-visible timing first.
* Any matrix fix must make missing rows executable pass/fail/not-applicable-with-evidence entries, not merely update prose.
