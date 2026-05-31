# Nexus Studio 架构

本文记录 Nexus Studio 当前 Local Studio 高层化改造的目标架构。重点是：原始基础模块保持独立可用，Local Studio 作为更高层的统一工作台复用这些能力，并通过 provider 与工具开关组织多功能会话。

## Local Studio 与 Google AI Studio 基础业务线

```mermaid
flowchart LR
    Nav["左侧导航"] --> Playground["Playground\n独立入口"]
    Nav --> ImagePage["图片生成\n独立入口"]
    Nav --> LocalStudio{{"Local Studio\n高层统一工作台"}}
    Nav --> RequestLogs["请求记录\n共享请求管理入口"]
    Nav --> AccountsPage["账号管理\n独立入口"]

    subgraph Base["原始基础模块继续独立工作"]
        ChatSearch["Google AI Studio\n对话 + 搜索线"]
        ChatImage["Google AI Studio\n对话 + 画图线"]
        AccountLine["Google AI Studio\n账号管理线"]
    end

    Playground --> ChatSearch
    ImagePage --> ChatImage
    AccountsPage --> AccountLine

    subgraph LSLayer["Local Studio 高层编排"]
        ProviderMgmt["Provider 管理"]
        ProviderChoice{"选择 Provider"}
        Common["通用设置\n模型 / Interface / Stream\nThinking / Timeout / 能力"]
        Cache["缓存默认开启"]
        ToolSwitch["工具开关\n搜索 / 画图"]
        Engine["统一对话引擎\nprovider + model + tools + options"]
    end

    LocalStudio --> ProviderMgmt
    LocalStudio --> Common
    LocalStudio --> Cache
    LocalStudio --> ToolSwitch
    ProviderMgmt --> ProviderChoice

    ChatSearch -. "包装为默认 provider 的搜索能力\n不影响 Playground" .-> GoogleProvider["默认 Provider\nGoogle AI Studio\n无需 URL / Token"]
    ChatImage -. "包装为默认 provider 的画图能力\n不影响图片生成" .-> GoogleProvider
    AccountLine -. "复用内置账号\n不影响账号管理" .-> GoogleProvider

    ProviderChoice --> GoogleProvider
    ProviderChoice --> OpenAIProvider["自定义 Provider\nOpenAI-compatible\nBase URL + Token"]

    GoogleProvider --> GeminiChat["Gemini 对话模型"]
    GoogleProvider --> GoogleSearch["Google Search 工具"]
    GoogleProvider --> GoogleImage["Google 画图工具"]

    OpenAIProvider --> OpenAIModels["兼容模型"]
    OpenAIProvider --> OpenAISearch["OpenAI Search 工具"]
    OpenAIProvider --> OpenAIImage["OpenAI 画图工具"]

    Common --> Engine
    Cache --> Engine
    ToolSwitch --> Engine
    GeminiChat --> Engine
    GoogleSearch --> Engine
    GoogleImage --> Engine
    OpenAIModels --> Engine
    OpenAISearch --> Engine
    OpenAIImage --> Engine

    Engine --> Output["Local Studio 输出\n聊天 / 搜索结果 / 图片\n工具过程 / 思考轨迹"]

    RequestService[["请求管理服务\n横向服务所有模块"]]
    ChatSearch --> RequestService
    ChatImage --> RequestService
    AccountLine --> RequestService
    Engine --> RequestService
    RequestLogs --> RequestService

    classDef nav fill:#f8fafc,stroke:#64748b,stroke-width:1.5px,color:#111827;
    classDef base fill:#f0fdf4,stroke:#16a34a,stroke-width:1.5px,color:#111827;
    classDef studio fill:#eef2ff,stroke:#4f46e5,stroke-width:2px,color:#111827;
    classDef provider fill:#fff7ed,stroke:#ea580c,stroke-width:1.5px,color:#111827;
    classDef request fill:#fefce8,stroke:#ca8a04,stroke-width:2px,color:#111827;
    classDef output fill:#fdf2f8,stroke:#db2777,stroke-width:1.5px,color:#111827;

    class Nav,Playground,ImagePage,RequestLogs,AccountsPage nav;
    class ChatSearch,ChatImage,AccountLine base;
    class LocalStudio,ProviderMgmt,ProviderChoice,Common,Cache,ToolSwitch,Engine studio;
    class GoogleProvider,OpenAIProvider,GeminiChat,GoogleSearch,GoogleImage,OpenAIModels,OpenAISearch,OpenAIImage provider;
    class RequestService request;
    class Output output;
```

