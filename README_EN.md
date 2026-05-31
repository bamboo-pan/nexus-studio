# Nexus Studio

Nexus Studio is a local API proxy for Google AI Studio. It exposes browser-backed AI Studio access through OpenAI-compatible endpoints and Gemini-native endpoints. Requests enter a local FastAPI server, are normalized, then replayed through a Camoufox browser session that carries Google account auth state and BotGuard snapshots.

[中文](./README.md)

Nexus Studio is a secondary development based on [chrysoljq/aistudio-api](https://github.com/chrysoljq/aistudio-api). It keeps the browser-backed Google AI Studio replay approach and continues to extend compatibility APIs, Local Studio, account management, and operational tooling in this repository.

## Features

- **OpenAI-compatible API**: `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/models`, `/v1/images/generations`
- **Gemini-native API**: `/v1beta/models`, `:generateContent`, `:streamGenerateContent`, `:countTokens`
- **WebUI**: built-in Playground, Local Studio, image studio, account management, and runtime stats
- **Streaming**: SSE for chat completions, Responses-compatible streams, Messages-compatible streams, and Gemini `streamGenerateContent`
- **Multimodal input**: image input plus local inline files when model capabilities allow them
- **Tools**: Google Search, common OpenAI/Anthropic search-tool aliases, Code Execution, function declarations, and tool-call mapping
- **Thinking**: supports `off`, `low`, `medium`, and `high` reasoning controls
- **Structured output**: `response_format` / `json_schema` support with model-capability validation
- **Image generation and editing**: OpenAI-compatible image generation, reference images, server-side image persistence, and image-session history
- **Account management**: browser login, credential import/export, account health checks, and Free/Pro/Ultra tier labels
- **Account rotation**: `round_robin` balanced mode, `lru`, `least_rl`, and `exhaustion` modes with account/model stats
- **Experimental pure HTTP mode**: limited plain-text requests without the browser; not full compatibility mode

## Quick Start

### Requirements

- Python 3.11+
- A system environment that can run Camoufox/Playwright Firefox
- At least one Google account that can access AI Studio

### Run From Source

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

Open http://localhost:8080, go to `Account Management`, add an account, and complete browser login. Saved account credentials live under `data/accounts/`, and subsequent requests use the active account.

### Install Console Scripts

```bash
pip install -e .

nexus-studio server --port 8080 --camoufox-port 9222
# Or start the server entrypoint directly
nexus-studio-server --port 8080 --camoufox-port 9222
```

The local root wrapper `python main.py ...` and the installed `nexus-studio ...` command expose the same subcommands: `server`, `client`, and `snapshot`. Legacy aliases `aistudio-api`, `aistudio-api-server`, and `aistudio-api-client` remain available for compatibility.

## WebUI

The service root redirects to `/static/index.html`.

- `#chat`: model Playground with capability-aware controls, attachments, streaming, Search, Thinking, and structured-output tests
- `#studio`: Local Studio for OpenAI or compatible `/v1` endpoints through provider profiles, with provider switching, selectable OpenAI Chat, OpenAI Responses, Gemini, and Claude modes plus streaming, token usage, attachments, local conversations, Local request cache, and the `web_search` and `gpt-image-2` tools in Responses mode
- `#images`: image generation/editing studio with size/count controls, reference images, material history, and saved sessions
- `#requests`: request logs with complete lifecycle viewing, export, and bulk deletion
- `#accounts`: account management with login, switching, health checks, tier labels, rotation modes, runtime stats, and credential import/export

### Local Studio

The `#studio` interface mode is independent from the Playground and can be switched between OpenAI Chat Completions, OpenAI Responses, Gemini, and Claude Messages. Provider profiles are stored in browser local storage and remember name, Base URL, token, timeout, and interface mode; the active provider is restored after refresh. Model loading, capability badges, reasoning/stream controls, `web_search`, Local request cache, and token usage follow the selected mode. Accepted sends clear the input immediately, and when request logging is enabled the Local Studio client request, upstream request, upstream response, and client response are saved in the same lifecycle view for compatible-service debugging.

Local request cache is Local Studio's own local result-reuse cache under `AISTUDIO_LOCAL_STUDIO_DIR/cache/requests`. It is isolated by provider, token hash, interface mode, model, request body, and namespace. It is separate from upstream `cached_tokens` / `cachedContentTokenCount` usage metrics and from the BotGuard snapshot cache.

#### Image Tool

In Responses mode, the `#studio` conversation model list hides specialist models such as `gpt-image-*`, audio, realtime, TTS, transcription, and embedding models so the default choice stays chat-oriented. The image tool uses fixed model `gpt-image-2`. Its size options follow the OpenAI prompting guide constraints: `1024x1024`, `1024x1536`, `1536x1024`, `1536x864`, `2560x1440`, and `3824x2144`, plus a custom `WIDTHxHEIGHT` field. The server validates custom sizes before sending them upstream: both edges must be multiples of 16, the longest edge must be less than `3840px`, the long-to-short-edge ratio must be at most `3:1`, and total pixels must be between `655,360` and `8,294,400`. Outputs above `2560x1440` are treated as experimental.

## Authentication And Accounts

The recommended path is the WebUI. The service opens a headed login browser, then saves Playwright storage state when login completes.

Useful account APIs:

```bash
# List accounts
curl http://localhost:8080/accounts

# Start browser login and receive session_id
curl http://localhost:8080/accounts/login/start \
  -H "Content-Type: application/json" \
  -d '{"name":"main"}'

# Poll login status
curl http://localhost:8080/accounts/login/status/login_xxxxxxxx

# Activate an account
curl -X POST http://localhost:8080/accounts/acc_xxxxxxxx/activate

# Health-check an account; may update Free/Pro/Ultra tier
curl -X POST http://localhost:8080/accounts/acc_xxxxxxxx/test
```

### Credential Import / Export

```bash
# Export all accounts
curl http://localhost:8080/accounts/export > credentials.backup.json

# Export one account
curl http://localhost:8080/accounts/acc_xxxxxxxx/export > one-account.backup.json

# Import a project backup package or one-account storage state
curl http://localhost:8080/accounts/import \
  -H "Content-Type: application/json" \
  --data-binary @credentials.backup.json
```

Backup files contain cookies and tokens that can grant account access. Store them only in trusted locations, do not commit them to Git, and do not share them.

## API Examples

### OpenAI Chat Completions

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash-preview",
    "messages": [{"role": "user", "content": "Hello, summarize what you can do."}],
    "stream": true,
    "thinking": "low",
    "grounding": true
  }'
