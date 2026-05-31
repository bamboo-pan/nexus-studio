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
