# Local Studio WebUI 真实系统测试计划

## 目标

这份计划用于验证 Local Studio、它复用的 WebUI 基础模块，以及 Provider Manager / shared provider-model pool 目标架构在真实环境中的完整用户路径和架构契约。测试必须从用户真实入口出发，覆盖浏览器 UI、后端 API、上游 provider、请求记录和本地持久化，不用 mock 结果替代真实链路。

本文件是 Local Studio WebUI、Provider Manager 控制面、shared provider-model pool、共享基础 WebUI 模块、兼容 API、本地持久化、请求记录和真实 provider 集成的全局最高测试纲领。任何任务内 smoke 脚本、临时验证脚本、人工验收清单、PR 说明或测试报告都不能降低本文件的通过标准；如果脚本结果和本文件冲突，以本文件为准，并且必须先补齐脚本断言再声明系统测试通过。

重点回归已报告问题：

* Google AI Studio provider 在 Local Studio Responses 模式下开启图片工具后，对话触发生图时上游返回 `Please enable tool_config.include_server_side_tool_invocations to use Built-in tools with Function calling.`。
* 自定义 OpenAI-compatible provider 在 Local Studio Responses 模式下开启 search 后，上游流式 HTTP 400 被后端错误读取为 `httpx.ResponseNotRead`，导致 ASGI 异常。
* 自定义 OpenAI-compatible provider 在 Local Studio Responses 模式下开启 search 后，请求体错误发送 `web_search_preview`，上游返回 `HTTP 400: Unsupported tool type: web_search_preview`。
* 自定义 OpenAI-compatible provider 在 Local Studio Responses 模式下开启 `reasoning=high` + stream 后，上游返回 reasoning summary 但最终 `local_studio.completed` conversation 和 UI 没有可见思考过程。
* Google/Gemini 账号预热完成后，首次真实对话仍长时间无输出；账号态文本预热必须先通过每账号 native UI worker pool 完成真实 warmup `GenerateContent` probe，并通过 `GET /health` 的 warmup 状态区分“API 已启动”和“账号 native worker 已就绪”。旧浏览器捕获/模板路径只作为 raw replay fallback 的兼容准备，不能作为账号态文本 readiness 的硬门槛。
* Google AI Studio 账号态文本请求必须通过每账号 native UI worker pool 复用独立干净进程；不能退回到每请求启动 helper，也不能回到同进程 hook 污染路径导致重新登录后 403。
* Google AI Studio native UI worker 在真实 AI Studio 页面中没有选中目标文本模型：例如请求 `gemini-3.5-flash` 时页面停在 `chat spark playground`，模型弹层只暴露 `Gemini 3 Flash Preview`，最终返回 `text_model_not_found`。系统测试必须通过真实用户可见的模型选择路径复现并判定该问题，不能只用 API 模型列表或脚本内置模型名假设选择成功。
* Local Studio 重复发送历史 prompt 会立即复用旧结果；结果重放缓存必须移除，重复 prompt 必须 fresh upstream。
* Local Studio stream 模式在 UI 中表现为一次性输出；SSE、no-buffer headers 和前端响应式更新必须让增量文本可见。

## 测试原则

* 所有 P0/P1 用例都在 WSL 临时目录的新副本中运行，不能直接污染开发工作区。
* 测试环境必须与开发环境分离。系统测试只能从干净临时副本执行，并记录源 commit、临时副本 commit、`git status --porcelain`、依赖安装命令和数据目录；任何直接在开发工作区、带未提交补丁的工作区、或复用开发数据目录跑出的结果都只能算诊断，不能声明通过。
* P0/P1 provider 集成路径必须使用本计划指定的真实凭据并真实通过：Google AI Studio 使用真实账号目录，OpenAI-compatible 使用真实 key 文件。假 token、stub provider、本地 mock upstream、空账号目录、只验证凭据文件存在或只跑单元测试，都只能算诊断/开发辅助，不能把对应 API/UI/系统用例标记为通过。
* Google/Gemini 真实请求必须采用额度友好的模型候选链和 fallback，不能每轮完整系统测试都盯着旗舰模型。默认候选链应优先低额度风险文本模型，例如 `gemini-3-flash-preview`、`gemini-3.1-flash-lite`、`gemma-4-31b-it`、`gemini-flash-lite-latest`、`gemini-flash-latest`，`gemini-3.5-flash` 只能作为显式回归目标或最后后备；`SYSTEM_TEST_MODEL_CANDIDATES` 和 `AISTUDIO_WARMUP_TEXT_MODEL_CANDIDATES` 必须使用同一条候选链。如果目标模型不在模型列表、官方 UI 不可见、无法选择，或只有当前单个 Gemini 模型触发模型级/候选级限额、冷却、不可用或选择失败，脚本必须换用下一个等价文本模型继续真实测试，并在产物中记录 requested model、selected model、fallback_used、candidate_chain、failed_candidate_reasons、quota_or_unavailable_scope、selection_probe_attempts、selection_verified、官方 UI label 和 request-log upstream model。单个 Gemini 模型触发限额不代表账号池、Google provider 或其他 Gemini 模型也被限额，不能把它当作整轮系统测试的外部配额阻塞。只有错误证据明确指向账号/项目级 Google 官方 current/daily quota 耗尽，或候选链中所有等价真实测试模型都因官方额度耗尽不可用时，才标记外部配额阻塞；此时不能通过继续无意义尝试更多 Gemini 模型掩盖真实 quota blocker。
* OpenAI-compatible 使用用户自付费凭据，额度风险低于 Google 账号池；默认文本模型使用 `gpt-5.4-mini`，优先选择便宜、响应快的小模型。若该模型不可用，允许 fallback 到同 provider 的可用 Responses 文本模型，但必须记录实际模型和探测结果；reasoning 用例也优先使用可用的小型 reasoning 模型。
* 每个真实用户路径必须同时有 API 级验证和浏览器 UI 级验证。
* Playwright 浏览器 UI 测试必须通过 MCP browser tools 可见执行：测试人员必须能看到页面导航、点击、输入、模型选择、发送、等待、错误/完成状态和截图/快照证据。纯 headless Playwright 脚本可作为批量自动化补充，但不能单独替代 P0/P1 UI 通过依据。
* UI 测试必须像真实用户一样操作可见控件。禁止把 `page.evaluate()`、Alpine/DOM 状态注入、localStorage 预置、静态 DOM 存在性检查或直接调用内部 JS 方法当作用户路径通过标准；这些只能在用户路径失败后作为诊断手段，并且诊断结果不得覆盖用户路径失败结论。
* UI 测试脚本遇到用户可见错误状态时必须有明确错误处理，而不能只等待最终成功条件：例如 `#images` 的 `.image-error`、Local Studio 的消息错误卡、timeout/重试按钮、provider 不可用或账号池不可用状态。脚本必须读取可见错误摘要，按真实用户路径点击 `重试` 或切换等价 fallback 模型/账号，记录每次 attempt、模型、状态码、错误前缀和最终失败分类；达到有限重试预算后要快速失败并保留证据。禁止在上游已返回可见错误后继续长时间等待不存在的图片、消息或完成状态。
* 请求记录必须开启，关键用例要检查完整生命周期：`client_request`、`upstream_request`、`upstream_response`、`client_response`。
* 用户发送消息的响应时间必须接近直接使用官方 AI Studio 网页的体验。每轮 Google AI Studio 账号态文本系统测试必须测量 Local Studio UI 与官方 AI Studio 直接 UI 在同一账号、同一网络、相同或同级模型、相同 prompt 下的首字延迟和完成耗时；超出本计划预算时即使最终文本正确也不能标为通过。
* 提供方、接口模式、流式、搜索、图片工具、重复 Prompt、Reasoning、附件和会话操作按下面的组合矩阵覆盖。
* 工具开关表示“允许使用”，不是“强制调用”。普通聊天在工具开启时仍必须能正常回答。
* Provider Manager / shared provider-model pool 用例按落地阶段执行。某阶段尚未实现时，可以标记 `not_applicable`，但必须写明当前代码证据，例如没有 Provider Manager route、导航入口、registry schema 或 shared runtime gateway；同一轮仍必须跑完当前阶段的 Local Studio 兼容门禁。
* Provider Manager control plane 和 shared runtime gateway data plane 必须分开验证。控制面负责 provider/model registry、credential references、model catalog、health checks、routing policies 和 audit safety；数据面负责 canonical request、protocol adapters、provider executors、fallback、canonical response、response conversion 和 request logs。
* 每个 P0/P1 用例都必须套用“架构契约断言”；不适用的断言要在结果中标记 `not_applicable` 并说明原因，不能静默跳过。
* 测试脚本中采集到的关键 oracle 字段必须进入失败判定，不能只写入结果文件。例如高推理用例里的 `assistant_has_thinking=false`、`reasoning_summary_visible=false`、`contains_*_error=true` 都必须让测试失败，除非同一结果明确标记 `not_applicable` 且有原因。
* 每次系统测试后必须做一次“计划-脚本对齐审计”：把本计划中的每条 P0/P1 通过标准映射到脚本断言或人工验收项；发现“计划写了但脚本只记录不 fail”的情况，测试结论必须标为失败或不完整。
* `系统测试矩阵覆盖跟踪.xlsx` 必须与本系统测试计划配套使用，作为可复用的测试状态追踪表；每轮真实系统测试后要同步更新矩阵中的用例状态、证据路径、失败原因和真实请求统计，确保用户能从计划理解验收标准，并从表格知晓当前覆盖进度与剩余风险。
* 测试可引用密钥/凭据路径，但不能把真实 token、cookie、storage state、请求日志导出或生成图片提交到 Git。

## 全局高风险遗漏类

这些类别是系统测试最容易“看起来跑了、实际上没判定”的风险区。每轮完整系统测试必须逐项覆盖并在 `architecture-contract-results` 或等价报告中标记 pass/fail/not_applicable；`not_applicable` 必须有具体原因和证据。