```

### Image And File Input

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
        {"type": "text", "text": "Summarize the image and file."}
      ]
    }]
  }'
```

File input is validated through `capabilities.file_input` and `capabilities.file_input_mime_types` from `/v1/models`. When chat completions targets an image-generation model, the shortcut supports text prompts only and rejects attachments.

### Responses API

```bash
curl http://localhost:8080/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash-preview",
    "instructions": "Return JSON only",
    "input": "Give a service health summary",
    "text": {"format": {"type": "json_schema", "name": "Health", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}
  }'
```

`/v1/responses` supports a practical client-compatibility SSE subset: text deltas, function-call output items, search-call placeholder items, and completion events. Hosted OpenAI features such as background mode, hosted `file_search`, `computer_use`, and full conversation state are outside browser replay mode.

### Messages API

```bash
curl http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash-preview",
    "system": "Keep answers concise",
    "messages": [{"role": "user", "content": "Write a one-sentence project intro."}],
    "max_tokens": 512
  }'
```

`/v1/messages` supports a practical Anthropic Messages subset: non-streaming text/`tool_use`, plus `message_start`, `content_block_*`, `message_delta`, and `message_stop` events when `stream: true` is set. `/v1/messages/count_tokens` is also available for Claude Code-style gateway probes. Prompt caching, Anthropic beta server tools, and exact thinking signatures are not simulated.

### Client Compatibility Recommendations

