# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

<!--
Document your project's quality standards here.

Questions to answer:
- What patterns are forbidden?
- What linting rules do you enforce?
- What are your testing requirements?
- What code review standards apply?
-->

(To be filled by the team)

---

## Forbidden Patterns

<!-- Patterns that should never be used and why -->

(To be filled by the team)

---

## Required Patterns

<!-- Patterns that must always be used -->

### Scenario: Provider Manager Model Discovery and Draft Provider Dialogs

#### 1. Scope / Trigger

- Trigger: Changing Provider Manager provider creation/editing, model discovery, model aliases, model capabilities, or provider credential handling.
- Scope: `routes_provider_manager.py`, `ProviderManagerStore`, static Provider Manager UI, provider credential storage, model catalog persistence, and audit events.

#### 2. Signatures

- Discovery API: `POST /api/provider-manager/model-catalog/discover`.
- Discovery request fields:
	- `provider_id?: str` - existing provider id; omitted for an unsaved draft provider.
	- `base_url?: str` - upstream OpenAI-compatible/Gemini-compatible base URL.
	- `timeout?: int` - clamped to `1..600`, default `120`.
	- `token?: str` - one-time API token from the dialog.
	- `credential_ref?: str` - optional stored credential reference for existing providers.
	- `interface_mode?: str` - normalized through `normalize_interface_mode`; defaults to `responses`.
- Discovery response fields: `object == "list"`, `total`, `data[]`, and `interface_mode`.
- Catalog entry fields: `id`, `provider_id`, `external_model_id`, `display_name`, `capabilities`, `modalities`, `aliases`, `defaults`, `context_window`, `source`, `metadata`.

#### 3. Contracts

- Unsaved provider dialogs must discover models using the validated preview id `provider_preview`; do not use hyphenated placeholder ids because Provider Manager id validation rejects them.
- Existing-provider discovery may reuse the stored token only when `provider_id` is present and the dialog did not submit a replacement token.
- Discovery errors and audit events must be secret-safe. Raw tokens must never be returned to the frontend, written to audit logs, or included in exception text.
- Model discovery must normalize upstream `/models` payloads into Provider Manager catalog entries and infer capabilities via `get_model_capabilities` when the upstream payload has no explicit `capabilities` map.
- Aliases are catalog metadata, not provider ids. Normalize aliases as a de-duplicated list of clean strings and keep default text/image selection in `defaults`.
- The provider creation UI must keep a dialog/draft state that can be reused for future edit flow instead of mutating the persisted provider list while the dialog is open.

#### 4. Validation & Error Matrix

| Condition | Required handling |
| --- | --- |
| Draft discovery has no `provider_id` | Use `provider_preview` and require a usable `base_url`; return normalized catalog rows without persisting a provider. |
| Existing provider discovery omits `base_url` or `token` | Load missing values from the stored provider and credential reference. |
| Upstream returns 4xx | Return the same 4xx class with `detail.type == "upstream_error"` and a token-redacted message. |
| Upstream returns 5xx or transport error | Return `502` with `detail.type == "upstream_error"` and a token-redacted message. |
| Upstream request times out | Return `504` with `detail.type == "upstream_timeout"`. |
| Invalid provider id, token shape, source, or health status | Return the normal Provider Manager validation error; record a failed audit event where applicable. |
| UI token visibility toggled | Change only the input type/display state; never print or persist the token outside the save/discover payload. |

#### 5. Good/Base/Bad Cases

- Good: The `新建 provider` button opens a configuration dialog, fetches models from the typed base URL and token, lets the user edit aliases/defaults, and saves one provider payload with a normalized `model_catalog`.
- Base: Manual model entry still works when upstream discovery is unavailable, and capabilities are inferred from known model ids.
- Bad: Calling discovery with `provider-id-preview`, leaking a bearer token in a 502 message, or forcing users to save a provider before they can test model discovery.

#### 6. Tests Required

