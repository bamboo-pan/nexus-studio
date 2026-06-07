# Comprehensive System Test Bug Fixes

## Goal

Execute the latest `SYSTEM_TEST_PLAN.md` as a real end-to-end system test in an isolated WSL clean copy, identify any P0/P1 failures, fix bugs in the project code, and rerun the relevant clean-copy verification until the tested gates are green or a true external environment blocker is documented.

## What I Already Know

- The user requested a comprehensive system test based on the latest system test plan and bug fixes.
- The repository is Trellis-managed and requires task artifacts, spec context, verification, commit, and finish workflow.
- The repository mainline is `main` / `origin/main`, not `master`; the feature branch for this task is `feature/system-test-bug-fixes-20260607`.
- `SYSTEM_TEST_PLAN.md` is the highest testing authority for Local Studio WebUI, Provider Manager/shared provider-model pool rollout gates, API compatibility, request logs, native UI worker readiness, and MCP-visible UI testing.
- Real tests must run from a WSL temporary clean copy under `/home/bamboo`, with isolated writable data directories and real credential sources only referenced or copied as allowed.
- API-only or headless-only success is not sufficient for P0/P1 UI gates; at least one MCP-visible UI phase must show real user navigation and operation.

## Requirements

- Run the system-test environment gates `ENV-01` through `ENV-04` from a WSL temporary clean copy.
- Run dependency setup, network preflight, native UI worker import/start preflight, service startup, health/model/account checks, and request-log enabling from the temporary copy.
- Execute P0/P1 API and UI coverage from `SYSTEM_TEST_PLAN.md`, prioritizing the named bug regressions and architecture-contract assertions.
- Use real credentials from the configured paths without printing or committing secrets.
- P0/P1 provider integration gates must use and pass with real credentials: Google AI Studio real accounts and the real OpenAI-compatible key file. The real OpenAI-compatible file is a provider credential file containing Base URL plus token, not merely a single opaque token. Fake tokens, stub providers, local mock upstreams, or credential-existence checks are diagnostic only and cannot satisfy system-test pass criteria.
- If failures are caused by product bugs, fix the root cause in the development workspace with minimal, style-consistent changes.
- After fixes, rerun affected unit tests and the relevant WSL clean-copy API/UI/system gates.
- Produce durable task notes summarizing environment evidence, failures, fixes, rerun results, and any blocked gates.
- Do not submit generated screenshots, request-log exports, server logs, secrets, account data, or generated images to git.

## Acceptance Criteria

- [ ] A WSL clean-copy system test run is created with source commit, clean status evidence, isolated data dirs, venv install evidence, native worker preflight result, service port, and artifact paths.
- [ ] P0 startup/shared-service checks, Provider Manager rollout phase gate, Local Studio bug regressions, request-log checks, and at least one MCP-visible UI Local Studio path are executed or marked blocked with explicit evidence.
- [ ] Any discovered product bug that is actionable in this repo is fixed and covered by tests or a targeted smoke rerun.
- [ ] Relevant unit tests, lint/type checks where available, and real WSL API/UI verification pass for fixed areas.
- [ ] Task artifacts include a concise report mapping tested plan items to pass/fail/not_applicable/blocked.
- [ ] Git commit includes code changes and `.trellis/tasks/06-07-system-test-bug-fixes/` task files only for this task.

## Definition of Done

- Tests and real smoke/system checks are run according to blast radius.
- Any remaining failures are external/environment blockers or clearly filed task notes with evidence.
- No secrets or bulky runtime artifacts are staged.
- Trellis spec-update judgment is performed before committing.

## Out of Scope

- Implementing the full future Provider Manager/shared runtime architecture if the current rollout phase is still Phase 0.
- Committing raw system-test artifacts such as screenshots, server logs, request-log exports, credentials, generated images, or storage states.
- Lowering `SYSTEM_TEST_PLAN.md` standards to match an incomplete script.

## Technical Notes

- Latest system-test summary is persisted in `research/system-test-plan-summary.md`.
- Relevant specs: `.trellis/spec/backend/index.md`, `.trellis/spec/backend/quality-guidelines.md`, and additional backend guideline files when touched.