## Local Studio 统一对话引擎工具调用语义

工具开关表示允许模型使用该工具，不表示每次请求都强制调用工具。普通对话仍然可以保持普通对话；只有当用户请求确实需要搜索或画图时，才调用已启用的工具。

```mermaid
flowchart TD
    User["用户发送消息"] --> Provider{"当前 Provider"}

    Provider -->|默认| Gemini["Gemini Provider\nGoogle AI Studio"]
    Provider -->|自定义| OpenAI["OpenAI-compatible Provider"]

    Gemini --> GeminiTools{"工具开关"}
    GeminiTools -->|搜索关 + 画图关| GeminiChatOnly["只发送聊天请求\n只能普通对话"]
    GeminiTools -->|搜索开和/或画图开| GeminiOptional["构造多功能会话\n把已启用工具作为可用能力"]

    GeminiOptional --> GeminiModelDecision{"模型判断当前消息是否需要工具"}
    GeminiModelDecision -->|不需要工具| GeminiNormal["普通对话回答"]
    GeminiModelDecision -->|需要搜索且搜索已启用| GeminiSearch["调用 Google Search 工具"]
    GeminiModelDecision -->|需要画图且画图已启用| GeminiImage["调用 Google 画图工具"]
    GeminiModelDecision -->|需要多个能力且均已启用| GeminiMulti["按需组合\n对话 + 搜索 + 画图"]

    OpenAI --> OpenAITools{"工具开关"}
    OpenAITools -->|搜索关 + 画图关| OpenAIChatOnly["只发送聊天请求\n只能普通对话"]
    OpenAITools -->|搜索开和/或画图开| OpenAIRequest["请求中包含已启用工具定义\nsearch / image 可选"]
    OpenAIRequest --> OpenAIModelDecision{"OpenAI-compatible 模型自动判断"}
    OpenAIModelDecision -->|不调用工具| OpenAINormal["普通对话回答"]
    OpenAIModelDecision -->|调用搜索工具| OpenAISearch["Provider 执行搜索工具"]
    OpenAIModelDecision -->|调用画图工具| OpenAIImage["Provider 执行画图工具"]
    OpenAIModelDecision -->|调用多个工具| OpenAIMulti["按 provider 规则组合执行"]

    GeminiChatOnly --> Output["统一输出\n文本 / 工具过程 / 搜索结果 / 图片"]
    GeminiNormal --> Output
    GeminiSearch --> Output
    GeminiImage --> Output
    GeminiMulti --> Output
    OpenAIChatOnly --> Output
    OpenAINormal --> Output
    OpenAISearch --> Output
    OpenAIImage --> Output
    OpenAIMulti --> Output

    Output --> RequestMgmt[["请求管理服务\n记录所有模块请求"]]

    classDef decision fill:#f8fafc,stroke:#64748b,stroke-width:1.5px,color:#111827;
    classDef gemini fill:#eef2ff,stroke:#4f46e5,stroke-width:1.5px,color:#111827;
    classDef openai fill:#fff7ed,stroke:#ea580c,stroke-width:1.5px,color:#111827;
    classDef output fill:#fdf2f8,stroke:#db2777,stroke-width:1.5px,color:#111827;
    classDef request fill:#fefce8,stroke:#ca8a04,stroke-width:2px,color:#111827;

    class User,Provider,GeminiTools,GeminiModelDecision,OpenAITools,OpenAIModelDecision decision;
    class Gemini,GeminiChatOnly,GeminiOptional,GeminiNormal,GeminiSearch,GeminiImage,GeminiMulti gemini;
    class OpenAI,OpenAIChatOnly,OpenAIRequest,OpenAINormal,OpenAISearch,OpenAIImage,OpenAIMulti openai;
    class Output output;
    class RequestMgmt request;
```

## Provider Manager and Shared Provider-Model Pool Target Architecture

The next architecture step is to move provider administration out of Local Studio into a dedicated Provider Manager control plane, then let Local Studio and external compatible API clients consume the same provider-model pool through a runtime data plane. Local Studio remains a first-class workspace, but it becomes one consumer of shared routing and execution instead of the owner of provider management.

Google AI Studio stays as the built-in provider. The original Google AI Studio base modules still remain independently usable through their existing navigation paths: Playground, image generation, account management, and request logs keep their current business boundaries. The built-in Google provider wraps those modules for the shared pool without making Local Studio a dependency of the base modules.

