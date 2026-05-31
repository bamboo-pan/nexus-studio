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

(To be filled by the team)

---

## Code Review Checklist

<!-- What reviewers should check -->

(To be filled by the team)
