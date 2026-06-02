# Implement Provider Manager shared pool foundation

## Goal

Implement the first code slice of the new Provider Manager / shared provider-model pool architecture. This task moves the project from Phase 0 documentation-only evidence to Phase 1 control-plane evidence: an independent Provider Manager API/UI can manage provider records, credential references, model catalog metadata, health state, and audit-safe events without entering a Local Studio conversation.

## What I Already Know

- `ARCHITECTURE.md` defines Provider Manager as the control plane and shared runtime gateway as the later data plane.
- `SYSTEM_TEST_PLAN.md` now gates Phase 1 with `PM-CP-*` and `PM-AUDIT-*`; Phase 2 runtime gateway and Phase 3 advanced routing remain separate future stages.
- Existing Local Studio provider settings live in the static UI and `/api/local-studio/*` request payloads.
- Existing route modules use FastAPI routers under `src/aistudio_api/api/` and are included from `api/app.py`.
- Existing persistence patterns use JSON files under configured data directories, for example Local Studio conversations and request logs.
- Existing frontend is a single Alpine-style static app in `src/aistudio_api/static/app.js`, `index.html`, and `style.css`.

## Requirements

- Add a backend Provider Manager control-plane API under a stable route prefix such as `/api/provider-manager`.
- Provide a built-in Google AI Studio provider that always exists, is not deletable, and does not require user base URL or token.
- Support custom OpenAI-compatible provider CRUD with enabled state, base URL, timeout, credential reference, manual model catalog entries, aliases/defaults metadata, and health state.
- Store secrets separately from public provider records; API/UI/model catalog/audit responses must never return raw token values.
- Add audit-safe events for provider create/update/enable-disable/delete and model catalog changes.
- Add a frontend Provider Manager navigation entry and page that can list providers, create/edit/enable/disable/delete custom providers, inspect model catalog/health/audit evidence, and make Phase 1 visibly independent from Local Studio chat.
- Change Provider Manager creation/editing to a configuration dialog opened by “新建 provider” / edit actions, with a write-only token input, token show/hide control, provider model discovery, model alias editing, and automatic model capability/catalog configuration.
- Add narrow Provider Manager model-discovery API support when needed, keeping raw tokens out of API responses, request logs, frontend state, audit events, and task artifacts.
- Fix account-management Google login startup when Camoufox's launchServer script expects Playwright's legacy `lib/browserServerImpl.js` but the installed Playwright driver uses the bundled package layout.
- Add unit/static contract tests covering API behavior, secret redaction, model catalog shape, audit events, and frontend entry points.
- Update `SYSTEM_TEST_PLAN.md` only if implementation changes the Phase 1 evidence wording or adds required oracles.

## Acceptance Criteria

- [ ] `GET /api/provider-manager/providers` returns the built-in Google provider and any persisted custom providers without secrets.
- [ ] Custom OpenAI-compatible provider create/update/delete round trips through API and persistence.
- [ ] Credential response fields are references or masked summaries only; raw tokens never appear in API responses, request logs, frontend state snapshots, or task artifacts.
- [ ] Model catalog entries include provider id, external model id, display name, capability metadata, modality metadata, aliases/defaults, and manual/discovered source.
- [ ] Health state distinguishes at least `ready`, `disabled`, and `unknown`, with room for `auth_failed`, `quota_exhausted`, and `degraded`.
- [ ] Provider Manager frontend route/page is reachable from navigation and does not require opening Local Studio.
- [ ] Clicking “新建 provider” opens a provider configuration dialog; the dialog can show/hide the token field, fetch models through the Provider Manager backend, edit aliases/default model metadata, and save normalized catalog capabilities.
- [ ] Existing Local Studio, Playground, Images, Request Logs, Accounts, and Config navigation remain available.
- [ ] Account-management Google login can start Camoufox under the installed WSL Playwright package layout without failing on missing `playwright/driver/package/lib/browserServerImpl.js`.
- [ ] Unit/static tests pass and full unit suite passes.
- [ ] WSL real system test covers Provider Manager API and UI Phase 1 gates plus existing Local Studio/compatible API smoke. If global Google warmup still fails, record it as a blocking environment/runtime issue and do not claim full system pass.

## Out of Scope

- Do not migrate Local Studio runtime chat execution through the shared runtime gateway in this task.
- Do not implement Phase 2 canonical request/response routing, provider executors, runtime fallback, Gemini provider CRUD, or Claude provider CRUD.
- Do not replace existing Local Studio provider payload behavior unless needed for non-regression compatibility.
- Do not commit real credentials, request-log exports, screenshots, generated images, or WSL artifacts.

## Technical Approach

- Add a provider manager application/infrastructure layer that persists public provider records, credential references, model catalog entries, health snapshots, and audit events in a dedicated data directory.
- Add a FastAPI router and register it in `api/app.py`.
- Reuse existing local model capability helpers where useful, but keep Phase 1 management data independent from runtime routing.
- Extend the static frontend with a compact operational Provider Manager page rather than a marketing-style page.
- Add focused unit tests with ASGI transport and static frontend string contracts matching existing test style.

## Definition of Done

- Code, tests, task files, and any necessary spec/test-plan updates are committed on `feature/provider-manager-shared-pool-foundation`.
- Full unit suite passes.
- Real WSL API/UI smoke is executed per `SYSTEM_TEST_PLAN.md`, with Provider Manager Phase 1 coverage and artifact paths recorded.
- `git fetch origin` + merge from `origin/main` is performed before final commit/PR.
- Final PR targets `main` because this repository has no `master` remote branch.