```mermaid
flowchart TB
    subgraph Consumers["Pool consumers"]
        LocalStudioConsumer["Local Studio UI\nchat + tools + images"]
        OpenAIResponsesClient["OpenAI Responses clients"]
        OpenAIChatClient["OpenAI Chat Completions clients"]
        GeminiClient["Gemini-compatible clients"]
        ClaudeClient["Claude Messages-compatible clients"]
    end

    subgraph ProtocolSurface["External compatible protocol surfaces"]
        ResponsesAdapter["OpenAI Responses adapter\n/v1/responses"]
        ChatAdapter["OpenAI Chat Completions adapter\n/v1/chat/completions"]
        GeminiAdapter["Gemini adapter\ngenerateContent + streamGenerateContent"]
        ClaudeAdapter["Claude Messages adapter\n/v1/messages"]
    end

    subgraph ControlPlane["Control plane: Provider Manager"]
        ProviderManagerPage["Provider Manager page"]
        ProviderCrud["Provider CRUD\nenabled state + base URLs + provider type"]
        CredentialStore["Credential references\nsecrets hidden from UI and logs"]
        ModelCatalog["Model catalog\ndiscovery + manual models + aliases"]
        HealthChecks["Health checks\nreadiness + degradation state"]
        RoutingPolicy["Routing policies\ndefaults + priorities + limits"]
    end

    subgraph DataPlane["Data plane: shared runtime gateway"]
        CanonicalRequest["Canonical request\nmessages + tools + modalities + stream intent"]
        ModelPoolRouter["Model pool router\nselect provider + model + attempt plan"]
        ProviderExecutor["Provider executor dispatch"]
        FallbackEngine["Fallback and retry controller"]
        CanonicalResponse["Canonical response\ntext + tool calls + images + stream events"]
        ResponseConverter["Protocol response converter"]
    end

    subgraph ProviderExecutors["Provider executors"]
        GoogleExecutor["Built-in Google AI Studio provider\nexisting chat/search/image/account modules"]
        OpenAIExecutor["OpenAI-compatible provider executor"]
        GeminiExecutor["Gemini-compatible provider executor"]
        ClaudeExecutor["Claude Messages-compatible provider executor"]
    end

    RequestLogService[["Request management service\nshared audit + replay records"]]

    LocalStudioConsumer --> ResponsesAdapter
    OpenAIResponsesClient --> ResponsesAdapter
    OpenAIChatClient --> ChatAdapter
    GeminiClient --> GeminiAdapter
    ClaudeClient --> ClaudeAdapter

    ResponsesAdapter --> CanonicalRequest
    ChatAdapter --> CanonicalRequest
    GeminiAdapter --> CanonicalRequest
    ClaudeAdapter --> CanonicalRequest

    ProviderManagerPage --> ProviderCrud
    ProviderCrud --> CredentialStore
    ProviderCrud --> ModelCatalog
    ModelCatalog --> HealthChecks
    HealthChecks --> RoutingPolicy

    ModelCatalog -. "provider and model snapshot" .-> ModelPoolRouter
    CredentialStore -. "credential references" .-> ProviderExecutor
    HealthChecks -. "health state" .-> ModelPoolRouter
    RoutingPolicy -. "routing rules" .-> ModelPoolRouter

    CanonicalRequest --> ModelPoolRouter
    ModelPoolRouter --> ProviderExecutor
    ProviderExecutor --> GoogleExecutor
    ProviderExecutor --> OpenAIExecutor
    ProviderExecutor --> GeminiExecutor
    ProviderExecutor --> ClaudeExecutor

    GoogleExecutor --> FallbackEngine
    OpenAIExecutor --> FallbackEngine
    GeminiExecutor --> FallbackEngine
    ClaudeExecutor --> FallbackEngine
    FallbackEngine --> CanonicalResponse
    CanonicalResponse --> ResponseConverter
    ResponseConverter --> ResponsesAdapter
    ResponseConverter --> ChatAdapter
    ResponseConverter --> GeminiAdapter
    ResponseConverter --> ClaudeAdapter

    CanonicalRequest --> RequestLogService
    ProviderExecutor --> RequestLogService
    CanonicalResponse --> RequestLogService

    classDef consumer fill:#f8fafc,stroke:#64748b,stroke-width:1.5px,color:#111827;
    classDef surface fill:#ecfeff,stroke:#0891b2,stroke-width:1.5px,color:#111827;
    classDef control fill:#eef2ff,stroke:#4f46e5,stroke-width:1.8px,color:#111827;
    classDef data fill:#fff7ed,stroke:#ea580c,stroke-width:1.8px,color:#111827;
    classDef executor fill:#f0fdf4,stroke:#16a34a,stroke-width:1.5px,color:#111827;
    classDef request fill:#fefce8,stroke:#ca8a04,stroke-width:2px,color:#111827;

    class LocalStudioConsumer,OpenAIResponsesClient,OpenAIChatClient,GeminiClient,ClaudeClient consumer;
    class ResponsesAdapter,ChatAdapter,GeminiAdapter,ClaudeAdapter surface;
    class ProviderManagerPage,ProviderCrud,CredentialStore,ModelCatalog,HealthChecks,RoutingPolicy control;
    class CanonicalRequest,ModelPoolRouter,ProviderExecutor,FallbackEngine,CanonicalResponse,ResponseConverter data;
    class GoogleExecutor,OpenAIExecutor,GeminiExecutor,ClaudeExecutor executor;
    class RequestLogService request;
```

