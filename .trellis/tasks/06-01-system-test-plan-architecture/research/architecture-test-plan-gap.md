# Architecture Test Plan Gap

## Source Architecture

- `ARCHITECTURE.md` now includes a Provider Manager / shared provider-model pool target architecture.
- The new section separates the Provider Manager control plane from the shared runtime gateway data plane.
- Local Studio should become a consumer of the shared pool instead of owning provider CRUD, credentials, model discovery, or global routing policy.
- External protocol consumers should share the same pool through OpenAI Responses, OpenAI Chat Completions, Gemini, and Claude Messages compatible entries.
- Google AI Studio remains the built-in provider and existing Playground, images, accounts, and request logs must stay independently usable.

## Current Test Plan Gap

- `SYSTEM_TEST_PLAN.md` already covers Local Studio provider isolation, tool optionality, reasoning, repeated prompt cache removal, request log lifecycle, and base module independence.
- It does not yet explicitly cover Provider Manager as an independent control-plane entry.
- It does not yet treat provider/model registry, credential references, model aliases, health, routing policy, or audit records as first-class test dimensions.
- It does not yet define API/UI/API-client coverage for the shared provider-model pool and protocol adapters as architecture rollout gates.
- It does not yet distinguish phase-1 registry compatibility tests from phase-2 runtime migration tests.

## Implementation Implications

- Update `SYSTEM_TEST_PLAN.md` to add Provider Manager, provider-model pool, control-plane/data-plane, protocol adapter, routing, fallback, and rollout-phase assertions.
- Add or update executable tests that lock the current phase-1 boundary: Local Studio still has current behavior, but the frontend/static contract should expose a future-independent Provider Manager entry/wording if implemented.
- Keep secrets out of committed files and generated artifacts.
- Run unit checks and then the required real WSL API/UI system test after implementation.