- Unit: discovery from an unsaved draft provider returns catalog entries using `provider_preview`.
- Unit: discovery for an existing provider can reuse the stored token and does not expose the token in response or audit data.
- Unit/static: provider dialog markup, token hide/show controls, discovery action, alias/default controls, and save payload fields are present.
- Real WSL API smoke: create a copied workspace, exercise discovery/create/catalog/audit routes, and record provider/model counts.
- Real WSL UI smoke: open Provider Manager, use the new-provider dialog, toggle token visibility, fetch models, edit alias/defaults, and save.

#### 7. Wrong vs Correct

Wrong:

```python
provider_id = req.provider_id or "provider-preview"
raise HTTPException(status_code=502, detail=str(exc))
```

Correct:

```python
provider_id = req.provider_id or "provider_preview"
message = _safe_error_message(str(exc), token)
raise HTTPException(status_code=502, detail=_error_detail(message, "upstream_error"))
```

### Scenario: Camoufox Playwright Driver Layout Compatibility

#### 1. Scope / Trigger

- Trigger: Changing account login browser startup, Camoufox launcher behavior, Playwright package versions, Node launcher scripts, proxy identity options, or WSL browser smoke coverage.
- Scope: `camoufox_launcher.py`, Camoufox launch options, Playwright package root discovery, generated compatibility launch scripts, and account login browser startup tests.

#### 2. Signatures

- Python entrypoint: `python src/aistudio_api/infrastructure/browser/camoufox_launcher.py --port <port> [--headless]`.
- Runtime function: `launch_camoufox_server(*, port: int, headless: bool)`.
- Package root helper: `_playwright_package_root(nodejs) -> Path(nodejs).parent / "package"`.
- Launch script selection: use bundled `LAUNCH_SCRIPT` when `lib/browserServerImpl.js` exists; use the Nexus compatibility script when only `lib/coreBundle.js` exists.

#### 3. Contracts

- The launcher must prune `None` values before passing options to Camoufox/Playwright so optional proxy fields do not become JavaScript `null` values.
- Playwright layouts differ by version. Do not assume `lib/browserServerImpl.js` exists; Playwright 1.60 can expose `lib/coreBundle.js` instead.
- The compatibility script must run with `cwd` set to the Playwright package root and must recreate `BrowserServerLauncherImpl` through `coreBundle.js` only when the legacy launcher module is absent.
- Environment options passed to Playwright must be normalized to Playwright's expected array shape when the compatibility path is used.
- A successful real startup smoke is a printed `Websocket endpoint` and no `Cannot find module ... browserServerImpl.js` failure.

#### 4. Validation & Error Matrix

| Condition | Required handling |
| --- | --- |
| `lib/browserServerImpl.js` exists | Use Camoufox's bundled `LAUNCH_SCRIPT`. |
| `lib/browserServerImpl.js` is missing and `lib/coreBundle.js` exists | Generate/use the Nexus compatibility launch script. |
| Both launcher modules are missing | Let Node startup fail visibly; do not mask it as a successful login. |
| Optional proxy or identity option is `None` | Prune it before serializing the launch config. |
| Launch process exits before printing a websocket endpoint | Raise `RuntimeError("Server process terminated unexpectedly")` and preserve command output for diagnosis. |

#### 5. Good/Base/Bad Cases

- Good: WSL account login starts Camoufox against a Playwright 1.60 package that has `coreBundle.js` but no `browserServerImpl.js`.
- Base: Older Playwright packages continue using Camoufox's bundled launch script without the compatibility shim.
- Bad: Editing `camoufox/launchServer.js` in site-packages, hardcoding a local absolute path, or installing a different Playwright version as the only fix.

#### 6. Tests Required

- Unit: `_resolve_launch_script` selects the bundled script for the legacy layout.
- Unit: `_resolve_launch_script` selects and writes the compatibility script for the `coreBundle.js` layout.
- Unit: generated compatibility script contains `coreBundle.js` fallback and `env` normalization.
- Real WSL: launch `camoufox_launcher.py` on a free localhost port and assert a `Websocket endpoint` line appears.

#### 7. Wrong vs Correct

Wrong:

```python
process = subprocess.Popen([nodejs, LAUNCH_SCRIPT], cwd=Path(nodejs).parent / "package")
```

Correct:

```python
launch_script = _resolve_launch_script(nodejs)
process = subprocess.Popen([nodejs, str(launch_script)], cwd=_playwright_package_root(nodejs))
```

### Scenario: Public Distribution Rename With Compatibility Aliases

#### 1. Scope / Trigger

- Trigger: Changing the public project or Python distribution name, or adding/removing console scripts.
- Scope: `pyproject.toml`, README command examples, CLI labels, and user-facing metadata.

#### 2. Signatures

- Preferred CLI names for the Nexus Studio distribution:
	- `nexus-studio = "aistudio_api.main:main"`
	- `nexus-studio-server = "aistudio_api.api.app:main"`
	- `nexus-studio-client = "aistudio_api.infrastructure.gateway.client:cli_main"`
- Legacy compatibility aliases that must remain unless a migration plan explicitly removes them:
	- `aistudio-api = "aistudio_api.main:main"`
	- `aistudio-api-server = "aistudio_api.api.app:main"`
	- `aistudio-api-client = "aistudio_api.infrastructure.gateway.client:cli_main"`

#### 3. Contracts

- The public distribution name may change independently from the Python import package.
- The import package remains `aistudio_api`.
- Runtime environment variables remain `AISTUDIO_*`.
- API paths, runtime data directories, and credential formats must not change as part of a public rename-only task.

#### 4. Validation & Error Matrix

| Condition | Required handling |
| --- | --- |
| New public CLI is added | Keep legacy aliases and update docs to prefer the new CLI. |
| Distribution name changes | Verify package metadata, import smoke, and console script declarations. |
| Existing import package is renamed | Reject unless the task includes a migration plan and broad test coverage. |
| `AISTUDIO_*` env keys are renamed | Reject unless the task includes backward-compatible env migration. |

#### 5. Good/Base/Bad Cases

- Good: `pyproject.toml` publishes `nexus-studio`, adds `nexus-studio*` scripts, and keeps `aistudio-api*` scripts.
- Base: README examples use preferred `nexus-studio*` commands and mention legacy aliases remain available.
- Bad: Removing `aistudio-api` aliases in the same task as a public rename with no deprecation period.

#### 6. Tests Required

- Parse `pyproject.toml` and assert the distribution name, version, preferred scripts, and legacy aliases.
- Run an import smoke for `aistudio_api`.
- Run targeted tests covering config and compatibility routes when labels or CLI metadata are touched.

#### 7. Wrong vs Correct

Wrong:

```toml
[project.scripts]
nexus-studio = "nexus_studio.main:main"
```

Correct:

```toml
[project.scripts]
nexus-studio = "aistudio_api.main:main"
aistudio-api = "aistudio_api.main:main"
```

---

## Testing Requirements

<!-- What level of testing is expected -->

### Scenario: Browser Startup Warmup Gates

#### 1. Scope / Trigger

- Trigger: Changing startup browser warmup, account-pool warmup, AI Studio template capture, `/health` warmup status, or the model used to prepare reusable text templates.
- Scope: `AIStudioClient.warmup`, `RequestCaptureService.warmup`, `BrowserSession` startup-only budgets, FastAPI lifespan warmup, `/health` response, and WSL warmup gate scripts/results.

#### 2. Signatures

- Startup retry owner: `aistudio_api.api.app._warmup_with_retries(warmup, label=..., attempts=..., backoff_seconds=...)`.
- Client warmup signature: `AIStudioClient.warmup(*, navigation_timeout_ms, chat_ready_timeout_ms, botguard_timeout_ms, template_capture_timeout_ms)`.
- Capture warmup signature: `RequestCaptureService.warmup(..., retry_template_capture, template_recovery_attempts, navigation_timeout_ms, chat_ready_timeout_ms, botguard_timeout_ms, template_capture_timeout_ms)`.
- Environment keys:
	- `AISTUDIO_DEFAULT_TEXT_MODEL`: default model for CLI/API requests.
	- `AISTUDIO_WARMUP_TEXT_MODEL`: model used only to capture the startup reusable text-request template.