### Control Plane Responsibilities

The control plane is the administrative side of the provider-model pool. It is owned by the Provider Manager page and supporting provider services, and it should be usable without entering a Local Studio conversation.

- Provider CRUD: create, edit, enable, disable, and delete provider records. The built-in Google AI Studio provider is present by default and does not require a custom base URL or token. Custom providers can include OpenAI-compatible endpoints first, with Gemini-compatible and Claude Messages-compatible provider executors available as the pool grows.
- Credential handling: store tokens, cookies, account bindings, or future secret references separately from display metadata. Secrets must not be returned to browser clients, copied into request logs, or included in model discovery output.
- Model discovery and manual models: discover provider models when the provider supports listing, and allow manual model registration for providers that do not expose reliable model-list endpoints. Each model entry should include provider ownership, external model id, friendly name, capabilities, context limits, modality support, and optional aliases.
- Health checks: track provider readiness, authentication failures, model availability, quota exhaustion, latency degradation, and last successful request time. Health state informs routing, but it should not delete provider configuration.
- Routing policy configuration: define defaults, aliases, weights, priorities, fallback chains, rate-limit budgets, and compatibility requirements outside Local Studio session state.
- Audit and safety: update request management records for configuration-affecting actions without storing sensitive credential values.

### Data Plane Responsibilities

The data plane is the runtime gateway that receives compatible API requests, normalizes them, routes them through the model pool, executes provider calls, and converts responses back to the caller protocol.

- Protocol adapters: expose OpenAI Responses, OpenAI Chat Completions, Gemini generateContent or streamGenerateContent, and Claude Messages-compatible request surfaces. Each adapter validates its native request shape before mapping into the canonical request.
- Canonical request shape: represent messages, system/developer instructions, tool definitions, tool choice, streaming intent, images or files, output modalities, sampling options, reasoning or thinking options, user metadata, and the requested model selector in one internal format.
- Model pool router: resolve aliases and defaults, match capabilities, rank candidate provider-model entries, apply health and policy constraints, and create an ordered attempt plan.
- Provider executors: translate the canonical request to the selected provider's native protocol. Google AI Studio uses the existing base modules as the built-in executor; custom OpenAI-compatible providers use OpenAI-shaped HTTP calls; Gemini and Claude-compatible executors translate to their own message and stream formats.
- Fallback controller: decide when an error is retryable, when to stay on the same provider, when to fall back to another provider/model, and how to preserve caller-visible semantics during streaming.
- Canonical response shape: capture text deltas, final text, tool calls, tool results, image outputs, usage, provider attempt metadata, finish reasons, and errors before protocol-specific conversion.
- Response conversion: return the native response or stream event format expected by the original caller, including OpenAI Responses events, Chat Completions chunks, Gemini stream parts, and Claude Messages content blocks.

### Compatible Protocol Surfaces

The pool is protocol-agnostic internally, but compatibility is exposed at the edges so existing AI clients can connect without knowing about Nexus Studio internals.

