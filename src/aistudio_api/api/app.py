"""FastAPI application entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from aistudio_api.infrastructure.generated_images import GeneratedImageStore
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.request_logs import RequestLogStore, new_request_chain_id, reset_request_chain_id, set_request_chain_id

from .routes_accounts import router as accounts_router
from .routes_gemini import router as gemini_router
from .routes_generated_images import register_generated_image_routes
from .routes_image_sessions import router as image_sessions_router
from .routes_local_studio import router as local_studio_router
from .routes_openai import router as openai_router
from .routes_provider_manager import router as provider_manager_router
from .routes_request_logs import router as request_logs_router
from .routes_system import router as system_router
from .state import runtime_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("aistudio.server")

_WARMUP_RETRY_ATTEMPTS = 3
_WARMUP_RETRY_BACKOFF_SECONDS = (2.0, 5.0)


def _is_validation_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if "ValidationError" in type(current).__name__:
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_transient_warmup_error(exc: BaseException) -> bool:
    if isinstance(exc, (ValueError, TypeError)) or _is_validation_error(exc):
        return False

    message = str(exc).lower()
    hard_markers = (
        "google sign-in",
        "sign in to",
        "not signed in",
        "login required",
        "auth state",
        "missing auth",
        "invalid account",
        "invalid auth",
        "permission denied",
        "unauthorized",
        "forbidden",
        "validation error",
    )
    if any(marker in message for marker in hard_markers):
        return False

    exc_type = type(exc)
    if exc_type.__module__.startswith("playwright") and "timeouterror" in exc_type.__name__.lower():
        return True

    readiness_markers = (
        "ai studio chat runtime not ready",
        "ai studio image runtime not ready",
    )
    if any(marker in message for marker in readiness_markers):
        return True

    timeout_markers = (
        "page.goto: timeout",
        "navigation timeout",
        "template capture timeout",
        "botguardservice capture timeout",
        "waiting until \"commit\"",
        "timeout 60000ms exceeded",
        "aistudio.google.com/",
    )
    return "timeout" in message and any(marker in message for marker in timeout_markers)


def _warmup_retry_delay(attempt: int, backoff_seconds: tuple[float, ...]) -> float:
    if not backoff_seconds:
        return 0.0
    return backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]


async def _warmup_with_retries(
    warmup: Callable[[], Awaitable[None]],
    *,
    label: str,
    attempts: int = _WARMUP_RETRY_ATTEMPTS,
    backoff_seconds: tuple[float, ...] = _WARMUP_RETRY_BACKOFF_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    last_attempt = max(1, attempts)
    for attempt in range(1, last_attempt + 1):
        try:
            await warmup()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt >= last_attempt or not _is_transient_warmup_error(exc):
                raise
            delay = _warmup_retry_delay(attempt, backoff_seconds)
            logger.warning(
                "%s warmup hit transient AI Studio navigation timeout on attempt %d/%d; retrying in %.1fs: %s",
                label,
                attempt,
                last_attempt,
                delay,
                exc,
            )
            await sleep(delay)


def _should_start_background_warmup(*, use_pure_http: bool, account_count: int) -> bool:
    return not use_pure_http and account_count == 0


def _should_start_account_pool_warmup(*, use_pure_http: bool, account_count: int, warmup_limit: int) -> bool:
    return not use_pure_http and account_count > 0 and warmup_limit > 0


def _account_pool_warmup_account_ids(
    accounts: list[Any],
    *,
    active_account_id: str | None,
    rotation_mode: str,
    warmup_limit: int,
) -> list[str]:
    if warmup_limit <= 0:
        return []

    available = [account for account in accounts if not getattr(account, "is_isolated", False)]
    if not available:
        return []

    candidates: list[str] = []

    def add(account: Any | None) -> None:
        if account is None or len(candidates) >= warmup_limit:
            return
        account_id = str(getattr(account, "id", "") or "")
        if account_id and account_id not in candidates:
            candidates.append(account_id)

    if active_account_id:
        add(next((account for account in available if account.id == active_account_id), None))

    add(available[0])
    add(next((account for account in available if getattr(account, "is_premium", False)), None))
    return candidates


@asynccontextmanager
async def lifespan(app: FastAPI):
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.account.account_store import AccountStore
    from aistudio_api.infrastructure.account.login_service import LoginService
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.application.account_rotator import init_rotator, RotationMode
    from aistudio_api.application.account_client_pool import AccountClientPool

    request_log_store = RequestLogStore()
    request_log_store.ensure_directory()
    runtime_state.request_log_store = request_log_store

    client = AIStudioClient(
        port=runtime_state.camoufox_port,
        use_pure_http=settings.use_pure_http,
        request_log_store=request_log_store,
    )
    runtime_state.client = client
    from aistudio_api.config import settings as app_settings
    runtime_state.busy_lock = asyncio.Semaphore(app_settings.max_concurrency)

    # 注入 snapshot 缓存引用，切号时需要清除
    from aistudio_api.infrastructure.gateway.client import _snapshot_cache
    runtime_state.snapshot_cache = _snapshot_cache

    # 初始化账号管理服务
    account_store = AccountStore()
    accounts = account_store.list_accounts()
    login_service = LoginService(port=settings.login_camoufox_port)
    account_service = AccountService(account_store, login_service)
    runtime_state.account_service = account_service

    active_account = account_store.get_active_account()
    active_auth_path = account_store.get_active_auth_path()
    if active_auth_path is not None:
        await client.switch_auth(str(active_auth_path))
        if active_auth_path.exists():
            logger.info("Loaded active account auth state: %s", active_auth_path)
        else:
            logger.warning("Active account auth state file is missing: %s", active_auth_path)

    # 初始化账号轮询器
    rotation_mode = getattr(settings, "account_rotation_mode", "round_robin")
    cooldown = getattr(settings, "account_cooldown_seconds", 60)
    rotator = init_rotator(
        account_store,
        mode=RotationMode(rotation_mode),
        cooldown_seconds=cooldown,
    )
    runtime_state.rotator = rotator
    runtime_state.account_client_pool = AccountClientPool(
        account_store,
        port=runtime_state.camoufox_port,
        use_pure_http=settings.use_pure_http,
        request_log_store=request_log_store,
    )

    logger.info(
        "Client initialized (camoufox port=%s, rotation=%s, accounts=%d)",
        runtime_state.camoufox_port,
        rotator.mode,
        len(accounts),
    )

    runtime_state.warmup_status = "idle"
    runtime_state.warmup_target_accounts = []
    runtime_state.warmup_completed_accounts = []
    runtime_state.warmup_failed_accounts = []

    # 后台预热浏览器，避免首次请求延迟
    warmup_task = None
    if _should_start_background_warmup(use_pure_http=settings.use_pure_http, account_count=len(accounts)):
        async def _warmup():
            runtime_state.warmup_status = "running"
            runtime_state.warmup_target_accounts = ["default"]
            runtime_state.warmup_completed_accounts = []
            runtime_state.warmup_failed_accounts = []
            try:
                await _warmup_with_retries(client.warmup, label="Default browser")
                runtime_state.warmup_completed_accounts = ["default"]
                runtime_state.warmup_status = "complete"
            except Exception as e:
                runtime_state.warmup_failed_accounts = ["default"]
                runtime_state.warmup_status = "failed"
                logger.warning("浏览器预热失败: %s", e)
        warmup_task = asyncio.create_task(_warmup())
    elif _should_start_account_pool_warmup(
        use_pure_http=settings.use_pure_http,
        account_count=len(accounts),
        warmup_limit=settings.account_warmup_limit,
    ):
        account_warmup_ids = _account_pool_warmup_account_ids(
            accounts,
            active_account_id=active_account.id if active_account is not None else None,
            rotation_mode=rotation_mode,
            warmup_limit=settings.account_warmup_limit,
        )

        async def _warmup_account_pool():
            pool = runtime_state.account_client_pool
            if pool is None:
                return
            for account_id in account_warmup_ids:
                try:
                    account_client = await pool.get_client(account_id)
                    if account_client is None:
                        runtime_state.warmup_failed_accounts = [*runtime_state.warmup_failed_accounts, account_id]
                        continue
                    await _warmup_with_retries(account_client.warmup, label=f"Account browser {account_id}")
                    runtime_state.warmup_completed_accounts = [*runtime_state.warmup_completed_accounts, account_id]
                    logger.info("Account browser warmup completed: account=%s", account_id)
                except asyncio.CancelledError:
                    runtime_state.warmup_status = "cancelled"
                    raise
                except Exception as e:
                    runtime_state.warmup_failed_accounts = [*runtime_state.warmup_failed_accounts, account_id]
                    logger.warning("Account browser warmup failed for account=%s: %s", account_id, e)
            if runtime_state.warmup_failed_accounts:
                runtime_state.warmup_status = "partial" if runtime_state.warmup_completed_accounts else "failed"
            else:
                runtime_state.warmup_status = "complete"

        if account_warmup_ids:
            runtime_state.warmup_status = "running"
            runtime_state.warmup_target_accounts = list(account_warmup_ids)
            runtime_state.warmup_completed_accounts = []
            runtime_state.warmup_failed_accounts = []
            logger.info("Starting account browser warmup: accounts=%s limit=%d", account_warmup_ids, settings.account_warmup_limit)
            warmup_task = asyncio.create_task(_warmup_account_pool())

    yield
    logger.info("Shutting down")
    if warmup_task and not warmup_task.done():
        warmup_task.cancel()
    if runtime_state.account_client_pool is not None:
        await runtime_state.account_client_pool.close()
    await client.close()
    runtime_state.client = None
    runtime_state.busy_lock = None
    runtime_state.account_service = None
    runtime_state.rotator = None
    runtime_state.account_client_pool = None
    runtime_state.request_log_store = None


app = FastAPI(title="Nexus Studio", lifespan=lifespan)


def _should_log_api_exchange(request: Request) -> bool:
    path = request.url.path
    return path == "/v1" or path.startswith("/v1/") or path.startswith("/v1beta/") or path in {"/api/local-studio/models", "/api/local-studio/chat"}


def _redact_logged_request_body(path: str, body: bytes) -> bytes:
    if not path.startswith("/api/local-studio/"):
        return body
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    redacted = dict(payload)
    for key in ("api_key", "apiKey", "token"):
        if redacted.get(key):
            redacted[key] = "***"
    return json.dumps(redacted, ensure_ascii=False).encode("utf-8")


def _request_model_from_body(request: Request, body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and payload.get("model"):
        return str(payload["model"])

    path = request.url.path
    marker = "/v1beta/models/"
    if path.startswith(marker):
        return path[len(marker) :].split(":", 1)[0]
    return ""


def _chunk_to_bytes(chunk) -> bytes:
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, bytearray):
        return bytes(chunk)
    if isinstance(chunk, memoryview):
        return chunk.tobytes()
    return str(chunk).encode("utf-8")


@app.middleware("http")
async def request_log_exchange_middleware(request: Request, call_next):
    store = runtime_state.request_log_store
    if store is None or not _should_log_api_exchange(request) or not store.is_enabled():
        return await call_next(request)

    request_body = await request.body()
    logged_request_body = _redact_logged_request_body(request.url.path, request_body)
    request_sent = False

    async def receive_logged_body():
        nonlocal request_sent
        if request_sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        request_sent = True
        return {"type": "http.request", "body": request_body, "more_body": False}

    logged_request = Request(request.scope, receive_logged_body)
    chain_id = new_request_chain_id()
    model = _request_model_from_body(request, request_body)
    started = time.perf_counter()
    token = set_request_chain_id(chain_id)
    try:
        try:
            store.save(
                kind="client_request",
                model=model,
                method=request.method,
                url=str(request.url),
                headers=request.headers,
                body=logged_request_body,
                transport="http",
                chain_id=chain_id,
                direction="inbound",
                phase="client_request",
            )
        except Exception as exc:
            logger.warning("Request log client request write failed: %s", exc)
        response = await call_next(logged_request)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            store.save(
                kind="client_response",
                model=model,
                method=request.method,
                url=str(request.url),
                headers={},
                body="",
                transport="http",
                chain_id=chain_id,
                direction="outbound",
                phase="client_response",
                status_code=500,
                response_body=str(exc),
                elapsed_ms=elapsed_ms,
            )
        except Exception as log_exc:
            logger.warning("Request log failed client response write failed: %s", log_exc)
        reset_request_chain_id(token)
        raise
    reset_request_chain_id(token)

    response_chunks: list[bytes] = []
    response_headers = dict(response.headers)

    async def body_iterator():
        stream_token = set_request_chain_id(chain_id)
        try:
            async for chunk in response.body_iterator:
                chunk_bytes = _chunk_to_bytes(chunk)
                response_chunks.append(chunk_bytes)
                yield chunk
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            response_body = b"".join(response_chunks)
            try:
                store.save(
                    kind="client_response",
                    model=model,
                    method=request.method,
                    url=str(request.url),
                    headers=response_headers,
                    body="",
                    transport="http",
                    chain_id=chain_id,
                    direction="outbound",
                    phase="client_response",
                    status_code=response.status_code,
                    response_headers=response_headers,
                    response_body=response_body,
                    elapsed_ms=elapsed_ms,
                )
            except Exception as exc:
                logger.warning("Request log client response write failed: %s", exc)
            reset_request_chain_id(stream_token)

    return StreamingResponse(
        body_iterator(),
        status_code=response.status_code,
        headers=response_headers,
        media_type=response.media_type,
        background=response.background,
    )
app.include_router(system_router)
app.include_router(gemini_router)
app.include_router(openai_router)
app.include_router(accounts_router)
app.include_router(image_sessions_router)
app.include_router(local_studio_router)
app.include_router(provider_manager_router)
app.include_router(request_logs_router)
register_generated_image_routes(app)


def _is_openai_compat_path(request: Request) -> bool:
    path = request.url.path
    return path == "/v1" or path.startswith("/v1/")


def _detail_from_exception(status_code: int, detail) -> dict:
    if isinstance(detail, dict):
        return detail
    error_type = "bad_request"
    if status_code == 401:
        error_type = "authentication_error"
    elif status_code == 404:
        error_type = "not_found"
    elif status_code == 429:
        error_type = "rate_limit_exceeded"
    elif status_code == 503:
        error_type = "service_unavailable"
    elif status_code >= 500:
        error_type = "server_error"
    return {"message": str(detail), "type": error_type}


def _openai_error_content(detail: dict) -> dict:
    error_type = detail.get("type") or "invalid_request_error"
    if error_type in {"bad_request", "not_found", "unsupported_feature"}:
        error_type = "invalid_request_error"
    return {
        "error": {
            "message": detail.get("message", "Request failed"),
            "type": error_type,
            "param": detail.get("param"),
            "code": detail.get("code"),
        }
    }


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", []) if part != "body")
    message = first.get("msg", "Invalid request body")
    if location:
        message = f"{location}: {message}"
    detail = {"message": message, "type": "bad_request"}
    content = _openai_error_content(detail) if _is_openai_compat_path(request) else {"detail": detail}
    return JSONResponse(status_code=400, content=content)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = _detail_from_exception(exc.status_code, exc.detail)
    content = _openai_error_content(detail) if _is_openai_compat_path(request) else {"detail": detail}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

# 挂载静态文件
import os
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

generated_image_store = GeneratedImageStore()
generated_image_store.ensure_directory()
app.mount(
    generated_image_store.public_route,
    StaticFiles(directory=str(generated_image_store.root)),
    name="generated-images",
)


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


def main():
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio OpenAI-compatible API Server")
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--camoufox-port", type=int, default=settings.camoufox_port)
    args = parser.parse_args()

    runtime_state.camoufox_port = args.camoufox_port

    import uvicorn

    logger.info("Starting server on port %s", args.port)
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