| 高风险遗漏类 | 典型遗漏 | 必须硬断言 |
| --- | --- | --- |
| UI 可见性与状态 | 脚本只检查 API 200，但 UI 没显示结果；`Reasoning summary`、图片、工具过程、引用、错误或 usage 被隐藏；pending/tool-running 状态残留 | 所有 `*_visible`、`has_*`、`completed`、pending/error/disabled 状态字段都必须进入 pass/fail；成功路径必须截图或 DOM 断言用户可见结果；失败路径必须断言输入框恢复可用 |
| 提供方与 request-log 签名 | 请求走错 provider/base URL/tool schema；OpenAI-compatible 使用 Google-only tool；Google provider 泄露 token；日志阶段缺失但 summary 仍通过 | `contains_*` 错误签名、provider/tool 名、upstream URL、Authorization 脱敏、lifecycle phase、group id 都必须断言；发现错误签名或缺阶段必须 fail |
| 账号预热状态 | 脚本在后台 warmup 未完成时开始计时，误把冷启动耗时算成“预热后首次请求”；旧 template capture 失败被误判成账号不可用，掩盖 native worker pool 已可真实生成 | 预热相关 smoke 必须先轮询 `GET /health` 的 `warmup.status`，只有 `complete` 后才测首次 Google/Gemini 文本请求延迟；账号态文本 warmup 必须有 native worker pool `GenerateContent` probe 成功证据；`partial`/`failed`/`cancelled` 必须 fail 或记录 controlled limitation |
| Native UI worker 池 | 脚本只看到 API 成功，但实际仍每请求启动 helper、同账号请求被主 Camoufox executor 串行化，或 worker 失败后静默 raw replay 掩盖污染路径 | 账号态文本 smoke 必须断言 `AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT` 默认/配置值、server.log 中 native worker 启动数不超过配置、重复请求复用已启动 worker、并发请求能租用多个 worker；native UI 返回 `401/403/429` 时必须作为上游结果进入失败分类，不能先被 raw replay 覆盖 |
| 能力过程保留 | reasoning/tool/search/image/usage/attachments 在 request log 有，但 API/UI/conversation 丢失；脚本只看最终文本 | 上游返回的 reasoning summary、tool call、search citation、image generation call、usage、附件引用必须至少在 API、最终 SSE、UI、conversation JSON、刷新恢复、request log 中按本计划要求保留；只剩最终文本或图片时必须 fail |
| 恢复与重复路径 | 首次发送通过，但刷新、rerun、重复 prompt、错误重试、删除/导出后状态不一致 | 每个能力至少覆盖一个恢复路径；刷新后 UI 和 conversation JSON 一致；rerun 不污染旧消息；重复 prompt 必须再次走 upstream 且无 cache 标记；删除/导出 lifecycle 可审计 |
| 结果缓存回归 | 同 prompt 第二次立即返回旧结果；UI 或 API 仍暴露 cache namespace / cache hit；脚本只看文本成功 | 重复 prompt 必须新增 upstream request-log phase；API 响应、SSE completed、assistant message 和 UI 均不得出现 Local Studio `cache.hit`/namespace 控件；provider-native 或浏览器底层缓存不得复用最终 assistant 结果 |
| Provider Manager 阶段误判 | 目标架构只写在文档里，测试报告没有说明当前实现阶段；Provider Manager 缺失时把共享池断言全部跳过；开始实现后仍只测 Local Studio 内部 provider 设置 | 每轮必须输出 rollout 阶段门禁结果；未实现阶段必须有代码证据；一旦 Provider Manager route、页面或 registry 存在，控制面/API/UI/日志断言必须进入 P0/P1 fail 条件 |
| shared provider-model pool 串线 | Local Studio、OpenAI Responses、OpenAI Chat Completions、Gemini、Claude Messages 入口各自绕过共享池，模型别名、健康、fallback 或请求日志不一致 | 已进入 shared runtime gateway 阶段后，所有协议入口必须记录同一 provider/model routing decision、attempt plan、credential reference、request log group 和 response conversion 证据 |
| `not_applicable` 滥用 | provider/model 没返回能力时直接跳过，掩盖未覆盖的正向路径 | 每个 `not_applicable` 必须写明具体条件、模型/provider、证据字段和替代正向覆盖用例；同一能力在整轮测试中不能只有 `not_applicable` |
| 测试 harness 漏判 | 结果文件记录 `assistant_has_thinking=false`、`secret_redacted=false`、`contains_*_error=true`、`reasoning_summary_visible=false`，但 `failures=[]` | 任何 expected/oracle 字段都必须映射到 fail 条件；计划-脚本对齐审计发现未映射字段时，本轮系统测试结论为失败或不完整 |
| 安全与 artifact 边界 | 截图、server.log、request-log export、conversation JSON 或任务文件带真实 token/cookie/storage state/大图 payload | 提交前和归档前必须扫描 artifacts 与仓库变更；真实凭据、Authorization 明文、Google cookie、storage state、原始大图 payload 出现即 fail，并不得提交 |
| 测试环境混用 | 在开发工作区临时改代码后跑通，或复用开发数据/缓存/账号副本导致新 pull 用户无法复现 | 系统测试报告必须包含 clean-copy evidence：源 commit、临时副本路径、`git status --porcelain` 为空、环境变量数据目录均指向本轮 `RUN_ROOT`，并且没有本轮临时补丁；缺任一项即 fail |
| MCP UI 不可见 | headless 脚本或内部状态注入声称 UI 通过，但没人看到真实用户路径 | P0/P1 UI 报告必须包含 MCP snapshot/screenshot、console/network 摘要、被点击/输入的可见控件列表、最终用户可见状态；没有 MCP 可见 UI 阶段即 fail |
| 官方网页性能差距 | Local Studio 最终返回正确文本，但首字明显慢于官方 AI Studio 直接网页，或等待长到用户以为卡死 | 同账号同网络对照测量官方网页直接 UI 与 Local Studio UI 的 `time_to_first_visible_token_ms`、`time_to_complete_ms`；Local Studio 预热后首字不得超过官方中位数 + 3s 或官方 2 倍中的较大值，完成耗时不得超过官方 1.5 倍 + 5s；超预算即 fail 或明确标记性能阻塞 |

## 测试环境与开发环境分离门禁

每次系统测试必须从用户真实 pull 后会得到的代码状态开始，而不是从当前开发会话里的临时修补状态开始。测试报告必须先输出 `ENV-*` 证据；缺失或失败时，后续 API/UI 成功只能作为诊断信息，不能作为系统测试通过结论。

| ID | 路径 | 步骤 | 通过标准 |
| --- | --- | --- | --- |
| ENV-01 | Git + WSL | 在开发工作区记录 `git rev-parse HEAD` 和 `git status --porcelain`；将仓库 rsync 到 `/home/bamboo/nexus-studio-system-test-YYYYMMDD-HHMMSS/repo`；在临时副本中再次记录 commit/status | 源工作区必须没有本任务以外未提交补丁；临时副本 `git status --porcelain` 为空；测试报告包含两个 commit/status 结果；若为验证已提交代码，临时副本必须从该提交或 PR head 创建 |
| ENV-02 | Data dirs | 设置 `AISTUDIO_LOCAL_STUDIO_DIR`、`AISTUDIO_REQUEST_LOGS_DIR`、`AISTUDIO_GENERATED_IMAGES_DIR`、`AISTUDIO_IMAGE_SESSIONS_DIR`、`AISTUDIO_PROVIDER_MANAGER_DIR` 到本轮 `RUN_ROOT/data/*`；真实账号目录只能作为只读来源或复制到临时目录 | 除 `AISTUDIO_ACCOUNTS_DIR` 明确只读引用外，所有可写数据目录都在本轮 `RUN_ROOT` 下；账号删除/编辑类用例必须使用账号目录副本；不得写入开发工作区 `data/` |
| ENV-03 | Dependencies | 在临时副本新建 venv 并执行 `pip install -e .`、浏览器安装和 native worker preflight；禁止复用开发 `.venv` 或已启动开发服务 | 报告包含 venv 路径、安装摘要、服务 PID/端口；服务命令从临时副本执行；如果端口连接到旧开发服务，本轮 fail |
| ENV-04 | Patch discipline | 测试期间如果为了诊断临时修改代码，必须重新从干净副本或提交后的代码重跑 P0/P1 门禁 | 任何“临时打补丁后通过”只能标记 `diagnostic_pass_after_patch`；不能标记 `SYSTEM_TEST_PASS`，除非补丁已经提交/纳入待验证代码并从干净副本重跑 |

## MCP 可见 UI 测试规则

P0/P1 浏览器用例必须有一段 MCP browser tools 可见执行过程。自动化脚本可以负责批量断言，但最终 UI 通过标准必须建立在用户能看到、能复查的页面操作证据上。

| 规则 | 必须执行 | 禁止替代 |
| --- | --- | --- |
| 页面可见 | 使用 MCP browser tools 打开 WSL 服务 URL，例如 `http://127.0.0.1:<port>/static/index.html#studio`，通过 snapshot/read_page 或等价工具确认当前页面和关键控件 | 只打开本地静态文件、只检查 HTML 字符串、只看 API health |
| 用户动作 | 通过可见控件完成导航、点击、输入、选择 provider/model/interface、切换 stream/search/image/reasoning、上传附件、发送消息、重跑、删除、导出 | `page.evaluate()` 直接写 Alpine state、调用内部 JS 方法、预写 localStorage、直接发 `/api/local-studio/chat` 后说 UI 已覆盖 |
| 可见等待 | 观察 pending/streaming/tool-running/completed/error 状态；记录首个可见 assistant token 的时间和完成时间 | 只等待网络请求返回或只看 request log |
| 证据输出 | 每个 UI 用例保存 MCP snapshot 摘要、截图、console error 列表、关键 network 响应、被操作控件清单、最终可见结果/错误文本 | 只打印 `passed`，或只保存无判定字段的 JSON |
| 失败诊断 | 用户路径失败后，可以再运行 headless Playwright 或状态注入脚本定位问题；诊断必须独立标注 | 用诊断脚本成功覆盖用户路径失败，或把状态注入结果计入 P0/P1 UI 通过 |

## 完整用户旅程覆盖清单

每轮完整系统测试必须按用户实际会使用的入口和功能建表执行。下面所有 P0/P1 用户功能都要在 `ui-results.json` 中有 `tested_by` 字段，取值为 `mcp_visible`、`automated_playwright`、`api_only_not_ui_feature` 或 `not_applicable_with_evidence`；用户可见功能不能只标 `api_only_not_ui_feature`。