| External surface | Inbound responsibility | Canonical mapping focus | Outbound responsibility |
| --- | --- | --- | --- |
| OpenAI Responses | Accept response creation, streaming, multimodal input, tool declarations, and response-format options. | Map instructions, input items, tools, stream intent, model aliases, and modalities into canonical request fields. | Emit Responses objects or streaming events with converted output items, tool calls, usage, and finish state. |
| OpenAI Chat Completions | Accept chat messages, system prompts, tools/functions, tool choice, streaming, and sampling options. | Map roles, message content parts, tools, tool-call ids, and model selectors into canonical request fields. | Emit Chat Completions responses or chunks, preserving choices, deltas, tool calls, usage, and finish reasons. |
| Gemini | Accept generateContent and streamGenerateContent style content, parts, generation config, safety settings, and tools where supported. | Map contents, parts, function declarations, generation config, modality inputs, and model names into canonical request fields. | Emit Gemini-compatible candidates, parts, safety metadata where available, and streaming updates. |
| Claude Messages | Accept messages, system prompt, tools, tool choice, streaming, max tokens, and content blocks. | Map content blocks, thinking/tool compatibility, model selector, and stop conditions into canonical request fields. | Emit Claude-compatible message responses or stream events with text blocks, tool-use blocks, usage, and stop reasons. |

### Routing Policy Considerations

Routing policy is the contract between Provider Manager configuration and runtime execution. The router should make deterministic decisions from a policy snapshot and request metadata, then record the selected attempt plan in request management logs.

- Aliases and defaults: support global defaults, per-protocol defaults, Local Studio defaults, and aliases such as `default`, `fast`, `vision`, or project-specific names that resolve to one or more provider-model candidates.
- Capability matching: require model support for text chat, streaming, tool/function calling, image input, image generation, search grounding, reasoning or thinking options, structured output, context length, and file handling before a candidate can be selected.
- Health and readiness: exclude disabled providers, unhealthy credentials, exhausted accounts, or degraded models according to policy. Allow explicit override only when the caller or administrator requests it.
- Quotas and rate limits: consider provider-level, credential-level, account-level, model-level, and per-client budgets. The router should avoid selecting a candidate that is known to be unavailable because of active limit windows.
- Priority and weight: use priority for ordered fallback preference and weight for load distribution among equivalent candidates. Health or quota state can temporarily reduce a candidate's effective weight.
- Cost and latency: allow policy to prefer lower latency, lower cost, higher quality, or balanced routing. Cost and latency should be recorded per attempt when providers expose enough usage data.
- Sticky behavior: optionally keep a conversation, client, or session on the same provider-model while it remains healthy to reduce behavior drift. Sticky routing must still respect hard capability, quota, and credential failures.
- Fallback behavior: define which errors are retryable, which require a different credential on the same provider, which require a different model, and which must be returned directly to the caller. Streaming fallback is only safe before any irreversible response chunk has been sent.
- Streaming, tool, and image compatibility: do not route streaming requests to non-streaming executors, tool requests to models without compatible tool-call semantics, image-input requests to text-only models, or image-generation requests to chat-only models unless a policy-defined adapter can preserve semantics.

### Local Studio Boundary After Extraction

Local Studio should read provider/model availability from the shared pool and store only conversation-scoped choices: selected model alias, enabled tools, interface settings, stream preference, thinking options, timeout, cache preference, and other per-session options. It should no longer own provider CRUD, credentials, model discovery, or global routing policy.

Other AI clients can call the same pool through OpenAI Responses, OpenAI Chat Completions, Gemini, or Claude Messages-compatible APIs. Those clients should receive protocol-native behavior while sharing the same health, credential, model catalog, routing policy, request logging, and provider executor infrastructure used by Local Studio.

### Google AI Studio Base Module Constraint

The Google AI Studio integration remains the built-in provider, not an implementation detail of Local Studio. The existing Playground, image generation, account management, and request log paths must keep working independently. Provider Manager can surface Google AI Studio as an always-available provider record and can read account/model health from the existing modules, but it must not move the base module workflows behind Local Studio-only state.

### Rollout Boundaries

- Documentation task boundary: this section is target architecture only. It does not introduce backend routes, storage migrations, Provider Manager UI code, or executor changes.
- First implementation boundary: create Provider Manager configuration surfaces and provider/model registry abstractions while preserving current Local Studio behavior through compatibility adapters.
- Second implementation boundary: move runtime request selection to the shared model pool and make Local Studio consume it through the same gateway as external compatible clients.
- Later implementation boundary: add advanced routing policies, provider health automation, quota-aware load balancing, cost/latency scoring, sticky routing, and controlled streaming fallback.
- Compatibility boundary: every rollout step must keep original Google AI Studio base modules independently usable and must preserve existing request management visibility across modules.