#### 3. Contracts

- `/health.warmup.status == "complete"` means the account browser warmup prepared a reusable text request template, not just that FastAPI is serving HTTP.
- `/health.warmup.status == "complete"` also requires one successful browser-context `GenerateContent` replay probe for the warmup template; route readiness, textarea readiness, and template capture alone are not sufficient.
- Startup warmup must use `AISTUDIO_WARMUP_TEXT_MODEL`; do not assume `AISTUDIO_DEFAULT_TEXT_MODEL` is safe for template capture because account/model permissions can differ from the request default.
- Startup warmup has one retry owner: the app-level `_warmup_with_retries`. Startup capture must disable the inner template retry (`retry_template_capture=False`, `template_recovery_attempts=1`) so attempts remain bounded and observable.
- Runtime request capture may keep its internal transient recovery retry; do not remove it just to simplify startup behavior.
- Auth/sign-in/invalid account/validation failures are hard failures and must not be retried as transient navigation problems.
- Browser login and credential import/export must preserve Playwright storage-state `origins[].indexedDB` when present. Google cookies and localStorage alone are not a sufficient AI Studio authorization proof.
- When AI Studio navigation uses `/u/<authuser>/...`, request headers such as `x-goog-authuser` must come from the captured browser request. Header-only authuser rewrites are invalid because the bearer/session token can belong to a different Google account slot.

#### 4. Validation & Error Matrix

| Condition | Required handling |
| --- | --- |
| `Page.goto`/readiness/template timeout during startup | Retry only through `_warmup_with_retries`, then mark `/health.warmup.status` failed/partial if exhausted. |
| Google sign-in, missing/invalid auth, unauthorized/forbidden, validation error | Do not retry; surface as hard warmup failure. |
| Warmup template capture succeeds but replay returns 401/403 | Raise `AuthError`, mark warmup failed/partial, and isolate the account with a secret-safe re-login/import reason. |
| Stored account has cookies/localStorage but no needed IndexedDB state | Local account health may report storage readability, but GenerateContent permission is verified only during browser warmup/request replay. |
| Warmup model produces upstream permission denied while another text model can capture | Move the startup template model to `AISTUDIO_WARMUP_TEXT_MODEL`; do not silently change API request defaults. |
| Route candidate needs a Google account slot | Prefer configured `/u/<authuser>/...` URL candidates, then unscoped legacy routes; do not rewrite only request headers after capture. |
| Direct `AIStudioClient.warmup` or direct lifespan probe passes but `/health` gate fails | Record as a blocker; do not claim complete system-test pass. |
| Provider Manager-only Phase 1 smoke passes while global Google warmup fails | Record Provider Manager pass separately and keep global system status blocked. |

#### 5. Good/Base/Bad Cases

- Good: `AIStudioClient.warmup()` captures `AISTUDIO_WARMUP_TEXT_MODEL`, disables inner startup retries, replays the rewritten `GenerateContent` probe once, and `/health` reaches `complete` only after upstream authorization succeeds.
- Base: Unit tests assert startup capture kwargs, retry attempt counts, transient classification, and hard auth behavior.
- Bad: Wrapping `AIStudioClient.warmup()` in app retries while `RequestCaptureService._ensure_template()` also retries internally for startup, multiplying minutes of hidden retry work.
- Bad: Treating an AI Studio page that can open and capture a request as a healthy account when the actual GenerateContent replay returns 401/403.
- Bad: Exporting/importing credentials through a schema that drops `origins[].indexedDB`.

#### 6. Tests Required