| 用户区域 | 必测用户动作 | 最低 UI 断言 |
| --- | --- | --- |
| 全局导航 | 在 `#studio`、`#chat`、`#images`、`#requests`、`#accounts`、存在时的 `#providers` 间切换并返回原入口 | URL/hash、页面标题/主控件、无 console error；切换后未丢失当前会话状态 |
| Local Studio 提供方设置 | 选择 Google AI Studio；新增/编辑/删除 OpenAI-compatible provider；显示/隐藏 token；加载模型；刷新恢复 | token 不明文泄露；provider 独立恢复；模型列表与 provider 对应；错误 provider 不污染基础模块 |
| Local Studio 模型与能力选择 | 通过 UI 选择 interface、chat model、image model、stream、search、image tool、reasoning、timeout、附件 | 控件启用/禁用符合模型能力；切换 provider/interface 后无残留；发送前摘要与实际请求一致 |
| Local Studio 对话 | 新建会话、发送普通文本、观察流式增量、非流式完成、重复 prompt、rerun、刷新恢复、重命名、单删、批量删除 | 首字/完成时间记录；每次发送都有新 upstream log；无 cache hit；刷新后文本、thinking、工具、图片、usage、错误一致 |
| Search/Image/Reasoning | 分别覆盖工具关闭、工具开启但普通聊天、搜索 prompt、图片生成 prompt、reasoning high + summary auto | 工具是可选能力；工具/引用/图片/思考过程可见或可审计；无空 assistant 卡片或残留 pending |
| Attachments | 上传图片、文本/PDF 类文件，预览、移除、发送或被 UI 阻止 | 支持模型可发送；不支持模型给出明确错误；附件信息刷新后不损坏 |
| Request logs | 开启/关闭保存、查看 group、详情、复制、导出、删除、批量删除 | lifecycle 阶段完整；导出 JSON 可解析且脱敏；Local Studio 与基础模块请求可区分 |
| Accounts | 列出账号、健康检查、激活/切换、池状态、临时副本账号删除 | `/accounts/{id}/test` 文案不等同生成可用；删除只影响临时副本；UI 无 500/ASGI 错误 |
| 基础模块 | 在 Local Studio 错误配置后使用 `#chat`、`#images`、`#requests`、`#accounts` 的基础功能 | 基础入口不继承 Local Studio provider/token/base URL；请求日志路径和 provider 隔离 |
| Provider Manager | 当 `#providers` 或 Provider Manager API 存在时，执行 provider/model registry control plane 用户路径 | 不进入 Local Studio 也能管理 provider；secret 边界、model catalog、health/audit 可见且脱敏 |

## 响应时间与官方 AI Studio 对照

Google AI Studio 账号态文本路径必须测量用户可感知延迟，而不是只测 API 完成。性能门禁只在网络预检和官方网页对照都可用时判定；如果官方网页本身不可达，系统测试以环境阻塞失败，不能把 Local Studio 性能标为通过。

| 指标 | 测量方式 | 预算 |
| --- | --- | --- |
| `official_time_to_first_visible_token_ms` | MCP 可见浏览器直接打开官方 AI Studio，同账号同模型或同级官方可选模型，发送短 prompt，从点击 Run 到首个可见回复 token | 作为本轮基准，至少测 3 次并取中位数 |
| `local_time_to_first_visible_token_ms` | MCP 可见浏览器打开 Local Studio，同账号同 prompt，预热完成后从点击发送到首个可见 assistant token | 不得超过 `max(official_median_first_token_ms * 2, official_median_first_token_ms + 3000)` |
| `official_time_to_complete_ms` | 官方 AI Studio 从点击 Run 到回复完成/停止按钮消失 | 作为本轮完成耗时基准，至少测 3 次并取中位数 |
| `local_time_to_complete_ms` | Local Studio 从点击发送到 UI completed，输入框恢复可用且 request log client response 完成 | 不得超过 `official_median_complete_ms * 1.5 + 5000` |
| `warmup_to_first_user_token_ms` | `/health warmup.status=complete` 后第一条 Local Studio Google 文本请求首字时间 | 必须满足 Local Studio 首字预算；若失败，报告 native worker pool、model selection、server log、network timing 证据 |

性能报告必须包含：账号 id 脱敏摘要、模型/官方页面 label、prompt、每次样本时间、是否命中预热、console/network 摘要、server log 中 native worker lease/matched-response 证据。禁止把冷启动、官方网页不可达、或模型选择失败样本混入性能通过统计。

如果 Google/Gemini 触发模型 fallback，官方 AI Studio baseline 与 Local Studio 性能样本必须使用同一个最终 selected model；报告必须把原 requested model 与最终 selected model 分开写清楚，不能把 fallback 后的样本伪装成原模型样本。

| ID | 路径 | 步骤 | 通过标准 |
| --- | --- | --- | --- |
| PERF-01 | 官方 AI Studio + Local Studio 可见 UI | 使用同一账号、同一网络、相同或同级模型和相同 prompt，官方 AI Studio 直接 UI 与 Local Studio UI 各完成至少 3 个可见样本，并记录首字和完成耗时 | 官方与 Local Studio 样本均可见完成；Local Studio 首字不超过 `max(official_median_first_token_ms * 2, official_median_first_token_ms + 3000)`；完成耗时不超过 `official_median_complete_ms * 1.5 + 5000`；报告包含 warmup、native worker、model selection、console/network 和 request-log 证据 |

## 真实环境

| 项目 | 要求 |
| --- | --- |
| WSL 工作目录 | 在 `/home/bamboo` 下新建临时目录，例如 `/home/bamboo/nexus-studio-system-test-YYYYMMDD-HHMMSS` |
| Google AI Studio 凭据 | 使用 `AGENTS.md` 指定的真实账号目录：Windows `\\wsl.localhost\Ubuntu-24.04\home\bamboo\nexus-studio\data\accounts`，WSL `/home/bamboo/nexus-studio/data/accounts` |
| OpenAI-compatible key | Windows `C:\Users\bamboo\Documents\github\key.txt`，WSL `/mnt/c/Users/bamboo/Documents/github/key.txt`；真实文件格式为第一条非空行 Base URL、第二条非空行 token，或显式 `OPENAI_BASE_URL=...` / `OPENAI_API_KEY=...`；测试脚本必须解析 Base URL + token 后真实调用上游，不能把 Base URL 当 token，也不能只检查文件存在。 |
| 模型默认与 fallback | Google/Gemini 文本默认使用 `SYSTEM_TEST_MODEL_CANDIDATES` 的第一个可用低额度风险候选，并把同一候选链传给 `AISTUDIO_WARMUP_TEXT_MODEL_CANDIDATES`；不默认使用 `gemini-3.5-flash`。OpenAI-compatible 默认 `OPENAI_COMPAT_TEXT_MODEL=gpt-5.4-mini`。测试产物必须保存最终实际模型和探测过程，例如 `google-model-selection.safe.json`、`google-model.safe.txt`、`openai-compatible-model-probe.safe.json`。 |
| 真实凭据通过门禁 | Google AI Studio 和 OpenAI-compatible 的 P0/P1 provider 用例必须分别用上面真实账号目录和真实 key 文件完成模型加载、至少一次真实上游请求、对应浏览器 UI 用户路径和 request-log 脱敏断言；报告必须包含脱敏证据（凭据路径、文件非空/账号数、模型/请求 group id、状态码/错误摘要），不得打印或提交真实 token/cookie/storage state。凭据缺失、不可读、上游未真实调用或只用 mock/stub 替代时，本轮系统测试不能标记通过。 |
| 浏览器 | Playwright/Camoufox 可启动真实 WebUI；UI 测试需截图和 console/network 记录 |
| 主机 UI 测试 | 服务必须在 WSL 临时副本中启动，浏览器必须从 Windows 主机 Playwright 打开 `http://127.0.0.1:<port>/static/index.html#studio` 并实际操作 UI；只跑 WSL 内 API、只打开静态文件、或只验证 DOM 加载都不能替代 UI 系统测试 |
| WSL 网络与 Camoufox 预检 | 启动服务前必须在同一 WSL 临时副本和 venv 中证明 `urllib.request.urlopen("https://aistudio.google.com/")` 成功，且 Camoufox `page.goto("https://aistudio.google.com/", wait_until="commit")` 成功；直连超时、代理 CONNECT 后 TLS 断流、`NS_ERROR_NET_INTERRUPT` 或 `about:blank` 必须写入 `network-preflight.safe.json` 并让系统测试以 `network_preflight_unavailable` 失败，不能继续启动服务后把环境问题伪装成 native worker、Local Studio 或账号不可用 |
| Native UI worker 预检 | 启动服务前必须在 WSL 临时副本中用同一 venv 做 worker import/start preflight：模拟 `python main.py` 的 source-tree 入口导入 `NativeUiWorker`，再启动 `python -m aistudio_api.infrastructure.gateway.native_ui_sender --worker` 子进程并确认未因 `ModuleNotFoundError: No module named 'aistudio_api'` 退出；账号启动 warmup 的真实 `GenerateContent` probe 必须读取 `AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS` 冷启动预算；`pip install -e .` 成功或 `/api/local-studio/health` 200 都不能替代此预检 |
| WSL 资源 | 浏览器密集型 API+UI+worker pool 系统测试需要可用 swap；若 `dmesg` 出现 `Out of memory: Killed process ... chrome-headless` 或 UI 阶段日志为空，先调整 `.wslconfig`（例如 `memory=4GB`、`swap=4GB`）并 `wsl --shutdown` 后重跑，不能把 OOM 误判为应用通过或失败 |
| 服务端口 | 优先使用临时端口，例如 `18080`，避免和本机开发服务冲突 |
| 数据目录 | 为每次测试设置独立 `AISTUDIO_LOCAL_STUDIO_DIR`、`AISTUDIO_REQUEST_LOGS_DIR`、`AISTUDIO_GENERATED_IMAGES_DIR`、`AISTUDIO_IMAGE_SESSIONS_DIR` |

推荐启动前置：

```bash
set -euo pipefail
set +x
RUN_ROOT="/home/bamboo/nexus-studio-system-test-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_ROOT"
cd /mnt/c/Users/bamboo/Desktop/nexus-studio
git rev-parse HEAD > "$RUN_ROOT/source-commit.txt"
git status --porcelain > "$RUN_ROOT/source-status.txt"
rsync -a --delete --exclude .venv --exclude venv --exclude data --exclude tmp /mnt/c/Users/bamboo/Desktop/nexus-studio/ "$RUN_ROOT/repo/"
cd "$RUN_ROOT/repo"
git rev-parse HEAD > "$RUN_ROOT/test-copy-commit.txt"
git status --porcelain > "$RUN_ROOT/test-copy-status.txt"
test -s "$RUN_ROOT/source-status.txt" && { echo "SOURCE_WORKTREE_DIRTY" >&2; exit 2; }
test -s "$RUN_ROOT/test-copy-status.txt" && { echo "TEST_COPY_WORKTREE_DIRTY" >&2; exit 2; }
python3 -m venv venv
. venv/bin/activate
pip install -e .
playwright install firefox
export AISTUDIO_PORT=18080
export AISTUDIO_ACCOUNTS_DIR=/home/bamboo/nexus-studio/data/accounts
export AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT=3
export AISTUDIO_LOCAL_STUDIO_DIR="$RUN_ROOT/data/local-studio"
export AISTUDIO_REQUEST_LOGS_DIR="$RUN_ROOT/data/request-logs"
export AISTUDIO_GENERATED_IMAGES_DIR="$RUN_ROOT/data/generated-images"
export AISTUDIO_IMAGE_SESSIONS_DIR="$RUN_ROOT/data/image-sessions"
export AISTUDIO_PROVIDER_MANAGER_DIR="$RUN_ROOT/data/provider-manager"
export OPENAI_COMPAT_KEY_FILE=/mnt/c/Users/bamboo/Documents/github/key.txt
python - <<'PY'
import os
import sys
import time
from pathlib import Path

repo = Path.cwd()
sys.path.insert(0, str(repo / "src"))
from aistudio_api.infrastructure.gateway.native_ui_worker_pool import NativeUiWorker

worker_env = os.environ.copy()
worker_env.pop("PYTHONPATH", None)
worker = NativeUiWorker(index=0, env=worker_env)
process = worker._ensure_started()
time.sleep(2)
if process.poll() is not None:
	raise SystemExit(f"NATIVE_WORKER_PREFLIGHT_FAIL code={process.returncode} stderr={worker._stderr_summary()}")
worker.close()
print("NATIVE_WORKER_PREFLIGHT_OK")
PY
python main.py
```

