# System Test Plan Summary

## Source

- `SYSTEM_TEST_PLAN.md` as of 2026-06-07.

## Highest-Priority Gates

- `ENV-01` to `ENV-04`: run from a clean WSL temporary copy, record source/test-copy commits and statuses, use isolated writable data directories, install dependencies in a new venv, and rerun from a clean copy after any diagnostic patch.
- Native UI worker preflight: from the same temporary venv, import `NativeUiWorker`, start `python -m aistudio_api.infrastructure.gateway.native_ui_sender --worker`, and reject `ModuleNotFoundError` or worker startup failure.
- WSL network/Camoufox preflight: prove Python HTTPS and Camoufox can reach `https://aistudio.google.com/` before starting the service; prove OpenAI-compatible reachability with product-equivalent `httpx` semantics using the real Base URL + token; otherwise mark an environment blocker, not an app pass.
- Warmup oracle: use global `GET /health` and require `warmup.status == "complete"` for account-native readiness; `/api/local-studio/health` is not enough.
- MCP-visible UI: P0/P1 UI pass requires visible browser navigation and user actions through MCP/browser tools, not just headless scripts or DOM state injection.
- Real credentials: P0/P1 provider integration paths must pass with the configured real Google AI Studio accounts and the real OpenAI-compatible credential file. The OpenAI-compatible file provides Base URL plus token; fake tokens, stub providers, mock upstreams, empty account dirs, or key-existence checks are diagnostic only and cannot mark system tests passed.
- OpenAI-compatible model availability: model-list presence is not sufficient evidence that the account can use a model. The runner must verify a candidate Responses text model with a minimal real upstream request before using it for Local Studio API/UI paths.
- Request logs: key paths must preserve lifecycle phases (`client_request`, `upstream_request`, `upstream_response`, `client_response`) and redact secrets.
- Plan/script alignment: every recorded oracle such as `assistant_has_thinking`, `reasoning_summary_visible`, `contains_*_error`, and `*_visible` must be tied to pass/fail logic.

## Named Bug Regressions

- Google Responses + search + image tool: no `tool_config.include_server_side_tool_invocations` error; generated image appears once; tool/reasoning process preserved or auditable.
- OpenAI-compatible Responses + stream + search + HTTP 400: SSE error is well-formed, UI recovers, request logs include upstream body/client response, and server stderr has no `httpx.ResponseNotRead` or ASGI exception group.
- OpenAI-compatible search tool type: use `web_search`, never `web_search_preview`; Google provider must still use `web_search_preview`.
- OpenAI-compatible Responses reasoning: preserve reasoning events/items/summary/tool details through API, SSE completed, UI, conversation JSON, refresh, and request logs when upstream returns them; hard-fail missing `thinking` or missing UI summary when upstream had reasoning.
- AI Studio native model selection: real visible AI Studio model picker must select the requested target/alias; `text_model_not_found`, wrong surface, wrong authuser, or mismatched label fails warmup and Google text pass.
- No Local Studio final-result cache: repeated prompts and changes in provider/interface/model/tools/reasoning/attachments/token must all create fresh upstream requests with no cache hit/namespace indicators.
- Streaming UI: incremental text must become visible, not only a final one-shot update.

## Provider Manager Rollout

- First determine whether current code is Phase 0, 1, 2, or 3 by checking route/page/registry/gateway/routing evidence.
- Phase 0 can mark future Provider Manager/shared runtime checks not applicable only with concrete missing-code evidence and must still pass Local Studio, base modules, compatible APIs, and request-log P0 gates.
- Any implemented Provider Manager route/page/schema/runtime gateway makes the matching `PM-*` gates P0.

## Expected Artifacts

Keep these in the WSL run root, not in git:

- `artifacts/summary.md`
- `artifacts/api-results.json`
- `artifacts/ui-results.json`
- `artifacts/mcp-visible-ui-results.json`
- `artifacts/performance-comparison-results.json`
- `artifacts/architecture-contract-results.json`
- `artifacts/provider-manager-phase-gate-results.json`
- `artifacts/screenshots/`
- sanitized `artifacts/server.log`

## Secret Boundary

Never print, stage, or commit OpenAI tokens, Google cookies, Authorization headers, storage states, request-log exports containing raw secrets, generated images, or screenshots with secret material.
