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
- Startup warmup must use `AISTUDIO_WARMUP_TEXT_MODEL`; do not assume `AISTUDIO_DEFAULT_TEXT_MODEL` is safe for template capture because account/model permissions can differ from the request default.
- Startup warmup has one retry owner: the app-level `_warmup_with_retries`. Startup capture must disable the inner template retry (`retry_template_capture=False`, `template_recovery_attempts=1`) so attempts remain bounded and observable.
- Runtime request capture may keep its internal transient recovery retry; do not remove it just to simplify startup behavior.
- Auth/sign-in/invalid account/validation failures are hard failures and must not be retried as transient navigation problems.

#### 4. Validation & Error Matrix

| Condition | Required handling |
| --- | --- |
| `Page.goto`/readiness/template timeout during startup | Retry only through `_warmup_with_retries`, then mark `/health.warmup.status` failed/partial if exhausted. |
| Google sign-in, missing/invalid auth, unauthorized/forbidden, validation error | Do not retry; surface as hard warmup failure. |
| Warmup model produces upstream permission denied while another text model can capture | Move the startup template model to `AISTUDIO_WARMUP_TEXT_MODEL`; do not silently change API request defaults. |
| Direct `AIStudioClient.warmup` or direct lifespan probe passes but `/health` gate fails | Record as a blocker; do not claim complete system-test pass. |
| Provider Manager-only Phase 1 smoke passes while global Google warmup fails | Record Provider Manager pass separately and keep global system status blocked. |

#### 5. Good/Base/Bad Cases

- Good: `AIStudioClient.warmup()` captures `AISTUDIO_WARMUP_TEXT_MODEL`, disables inner startup retries, and `/health` reaches `complete` only after template readiness.
- Base: Unit tests assert startup capture kwargs, retry attempt counts, transient classification, and hard auth behavior.
- Bad: Wrapping `AIStudioClient.warmup()` in app retries while `RequestCaptureService._ensure_template()` also retries internally for startup, multiplying minutes of hidden retry work.

#### 6. Tests Required

- Unit: classify transient vs hard warmup errors.
- Unit: startup warmup passes bounded navigation/readiness/template budgets and uses `AISTUDIO_WARMUP_TEXT_MODEL`.
- Unit: startup warmup does not internally retry template capture and outer retry controls attempt count.
- Full unit suite after warmup changes.
- Real WSL: `/health` gate must report `complete` before claiming global system-test pass; preserve artifact root and failure signature if it does not.

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
await capture_service.warmup(
		model=DEFAULT_WARMUP_TEXT_MODEL,
		retry_template_capture=False,
		template_recovery_attempts=1,
)
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