读取 OpenAI-compatible 凭据时只在测试进程内读取，禁止打印。真实文件支持第一条非空行 Base URL、第二条非空行 token，或显式 `OPENAI_BASE_URL=...` / `OPENAI_API_KEY=...`：

```bash
OPENAI_COMPAT_BASE_URL="$(grep -m1 -E '^(https?://|OPENAI_BASE_URL=)' "$OPENAI_COMPAT_KEY_FILE" | sed 's/^OPENAI_BASE_URL=//' | tr -d '\r')"
OPENAI_COMPAT_API_KEY="$(grep -m1 -E '^(sk-|OPENAI_API_KEY=|Bearer )' "$OPENAI_COMPAT_KEY_FILE" | sed -e 's/^OPENAI_API_KEY=//' -e 's/^Bearer //' | tr -d '\r')"
```

## 覆盖维度

| 维度 | 必测取值 | 说明 |
| --- | --- | --- |
| WebUI 入口 | `#chat`、`#studio`、`#images`、`#requests`、`#accounts` | Local Studio 不能破坏基础业务线 |
| 提供方类型 | Google AI Studio、OpenAI-compatible | Google 走内置账号；OpenAI-compatible 走 Base URL + Token |
| Local Studio 接口模式 | OpenAI Chat、OpenAI Responses、Gemini、Claude | UI 允许切换的模式都要测；不兼容组合必须优雅失败 |
| 流式开关 | on、off | SSE 和普通 JSON 响应都要覆盖 |
| 搜索开关 | off、on | on 时必须作为可选能力，不得强制普通问题走工具 |
| 图片工具 | off、on | 仅 Responses 面板有效；Google 和 OpenAI-compatible 控件/参数不同 |
| Reasoning 能力 | off、high + summary auto | 仅在能力可用时发送；不可用时 UI 控件禁用或请求省略；如果上游返回 reasoning summary/tool details，API、UI、conversation 和 request log 不得丢失；stream parser 必须覆盖 `response.reasoning.*`、`response.reasoning_text.*`、`response.reasoning_summary_text.*`、`response.reasoning_summary_part.*` 和 reasoning `response.output_item.*`；不要求展示私有完整 chain-of-thought |
| 重复 Prompt | 同 prompt 连续发送、刷新后再发送、rerun 后再发送 | Local Studio 不允许 final-result replay cache；每次发送都必须再次走 provider/upstream，并且 API/UI/request log 不出现 cache hit 或 namespace 控件 |
| 附件 | 无附件、图片、文本/PDF 类文件 | 只在当前模型能力允许时发送；不支持时 UI 必须阻止或提示 |
| 会话 | 新建、发送、刷新恢复、重跑、重命名、单删、批量删除 | 验证本地持久化和 UI 状态恢复 |
| 请求记录 | 关闭、开启、查看详情、导出、删除 | 开启后必须保存完整 lifecycle，敏感字段必须脱敏 |
| Google AI Studio Native UI Worker | 默认 3、显式 1、重复请求复用、同账号并发、worker 失败重启、账号切换关闭旧池 | 账号态纯文本请求必须走每账号独立 native UI worker pool；worker 复用独立干净进程而不是每请求启动；native UI 不能安装主进程 hook/init script；只在无法解析/发送 native UI 时允许 raw replay fallback |
| Provider Manager 阶段 | Phase 0 当前 Local Studio 兼容基线、Phase 1 control plane/registry、Phase 2 shared runtime gateway、Phase 3 advanced routing | 每轮先判定当前实现阶段；未实现阶段只能标记带证据的 `not_applicable`；已实现阶段必须跑对应 P0/P1 门禁 |
| 控制面合约 | Provider CRUD、enabled 状态、provider 类型、credential references、model catalog、manual models、aliases、health checks、routing policies、audit safety | Provider Manager 必须可在不进入 Local Studio 对话的情况下独立管理；secret 不返回 UI、不进入 request log/export；配置变更有审计记录 |
| 数据面合约 | canonical request、canonical response、protocol adapters、provider executors、response conversion、request logs | OpenAI Responses、OpenAI Chat Completions、Gemini、Claude Messages 和 Local Studio 消费同一 routing/attempt/logging 语义 |
| 路由策略 | aliases/defaults、capability matching、health、quotas、priority/weight、fallback、sticky routing、streaming/tool/image compatibility | 路由结果必须确定、可审计、可解释；不兼容能力不得被选中；fallback 只在语义安全时发生 |

## Provider Manager / 共享 provider-model pool 架构覆盖

Provider Manager 是 provider-model 池的控制面，不是 Local Studio 会话内的一组控件。shared runtime gateway 是运行时数据面，不是某个单一协议的实现细节。系统测试必须把这两个边界拆开记录，避免在未来迁移时只看到“Local Studio 还能聊天”却漏掉共享池契约。

### Rollout 阶段门禁（Rollout phase gates）

| 阶段 | 适用范围 | 必须通过的门禁 |
| --- | --- | --- |
| Phase 0：当前兼容基线 | 尚未实现 Provider Manager route/page/registry/gateway，Local Studio 仍直接拥有当前 provider 设置 | `PM-ROLL-00` 必须记录当前缺口证据；所有既有 Local Studio、基础模块、兼容 API 和 request log P0/P1 继续通过；不得把目标架构缺失误报为已经通过 |
| Phase 1：Provider Manager 控制面 + provider/model 注册表 | 已出现独立 Provider Manager UI/API、provider/model registry、credential references 或 model catalog 任一能力 | `PM-CP-*`、`PM-AUDIT-*` 成为 P0；Local Studio 仍必须保持当前 provider/interface/tool/reasoning 行为兼容；secret 不得从控制面泄露到 UI、日志或导出 |
| Phase 2：shared runtime gateway 数据面 | Local Studio 或任一兼容 API 开始通过 shared provider-model pool 执行请求 | `PM-DP-*`、`PM-PROTO-*`、`PM-RT-*` 成为 P0；所有消费者必须共享 canonical request/response、provider executor、routing decision、fallback 和 request log 证据 |
| Phase 3：高级路由策略 | 已实现 quota、priority/weight、cost/latency、sticky routing 或受控流式 fallback | `PM-RT-*` 扩展到正向和负向矩阵；权重、限流、降级、sticky、fallback 与流式/tool/image 兼容性必须可重复验证 |

`not_applicable` 只能用于尚未进入的阶段，且必须包含：当前 git commit、缺失的 route/page/schema/function 名称、替代执行的上一阶段门禁、以及下一阶段触发条件。一旦某个阶段的任一用户入口、API route、存储 schema 或 routing function 出现，对应阶段不能整体标记不适用。

### 共享池断言

| 架构对象 | 系统测试断言 |
| --- | --- |
| Provider Manager 控制面 | Provider Manager 页面/API 能在不打开 Local Studio 会话的情况下列出、创建、编辑、启用/停用和删除 provider；内置 Google AI Studio provider 始终存在且不需要用户 base URL/token；自定义 provider 至少保留 provider 类型、base URL、enabled 状态、timeout 和 credential reference |
| provider/model 注册表 | 每个 model catalog 条目必须带 provider 归属、外部模型 id、友好名称、能力、上下文限制、模态支持和 aliases/defaults；自动发现与 manual models 均可审计；删除或停用 provider 不得留下可路由的悬空模型 |
| credential 引用 | UI、API 响应、model discovery、request log、server.log、导出文件和截图只能出现 credential reference 或脱敏摘要，不能出现真实 token、cookie、storage state 或 Authorization 明文 |
| 健康检查 | provider/model/credential 健康状态必须区分 ready、disabled、auth_failed、quota_exhausted、degraded、unknown 和 last_success；健康状态影响 routing policies，但不能自动删除配置 |
| 路由策略 | aliases/defaults、capability matching、health、quotas、priority/weight、fallback、sticky routing、streaming/tool/image compatibility 都必须进入 routing decision 证据；显式用户选择和策略默认值要能区分 |
| shared runtime gateway 数据面 | 每个 compatible protocol adapter 先校验自身原生请求，再生成 canonical request；provider executors 只接收 canonical request；canonical response 先保存文本、工具、图像、usage、attempt metadata 和错误，再 response conversion 到调用方协议 |
| 兼容协议消费者 | Local Studio、OpenAI Responses、OpenAI Chat Completions、Gemini generateContent/streamGenerateContent、Claude Messages 都必须能作为共享池消费者记录同一 provider/model pool 决策；协议原生响应格式保持各自兼容 |
| fallback 控制器 | 可重试错误、同 provider 换 credential、换 model、换 provider 和直接返回错误必须有明确分类；streaming fallback 只有在未发送不可撤销 chunk 前允许；fallback attempt plan 写入 request logs |
| 审计安全 | Provider Manager 的配置变更、模型发现、健康检查、路由策略更新和 runtime attempt 都必须有可审计记录；审计记录包含 actor/action/target/status/time/error 摘要，但不包含 secret |

## 架构契约断言

下面断言用于把 `ARCHITECTURE.md` 的设计边界转成可执行 oracle。除非用例明确不涉及该能力，或当前 rollout phase 尚未进入该能力，否则每个 API/UI 用例都要在结果中记录这些断言的通过、失败或不适用状态。

