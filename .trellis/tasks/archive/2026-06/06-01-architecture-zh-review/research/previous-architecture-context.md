# Previous Architecture Context

## Source Task

- Previous task: `.trellis/tasks/archive/2026-05/05-31-provider-manager-architecture`.
- Previous task goal: expand `ARCHITECTURE.md` with a target architecture for extracting Local Studio provider management into Provider Manager while preserving independent Google AI Studio base modules.

## Content Added By Previous Task

- A Provider Manager / shared provider-model pool target architecture section.
- One Mermaid diagram showing pool consumers, protocol surfaces, control plane, data plane, provider executors, and request management service.
- Control-plane responsibilities: provider CRUD, credential handling, model catalog, health checks, routing policies, audit/safety.
- Data-plane responsibilities: protocol adapters, canonical request and response, model pool router, provider executors, fallback controller, protocol response conversion.
- Compatible protocol surfaces: OpenAI Responses, OpenAI Chat Completions, Gemini, Claude Messages.
- Routing policy considerations: aliases/defaults, capability matching, health/readiness, quota/rate limit, priority/weight, cost/latency, sticky behavior, fallback, streaming/tool/image compatibility.
- Boundary notes: Local Studio becomes a pool consumer; Google AI Studio remains the built-in provider and base modules stay independently usable; rollout is documentation-only for that task.

## Translation Constraints

- Translate prose and visible Mermaid labels into Chinese for consistency with the rest of `ARCHITECTURE.md`.
- Keep stable product/protocol names in English where they are external identifiers: Local Studio, Provider Manager, Google AI Studio, OpenAI Responses, OpenAI Chat Completions, Gemini, Claude Messages.
- Preserve all architecture relationships and rollout boundaries from the previous task.