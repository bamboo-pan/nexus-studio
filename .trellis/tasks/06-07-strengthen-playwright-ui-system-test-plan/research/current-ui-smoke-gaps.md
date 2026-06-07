# Current UI Smoke Gaps

## Observations

* `SYSTEM_TEST_PLAN.md` already has strong API/UI language, but it does not explicitly require visible MCP browser-tool execution for UI tests.
* Existing temporary UI smoke scripts use Playwright directly and usually launch headless Chromium.
* `tmp/wsl-aistudio-permission-ui-smoke.sh` opens `#studio`, then directly sets Alpine root fields such as provider, interface, model, stream, reasoning, search, and draft through `page.evaluate()` before pressing send. This proves a narrow error state, but it bypasses the real user controls that failed in the reported model-selection path.
* `tmp/wsl-provider-ui-smoke.py` covers provider dialog creation/discovery/save and token visibility, but it is a focused smoke rather than a complete user journey.
* Existing scripts collect screenshots/console/network in some cases, but the plan should require every P0/P1 UI test result to tie visible UI assertions to hard pass/fail conditions.

## Failure Mode Behind False Confidence

* API success and direct state injection can skip the actual browser UI steps where model selectors, navigation state, account context, and official AI Studio UI surfaces drift.
* Running from the development workspace can pass with uncommitted local edits or generated state that a fresh user pull does not have.
* Temporary patches during smoke testing can make a local run pass without proving the committed code works from a clean checkout.
* Headless-only runs make it hard to inspect whether the same controls a user sees were actually used.

## Plan Implications

* The system plan should require tests to start from a clean WSL temporary copy and record commit/dirty-state evidence.
* UI tests should be visible through MCP browser tools, not only hidden Playwright scripts.
* Direct state mutation can be used only for diagnostics after the user-flow test has failed, never as the passing oracle.
* Response time must measure user-visible latency from send click to first visible assistant output and completion, and compare it with official AI Studio direct UI under the same network/account/model conditions.