| 契约 | 必须断言 |
| --- | --- |
| Provider Manager 控制面独立性 | Provider Manager control plane 必须能在不进入 Local Studio 对话的情况下管理 provider/model registry；Local Studio 只保存会话范围内的 provider/model alias、tools、interface、stream、thinking、timeout 和附件设置，不拥有全局 Provider CRUD、credential references、model catalog、health checks 或 routing policies。 |
| provider/model registry 与 credential boundary | provider/model registry、model catalog、aliases/defaults、capability metadata、health 状态和 routing policy snapshot 必须可查询且不返回 secret；credential references 只以引用或脱敏摘要出现，真实 token/cookie/storage state 不进入 UI、conversation、request log/export、server.log 或截图。 |
| shared runtime gateway data plane | OpenAI Responses、OpenAI Chat Completions、Gemini、Claude Messages 和 Local Studio 请求在进入共享池阶段后必须映射到 canonical request，经 provider executors 执行，产出 canonical response，再由 response conversion 返回调用方协议；request logs 必须保存 adapter、canonical fields、routing decision、attempt plan 和 provider response 摘要。 |
| compatible protocol consumers | Local Studio 是共享池消费者之一，不是 provider 管理所有者；外部 OpenAI Responses、OpenAI Chat Completions、Gemini 和 Claude Messages 客户端应获得协议原生响应，同时共享同一 provider/model pool、健康状态、credential references、routing policies、fallback 和 request log 语义。 |
| Routing policy 与 fallback | aliases/defaults、capability matching、health、quotas、priority/weight、cost/latency、sticky routing、fallback、streaming/tool/image compatibility 必须影响可审计 routing decision；不兼容模型不能被选中；fallback 不得破坏调用方已收到的 streaming/tool/image 语义。 |
| Rollout phase gate | 每轮系统测试必须声明当前 Provider Manager/shared pool rollout phase，并给出 pass/fail/not_applicable 证据；Phase 0 不要求未实现的 Provider Manager runtime 通过，但必须证明当前 Local Studio 和基础模块兼容；进入 Phase 1/2/3 后，对应 `PM-*` 用例不能再整体跳过。 |
| 提供方路由隔离 | 当前 provider 决定 model list 来源、upstream URL、鉴权方式和 tool schema。Google AI Studio provider 不需要也不转发用户 token；OpenAI-compatible provider 只向配置的 Base URL 转发脱敏后的 Authorization；切换 provider 后不能残留上一 provider 的模型、图片工具参数或错误状态。 |
| 接口模式语义隔离 | OpenAI Chat、OpenAI Responses、Gemini、Claude 的请求路径、请求体、stream parser、错误格式和会话 `interface_mode` 必须互相隔离；不兼容组合要受控失败，不能串用另一个 interface 的 payload。 |
| 工具可选语义 | Search/Image Tool 开启只表示模型可用这些能力。普通 prompt 不应强制触发 search、image 或多余 upstream call；当模型选择工具时，工具调用过程、引用、图片候选和最终结果必须可追踪。 |
| Reasoning / Tool 过程保留 | 如果上游返回 reasoning summary、reasoning item、tool call、search citation、image generation invocation 或 usage，API 响应、UI、conversation JSON、刷新恢复、rerun、重复 prompt 和 request log 至少保留一份可展示或可审计结构；流式路径只要收到 reasoning 相关 SSE event，最终 `local_studio.completed` 的 assistant 必须有 `thinking`，UI 刷新后必须能看到 `Reasoning summary` 或等价入口；如果上游没有返回 summary，UI 必须显示可理解的空状态或省略入口，不能像丢失数据一样静默消失。 |
| 无结果缓存 | Local Studio chat route 不得把最终 assistant 结果缓存并在等价 prompt 上复用；重复 prompt、跨 provider/mode/model/tool/reasoning/附件/token 变化都必须发起新的 upstream 调用；旧 `cache_enabled`/`cache_namespace` 字段如由兼容脚本发送必须被忽略。 |
| 基础模块独立性 | Local Studio provider、tool、reasoning 设置不能污染 `#chat`、`#images`、`#accounts` 的原始业务线；即使 Local Studio 当前 provider 配置错误，基础入口仍应走原始账号池/基础 API 并可用。 |
| Google AI Studio native UI worker 边界 | 账号态文本 `GenerateContent` 必须先解析 wire body 为纯文本 `(model, prompt)` 并交给每账号 `NativeUiWorkerPool`；worker 子进程通过 `native_ui_sender --worker` 复用独立 Camoufox/browser/context，不安装主进程 hook/init script；默认每账号 3 个 worker，重复请求复用，成功匹配的 native HTTP status/body 是权威结果；账号启动 warmup 以 native worker pool probe 成功作为 readiness；只在 native UI 无法解析/发送时才允许 raw replay fallback。 |
| 请求记录横向服务 | Local Studio 和基础模块都必须以 group 展示完整生命周期；失败路径也必须保存 upstream request/response 或明确的未发起原因，导出 JSON 可解析且脱敏。 |
| 错误一致性 | API error、SSE error、UI 当前会话错误、conversation JSON、request log、server stderr 和 health 状态必须一致；错误后输入框恢复可用，服务健康接口继续 200。 |
| 敏感信息边界 | 真实 token、Authorization、Google cookie、storage state、账号凭据、原始大图 payload 不得出现在 UI 文案、conversation JSON、request log 导出、截图、server.log 或本仓库提交文件中。 |
| 持久化恢复 | 刷新页面后 provider、interface、model、stream、reasoning、search/image settings、消息、图片、usage、错误和 tool/reasoning details 必须恢复一致；不得恢复或显示 Local Studio cache 控件/标记。 |
| 前端状态机 | idle、pending、streaming、tool-running、completed、error、retry/rerun 状态必须单向可解释地转换；结束后不能残留“正在等待模型/工具”、空 assistant 卡片、禁用输入框或重复发送按钮状态。 |

## 组合规则

下面规则用于把“所有真实用户路径组合”落成可执行矩阵，避免只测单点成功路径。

1. 对每个有效的 `Provider x Interface` 组合，必须运行基础聊天 `Stream on/off x Search off/on` 四种组合，并套用“架构契约断言”。
2. 对每个 Responses 组合，必须额外运行 `Image Tool off/on x Search off/on` 四种组合，并验证普通问题不会因为工具开启而强制调用工具。
	* Google AI Studio provider 的 Responses search 请求体必须使用 `web_search_preview`。
	* OpenAI-compatible provider 的 Responses search 请求体必须使用 `web_search`，且不得出现 `web_search_preview`。
3. 对每个 Responses provider 至少运行一次 `reasoning=off` 和一次 `reasoning=high + summary=auto`；支持 reasoning 的模型必须断言请求体包含 provider 支持的 reasoning 参数，且上游返回的 reasoning summary/tool details 不在 API/UI/持久化/request log 之间丢失。
	* 流式 reasoning 用例必须同时断言：最终 SSE `local_studio.completed` conversation 的最后一条 assistant `thinking` 非空、刷新后的 UI 显示 `Reasoning summary` 或等价入口、request log/export 中可审计到对应 reasoning upstream event 或 response item。
	* 如果脚本记录了 `assistant_has_thinking`、`thinking_length`、`reasoning_summary_visible`、`no_reasoning_summary` 等字段，必须把它们纳入 pass/fail 逻辑；禁止只记录不判定。
4. 对每个提供方至少运行一次 repeated-prompt 测试，并额外验证 provider、interface、model、tool、reasoning、attachment 或 token 任一维度变化后仍会 fresh upstream，而不是复用旧 assistant 结果。
5. 对每个提供方至少运行一次图片附件和一次非图片附件路径；如果模型不支持附件，预期结果是 UI 阻止发送并给出错误提示。
6. 对每个提供方至少运行一次会话恢复和重跑；其中一次必须在页面刷新后恢复，并检查 reasoning/tool/search/image details 仍可见或可审计。
7. 每个预期失败或 provider 不兼容组合必须验证“优雅失败”：前端显示可理解错误、会话保存错误、请求记录完整、服务健康接口仍可用。
8. 每个 P0 bug 回归用例必须同时保留 API 原始响应、WebUI 断言、请求记录 group id、服务端 stderr 摘要和截图。

## P0 启动与共享服务

| ID | 路径 | 步骤 | 通过标准 |
| --- | --- | --- | --- |
| BOOT-01 | API | 启动服务后请求 `/api/local-studio/health`、`/request-logs/status`、`/v1/models`、`/v1beta/models` | 全部返回 200；模型列表非空；无未捕获异常 |
| BOOT-02 | UI | 打开 `/static/index.html#studio`，再依次进入 `#chat`、`#images`、`#requests`、`#accounts` | 页面可导航，核心控件可见，console 无错误 |
| LOG-01 | API + UI | 在 `#requests` 开启请求保存，然后 API 查询 `/request-logs/status` | UI 显示保存开启，API `enabled=true` |
| LOG-02 | API + UI | 执行一次 Local Studio 请求后打开请求详情、导出当前、删除当前 | 阶段卡片完整；导出 JSON 可解析；删除后列表消失 |
| SEC-01 | API + UI | 使用 OpenAI-compatible token 加载模型后检查 request log 导出 | `api_key`、`apiKey`、`token`、`Authorization` 都不出现真实 key；只允许 `***` 或 `Bearer ***` |

## P0 Provider Manager / 共享 provider-model pool 分阶段门禁

| ID | 适用阶段 | 路径 | 步骤 | 通过标准 |
| --- | --- | --- | --- | --- |
| PM-ROLL-00 | Phase 0+ | 报告 + API + UI | 先判定当前代码是否存在 Provider Manager route/page、provider/model registry schema、shared runtime gateway 或 routing function；再执行当前阶段对应门禁 | 报告包含当前 rollout phase、git commit、存在/缺失证据和 `PM-*` not_applicable 原因；Phase 0 必须同时跑完 BOOT、Local Studio、基础模块和 request log P0；不能把未实现目标架构标成通过 |
| PM-CP-01 | Phase 1+ | Provider Manager UI + API | 从独立 Provider Manager 入口列出内置 Google AI Studio provider，新增/编辑/停用/删除一个自定义 OpenAI-compatible provider | 不需要进入 Local Studio 会话；Google provider 始终存在且无用户 token/base URL；自定义 provider CRUD 后状态一致；删除或停用不会污染既有 Local Studio 会话和基础模块 |
| PM-CP-02 | Phase 1+ | API + 日志 | 保存 provider credential references、加载模型、查看 model catalog、导出 request/audit 记录 | API/UI/log/export 只出现 credential reference 或脱敏摘要；model catalog 包含 provider 归属、model id、能力、上下文、模态、aliases/defaults；无真实 secret、cookie、Authorization 明文 |
| PM-CP-03 | Phase 1+ | API + UI | 执行 health checks，模拟 disabled、auth_failed、quota_exhausted、degraded、ready provider/model 状态 | 健康状态显示清晰且可审计；health 影响 routing policies 的候选集合；健康失败不自动删除 provider 配置；Local Studio 当前会话只看到可用性或受控错误 |
| PM-AUDIT-01 | Phase 1+ | Provider Manager API + UI | 创建、编辑、停用 provider，更新模型目录和 aliases/defaults，查看 audit 记录 | 审计记录包含 actor/action/target/status/time/error 摘要；不含 secret；失败配置变更也有受控 audit 记录；runtime attempt/request log group 证据从 Phase 2 shared runtime gateway 开始要求 |
| PM-DP-01 | Phase 2+ | API 客户端 | 分别通过 OpenAI Responses、OpenAI Chat Completions、Gemini、Claude Messages 和 Local Studio 发送同等文本请求 | 每个 protocol adapter 校验原生请求并生成 canonical request；request log 记录 adapter、canonical fields、selected provider/model、attempt plan；response conversion 返回协议原生格式 |
| PM-PROTO-01 | Phase 2+ | API + UI | 对 text、stream、tools/functions、search、image input、image generation、reasoning/thinking 逐项运行兼容协议矩阵 | capability matching 阻止不兼容模型；支持能力时保留工具、图像、reasoning、usage 和错误结构；不把 Responses-only、Gemini-only 或 Claude-only 字段串到其他协议 |
| PM-RT-01 | Phase 2+ | API + 日志 | 配置 aliases/defaults 和 priority fallback 链，制造首选 provider 失败后切换候选 | routing decision 确定且可重复；request log 保存 aliases/defaults 解析、candidate list、失败 attempt、fallback attempt、最终 response/error；streaming 已发送 chunk 后不执行破坏语义的 fallback |
| PM-RT-02 | Phase 3+ | API + 日志 | 配置 health、quotas、priority/weight、sticky routing、streaming/tool/image compatibility 的组合矩阵 | routing policy 按健康、限额、权重和 sticky 规则选择候选；不把 stream 请求路由到非 stream executor，不把 tool/image 请求路由到不兼容模型；sticky 不能覆盖硬性凭据或能力失败 |

