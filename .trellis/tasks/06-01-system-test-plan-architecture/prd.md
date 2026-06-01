# Update System Test Plan For Latest Architecture

## Goal

Update the real system test plan so it matches the latest Provider Manager and shared provider-model pool architecture, then implement the corresponding project changes needed to make that plan executable and verifiable.

## What I Already Know

- User requested: update the test plan according to the latest architecture, then start implementation, and run the full system test after implementation.
- `ARCHITECTURE.md` now defines a Provider Manager control plane, shared runtime gateway data plane, provider/model registry, credential boundary, health/routing policy, fallback, and four compatible protocol surfaces.
- `SYSTEM_TEST_PLAN.md` already has strong Local Studio P0/P1 coverage for provider isolation, tools, reasoning, repeated prompts, request logs, UI state, and base module independence.
- The current plan needs explicit coverage for the Provider Manager / shared pool target architecture and staged rollout gates.
- The repo has backend and static frontend tests; real system tests must run in a WSL temporary copy with real API and UI paths, per `AGENTS.md` and `SYSTEM_TEST_PLAN.md`.

## Requirements

- Update `SYSTEM_TEST_PLAN.md` to reflect the newest `ARCHITECTURE.md` Provider Manager and shared provider-model pool architecture.
- Add explicit test coverage dimensions and architecture contracts for:
  - Provider Manager as a control plane independent from Local Studio conversations.
  - Provider/model registry, credential references, model catalog, health checks, routing policies, and audit safety.
  - Shared runtime gateway data-plane contracts: canonical request/response, protocol adapters, provider executors, response conversion, and request logs.
  - Compatible protocol consumers: OpenAI Responses, OpenAI Chat Completions, Gemini, Claude Messages, and Local Studio as one consumer.
  - Routing policy behavior: aliases/defaults, capability matching, health, quotas, priority/weight, fallback, sticky routing, and streaming/tool/image compatibility.
  - Rollout phase gates that keep current Local Studio behavior compatible while the shared pool is introduced incrementally.
- Implement focused code/test changes if the updated plan exposes an immediately testable missing contract in the current app.
- Keep changes scoped; do not implement the full Provider Manager runtime migration unless it is already present or required for the test-plan alignment.
- Run project unit tests relevant to changed code/docs and then execute the full required WSL real system test flow with API and UI coverage.
- Commit the task changes, including `.trellis/tasks/06-01-system-test-plan-architecture/` task files.

## Acceptance Criteria

- [ ] `SYSTEM_TEST_PLAN.md` includes Provider Manager and shared provider-model pool coverage aligned with `ARCHITECTURE.md`.
- [ ] The plan distinguishes control-plane, data-plane, protocol adapter, routing, fallback, audit, and rollout-phase assertions.
- [ ] Any implemented code/test changes are covered by unit tests.
- [ ] Full WSL real system testing is attempted from a temporary `/home/bamboo` copy with API and browser UI coverage, real credentials paths, isolated data dirs, and sanitized artifacts.
- [ ] Test results are summarized, including pass/fail/not-applicable items and any blockers.
- [ ] No real secrets, cookies, storage state, request-log exports, screenshots, or generated large image payloads are committed.

## Definition of Done

- Test plan updated.
- Implementation completed for any immediate executable contract gaps.
- Unit/lint checks run.
- Real system test run or clearly blocked with evidence.
- Trellis check/spec-update judgment completed.
- Work committed on the feature branch.

## Technical Approach

Use the latest architecture as the source of truth. Update the system test plan first, then inspect the app/tests for concrete gaps. Prefer narrow implementation: static/frontend contract tests and test-plan executable hooks when appropriate, not a full Provider Manager migration unless required by existing architecture phase boundaries.

## Decision (ADR-lite)

**Context**: The newest architecture introduces Provider Manager and shared provider-model pool concepts, while the existing system test plan still focuses mostly on Local Studio provider settings.

**Decision**: Expand the system plan into a phased architecture validation plan. Phase 1 preserves current Local Studio compatibility while adding Provider Manager/registry/gateway contract gates; future phases will require runtime migration tests when those features are implemented.

**Consequences**: The plan becomes stricter and more future-proof without forcing a large unscoped runtime migration in this task. Immediate implementation can remain focused on executable checks that match currently shipped surfaces.

## Out of Scope

- Full Provider Manager backend storage migration.
- Full shared runtime gateway / model pool router implementation if not already present.
- Adding real secret values or committing generated system-test artifacts.

## Technical Notes

- Architecture source: `ARCHITECTURE.md`.
- Test-plan source: `SYSTEM_TEST_PLAN.md`.
- Gap research: `research/architecture-test-plan-gap.md`.
- Backend spec index: `.trellis/spec/backend/index.md`.
- Development workflow requires feature branch work, merging latest default branch before final tests, committing task files, PR creation, merge back to default branch, and cleanup when possible.
