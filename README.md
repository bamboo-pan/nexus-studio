# Nexus Studio

Nexus Studio 是 Google AI Studio 的本地 API 代理，把浏览器态 AI Studio 能力包装成 OpenAI 兼容接口和 Gemini 原生接口。项目使用 Camoufox 浏览器携带 Google 账号登录态与 BotGuard snapshot，请求从本地 FastAPI 服务进入，再由浏览器回放到 AI Studio。

[English](./README_EN.md)

Nexus Studio 基于 [chrysoljq/aistudio-api](https://github.com/chrysoljq/aistudio-api) 二次开发，保留浏览器回放 Google AI Studio 的核心思路，并在本仓库继续扩展兼容 API、Local Studio、账号管理和运维体验。

## 功能概览

- **OpenAI 兼容接口**：`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/models`、`/v1/images/generations`
- **Gemini 原生接口**：`/v1beta/models`、`:generateContent`、`:streamGenerateContent`、`:countTokens`
- **WebUI**：内置 Playground、Local Studio、图片生成工作台、账号管理和运行统计视图
- **流式输出**：chat completions、Responses 兼容流、Messages 兼容流与 Gemini `streamGenerateContent` 支持 SSE
- **多模态输入**：支持图片输入，也支持模型能力允许的本地文件 inline data
- **工具能力**：支持 Google Search、OpenAI/Anthropic 常见搜索工具别名、Code Execution、函数声明/工具调用映射
- **Thinking**：支持 `off`、`low`、`medium`、`high` 推理强度控制
- **结构化输出**：支持 `response_format` / `json_schema`，按模型能力校验
- **图片生成与编辑**：支持 OpenAI 兼容生图接口、参考图输入、服务端图片持久化和会话历史
- **账号管理**：支持浏览器登录、凭证导入/导出、账号健康检查、Free/Pro/Ultra 等级标记
- **账号轮询**：支持 `round_robin` 均衡模式、`lru`、`least_rl`、`exhaustion`，并记录账号和模型运行统计
- **纯 HTTP 实验模式**：可绕过浏览器尝试有限文本请求，不作为完整兼容模式

## 快速开始

### 环境要求

- Python 3.11+
- 可以运行 Camoufox/Playwright Firefox 的系统环境
- 至少一个可登录 AI Studio 的 Google 账号

### 本地源码运行

```bash
git clone https://github.com/bamboo-pan/nexus-studio.git
cd nexus-studio

python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux / macOS
# source .venv/bin/activate

pip install -r requirements.txt
python main.py server --port 8080 --camoufox-port 9222
```

打开 http://localhost:8080 ，在 `账号管理` 页面点击添加账号并完成浏览器登录。登录成功后，账号凭证会保存到 `data/accounts/`，后续请求会使用当前激活账号。

### 安装为命令行工具

```bash
pip install -e .

nexus-studio server --port 8080 --camoufox-port 9222
# 或直接启动服务入口
nexus-studio-server --port 8080 --camoufox-port 9222
```

本地根目录的 `python main.py ...` 和安装后的 `nexus-studio ...` 使用同一套子命令：`server`、`client`、`snapshot`。兼容旧命令 `aistudio-api`、`aistudio-api-server`、`aistudio-api-client`。

## WebUI

服务启动后根路径会跳转到 `/static/index.html`。

- `#chat`：模型调试 Playground，支持模型能力感知的参数控制、附件上传、流式输出、搜索、Thinking 和结构化输出测试
- `#studio`：Local Studio，可通过 provider profile 连接 OpenAI 或兼容 `/v1` 地址，并可在 OpenAI Chat、OpenAI Responses、Gemini、Claude 模式间切换；支持 provider 下拉选择、流式输出、token 统计、附件、本地会话、Local request cache、Responses 模式下的 `web_search` 和 `gpt-image-2` 工具
- `#images`：图片生成与编辑工作台，支持尺寸/数量选择、参考图、历史素材、会话保存与恢复
- `#requests`：请求记录，支持保存、查看、导出和批量删除完整请求生命周期
- `#accounts`：账号管理，支持登录、切换、健康检查、等级标记、轮询模式、运行统计、凭证导入/导出

### Local Studio

`#studio` 的接口模式与 Playground 独立，可自由选择 OpenAI Chat Completions、OpenAI Responses、Gemini 或 Claude Messages。Provider profile 会在浏览器本地记住名称、Base URL、token、timeout 和接口模式，当前选中的 provider 会随页面刷新恢复。模型列表、能力标记、推理/流式开关、`web_search`、Local request cache 和 token 统计会按当前模式工作；发送后输入框会立即清空，请求记录开启时也会保存 Local Studio 的客户端请求、上游请求、上游响应和客户端响应阶段，便于排查兼容服务问题。

Local request cache 是 Local Studio 自己的本地结果复用缓存，存储在 `AISTUDIO_LOCAL_STUDIO_DIR/cache/requests` 下，按 provider、token hash、接口模式、模型、请求体和 namespace 隔离。它不同于上游返回的 `cached_tokens` / `cachedContentTokenCount` 用量指标，也不同于 BotGuard snapshot cache。

#### 图片工具

Responses 模式下的对话模型列表会隐藏 `gpt-image-*`、音频、实时、TTS、转写和 embedding 等专用模型，避免误选非聊天入口。图片工具固定使用 `gpt-image-2`，尺寸选项按 OpenAI 官方 prompting guide 约束提供：`1024x1024`、`1024x1536`、`1536x1024`、`1536x864`、`2560x1440`、`3824x2144`，也可输入自定义 `WIDTHxHEIGHT`。本地会在发送前校验两边必须是 16 的倍数、最长边小于 `3840px`、长短边比例不超过 `3:1`、总像素在 `655,360` 到 `8,294,400` 之间。超过 `2560x1440` 的输出按官方说明视为实验性尺寸。

## 认证与账号

推荐通过 WebUI 添加账号。服务会启动一个有头登录浏览器，完成登录后保存 Playwright storage state。

可用账号 API：

```bash
# 列出账号
curl http://localhost:8080/accounts

# 启动登录流程，返回 session_id
curl http://localhost:8080/accounts/login/start \
  -H "Content-Type: application/json" \
  -d '{"name":"main"}'

# 查询登录状态
curl http://localhost:8080/accounts/login/status/login_xxxxxxxx

# 激活账号
curl -X POST http://localhost:8080/accounts/acc_xxxxxxxx/activate

# 账号健康检查，可能同步更新 Free/Pro/Ultra 等级
curl -X POST http://localhost:8080/accounts/acc_xxxxxxxx/test
```

### 凭证导入 / 导出

```bash
# 导出全部账号
curl http://localhost:8080/accounts/export > credentials.backup.json

# 导出单个账号
curl http://localhost:8080/accounts/acc_xxxxxxxx/export > one-account.backup.json

# 导入项目备份包或单账号 storage state
curl http://localhost:8080/accounts/import \
  -H "Content-Type: application/json" \
  --data-binary @credentials.backup.json
```

备份文件包含可用于登录的 Cookie 和令牌。只把它们保存在可信位置，不要提交到 Git，也不要分享给他人。

## API 示例

### OpenAI Chat Completions

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash-preview",
    "messages": [{"role": "user", "content": "你好，介绍一下你能做什么"}],
    "stream": true,
    "thinking": "low",
    "grounding": true
  }'