## P0 Local Studio：Google AI Studio 提供方

| ID | 接口模式 | 流式 | 搜索 | 图片工具 | 重复 | 用户路径 | 通过标准 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| G-LS-01 | Responses | on | off | off | first | 选择 Google AI Studio，加载模型，选聊天模型，发送 `回复 ok` | UI 流式显示 assistant；请求记录含 `/api/local-studio/chat` 和内部 `/v1/responses`；无 error event |
| G-LS-02 | Responses | off | off | off | repeat | 重复 G-LS-01 的同一 prompt | 仍发起新的 upstream 调用；API/UI/SSE/conversation 均无 cache hit 或 namespace 标记；请求体不泄露凭据 |
| G-LS-03 | Responses | on | on | off | first | 发送 `搜索今天一条科技新闻并用一句话总结` | 请求体包含 `web_search_preview`；UI 正常结束或受控显示上游错误；服务不崩溃 |
| G-LS-04 | Responses | on | off | on | first | 选择 Gemini 图片模型和尺寸，发送 `生成一张简单蓝色方形图标` | 生成图片只渲染一次；图片 URL 可打开；请求记录不出现重复大图 payload |
| G-LS-05 | Responses | on | on | on | first | 普通聊天 `你好，只回复文本` | Search/Image 均为可选能力；不得强制生成图片；UI 返回文本 |
| G-LS-06 | Responses | on | on | on | first | 复现用户路径：先问候、询问身份、请求新闻，再发送 `做成图片` | 不再出现 `include_server_side_tool_invocations` 错误；如触发图片工具则只保存/展示一张对应图片；思考/工具过程不丢失 |
| G-LS-07 | Responses | off | on | on | first | 发送 `把今天科技新闻做成简洁信息图` | 非流式也能保存图片/文本/错误；不会依赖 SSE 才正确 |
| G-LS-08 | Responses | on/off | off/on | off/on | first/repeat | 选择支持 thinking/reasoning 的 Gemini 模型，设置 `reasoning=high`、`summary=auto`，发送需要分步判断的 prompt | 能力可用时请求体包含对应 thinking/reasoning 设置；如果上游返回思考摘要、搜索引用或工具过程，API/UI/conversation/request log 不丢失；重复 prompt 仍为 fresh upstream；不支持时请求省略且 UI 明确禁用或说明 |
| G-LS-09 | Gemini | on/off | off/on | 不适用 | first/repeat | 切到 Gemini interface，分别测普通聊天和 search prompt | `#studio` 调用内部 `/v1beta/models/...:generateContent` 或 `streamGenerateContent`；search on 时使用 Google Search；无 Responses 图片工具面板；重复 prompt fresh upstream |
| G-LS-10 | OpenAI Chat | on/off | off/on | 不适用 | first/repeat | 切到 OpenAI Chat interface，测普通聊天和 search prompt | 内部 `/v1/chat/completions` 正常；search on 映射为当前项目支持的搜索字段；UI/日志完整；重复 prompt fresh upstream |
| G-LS-11 | Claude | on/off | off/on | 不适用 | first/repeat | 切到 Claude interface，测普通聊天和 search prompt | 内部 `/v1/messages` 正常或受控失败；失败时会话/请求记录/服务健康均正常；重复 prompt fresh upstream |

## P0 Local Studio：OpenAI 兼容提供方

| ID | 接口模式 | 流式 | 搜索 | 图片工具 | 重复 | 用户路径 | 通过标准 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| O-LS-01 | Models | 不适用 | 不适用 | 不适用 | 不适用 | 在 UI 新增 OpenAI-compatible provider，填 Base URL、从 key 文件读取 token，点击加载模型 | 模型列表加载；token 输入框不回显明文到日志；刷新后 provider 可恢复 |
| O-LS-02 | Responses | on/off | off | off | first/repeat | 选择聊天模型，发送 `回复 ok` | 流式和非流式均可完成；请求记录目标为自定义 Base URL `/responses`；重复 prompt fresh upstream |
| O-LS-03 | Responses | on | on | off | first | 发送 `搜索今天一条科技新闻并总结` | 请求体 `tools` 使用 `web_search` 且不出现 `web_search_preview`；如果 provider 支持 search，应正常完成；如果返回 4xx，UI 只显示一个受控错误，服务端不得出现 `ResponseNotRead` 或 ASGI exception group；不得出现 `Unsupported tool type: web_search_preview` |
| O-LS-04 | Responses | on/off | off | on | first | 选择 `gpt-image-2`，尺寸 `1024x1024`，发送 `生成一个测试图标` | 支持图片的 provider 返回图片并渲染一次；不支持时优雅失败且服务健康保持 200 |
| O-LS-05 | Responses | on | on | on | first | 普通聊天 `不要搜索，不要画图，只回复 ok` | 工具开启但不强制调用；不会错误生成图片 |
| O-LS-06 | Responses | on/off | off/on | off/on | first/repeat | 选择支持 reasoning 的 OpenAI-compatible Responses 模型，设置 `reasoning=high`、`summary=auto`，发送 `请分步骤判断 17*23 是否大于 390，并给出简短结论` | upstream request 包含 Responses reasoning 参数；若上游返回 `response.reasoning*`、`response.reasoning_text*`、`response.reasoning_summary_text*`、`response.reasoning_summary_part*` 或 reasoning `response.output_item.*`，API 响应、最终 SSE completed、UI、conversation JSON、刷新恢复和 request log 均保留；重复 prompt fresh upstream；stream on 时 assistant `thinking` 非空且 UI 显示 `Reasoning summary`；若上游不返回 summary，UI 显示受控空状态；结束后不残留 pending/tool-running 状态 |
| O-LS-07 | Responses | on/off | on | on | first | 开启 search、image tool、reasoning，发送普通聊天 `不要搜索，不要画图，只解释 2+2` | 三个能力均为可选；请求体允许工具但不得强制调用；如果只返回文本，也要保留 reasoning/usage 或明确无 reasoning summary；不会出现空 assistant 卡片 |
| O-LS-08 | OpenAI Chat | on/off | off/on | 不适用 | first/repeat | 切到 OpenAI Chat interface，测普通聊天和 search toggle | 正常完成或 provider 兼容性错误受控显示；无未捕获后端异常；不会发送 Responses-only reasoning/tool 字段；重复 prompt fresh upstream |
| O-LS-09 | Claude | on/off | off/on | 不适用 | first/repeat | 如果 provider 支持 Messages，测普通聊天；否则保留负向兼容测试 | 成功或优雅失败；请求路径、状态码、错误文案记录完整；重复 prompt fresh upstream |
| O-LS-10 | Gemini | on/off | off/on | 不适用 | first/repeat | 用户误选 Gemini interface 连接 OpenAI-compatible Base URL | UI 不崩溃；API 返回受控错误；会话错误可见；服务健康仍 200；重复 prompt 不复用旧 assistant 结果 |

## P1 Local Studio UI 状态与持久化

| ID | 路径 | 步骤 | 通过标准 |
| --- | --- | --- | --- |
| LS-UI-01 | 提供方设置 | 新增两个 OpenAI-compatible provider，切换后刷新页面 | 每个 provider 保留独立 Base URL、token、timeout、interface；当前 provider 恢复正确 |
| LS-UI-02 | 图片工具 UI | Google provider 与 OpenAI-compatible provider 间切换 | Google 显示 Gemini 图片模型/尺寸；OpenAI 显示 `gpt-image-2` 和质量/背景/格式/压缩；无跨 provider 残留 |
| LS-UI-03 | 模型过滤 | Responses 模式加载模型 | 聊天模型列表不出现 `gpt-image-*` 或 Gemini image-only 模型；图片模型出现在 Image Tool 选择器 |
| LS-UI-04 | Pending 状态 | 发送流式请求时观察 pending 区域 | 显示当前 interface、model、stream、search/image tool/reasoning 摘要；流式文本逐段可见；进入工具调用时状态文案更新；结束或错误后消失 |
| LS-UI-05 | 错误状态 | 故意使用错误 token 或错误 Base URL | 错误显示在当前会话；输入框可继续使用；请求记录保存失败阶段 |
| LS-UI-06 | 附件 | 上传图片、文本/PDF 附件后发送 | 支持模型正常发送；不支持模型 UI 阻止或给出明确错误；附件预览和移除可用 |
| LS-UI-07 | 会话 | 新建、发送、刷新、恢复、重跑、重命名、单删、批量删除 | 历史列表、消息内容、图片、usage 和错误都能持久化并正确删除；不会出现 cache 标记 |
| LS-UI-08 | 重复 Prompt | 同 prompt 连续发送，刷新页面后再发送一次 | 每次都显示新的发送/等待/完成流程；request log 新增 upstream 调用；UI 无 Cache Namespace 控件或 cache hit 标记 |
| LS-UI-09 | Reasoning / 能力 | 在 Google 和 OpenAI-compatible Responses 下切换支持/不支持 reasoning 的模型，分别设置 off 与 high + summary auto 后发送，stream on/off 都覆盖；对 stream on 用例刷新页面后重新打开同一会话 | 支持时请求体包含 provider 支持的 reasoning/thinking 参数；不支持时 UI 控件禁用或请求省略；上游返回 reasoning 时当前消息和刷新恢复后都显示 `Reasoning summary` 或等价入口；conversation JSON 的 assistant `thinking` 非空；没有上游 summary 时显示受控空状态 |
| LS-UI-10 | 提供方 CRUD | 编辑 OpenAI-compatible provider 的 Base URL/token/timeout，再删除当前 provider | 编辑后重新加载模型走新配置；删除后回退到可用 provider；会话和请求日志不保存明文 token |
| LS-UI-11 | 超时 | 设置极短 timeout 访问慢/不可达 Base URL 后发送 | UI 显示受控 timeout 错误；输入框恢复可用；会话保存错误；服务健康接口仍 200；请求记录阶段完整 |
| LS-UI-12 | 默认无结果缓存 | 新临时环境首次打开 Local Studio，使用默认设置重复发送同 prompt | 不存在 cache 默认开启文案、开关或 namespace；首次和重复发送都走正常请求流程并新增 upstream log |
| LS-UI-13 | 无结果缓存隔离 | 同 prompt 依次改变 provider、interface、model、search、image tool、reasoning、附件，再切回原配置 | 任一配置下都不得复用旧 assistant 结果；切回完全相同配置也必须 fresh upstream；UI、conversation 和 request log 无 cache hit 标记 |
| LS-UI-14 | 提供方独立性 | 在 Local Studio 配置错误 OpenAI-compatible Base URL/token 后，切到 `#chat`、`#images`、`#accounts` 执行基础路径 | 基础入口仍走原始账号池和基础 API；不继承 Local Studio 错误 provider/token/base URL；request log 路径能区分基础模块与 Local Studio |
| LS-UI-15 | UI 状态机 | 分别触发成功流式、非流式成功、工具调用成功、上游 4xx、timeout、rerun、重复 prompt | idle/pending/streaming/tool-running/completed/error/retry 状态转换清晰；结束后无残留等待文案、空 assistant 卡片、重复禁用输入框或重复发送按钮状态 |

