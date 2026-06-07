# Strengthen Playwright UI System Test Plan

## Goal

Redesign `SYSTEM_TEST_PLAN.md` so the project cannot claim system-test success from narrow API smoke scripts, direct state injection, or temporary local patches. The plan must require a clean user-like environment, visible browser UI testing with MCP browser tools, complete user workflow coverage, and response-time benchmarks comparable to direct use of the official AI Studio web UI.

## What I Already Know

* The user explicitly reported a real pull-and-run failure after previous testing had been marked as passing.
* The current failure class is native AI Studio UI model selection during warmup: `AI Studio text model not selected in native UI sender: gemini-3.5-flash`, with AI Studio showing a different current surface (`chat spark playground`) and a visible model list containing `Gemini 3 Flash Preview`.
* Existing `SYSTEM_TEST_PLAN.md` already requires WSL temporary copies, real credentials, API + UI coverage, request log assertions, and native worker pool oracles.
* Some existing `tmp/` UI smoke scripts are still narrow: they use headless Playwright and in at least one case directly mutate Alpine state before sending, which can bypass real user selection/navigation flows.
* The user requires Playwright UI testing through MCP tools so the UI process is visible and inspectable during the test run.
* The user requires all UI used by users and all functions used by users to be tested, not merely assumed from API success.
* The user requires message-response latency to be at the same practical level as direct official AI Studio web UI usage.

## Requirements

* Update the system test plan to define a strict separation between development workspace and test workspace.
* Require full clean-environment reproduction from a fresh WSL temporary copy, including dependency install and no uncommitted local patches.
* Require UI tests to use MCP browser tools for visible, user-like interaction, with snapshots/screenshots/console/network evidence.
* Prohibit direct application-state mutation, static DOM-only checks, and API-only checks from satisfying browser UI coverage.
* Define a complete UI workflow inventory covering navigation, provider setup, model loading/selection, chat send/streaming, search, image tools, reasoning, attachments, conversations, request logs, accounts, and base modules.
* Add a specific regression oracle for the native UI model-selection failure shown in the user log.
* Add response-time requirements and measurement gates comparing Local Studio user-visible latency against official AI Studio direct web UI in the same environment/account/model class.
* Require test reports to map every P0/P1 plan item to either an automated assertion or a visible manual/MCP UI assertion.

## Acceptance Criteria

* [ ] `SYSTEM_TEST_PLAN.md` contains explicit MCP visible UI test rules.
* [ ] `SYSTEM_TEST_PLAN.md` contains an environment-isolation gate that fails runs using the development workspace, dirty local patches, or reused test data.
* [ ] `SYSTEM_TEST_PLAN.md` contains a full user-journey UI coverage matrix and forbids state injection as a substitute for user actions.
* [ ] `SYSTEM_TEST_PLAN.md` contains a native UI model-selection regression case for `text_model_not_found` / wrong current AI Studio surface.
* [ ] `SYSTEM_TEST_PLAN.md` contains official-web-comparison response-time metrics, budgets, and failure rules.
* [ ] Trellis task files include the PRD and context files needed for implementation/check.

## Out of Scope

* Implementing new automated test scripts in this task.
* Fixing the native UI model-selection bug itself.
* Running a full real WSL system test, because this task is documentation/test-plan redesign only.

## Technical Notes

* Existing plan: `SYSTEM_TEST_PLAN.md`.
* Relevant existing UI smoke examples: `tmp/wsl-aistudio-permission-ui-smoke.sh`, `tmp/wsl-provider-ui-smoke.py`, `tmp/wsl-provider-manager-system-smoke.sh`.
* Relevant repo memory: native UI worker oracles and warmup health requirements.