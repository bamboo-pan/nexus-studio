# Provider Manager Foundation Research

## Current Code Evidence

- FastAPI route modules live in `src/aistudio_api/api/routes_*.py` and are registered in `src/aistudio_api/api/app.py`.
- Existing Local Studio provider/model behavior is currently request-payload driven in `routes_local_studio.py` and `infrastructure/local_studio.py`.
- Local Studio persistence uses JSON files under `settings.local_studio_dir`; request logs use a separate `RequestLogStore` with redaction expectations.
- Frontend navigation and views are static Alpine-style code in `src/aistudio_api/static/index.html`, `app.js`, and `style.css`; tests in `tests/unit/test_static_frontend_capabilities.py` assert important strings.
- `SYSTEM_TEST_PLAN.md` now defines Phase 1 Provider Manager control-plane gates and keeps Phase 2 runtime gateway out of the current implementation stage.

## Recommended MVP

- Implement Phase 1 only: Provider Manager control-plane API + frontend page + persisted registry evidence.
- Keep Local Studio runtime execution unchanged during this task to avoid pretending Phase 2 shared runtime gateway is complete.
- Persist a non-deletable built-in Google AI Studio provider with no base URL/token and allow CRUD for custom OpenAI-compatible providers.
- Separate public provider records from credential references; return only masked summaries or reference ids.
- Include audit-safe provider/model events so `PM-AUDIT-01` can be tested without exposing secrets.

## Risks

- Adding a Provider Manager route/page means Phase 1 gates become applicable; tests must cover `PM-CP-*` and `PM-AUDIT-*` rather than leaving them not applicable.
- Any UI state containing raw tokens can leak into screenshots, localStorage, logs, or test artifacts; token entry should be write-only or immediately masked after save.
- The known global `/health` warmup failure may still block a complete system-test pass even if Provider Manager API/UI checks pass.

## Follow-Up Phase

Phase 2 should route Local Studio and compatible APIs through a canonical request/response data plane with provider executors, routing decisions, attempt plans, and fallback evidence in request logs.