- Unit: classify transient vs hard warmup errors.
- Unit: startup warmup passes bounded navigation/readiness/template budgets and uses `AISTUDIO_WARMUP_TEXT_MODEL`.
- Unit: startup warmup does not internally retry template capture and outer retry controls attempt count.
- Unit: warmup replay turns 401/403 into `AuthError` and does not report complete on permission failure.
- Unit: credential import/export preserves optional `indexedDB` arrays.
- Unit: account-pool warmup isolates auth failures with a secret-safe guidance reason.
- Full unit suite after warmup changes.
- Real WSL API: exercise a copied real accounts directory and assert success or a diagnosed `authentication_error` rather than false-ready/500 behavior.
- Real WSL UI: open Local Studio in Playwright, send a prompt, and assert `localStudioBusy == false` plus `localStudioCanSend == true` after success or any diagnosed upstream auth error.

#### 7. Wrong vs Correct

Wrong:

```python
await _warmup_with_retries(client.warmup, label="Account browser")
# Inside client.warmup, capture warmup still retries template capture internally.
await capture_service.warmup(model=DEFAULT_TEXT_MODEL)
```

Correct:

```python
await _warmup_with_retries(client.warmup, label="Account browser")
captured = await capture_service.warmup(
		model=DEFAULT_WARMUP_TEXT_MODEL,
		retry_template_capture=False,
		template_recovery_attempts=1,
)
await replay_service.replay(captured, kind="warmup_probe")
```

Wrong:

```python
state = await context.storage_state()
path.write_text(json.dumps(state))
```

Correct:

```python
state = await context.storage_state(indexed_db=True)
path.write_text(json.dumps(state))
```

### Scenario: Architecture-Driven System Test Plan Updates

#### 1. Scope / Trigger

- Trigger: Updating `ARCHITECTURE.md`, introducing a new runtime boundary, or changing which product surfaces share backend control/data-plane behavior.
- Scope: `SYSTEM_TEST_PLAN.md` and the executable contract test that keeps it aligned with architecture claims.

#### 2. Signatures

- Source architecture document: `ARCHITECTURE.md`.
- Required global system plan: `SYSTEM_TEST_PLAN.md`.
- Required contract test file for architecture/test-plan alignment: `tests/unit/test_system_test_plan_architecture_contract.py`.

#### 3. Contracts

- `SYSTEM_TEST_PLAN.md` must name every new shared runtime boundary that affects real end-to-end verification.
- Future-facing architecture claims must be gated by evidence checks, not marked passing before routes, UI, registry state, gateway integration, and request logs exist.
- Real system test requirements must distinguish local unit/doc contracts from WSL API/UI smoke results and external-service blockers.

#### 4. Validation & Error Matrix

| Condition | Required handling |
| --- | --- |
| Architecture adds a shared control plane or data plane | Add explicit test-plan coverage and a doc contract assertion. |
| Architecture describes a staged rollout | Add phase gates and define pass/fail/not-applicable evidence. |
| A future feature has no implemented API/UI/runtime evidence | Gate it as not applicable or blocked; do not record it as pass. |
| WSL real system test fails on an external dependency | Preserve the artifact path and failure reason; do not claim complete system-test pass. |

#### 5. Good/Base/Bad Cases

- Good: Provider Manager, shared provider-model pool, runtime gateway, routing policy, fallback, and audit logs are all present in `SYSTEM_TEST_PLAN.md` and asserted by a unit contract.
- Base: Existing Local Studio API/UI smoke remains unchanged while new architecture sections add phase gates for not-yet-implemented surfaces.
- Bad: Updating architecture with Provider Manager claims while the system plan only tests legacy Local Studio endpoints.

#### 6. Tests Required

- Unit contract: assert that `SYSTEM_TEST_PLAN.md` contains the architecture terms and evidence gates needed for the changed runtime boundary.
- Full unit suite: run all `tests/unit` after modifying the contract.
- Real WSL system test: run API and UI smoke from a copied workspace with real configured credentials, and record any controlled limitation or blocker.

#### 7. Wrong vs Correct

Wrong:

```markdown
Provider Manager testing: covered by existing Local Studio smoke.
```

Correct:

```markdown
Provider Manager gates are not applicable until route/page/registry/gateway/log evidence exists; Local Studio smoke remains a separate shared-runtime consumer check.
```

---

## Code Review Checklist

<!-- What reviewers should check -->

(To be filled by the team)