| Client | Recommended endpoint | Support notes |
|--------|----------------------|---------------|
| CherryStudio / OpenAI-compatible clients | `http://localhost:8080/v1`, Chat Completions | Basic chat, streaming, function tools, and search are available. Search accepts the project-specific `grounding: true` flag and common `web_search`, `web_search_preview`, and `browser_search` tool shapes, all mapped to AI Studio Google Search. |
| OpenCode | Custom OpenAI-compatible provider using `/v1/chat/completions` | Prefer OpenCode's `@ai-sdk/openai-compatible` path; tool calls and streaming use Chat Completions. |
| Claude Code | Anthropic Messages gateway `/v1/messages` | Supports basic Messages, streaming events, `tool_use`, and `/v1/messages/count_tokens`. If Claude Code model discovery does not show Gemini IDs, configure the model explicitly through Claude Code model environment variables. |
| Codex / OpenAI Responses clients | `/v1/responses` | Supports non-streaming and streaming Responses subsets, JSON schema, function calls, and search mapping; hosted OpenAI tools and background Responses are not supported. |

OpenAI-style search tool example:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash-preview",
    "messages": [{"role": "user", "content": "What are today's important tech news stories?"}],
    "tools": [{"type": "web_search"}],
    "stream": true
  }'
```

### OpenAI-Compatible Image Generation

```bash
curl http://localhost:8080/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-preview",
    "prompt": "Draw a neon city in the rain",
    "n": 2,
    "size": "1024x1024",
    "response_format": "url",
    "timeout": 180
  }'
```

`timeout` is an optional per-request image generation timeout in seconds. When omitted, the server uses `AISTUDIO_TIMEOUT_REPLAY` (default 120 seconds). The WebUI image generation page also includes a timeout field; leave it blank to use the server default.

The response includes `url`, `b64_json`, `path`, `delete_url`, `mime_type`, and `size_bytes`. Generated images are persisted on the server. Delete them from the WebUI or call:

```bash
curl -X DELETE http://localhost:8080/generated-images/20260515/example.png
```

Image editing uses the `images` field. Each item must be a data URI, an HTTP(S) URL, or `{ "url": "..." }`:

```bash
curl http://localhost:8080/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-preview",
    "prompt": "Keep the composition and turn it into watercolor",
    "images": ["data:image/png;base64,iVBOR..."],
    "size": "1024x1024",
    "response_format": "url"
  }'
```

### Gemini-Native API

```bash
# Generate content
curl http://localhost:8080/v1beta/models/gemini-3-flash-preview:generateContent \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "What is the latest weather in Shanghai?"}]}],
    "tools": [{"googleSearchRetrieval": {}}]
  }'

# Stream content
curl http://localhost:8080/v1beta/models/gemini-3-flash-preview:streamGenerateContent \
  -H "Content-Type: application/json" \
  -d '{"contents": [{"role": "user", "parts": [{"text": "Write a short poem"}]}]}'

# Estimate tokens
curl http://localhost:8080/v1beta/models/gemini-3-flash-preview:countTokens \
  -H "Content-Type: application/json" \
  -d '{"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]}'
```

`embedContent`, `batchEmbedContents`, `cachedContent`, and remote `fileData.fileUri` are outside the current browser replay implementation and return clear 501 or 400 errors.

### Python OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="unused")

stream = client.chat.completions.create(
    model="gemini-3-flash-preview",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)

for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

### CLI Client

```bash
# Quick chat
python main.py client "What's the weather today?" --search

# With an image
python main.py client "What is this?" -a photo.jpg

# Image generation
python main.py client "Draw a cat" --image --save cat.png

# Installed alternatives
# nexus-studio client "Hello" --search
# nexus-studio-client "Hello" --search
# Legacy aliases: aistudio-api / aistudio-api-client
```

## Runtime Status And Rotation

```bash
# Health check
curl http://localhost:8080/health

# Per-model stats
curl http://localhost:8080/stats

# Rotation status and account stats
curl http://localhost:8080/rotation

# Set rotation mode
curl http://localhost:8080/rotation/mode \
  -H "Content-Type: application/json" \
  -d '{"mode":"exhaustion","cooldown_seconds":60}'