```

### 图片与文件输入

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash-preview",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR..."}},
        {"type": "file", "file": {"file_data": "data:text/plain;base64,SGVsbG8=", "filename": "note.txt", "mime_type": "text/plain"}},
        {"type": "text", "text": "总结图片和文件内容"}
      ]
    }]
  }'
```

文件输入会根据 `/v1/models` 返回的 `capabilities.file_input` 和 `capabilities.file_input_mime_types` 校验。图片模型通过 chat completions 触发生图时只支持文本提示词，不支持附件。

### Responses API

```bash
curl http://localhost:8080/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash-preview",
    "instructions": "只返回 JSON",
    "input": "给出服务健康摘要",
    "text": {"format": {"type": "json_schema", "name": "Health", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}
  }'
```

`/v1/responses` 支持面向客户端兼容的 SSE 子集：文本增量、函数调用输出项、搜索调用占位项和完成事件。后台任务、托管 `file_search`、`computer_use`、完整会话状态等 OpenAI 云端特性不在浏览器回放模式范围内。

### Messages API

```bash
curl http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash-preview",
    "system": "回答保持简洁",
    "messages": [{"role": "user", "content": "写一句项目介绍"}],
    "max_tokens": 512
  }'
```

`/v1/messages` 支持 Anthropic Messages 的实用子集：非流式文本/`tool_use`，以及 `stream: true` 时的 `message_start`、`content_block_*`、`message_delta`、`message_stop` 事件。还提供 `/v1/messages/count_tokens` 供 Claude Code 类网关探测使用。Prompt cache、Anthropic beta 服务端工具、精确 thinking 签名等云端特性不会模拟。