## P1 基础模块回归

| ID | 入口 | 用户路径 | 通过标准 |
| --- | --- | --- | --- |
| BASE-CHAT-01 | `#chat` | 先在 Local Studio 配置错误 provider，再选择 OpenAI Responses、Gemini、Claude、OpenAI Chat 中至少两个模式，分别发送普通消息 | Playground 仍独立可用；Local Studio provider 设置不影响 Playground；请求记录显示基础 `/v1` 或 `/v1beta` 路径而非 Local Studio provider Base URL |
| BASE-CHAT-02 | `#chat` | 开启 Search 发送新闻类 prompt | 搜索能力仍按 Playground 语义工作；请求记录显示基础 `/v1` 或 `/v1beta` 路径 |
| BASE-IMG-01 | `#images` | 选择图片模型，生成一张 1:1 图片 | 独立图片生成页可用；生成图片保存到 generated-images；不依赖 Local Studio Image Tool |
| BASE-IMG-02 | `#images` | 上传历史图/参考图后做编辑或重试 | 参考图、基图、历史会话、下载/删除不回归 |
| BASE-REQ-01 | `#requests` | 查看、复制、导出、批量删除多组请求 | 对 Local Studio 和基础模块请求都能按 group 展示完整 lifecycle |
| BASE-ACC-01 | `#accounts` | 列出账号、健康检查、切换/激活账号、查看池状态 | 账号管理线仍独立工作；不会因 Local Studio provider 封装被隐藏或破坏 |
| BASE-ACC-02 | `#accounts` | 在 WSL 临时目录复制真实账号目录副本，删除副本中的一个非唯一账号，再刷新账号页 | API `DELETE /accounts/{id}` 返回 200 且账号目录被删除；UI 不出现 500/ASGI exception；真实源账号目录不被修改；若只剩一个账号则先导入/复制临时账号再删除 |

## P1 API 级直接验证

| ID | API | 请求体 / 场景 | 通过标准 |
| --- | --- | --- | --- |
| API-LS-01 | `POST /api/local-studio/models` | Google provider + Responses/Gemini/OpenAI/Claude mode | 返回 chat models 和 image models；Google provider 不需要 Authorization |
| API-LS-02 | `POST /api/local-studio/models` | OpenAI-compatible provider + token | Authorization 发送到上游但 request log 脱敏 |
| API-LS-03 | `POST /api/local-studio/chat` | Google Responses + `search=false` + `image_tool_enabled=false` | 文本回复，conversation JSON 保存 user/assistant |
| API-LS-04 | `POST /api/local-studio/chat` | Google Responses + `search=true` + `image_tool_enabled=true` + image prompt | 覆盖 Gemini 图片工具故障路径；无 `include_server_side_tool_invocations` 错误 |
| API-LS-05 | `POST /api/local-studio/chat` | OpenAI Responses stream + `search=true` | 请求体工具类型为 `web_search` 且不包含 `web_search_preview`；覆盖 OpenAI search 4xx 流式路径；无 `ResponseNotRead`；不得出现 `Unsupported tool type: web_search_preview` |
| API-LS-06 | `GET /api/local-studio/assets/{path}` | 打开 Local Studio 生成图 URL | 返回图片 MIME；路径穿越返回 400/404 |
| API-LS-07 | `POST /api/local-studio/chat` | Google Responses + `search=true`；OpenAI Responses + `search=true` | Provider-aware search oracle：Google upstream request 包含 `web_search_preview`；OpenAI-compatible upstream request 包含 `web_search`；两者均保留 search/image tool 可选语义 |
| API-LS-08 | `POST /api/local-studio/chat` | OpenAI-compatible Responses + `reasoning_effort=high` + `reasoning_summary=auto`，分别 stream on/off | upstream request 包含 Responses reasoning 参数；响应解析不丢弃上游返回的 reasoning summary/item/tool details；stream on 时最终 SSE completed event 的 assistant `thinking` 非空，非流式 JSON 的 assistant `thinking` 非空，conversation JSON 和 request log 可审计；若上游没有 summary，必须返回 `no_reasoning_summary` 或等价受控空状态，不能与解析丢失混淆 |
| API-LS-09 | `POST /api/local-studio/chat` | 同 prompt 连续发送，并分别改变 provider、interface、model、tools、reasoning、attachments、token；兼容性 payload 可额外携带旧 `cache_enabled/cache_namespace` | 每次都发起新的 upstream 调用；响应、SSE completed、assistant message、request log 均无 Local Studio `cache.hit`；导出日志不含真实 token |
| API-LS-10 | `POST /api/local-studio/chat` | Local Studio 使用错误 OpenAI-compatible provider 后再调用基础 `/v1/*` 与 `/v1beta/*` API smoke | Local Studio 错误被保存为受控错误；基础 API 不受影响；请求日志 group 能清楚区分 Local Studio provider 请求与基础业务线请求 |
| API-REQ-01 | `/request-logs/*` | status、list、detail、export、delete | lifecycle 完整且导出 JSON 可解析 |
| API-BASE-01 | `/v1/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/images/generations` | 基础 API smoke | 基础兼容 API 不因 Local Studio 改造回归 |
| API-ACC-01 | `GET /health`、`GET /accounts`、`POST /accounts/{id}/test`、`DELETE /accounts/{id}` | 使用 WSL 临时账号目录副本；先确认 startup warmup `complete` 或按失败状态明确判定不可用，再对临时副本账号执行健康检查和删除 | `/accounts/{id}/test` 只作为凭据形状检查，不得替代真实生成或 `GET /health` warmup oracle；删除账号必须返回 200/404 的受控结果且不得出现 500；删除后 registry、账号目录、active account 一致 |

## 缺陷专项断言

### BUG-GEMINI-IMAGE-TOOL-01

复现链路：Google AI Studio provider，Local Studio Responses interface，stream on，search on，image tool on，历史中先产生新闻回答，再发送 `做成图片`。

必须断言：

* 浏览器没有未捕获 console error。
* 客户端 SSE 不包含 `event: error`，或如果上游确实失败，UI 只显示受控错误且会话保存该错误。
* 服务端日志不包含 ASGI exception。
* 请求记录 upstream response 不包含 `Please enable tool_config.include_server_side_tool_invocations`。
* 最终如果有图片，UI 和 conversation JSON 中同一张图片只出现一次。
* Reasoning/tool details 有可见入口或被保存，不能只剩 `Generated image` 且完全丢失过程。

### BUG-OPENAI-SEARCH-STREAM-01

复现链路：OpenAI-compatible provider，Local Studio Responses interface，stream on，search on，上游返回 HTTP 400。

必须断言：

* `/api/local-studio/chat` 返回一条格式正确的 SSE error 事件，不断开成浏览器网络错误。
* UI 当前会话显示错误，输入框恢复可用。
* 服务端 stderr 不包含 `httpx.ResponseNotRead`、`ExceptionGroup`、`Exception in ASGI application`。
* 请求记录包含 upstream 400 response body 和 client response，不缺阶段。
* 故障后 `/api/local-studio/health`、`/request-logs/status` 仍返回 200。

### BUG-OPENAI-SEARCH-TOOL-TYPE-01

复现链路：OpenAI-compatible provider，Local Studio Responses interface，stream on/off，search on，发送任意搜索类 prompt。

必须断言：

* 请求记录 upstream request 的 `tools` 只包含 `{"type":"web_search"}` 作为搜索工具，不包含 `web_search_preview`。
* API 响应和 UI 当前会话不得出现 `HTTP 400: Unsupported tool type: web_search_preview`。
* 若上游仍因其他兼容性原因返回 4xx，错误必须是受控错误，服务端 stderr 不包含 `httpx.ResponseNotRead`、`ExceptionGroup`、`Exception in ASGI application`。
* 故障或成功后 `/api/local-studio/health`、`/request-logs/status` 仍返回 200。
* 同一轮回归还要验证 Google AI Studio provider 继续使用 `web_search_preview`，防止修复 OpenAI provider 时破坏内置 Google provider。

### BUG-OPENAI-RESPONSES-REASONING-01

复现链路：OpenAI-compatible provider，Local Studio Responses interface，选择 UI 标记 reasoning 可用的模型，设置 `reasoning=high`、`summary=auto`，stream on/off 各发送一次需要分步判断的 prompt。

必须断言：