# Force switch to the next available account
curl -X POST http://localhost:8080/rotation/next
```

Rotation modes:

| Mode | Behavior |
|------|----------|
| `round_robin` | Balanced mode; distribute requests by account-pool load, request counts, and lightweight session affinity |
| `lru` | Prefer the least recently used account |
| `least_rl` | Prefer the account with the fewest rate-limit events |
| `exhaustion` | Keep using the current healthy account until it becomes rate-limited, isolated, expired, missing auth, or unsuitable for the selected model |

Image models prefer healthy accounts marked Pro/Ultra. If no premium account is available, the service falls back to an available account and logs a warning.

## Supported Models

`/v1/models` returns capability metadata for every registered model. Current registered models:

| Model ID | Type | Search | Tool calls | Thinking | Structured output | File input | Image sizes |
|----------|------|--------|------------|----------|-------------------|------------|-------------|
| `gemma-4-31b-it` | Text | Default available | Yes | Yes | Yes | No | - |
| `gemma-4-26b-a4b-it` | Text | Default available | Yes | Yes | Yes | No | - |
| `gemini-3-flash-preview` | Text/multimodal | Yes | Yes | Yes | Yes | Yes | - |
| `gemini-3.1-pro-preview` | Text/multimodal | Yes | Yes | Yes | Yes | Yes | - |
| `gemini-3.1-flash-lite` | Text/multimodal | Yes | Yes | Yes | Yes | Yes | - |
| `gemini-3.1-flash-image-preview` | Image generation/editing | No | No | No | No | No | `512x512`, `1024x1024`, `1024x1792`, `1792x1024` |
| `gemini-3-pro-image-preview` | Image generation/editing | No | No | No | No | No | Flash sizes + `2048x2048`, `1536x2816`, `2816x1536`, `4096x4096`, `2304x4096`, `4096x2304` |
| `gemini-3.1-flash-live-preview` | Text/multimodal | Yes | No | Yes | Yes | Yes | - |
| `gemini-3.1-flash-tts-preview` | TTS text | No | No | No | No | No | - |
| `gemini-pro-latest` | Text/multimodal | Yes | Yes | Yes | Yes | Yes | - |
| `gemini-flash-latest` | Text/multimodal | Yes | Yes | Yes | Yes | Yes | - |
| `gemini-flash-lite-latest` | Text/multimodal | Yes | Yes | Yes | Yes | Yes | - |

Text models generally allow inline `image/*`, `application/pdf`, `text/plain`, `text/markdown`, `text/csv`, `application/json`, `audio/*`, and `video/*`, but always prefer the exact `/v1/models` response. Unknown models on non-strict paths are inferred as generic text or image models by name; model detail lookup requires a registered model.

## Configuration

Use environment variables or a `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `AISTUDIO_PORT` | `8080` | API server port |
| `AISTUDIO_CAMOUFOX_PORT` | `9222` | Gateway Camoufox debug port |
| `AISTUDIO_LOGIN_CAMOUFOX_PORT` | `9223` | Headed browser port used for account login |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | `gemma-4-31b-it` | Default text model |
| `AISTUDIO_DEFAULT_IMAGE_MODEL` | `gemini-3.1-flash-image-preview` | Default image model |
| `AISTUDIO_AUTH_FILE` | auto-discovered | Legacy single storage-state file; the account pool under `data/accounts` takes priority for normal runs |
| `AISTUDIO_ACCOUNTS_DIR` | `./data/accounts` | Account registry and per-account `auth.json` directory |
| `AISTUDIO_TMP_DIR` | system temp directory | Temporary image/file conversion directory |
| `AISTUDIO_CAMOUFOX_HEADLESS` | `1` | Whether the gateway browser runs headless; login browser is always headed |
| `AISTUDIO_CAMOUFOX_PYTHON` | empty | Python executable used to launch Camoufox |
| `AISTUDIO_PROXY_SERVER` | empty | Camoufox browser proxy, for example `http://<WSL gateway IP>:7890` when WSL must use a Windows proxy |
| `AISTUDIO_TIMEOUT_REPLAY` | `120` | Default non-streaming replay timeout in seconds; image generation can override it per request from the WebUI or `timeout` request field |
| `AISTUDIO_TIMEOUT_STREAM` | `120` | Streaming request timeout in seconds |
| `AISTUDIO_TIMEOUT_CAPTURE` | `30` | Request-capture timeout in seconds |
| `AISTUDIO_SNAPSHOT_CACHE_TTL` | `3600` | BotGuard snapshot cache TTL in seconds |
| `AISTUDIO_SNAPSHOT_CACHE_MAX` | `100` | Maximum snapshot cache entries |
| `AISTUDIO_DUMP_RAW_RESPONSE` | `0` | Dump raw request/response exchanges to disk |
| `AISTUDIO_DUMP_RAW_RESPONSE_DIR` | `/tmp` | Raw exchange dump directory |
| `AISTUDIO_GENERATED_IMAGES_DIR` | `./data/generated-images` | Directory for persisted generated images |
| `AISTUDIO_IMAGE_SESSIONS_DIR` | `./data/image-sessions` | Image-session history directory |
| `AISTUDIO_LOCAL_STUDIO_DIR` | `./data/local-studio` | Local Studio conversations, attachments, generated image assets, and Local request cache |
| `AISTUDIO_GENERATED_IMAGES_ROUTE` | `/generated-images` | Static serving and deletion route prefix for generated images |
| `AISTUDIO_ACCOUNT_ROTATION_MODE` | `round_robin` | Default account rotation mode; `round_robin` means balanced mode |
| `AISTUDIO_ACCOUNT_COOLDOWN_SECONDS` | `60` | Cooldown after rate limit |
| `AISTUDIO_ACCOUNT_MAX_RETRIES` | `3` | Account-related max retry setting |
| `AISTUDIO_ACCOUNT_WARMUP_LIMIT` | `2` | Number of account browsers to warm in the background after startup; set `0` to disable account-pool warmup |
| `AISTUDIO_MAX_CONCURRENCY` | `3` | Server-side concurrency semaphore size |
| `AISTUDIO_USE_PURE_HTTP` | `0` | Enable experimental pure HTTP mode |

> `AISTUDIO_USE_PURE_HTTP=1` is still experimental. It only attempts single-turn, non-streaming plain-text requests today. Streaming, images, tools, image input, thinking, system instructions, multi-turn conversations, safety overrides, structured output, and missing BotGuard snapshot support return clear `501` unsupported errors. Use the default browser mode for production or full compatibility.

## Architecture

```text
Client (OpenAI SDK / Gemini SDK / curl / WebUI)
    |
    v
FastAPI app
    |-- OpenAI-compatible routes: /v1/chat/completions, /v1/responses, /v1/messages, /v1/images/generations
    |-- Gemini-native routes: /v1beta/models, :generateContent, :streamGenerateContent, :countTokens
    |-- Runtime routes: /accounts, /rotation, /stats, /image-sessions, /generated-images
    v
Application service layer
    |-- request normalization, model-capability validation, structured output, tools/search/thinking config
    |-- account selection, rate-limit retry, image persistence, stats recording
    v
AI Studio gateway
    |-- captures an AI Studio request template
    |-- generates or reuses a BotGuard snapshot
    |-- rewrites the gRPC body and parses the response
    v
Camoufox browser session
    |
    v
Google AI Studio
```

### How BotGuard Works

Google AI Studio requests require a BotGuard snapshot, which proves the request came from a real browser environment. This project locates the frontend snapshot function at runtime, uses feature matching and caching to generate snapshots, then injects the normalized request body through the browser. Google bundle function names may change, but the feature pattern is more stable.

## Development

```bash
# Run all unit tests
python -m pytest tests/

# Run common focused tests
python -m pytest tests/unit/test_model_capabilities.py tests/unit/test_static_frontend_capabilities.py

# Extract snapshot for debugging
python main.py snapshot "test prompt"
```

Project code lives in `src/aistudio_api/`, the static WebUI lives in `src/aistudio_api/static/`, and runtime data defaults to `data/`.

## Acknowledgements

- https://github.com/LuanRT/BgUtils
- https://github.com/iBUHub/AIStudioToAPI
- https://linux.do

## License

MIT