### 客户端兼容建议

| 客户端 | 推荐入口 | 支持说明 |
|--------|----------|----------|
| CherryStudio / OpenAI 兼容客户端 | `http://localhost:8080/v1`，Chat Completions | 基础聊天、流式、函数工具和搜索可用。搜索既兼容项目字段 `grounding: true`，也兼容常见 `web_search`、`web_search_preview`、`browser_search` 工具形状，并映射到 AI Studio Google Search。 |
| OpenCode | 自定义 OpenAI-compatible provider，使用 `/v1/chat/completions` | 推荐使用 OpenCode 的 `@ai-sdk/openai-compatible` 路径；工具调用和流式输出走 Chat Completions。 |
| Claude Code | Anthropic Messages 网关 `/v1/messages` | 支持基础 Messages、流式事件、`tool_use` 和 `/v1/messages/count_tokens`。若 Claude Code 模型发现不显示 Gemini ID，请通过 Claude Code 模型环境变量手动指定模型。 |
| Codex / OpenAI Responses 客户端 | `/v1/responses` | 支持非流式与流式 Responses 子集、JSON schema、函数调用和搜索映射；不支持 OpenAI 托管工具和后台 Responses。 |

OpenAI 风格搜索工具示例：

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash-preview",
    "messages": [{"role": "user", "content": "今天有什么重要科技新闻？"}],
    "tools": [{"type": "web_search"}],
    "stream": true
  }'
```

### OpenAI 兼容图片生成

```bash
curl http://localhost:8080/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-preview",
    "prompt": "画一座雨夜霓虹城市",
    "n": 2,
    "size": "1024x1024",
    "response_format": "url",
    "timeout": 180
  }'
```

`timeout` 为可选的单次图片生成超时秒数；不填时使用 `AISTUDIO_TIMEOUT_REPLAY`（默认 120 秒）。WebUI 图片生成页也提供“超时秒数”输入框，留空即使用服务端默认。

响应会包含 `url`、`b64_json`、`path`、`delete_url`、`mime_type` 和 `size_bytes`。生成图片会持久化到服务端目录，可通过 WebUI 删除，也可调用：

```bash
curl -X DELETE http://localhost:8080/generated-images/20260515/example.png
```

参考图编辑使用 `images` 字段，条目必须是 data URI、HTTP(S) URL，或 `{ "url": "..." }`：

```bash
curl http://localhost:8080/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-preview",
    "prompt": "保持构图，改成水彩风格",
    "images": ["data:image/png;base64,iVBOR..."],
    "size": "1024x1024",
    "response_format": "url"
  }'
```

### Gemini 原生接口

```bash
# 生成内容
curl http://localhost:8080/v1beta/models/gemini-3-flash-preview:generateContent \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "今天上海天气怎么样？"}]}],
    "tools": [{"googleSearchRetrieval": {}}]
  }'

# 流式生成
curl http://localhost:8080/v1beta/models/gemini-3-flash-preview:streamGenerateContent \
  -H "Content-Type: application/json" \
  -d '{"contents": [{"role": "user", "parts": [{"text": "写一段短诗"}]}]}'

# 估算 token
curl http://localhost:8080/v1beta/models/gemini-3-flash-preview:countTokens \
  -H "Content-Type: application/json" \
  -d '{"contents": [{"role": "user", "parts": [{"text": "你好"}]}]}'
```

`embedContent`、`batchEmbedContents`、`cachedContent` 和远程 `fileData.fileUri` 不是当前浏览器回放模式的实现范围，会返回清晰的 501 或 400 错误。

### Python OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="unused")

stream = client.chat.completions.create(
    model="gemini-3-flash-preview",
    messages=[{"role": "user", "content": "你好！"}],
    stream=True,
)

for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

### 命令行客户端

```bash
# 快速对话
python main.py client "今天天气怎么样？" --search

# 附带图片
python main.py client "这张图是什么？" -a photo.jpg

# 生图
python main.py client "画一只猫" --image --save cat.png

# 安装后也可使用
# nexus-studio client "你好" --search
# nexus-studio-client "你好" --search
# 兼容旧命令：aistudio-api / aistudio-api-client
```

## 运行状态与轮询

```bash
# 健康检查
curl http://localhost:8080/health

# 模型维度统计
curl http://localhost:8080/stats

# 查看轮询状态与账号统计
curl http://localhost:8080/rotation

# 设置轮询模式
curl http://localhost:8080/rotation/mode \
  -H "Content-Type: application/json" \
  -d '{"mode":"exhaustion","cooldown_seconds":60}'