* upstream request 包含 OpenAI Responses 支持的 `reasoning` 参数，且不会在 OpenAI Chat、Gemini、Claude interface 中误发 Responses-only 字段。
* 流式 parser 覆盖所有已知 Responses reasoning 事件变体：`response.reasoning.delta/done`、`response.reasoning_text.delta/done`、`response.reasoning_summary_text.delta/done`、`response.reasoning_summary_part.added/done`、reasoning `response.output_item.added/done`。
* 如果 upstream response 或 SSE event 返回 reasoning summary、reasoning item、tool call、search citation、image generation call 或 usage，后端解析后的 API 响应、最终 SSE `local_studio.completed`、conversation JSON、UI 当前消息和 request log detail/export 都能保留或展示对应结构。
* 测试脚本必须 hard-fail：当 upstream 有 reasoning event/item 而 `local_studio.completed` 的最后一条 assistant 没有非空 `thinking`，或浏览器 UI/刷新恢复后没有 `Reasoning summary` 等价入口时，不能只把 `assistant_has_thinking=false` 写进结果。
* 如果上游模型没有返回 reasoning summary，UI 必须显示受控空状态或隐藏 reasoning 入口，并在 API/UI 结果中记录 `no_reasoning_summary` 这类可解释状态；不得把“没有返回”和“解析/保存丢失”混在一起。
* 刷新页面、重跑该轮、重复 prompt 后，reasoning/tool details、usage 和错误状态仍一致，且不出现 Local Studio cache 标记。
* 当前 assistant 消息不能只剩最终文本或图片而完全丢失过程；不能残留“正在等待模型与图片工具”等 pending 状态；不能生成空 assistant 卡片。
* 服务端 stderr 不包含 `ResponseNotRead`、`ExceptionGroup`、`Exception in ASGI application`，故障或成功后 `/api/local-studio/health`、`/request-logs/status` 仍返回 200。

### BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01

复现链路：Google AI Studio 账号态文本，warmup 或 Local Studio Google 文本请求先按 `SYSTEM_TEST_MODEL_CANDIDATES` 选择低额度风险文本模型；当显式回归模型选择问题时可以指定 `gemini-3.5-flash`，但常规完整系统测试不得默认盯着该旗舰模型。native UI worker 打开官方 AI Studio 页面并通过真实模型选择控件选择最终 selected model。

必须断言：

* MCP 可见官方 AI Studio 页面当前处于可发送 prompt 的聊天界面，而不是 `chat spark playground`、空白页、账号选择页或其他非目标 surface。
* 模型选择必须通过可见模型控件完成，并记录当前已选模型 label、候选模型 label 列表、AI Studio URL path、authuser、截图和 snapshot 摘要。
* 请求任何候选 Gemini 文本模型时，native sender 不能只因为候选列表里出现相似 label 就当作匹配成功；必须证明 wire model、UI label 和 request-log upstream model 映射到同一个 selected model 或受控 alias。显式回归 `gemini-3.5-flash` 时，不能只因为候选列表里出现 `Gemini 3 Flash Preview` 就当作匹配成功。
* 如果 requested model 不在模型列表、官方 UI 不可见或选择失败，脚本必须尝试下一个等价文本模型，并记录 requested model、selected model、fallback_used、candidate_chain、selection_probe_attempts、selection_verified 和失败候选原因；如果所有候选都不可用，本用例 fail。
* 如果返回 `text_model_not_found`、`selected=false`、`current=chat spark playground`、目标模型不在可见候选列表、或 URL 从 `/u/<n>/prompts/new_chat` 跳到错误账号/错误 surface，本用例 fail，且不能继续把 warmup 标记为 complete。
* 失败报告必须包含 server log 中 `native_ui_sender stage=*` 摘要、MCP screenshot/snapshot、可见模型列表、`/health` warmup 状态和 Local Studio UI 错误状态；不得只记录 API 失败文本。
* 通过报告必须包含 native worker pool 复用证据、matched `GenerateContent` response 证据、request log group id、Local Studio UI 首字/完成时间和官方网页对照时间。

## 执行顺序

1. 先执行 `ENV-01` 到 `ENV-04`，证明本轮测试来自干净 WSL 临时副本、独立数据目录、独立 venv 和无临时补丁状态；失败时停止并把系统测试标记为环境/流程失败。
2. 启动 WSL 临时环境前先运行 Native UI worker import/start preflight；该预检必须使用同一 venv、清空外部 `PYTHONPATH`，并从源码入口导入父进程模块后启动 worker 子进程。
3. 启动 WSL 临时环境，确认 health/model/account 预检通过。
4. 开启 request logs。
5. 判定 Provider Manager / shared provider-model pool rollout phase，执行 `PM-ROLL-00`，并为尚未进入的阶段记录带证据的 `not_applicable`。
6. 执行 API 级 P0 smoke，先验证 provider/model/chat/search/image/reasoning 基础链路；Google 账号态文本 API smoke 必须确认 server.log 中出现 native worker pool ready/start/matched-response 证据，且没有 `ModuleNotFoundError: No module named 'aistudio_api'`。
7. 使用 MCP browser tools 可见打开官方 AI Studio，完成官方网页直接 UI 基准测量；如果官方网页不可达或目标模型不可选，本轮标记环境/账号/model-selection 阻塞，不能继续声明 Local Studio 性能通过。
8. 使用 MCP browser tools 从 Windows 主机打开 WSL 服务的真实 WebUI，执行浏览器 P0 Local Studio 矩阵、完整用户旅程覆盖清单、bug 专项和架构契约断言；不能用 WSL 内 headless API smoke、静态 DOM 检查或状态注入替代主机 UI 操作。
9. 如果当前代码已进入 Phase 1/2/3，执行对应 `PM-CP-*`、`PM-DP-*`、`PM-PROTO-*`、`PM-RT-*` 和 `PM-AUDIT-*` 门禁。
10. 执行 P1 UI 状态、会话、无结果缓存回归、附件、基础模块回归和 provider/reasoning 隔离用例。
11. 汇总 `architecture-contract-results`、`provider-manager-phase-gate-results`、`mcp-visible-ui-results` 和 `performance-comparison-results`，逐项标记每条架构契约断言、用户功能和性能预算的 pass/fail/not_applicable。
12. 导出必要请求记录和截图到本次临时目录的 `artifacts/`，检查脱敏后再附到人工报告；不要提交。
13. 清理临时服务、浏览器进程和临时数据目录。

## 通过门禁

* P0 全部通过；P1 不通过项必须有明确 bug 编号、日志、截图和请求记录 group id。
* `ENV-01` 到 `ENV-04` 必须全部通过；任何开发工作区运行、脏工作区运行、临时补丁后运行、复用开发数据目录或连接旧开发服务的结果都不能标记 `SYSTEM_TEST_PASS`。
* 真实凭据门禁必须通过；Google AI Studio 账号态路径和 OpenAI-compatible 路径必须分别使用真实凭据完成真实 API 与真实 UI 用户路径，且 request log 证明发生了真实上游调用并完成脱敏。任何假凭据、mock provider、stub upstream、只检查 key 文件存在、或只用单元测试覆盖 provider 集成都不能标记 `SYSTEM_TEST_PASS`。
* 没有未捕获 ASGI 异常、浏览器 console error 或 Playwright 页面崩溃。
* 所有成功路径都能在 UI 中看到用户可理解结果，并在 API/request log 中看到对应请求。
* 所有失败路径都是受控失败，且服务继续可用。
* WSL Native UI worker import/start preflight 必须通过；server.log 不得出现 worker 子进程 `ModuleNotFoundError: No module named 'aistudio_api'`、`Error while finding module specification for 'aistudio_api.infrastructure.gateway.native_ui_sender'` 或以 `/api/local-studio/health` 成功掩盖 worker 不可用的情况。
* 主机 Playwright UI 测试必须通过 MCP browser tools 可见连接 WSL 服务并完成 Local Studio 用户操作；测试报告需包含 MCP snapshot、UI 断言、console/network 摘要、被操作控件清单和截图路径。没有 MCP 可见 UI 阶段、UI 阶段为空、或用状态注入替代用户操作，本轮系统测试结论必须为失败或不完整。
* Google AI Studio 账号态文本路径必须证明 native UI worker pool 生效：配置值可查询、重复请求未每次启动新 helper、同账号并发可以租用多个 worker、worker 失败会重启，且 native UI 匹配到的 `401/403/429` 不被 raw replay 覆盖。
* `BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01` 必须通过；出现 `text_model_not_found`、错误 AI Studio surface、错误 authuser、错误模型 label 映射或目标模型不可见时，warmup 和 Google 文本 UI/API 用例不能标记通过。
* Google AI Studio 账号态文本响应时间必须满足官方网页对照预算；如果 Local Studio 首字或完成时间超预算，必须标记性能失败并附官方/UI 两侧样本，而不是用最终返回文本掩盖体验退化。
* 所有适用的架构契约断言必须通过；`not_applicable` 必须有明确原因，不能用于掩盖未覆盖路径。
* Provider Manager / shared provider-model pool 阶段门禁必须通过：Phase 0 要证明当前 Local Studio 兼容基线未回归；Phase 1/2/3 一旦有对应实现入口，对应控制面、数据面、协议适配、路由/fallback 和审计安全断言不得整体跳过。
* 测试 harness 本身也要通过门禁：每个 P0/P1 expected 字段、`contains_*` 错误签名、`assistant_has_*`/`*_visible` 可见性字段都必须有对应 fail 条件；只采集不判定的脚本不能作为“全部通过”的依据。
* Reasoning/tool/search/image 过程信息如果由上游返回，不能只在 request log 里存在而 UI/conversation 完全丢失；如果上游未返回，必须有可解释空状态。
* Local Studio 不得跨 provider、interface、model、tool、reasoning、attachment 或 token 复用旧 assistant 结果；重复 prompt 也必须 fresh upstream。
* Request log、截图、导出文件不包含真实 OpenAI token、Google cookie、Authorization header 明文或账号 storage state。
* Local Studio 的 provider 封装不影响 Playground、图片生成、请求记录和账号管理独立入口。

## 建议输出物

每次完整系统测试完成后，在临时目录保存：

* `artifacts/summary.md`：执行环境、git commit、服务端口、通过/失败列表。
* `artifacts/api-results.json`：每个 API 用例的状态码、耗时、request log group id、脱敏错误摘要。
* `artifacts/ui-results.json`：每个 UI 用例的页面、断言、截图路径、console/network 摘要。
* `artifacts/mcp-visible-ui-results.json`：每个 MCP 可见 UI 用例的页面 URL、snapshot 摘要、被操作控件、用户输入、可见等待状态、截图路径和 pass/fail 结论。
* `artifacts/performance-comparison-results.json`：官方 AI Studio 直接 UI 与 Local Studio UI 的首字/完成耗时样本、中位数、预算计算、warmup 状态和性能结论。
* `artifacts/architecture-contract-results.json`：每条架构契约断言在各用例中的 pass/fail/not_applicable 状态和证据路径。
* `artifacts/provider-manager-phase-gate-results.json`：当前 rollout phase、`PM-*` 用例结果、未适用阶段证据、routing decision / attempt plan / audit safety 摘要。
* `artifacts/screenshots/`：关键 UI 成功/失败截图。
* `artifacts/server.log`：服务端日志，检查后确认无 secrets。

这些输出物用于人工验收或 bug 附件，不进入仓库。
