# Provider Manager Architecture

## Goal

Expand the existing Nexus Studio architecture document to capture a fuller target design for extracting Local Studio provider management into a dedicated Provider Manager page, while preserving the current independent Google AI Studio base modules.

## What I Already Know

- Existing `ARCHITECTURE.md` already describes Local Studio as a higher-level workspace that reuses Playground, image generation, account management, and request logs.
- The desired direction is to move Local Studio-level provider management into an independent page named Provider Manager.
- Google AI Studio should be represented as a built-in provider.
- Users may add multiple custom OpenAI-compatible providers.
- Providers and their available models form a shared pool.
- The pool should serve multiple external protocol shapes: OpenAI Responses, OpenAI Chat Completions, Gemini, and Claude Messages.
- Local Studio and other AI clients should call the same pool through whichever compatible protocol they need.
- The pool should define routing rules to choose an appropriate provider/model for each request.

## Requirements

- Update `ARCHITECTURE.md` with a comprehensive Provider Manager and provider/model pool target architecture.
- Keep existing diagrams and their intent intact; supplement rather than replace the current architecture.
- Describe control-plane responsibilities: provider CRUD, credential handling, model discovery/manual model registration, health checks, and routing policy configuration.
- Describe data-plane responsibilities: protocol adapters, canonical request/response shape, model pool router, provider executors, fallback and response conversion.
- Explicitly cover the four external protocol surfaces: OpenAI Responses, OpenAI Chat Completions, Gemini, Claude Messages.
- Show how Local Studio becomes one consumer of the shared pool rather than the owner of provider management.
- Include routing-policy considerations: model aliases, capability matching, health, quotas/rate limits, priority/weight, cost/latency, sticky/fallback behavior, streaming/tool/image compatibility.
- Preserve the principle that original Google AI Studio base modules remain independently usable.

## Acceptance Criteria

- [x] `ARCHITECTURE.md` includes a new Provider Manager / provider-model pool section.
- [x] The new section includes at least one clear Mermaid architecture diagram.
- [x] The documentation clearly distinguishes Provider Manager control plane from runtime gateway data plane.
- [x] The documentation explains how the four compatible API formats map into one shared model pool.
- [x] The documentation calls out rollout boundaries and design constraints for future implementation.
- [x] Markdown renders with valid Mermaid syntax.

## Out of Scope

- No backend implementation in this task.
- No frontend Provider Manager page implementation in this task.
- No API contract or storage schema migration in this task.
- No real environment test required beyond documentation validation, because this is documentation-only.

## Technical Notes

- Existing architecture doc: `ARCHITECTURE.md`.
- Existing API route analysis from this session shows current exposed OpenAI, Gemini, Local Studio, accounts, image sessions, request log, static, and generated image endpoints.
- This task should produce a design-ready architecture note that can later be decomposed into implementation tasks.
- Spec update decision: no `.trellis/spec/` update is required for this documentation-only task because it does not introduce executable API signatures, storage contracts, validation matrices, or implementation conventions.