# 手动切到下一个可用账号
curl -X POST http://localhost:8080/rotation/next
```

轮询模式：

| 模式 | 行为 |
|------|------|
| `round_robin` | 均衡模式；按账号池负载、请求统计和轻量会话亲和关系分配请求 |
| `lru` | 优先选择最久未使用账号 |
| `least_rl` | 优先选择限流次数最少账号 |
| `exhaustion` | 持续使用当前健康账号，直到限流、隔离、过期、缺少凭证或不适合当前模型再切换 |

图片模型会优先使用标记为 Pro/Ultra 的健康账号；如果没有可用高级账号，会降级使用当前可用账号并记录告警。

## 支持的模型

`/v1/models` 会返回每个模型的 `capabilities` 元数据。当前注册模型如下：

| 模型 ID | 类型 | 搜索 | 工具调用 | Thinking | 结构化输出 | 文件输入 | 图片尺寸 |
|---------|------|------|----------|----------|------------|----------|----------|
| `gemma-4-31b-it` | 文本 | 默认可用 | 可用 | 可用 | 可用 | 不支持 | - |
| `gemma-4-26b-a4b-it` | 文本 | 默认可用 | 可用 | 可用 | 可用 | 不支持 | - |
| `gemini-3-flash-preview` | 文本/多模态 | 可用 | 可用 | 可用 | 可用 | 支持 | - |
| `gemini-3.1-pro-preview` | 文本/多模态 | 可用 | 可用 | 可用 | 可用 | 支持 | - |
| `gemini-3.1-flash-lite` | 文本/多模态 | 可用 | 可用 | 可用 | 可用 | 支持 | - |
| `gemini-3.1-flash-image-preview` | 图片生成/编辑 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | `512x512`、`1024x1024`、`1024x1792`、`1792x1024` |
| `gemini-3-pro-image-preview` | 图片生成/编辑 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | Flash 尺寸 + `2048x2048`、`1536x2816`、`2816x1536`、`4096x4096`、`2304x4096`、`4096x2304` |
| `gemini-3.1-flash-live-preview` | 文本/多模态 | 可用 | 不支持 | 可用 | 可用 | 支持 | - |
| `gemini-3.1-flash-tts-preview` | TTS 文本 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | - |
| `gemini-pro-latest` | 文本/多模态 | 可用 | 可用 | 可用 | 可用 | 支持 | - |
| `gemini-flash-latest` | 文本/多模态 | 可用 | 可用 | 可用 | 可用 | 支持 | - |
| `gemini-flash-lite-latest` | 文本/多模态 | 可用 | 可用 | 可用 | 可用 | 支持 | - |

文本模型默认支持 `image/*`、`application/pdf`、`text/plain`、`text/markdown`、`text/csv`、`application/json`、`audio/*`、`video/*` 等 inline 文件类型，具体以 `/v1/models` 返回为准。未知模型在非严格路径会按名称推断通用文本或图片能力，但模型详情查询会要求注册模型。

## 配置

可通过环境变量或 `.env` 文件配置：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AISTUDIO_PORT` | `8080` | API 服务端口 |
| `AISTUDIO_CAMOUFOX_PORT` | `9222` | 网关 Camoufox 调试端口 |
| `AISTUDIO_LOGIN_CAMOUFOX_PORT` | `9223` | 账号登录用有头浏览器端口 |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | `gemma-4-31b-it` | 默认文本模型 |
| `AISTUDIO_DEFAULT_IMAGE_MODEL` | `gemini-3.1-flash-image-preview` | 默认图片模型 |
| `AISTUDIO_AUTH_FILE` | 自动发现 | 兼容遗留单文件 storage state；新账号池优先使用 `data/accounts` |
| `AISTUDIO_ACCOUNTS_DIR` | `./data/accounts` | 多账号注册表和各账号 `auth.json` 目录 |
| `AISTUDIO_TMP_DIR` | 系统临时目录 | 临时图片/文件转换目录 |
| `AISTUDIO_CAMOUFOX_HEADLESS` | `1` | 网关浏览器是否无头运行；登录浏览器始终有头 |
| `AISTUDIO_CAMOUFOX_PYTHON` | 空 | 指定启动 Camoufox 的 Python 解释器 |
| `AISTUDIO_PROXY_SERVER` | 空 | Camoufox 浏览器代理，例如 WSL 使用 Windows 代理时可设为 `http://<WSL 网关 IP>:7890` |
| `AISTUDIO_TIMEOUT_REPLAY` | `120` | 非流式回放默认超时秒数；图片生成可在 WebUI 或请求 `timeout` 字段中按单次请求覆盖 |
| `AISTUDIO_TIMEOUT_STREAM` | `120` | 流式请求超时秒数 |
| `AISTUDIO_TIMEOUT_CAPTURE` | `30` | 请求捕获超时秒数 |
| `AISTUDIO_SNAPSHOT_CACHE_TTL` | `3600` | BotGuard snapshot 缓存时间秒数 |
| `AISTUDIO_SNAPSHOT_CACHE_MAX` | `100` | snapshot 缓存最大条目数 |
| `AISTUDIO_DUMP_RAW_RESPONSE` | `0` | 是否保存原始请求/响应到磁盘 |
| `AISTUDIO_DUMP_RAW_RESPONSE_DIR` | `/tmp` | 原始请求/响应落盘目录 |
| `AISTUDIO_GENERATED_IMAGES_DIR` | `./data/generated-images` | 生成图片持久化目录 |
| `AISTUDIO_IMAGE_SESSIONS_DIR` | `./data/image-sessions` | 图片会话历史目录 |
| `AISTUDIO_LOCAL_STUDIO_DIR` | `./data/local-studio` | Local Studio 会话、附件、生成图片和 Local request cache 目录 |
| `AISTUDIO_GENERATED_IMAGES_ROUTE` | `/generated-images` | 生成图片静态访问和删除路由前缀 |
| `AISTUDIO_ACCOUNT_ROTATION_MODE` | `round_robin` | 默认账号轮询模式；`round_robin` 表示均衡模式 |
| `AISTUDIO_ACCOUNT_COOLDOWN_SECONDS` | `60` | 限流后冷却时间 |
| `AISTUDIO_ACCOUNT_MAX_RETRIES` | `3` | 账号相关最大重试配置 |
| `AISTUDIO_ACCOUNT_WARMUP_LIMIT` | `2` | 启动后后台预热的账号浏览器数量；设为 `0` 可关闭账号池预热 |
| `AISTUDIO_MAX_CONCURRENCY` | `3` | 服务端并发信号量大小 |
| `AISTUDIO_USE_PURE_HTTP` | `0` | 启用纯 HTTP 实验模式 |

> `AISTUDIO_USE_PURE_HTTP=1` 仍是实验模式：目前只尝试单轮、非流式纯文本请求。流式、图片、工具、图片输入、Thinking、多轮对话、系统指令、安全覆盖、结构化输出以及 BotGuard snapshot 依赖缺失都会返回清晰的 `501` 不支持错误。生产或完整兼容场景请使用默认浏览器模式。

## 架构

```text
客户端（OpenAI SDK / Gemini SDK / curl / WebUI）
    |
    v
FastAPI 应用
    |-- OpenAI 兼容路由：/v1/chat/completions、/v1/responses、/v1/messages、/v1/images/generations
    |-- Gemini 原生路由：/v1beta/models、:generateContent、:streamGenerateContent、:countTokens
    |-- 运行管理路由：/accounts、/rotation、/stats、/image-sessions、/generated-images
    v
应用服务层
    |-- 请求标准化、模型能力校验、结构化输出、工具/搜索/Thinking 配置
    |-- 账号选择、限流重试、图片持久化、统计记录
    v
AI Studio Gateway
    |-- 捕获 AI Studio 请求模板
    |-- 生成或复用 BotGuard snapshot
    |-- 重写 gRPC body 并解析响应
    v
Camoufox 浏览器会话
    |
    v
Google AI Studio
```

### BotGuard 工作原理

Google AI Studio 请求需要 BotGuard snapshot，用来证明请求来自真实浏览器环境。本项目在运行时定位前端 snapshot 生成函数，通过特征匹配和缓存生成 snapshot，再把标准化后的请求体注入浏览器执行。Google bundle 中的函数名可能变化，但特征模式相对稳定。

## 开发

```bash
# 运行全部单元测试
python -m pytest tests/

# 运行常用重点测试
python -m pytest tests/unit/test_model_capabilities.py tests/unit/test_static_frontend_capabilities.py

# 抓取 snapshot 调试
python main.py snapshot "测试 prompt"
```

项目代码位于 `src/aistudio_api/`，静态 WebUI 位于 `src/aistudio_api/static/`，运行时数据默认写入 `data/`。

## 致谢

- https://github.com/LuanRT/BgUtils
- https://github.com/iBUHub/AIStudioToAPI
- https://linux.do

## License

MIT
