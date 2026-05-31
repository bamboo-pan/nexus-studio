# Rename Repository To Nexus Studio

## Goal

Rename the public repository/project identity to **Nexus Studio**, update relevant documentation and package metadata, record that this project is a secondary development based on `https://github.com/chrysoljq/aistudio-api`, then publish the repository to `https://github.com/bamboo-pan/nexus-studio` with Git tag `1.0`.

## Requirements

- Update public-facing project titles and introductory text from `AI Studio API` / `aistudio-api` to `Nexus Studio` where the text describes this repository or its install/clone flow.
- Update clone/setup examples to use `https://github.com/bamboo-pan/nexus-studio.git` and `cd nexus-studio`.
- Add clear upstream attribution in both Chinese and English READMEs: Nexus Studio is a secondary development based on `chrysoljq/aistudio-api`.
- Update package metadata to use the new distribution identity `nexus-studio` and release version `1.0.0` for the `1.0` tag.
- Prefer new console script names (`nexus-studio`, `nexus-studio-server`, `nexus-studio-client`) while preserving old `aistudio-api*` aliases for compatibility.
- Update related docs such as `.env.example`, `SYSTEM_TEST_PLAN.md`, and architecture wording when they refer to this repository/project rather than the upstream Google AI Studio service.
- Keep stable runtime contracts unchanged: Python import package `aistudio_api`, `AISTUDIO_*` environment variables, request/response APIs, saved data layouts, and Google AI Studio terminology.
- Do not commit runtime data, credentials, request logs, local conversations, generated images, virtual environments, or other ignored local artifacts.
- Push the repository to `https://github.com/bamboo-pan/nexus-studio` and create/push Git tag `1.0` after verification.

## Acceptance Criteria

- [ ] `README.md` and `README_EN.md` present Nexus Studio as the project name and include upstream attribution.
- [ ] Quick-start clone paths and console command examples use the new repository and preferred `nexus-studio*` commands.
- [ ] `pyproject.toml` metadata identifies the distribution as `nexus-studio` version `1.0.0`, with compatibility aliases retained.
- [ ] Related docs no longer point users at the old `bamboo-pan/aistudio-api` repo for this project.
- [ ] Tests/type checks relevant to metadata/docs changes pass, or any skipped real-environment coverage is explicitly reported.
- [ ] A Git repository exists locally, remote `origin` points to `https://github.com/bamboo-pan/nexus-studio`, current work is committed, and tag `1.0` is pushed.

## Definition of Done

- Focused code/docs changes only; no unrelated refactors.
- Unit tests or lightweight verification pass locally.
- Sensitive local data remains ignored and uncommitted.
- The GitHub remote and release tag are created/pushed successfully, unless blocked by authentication or remote permissions.

## Technical Approach

Use a conservative rename: change public branding, documentation, package distribution metadata, and preferred command examples, but keep internal module names and environment variable names stable to avoid breaking imports, tests, existing user configs, and runtime data. Add new console scripts alongside existing aliases rather than removing aliases.

## Decision (ADR-lite)

**Context**: The repository contains extensive imports and tests under `src/aistudio_api/`, and runtime configuration uses `AISTUDIO_*` variables. Renaming those internals would be a broad compatibility migration unrelated to the requested repository/docs rename.

**Decision**: Rename public identity to Nexus Studio and add new package/CLI names, while preserving internal `aistudio_api` module names, `AISTUDIO_*` environment variables, and legacy CLI aliases.

**Consequences**: The release can be published safely as `nexus-studio` without breaking existing scripts. Some internal implementation names still mention AI Studio because they describe the upstream Google AI Studio service or compatibility layer.

## Out of Scope

- Renaming the Python import package from `aistudio_api`.
- Renaming `AISTUDIO_*` environment variables or runtime data directories.
- Changing API behavior, UI behavior, authentication, or provider routing.
- Migrating historical local data.

## Technical Notes

- Public docs inspected: `README.md`, `README_EN.md`, `ARCHITECTURE.md`, `SYSTEM_TEST_PLAN.md`, `.env.example`.
- Package metadata inspected: `pyproject.toml`.
- `.gitignore` already excludes `/data/`, `.venv/`, generated artifacts, `.env`, and local test captures.
- Upstream project to credit: `https://github.com/chrysoljq/aistudio-api`.
- Target remote: `https://github.com/bamboo-pan/nexus-studio`.
