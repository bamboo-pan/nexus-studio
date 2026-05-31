"""OpenAI-compatible local studio routes."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.responses import FileResponse

from aistudio_api.infrastructure.local_studio import (
    LOCAL_STUDIO_PROVIDER_GOOGLE,
    LocalStudioStore,
    build_local_studio_chat_payload,
    filter_chat_models,
    filter_image_models,
    local_studio_chat_path,
    local_studio_models_path,
    resolve_local_studio_provider_settings,
    normalize_interface_mode,
    parse_local_studio_output,
    parse_local_studio_stream_event,
    upstream_url,
)

from .state import runtime_state


router = APIRouter(prefix="/api/local-studio")


def _error_detail(message: str, error_type: str = "bad_request") -> dict[str, str]:
    return {"message": message, "type": error_type}


def _internal_base_url(request: Request | None, mode: str) -> str | None:
    if request is None:
        return None
    version = "v1beta" if normalize_interface_mode(mode) == "gemini" else "v1"
    return str(request.base_url).rstrip("/") + f"/{version}"


def _settings_from_payload(payload: dict[str, Any], request: Request | None = None) -> tuple[str, str, str, int]:
    try:
        mode = str(payload.get("interface_mode") or payload.get("mode") or "responses")
        provider_type, base_url, token = resolve_local_studio_provider_settings(
            provider_type=str(payload.get("provider_type") or payload.get("providerType") or payload.get("provider_kind") or payload.get("providerKind") or ""),
            base_url=str(payload.get("base_url") or payload.get("apiUrl") or ""),
            token=str(payload.get("api_key") or payload.get("token") or ""),
            mode=mode,
            internal_base_url=_internal_base_url(request, mode),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc), "invalid_request_error")) from exc
    default_timeout = 300 if provider_type == LOCAL_STUDIO_PROVIDER_GOOGLE else 120
    timeout = int(payload.get("timeout") or default_timeout)
    if provider_type == LOCAL_STUDIO_PROVIDER_GOOGLE:
        timeout = max(timeout, default_timeout)
    timeout = max(1, min(timeout, 600))
    return provider_type, base_url, token, timeout


def _mode_from_payload(payload: dict[str, Any], *, default: str = "responses") -> str:
    try:
        return normalize_interface_mode(str(payload.get("interface_mode") or payload.get("mode") or default), default=default)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc), "invalid_request_error")) from exc


def _options_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    options = dict(payload.get("options") or {}) if isinstance(payload.get("options"), dict) else {}
    if "stream" in payload and "stream" not in options:
        options["stream"] = bool(payload.get("stream"))
    return options


def _auth_headers(token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _new_http_client(timeout: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _json_body(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _redacted_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        redacted[key] = "Bearer ***" if key.lower() == "authorization" and value else value
    return redacted


def _record_upstream_request(*, kind: str, model: str, method: str, url: str, headers: dict[str, str], body: Any) -> dict[str, Any] | None:
    store = runtime_state.request_log_store
    if store is None:
        return None
    try:
        return store.save(
            kind=kind,
            model=model,
            method=method,
            url=url,
            headers=_redacted_headers(headers),
            captured_headers=_redacted_headers(headers),
            body=_json_body(body),
            transport="local_studio_http",
            direction="outbound",
            phase="upstream_request",
        )
    except Exception:
        return None


def _record_upstream_response(
    *,
    entry: dict[str, Any] | None,
    kind: str,
    model: str,
    method: str,
    url: str,
    headers: dict[str, str] | None,
    status_code: int,
    response_headers: dict[str, str] | None,
    response_body: bytes | str,
    elapsed_ms: float,
) -> None:
    store = runtime_state.request_log_store
    if store is None:
        return
    chain_id = entry.get("chain_id") if isinstance(entry, dict) else None
    try:
        if isinstance(entry, dict) and entry.get("id"):
            store.attach_response(
                str(entry["id"]),
                status_code=status_code,
                response_headers=response_headers,
                response_body=response_body,
                elapsed_ms=elapsed_ms,
            )
        store.save(
            kind=kind,
            model=model,
            method=method,
            url=url,
            headers=_redacted_headers(headers or {}),
            body="",
            transport="local_studio_http",
            chain_id=chain_id,
            direction="inbound",
            phase="upstream_response",
            status_code=status_code,
            response_headers=response_headers,
            response_body=response_body,
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        return


def _response_text(response: httpx.Response, body: bytes | None = None) -> str:
    if body is not None:
        return body.decode(response.encoding or "utf-8", errors="replace")
    try:
        return response.text
    except httpx.ResponseNotRead:
        return ""


def _response_json(response: httpx.Response, body: bytes | None = None) -> Any:
    try:
        if body is not None:
            return json.loads(_response_text(response, body)) if body else None
        return response.json()
    except (ValueError, httpx.ResponseNotRead):
        return None


async def _read_response_body(response: httpx.Response) -> bytes:
    try:
        return response.content
    except httpx.ResponseNotRead:
        try:
            return await response.aread()
        except httpx.HTTPError:
            return b""


def _upstream_error_from_response(response: httpx.Response, body: bytes | None = None) -> HTTPException:
    message = _response_text(response, body) or response.reason_phrase or f"HTTP {response.status_code}"
    data = _response_json(response, body)
    try:
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or message)
            elif data.get("detail"):
                detail = data["detail"]
                message = str(detail.get("message") if isinstance(detail, dict) else detail)
    except (TypeError, ValueError):
        pass
    status = response.status_code if 400 <= response.status_code < 500 else 502
    return HTTPException(status_code=status, detail=_error_detail(f"HTTP {response.status_code}: {message}", "upstream_error"))


def _upstream_error(exc: httpx.HTTPStatusError, body: bytes | None = None) -> HTTPException:
    return _upstream_error_from_response(exc.response, body)


@router.get("/health")
async def health() -> dict[str, Any]:
    store = LocalStudioStore()
    store.ensure_directory()
    return {"ok": True, "storage": str(store.root)}


@router.post("/models")
async def list_models(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    provider_type, base_url, token, timeout = _settings_from_payload(payload, request)
    mode = _mode_from_payload(payload)
    url = upstream_url(base_url, local_studio_models_path(mode))
    headers = _auth_headers(token)
    entry = _record_upstream_request(kind=f"local_studio_models_{mode}", model="", method="GET", url=url, headers=headers, body={"interface_mode": mode})
    started = time.perf_counter()
    try:
        async with _new_http_client(timeout) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _record_upstream_response(entry=entry, kind=f"local_studio_models_{mode}", model="", method="GET", url=url, headers=headers, status_code=exc.response.status_code, response_headers=dict(exc.response.headers), response_body=exc.response.content, elapsed_ms=(time.perf_counter() - started) * 1000)
        raise _upstream_error(exc) from exc
    except httpx.TimeoutException as exc:
        _record_upstream_response(entry=entry, kind=f"local_studio_models_{mode}", model="", method="GET", url=url, headers=headers, status_code=504, response_headers={}, response_body=f"Model list request timed out after {timeout}s", elapsed_ms=(time.perf_counter() - started) * 1000)
        raise HTTPException(status_code=504, detail=_error_detail("Model list request timed out", "upstream_timeout")) from exc
    except httpx.HTTPError as exc:
        _record_upstream_response(entry=entry, kind=f"local_studio_models_{mode}", model="", method="GET", url=url, headers=headers, status_code=502, response_headers={}, response_body=str(exc), elapsed_ms=(time.perf_counter() - started) * 1000)
        raise HTTPException(status_code=502, detail=_error_detail(str(exc), "upstream_error")) from exc

    _record_upstream_response(entry=entry, kind=f"local_studio_models_{mode}", model="", method="GET", url=url, headers=headers, status_code=response.status_code, response_headers=dict(response.headers), response_body=response.content, elapsed_ms=(time.perf_counter() - started) * 1000)
    data = response.json()
    models = data.get("models") if mode == "gemini" and isinstance(data, dict) else data.get("data") if isinstance(data, dict) else []
    model_list = models if isinstance(models, list) else []
    return {
        "object": "list",
        "data": filter_chat_models(model_list, mode=mode),
        "image_models": filter_image_models(model_list, mode=mode, provider_type=provider_type),
        "interface_mode": mode,
    }


@router.get("/conversations")
async def list_conversations() -> dict[str, Any]:
    return {"data": LocalStudioStore().list()}


@router.post("/conversations")
async def create_conversation(payload: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
    try:
        return LocalStudioStore().create(payload or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    try:
        return LocalStudioStore().get(conversation_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_detail("conversation not found", "not_found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return LocalStudioStore().patch(conversation_id, payload)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_detail("conversation not found", "not_found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, Any]:
    store = LocalStudioStore()
    try:
        deleted = store.delete(conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=_error_detail("conversation not found", "not_found"))
    return {"ok": True, "id": conversation_id}


@router.post("/conversations/bulk-delete")
async def bulk_delete_conversations(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ids = payload.get("ids") if isinstance(payload, dict) else []
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail=_error_detail("ids must be a list"))
    try:
        return LocalStudioStore().bulk_delete(ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.get("/assets/{asset_path:path}")
async def get_asset(asset_path: str) -> FileResponse:
    store = LocalStudioStore()
    try:
        path = store.resolve_asset_path(asset_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_detail("asset not found", "not_found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc
    return FileResponse(path)


@router.post("/chat")
async def chat(request: Request, payload: dict[str, Any] = Body(...)):
    store = LocalStudioStore()
    provider_type, base_url, token, timeout = _settings_from_payload(payload, request)
    mode = _mode_from_payload(payload)
    options = _options_from_payload(payload)
    model = str(payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail=_error_detail("model is required", "invalid_request_error"))

    conversation_id = str(payload.get("conversation_id") or "").strip()
    try:
        conversation = store.get(conversation_id) if conversation_id else store.create({"model": model, "interface_mode": mode, "settings": options})
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_detail("conversation not found", "not_found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc

    rerun_index = payload.get("rerun_from")
    if rerun_index is not None:
        try:
            store.truncate_for_rerun(conversation, int(rerun_index))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc
    else:
        content = str(payload.get("message") or "")
        files = payload.get("files") if isinstance(payload.get("files"), list) else []
        if not content.strip() and not files:
            raise HTTPException(status_code=400, detail=_error_detail("message or files are required", "invalid_request_error"))
        store.add_user_message(conversation, content, files)

    conversation["model"] = model
    conversation["interface_mode"] = mode
    conversation["settings"] = dict(options)

    try:
        request_body = build_local_studio_chat_payload(
            mode=mode,
            model=model,
            messages=conversation.get("messages", []),
            options=options,
            asset_resolver=store.asset_to_data_url,
            provider_type=provider_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc), "invalid_request_error")) from exc

    if options.get("stream"):
        return StreamingResponse(
            _stream_local_studio_chat(
                store=store,
                conversation=conversation,
                base_url=base_url,
                token=token,
                timeout=timeout,
                mode=mode,
                model=model,
                request_body=request_body,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return await _complete_local_studio_chat(
        store=store,
        conversation=conversation,
        base_url=base_url,
        token=token,
        timeout=timeout,
        mode=mode,
        model=model,
        options=options,
        request_body=request_body,
    )


async def _complete_local_studio_chat(
    *,
    store: LocalStudioStore,
    conversation: dict[str, Any],
    base_url: str,
    token: str,
    timeout: int,
    mode: str,
    model: str,
    options: dict[str, Any],
    request_body: dict[str, Any],
) -> dict[str, Any]:
    path = local_studio_chat_path(mode, model, stream=False)
    url = upstream_url(base_url, path)
    headers = _auth_headers(token)
    kind = f"local_studio_{mode}"
    entry = _record_upstream_request(kind=kind, model=model, method="POST", url=url, headers=headers, body=request_body)

    started = time.perf_counter()
    response_data: dict[str, Any] = {}
    try:
        async with _new_http_client(timeout) as client:
            response = await client.post(url, headers=headers, json=request_body)
            response.raise_for_status()
            _record_upstream_response(entry=entry, kind=kind, model=model, method="POST", url=url, headers=headers, status_code=response.status_code, response_headers=dict(response.headers), response_body=response.content, elapsed_ms=(time.perf_counter() - started) * 1000)
            raw_response_data = response.json()
            response_data = raw_response_data if isinstance(raw_response_data, dict) else {}
    except httpx.HTTPStatusError as exc:
        _record_upstream_response(entry=entry, kind=kind, model=model, method="POST", url=url, headers=headers, status_code=exc.response.status_code, response_headers=dict(exc.response.headers), response_body=exc.response.content, elapsed_ms=(time.perf_counter() - started) * 1000)
        error = _upstream_error(exc)
        store.add_assistant_message(conversation, error=error.detail["message"] if isinstance(error.detail, dict) else str(error.detail))
        store.save(conversation)
        raise error
    except httpx.TimeoutException as exc:
        message = f"HTTP 504: upstream request timed out after {timeout}s"
        _record_upstream_response(entry=entry, kind=kind, model=model, method="POST", url=url, headers=headers, status_code=504, response_headers={}, response_body=message, elapsed_ms=(time.perf_counter() - started) * 1000)
        store.add_assistant_message(conversation, error=message)
        store.save(conversation)
        raise HTTPException(status_code=504, detail=_error_detail(message, "upstream_timeout")) from exc
    except httpx.HTTPError as exc:
        message = str(exc)
        _record_upstream_response(entry=entry, kind=kind, model=model, method="POST", url=url, headers=headers, status_code=502, response_headers={}, response_body=message, elapsed_ms=(time.perf_counter() - started) * 1000)
        store.add_assistant_message(conversation, error=message)
        store.save(conversation)
        raise HTTPException(status_code=502, detail=_error_detail(message, "upstream_error")) from exc

    data = response_data
    parsed = parse_local_studio_output(mode, data if isinstance(data, dict) else {})
    images = store.save_response_images(parsed["image_candidates"])
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    content = parsed["content"] or ("Generated image" if images else "")
    store.add_assistant_message(conversation, content=content or "(no response content)", thinking=parsed["thinking"], usage=parsed["usage"], images=images)
    saved = store.save(conversation)
    result = {"conversation": saved, "request": request_body, "elapsed_ms": elapsed_ms, "upstream_id": data.get("id") if isinstance(data, dict) else None, "interface_mode": mode}
    return result


async def _stream_local_studio_chat(
    *,
    store: LocalStudioStore,
    conversation: dict[str, Any],
    base_url: str,
    token: str,
    timeout: int,
    mode: str,
    model: str,
    request_body: dict[str, Any],
):
    path = local_studio_chat_path(mode, model, stream=True)
    url = upstream_url(base_url, path)
    headers = _auth_headers(token)
    kind = f"local_studio_{mode}_stream"
    entry = _record_upstream_request(kind=kind, model=model, method="POST", url=url, headers=headers, body=request_body)
    started = time.perf_counter()
    response_chunks: list[bytes] = []
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    image_candidates: list[dict[str, Any]] = []
    partial_image_candidates: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    error_message = ""
    status_code = 0
    response_headers: dict[str, str] = {}

    def append_completed_thinking(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        if any(text == part or text in part for part in thinking_parts):
            return
        thinking_parts.append(text)

    try:
        async with _new_http_client(timeout) as client:
            async with client.stream("POST", url, headers=headers, json=request_body) as response:
                status_code = response.status_code
                response_headers = dict(response.headers)
                if status_code >= 400:
                    body = await response.aread()
                    if body:
                        response_chunks.append(body)
                    error = _upstream_error_from_response(response, body)
                    error_message = error.detail["message"] if isinstance(error.detail, dict) else str(error.detail)
                    yield f"data: {json.dumps({'type':'error','error':{'message':error_message}}, ensure_ascii=False)}\n\n".encode("utf-8")
                    return
                response.raise_for_status()
                async for line in response.aiter_lines():
                    raw = (line + "\n").encode("utf-8")
                    response_chunks.append(raw)
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_text = line[6:].strip()
                        if data_text == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_text)
                        except json.JSONDecodeError:
                            continue
                        event_type = str(event.get("type") or "") if isinstance(event, dict) else ""
                        parsed = parse_local_studio_stream_event(mode, event if isinstance(event, dict) else {})
                        is_completed_event = event_type == "response.completed"
                        had_content_parts = bool(content_parts)
                        had_thinking_parts = bool(thinking_parts)
                        if parsed.get("error"):
                            error_message = str(parsed["error"])
                        if parsed.get("content") and (not is_completed_event or not content_parts):
                            content_parts.append(str(parsed["content"]))
                        if parsed.get("thinking"):
                            if is_completed_event:
                                append_completed_thinking(parsed["thinking"])
                            else:
                                thinking_parts.append(str(parsed["thinking"]))
                        if isinstance(parsed.get("image_candidates"), list):
                            parsed_candidates = [candidate for candidate in parsed["image_candidates"] if isinstance(candidate, dict)]
                            if event_type == "response.image_generation_call.partial_image":
                                partial_image_candidates.extend(parsed_candidates)
                            else:
                                image_candidates.extend(parsed_candidates)
                        if isinstance(parsed.get("usage"), dict):
                            usage = dict(parsed["usage"])
                        delta_content = "" if is_completed_event and had_content_parts else parsed.get("content") or ""
                        delta_thinking = "" if is_completed_event and had_thinking_parts else parsed.get("thinking") or ""
                        delta = {"type": "local_studio.delta", "content": delta_content, "thinking": delta_thinking, "usage": parsed.get("usage") or None, "error": parsed.get("error") or ""}
                        if delta["content"] or delta["thinking"] or delta["usage"] or delta["error"]:
                            yield f"data: {json.dumps(delta, ensure_ascii=False)}\n\n".encode("utf-8")
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        response_headers = dict(exc.response.headers)
        body = await _read_response_body(exc.response)
        if body:
            response_chunks.append(body)
        error = _upstream_error(exc, body)
        error_message = error.detail["message"] if isinstance(error.detail, dict) else str(error.detail)
        yield f"data: {json.dumps({'type':'error','error':{'message':error_message}}, ensure_ascii=False)}\n\n".encode("utf-8")
    except httpx.TimeoutException:
        status_code = 504
        error_message = f"HTTP 504: upstream request timed out after {timeout}s"
        response_chunks.append(error_message.encode("utf-8"))
        yield f"data: {json.dumps({'type':'error','error':{'message':error_message}}, ensure_ascii=False)}\n\n".encode("utf-8")
    except httpx.HTTPError as exc:
        status_code = 502
        error_message = str(exc)
        response_chunks.append(error_message.encode("utf-8"))
        yield f"data: {json.dumps({'type':'error','error':{'message':error_message}}, ensure_ascii=False)}\n\n".encode("utf-8")
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        response_body = b"".join(response_chunks)
        _record_upstream_response(entry=entry, kind=kind, model=model, method="POST", url=url, headers=headers, status_code=status_code or 200, response_headers=response_headers, response_body=response_body, elapsed_ms=elapsed_ms)
        images: list[dict[str, Any]] = []
        save_candidates = image_candidates or partial_image_candidates
        if not error_message or save_candidates:
            images = store.save_response_images(save_candidates)
        content = "".join(content_parts).strip()
        thinking = "\n".join(part for part in thinking_parts if part).strip()
        if error_message and (content or thinking or images or usage):
            partial_note = f"Partial response saved before upstream stream ended: {error_message}"
            store.add_assistant_message(conversation, content=content or ("Generated image" if images else ""), thinking=thinking, usage=usage, images=images, error=partial_note)
        elif error_message:
            store.add_assistant_message(conversation, error=error_message)
        else:
            store.add_assistant_message(conversation, content=content or ("Generated image" if images else "(no response content)"), thinking=thinking, usage=usage, images=images)
        saved = store.save(conversation)
        done = {"type": "local_studio.completed", "conversation": saved, "elapsed_ms": elapsed_ms, "interface_mode": mode}
        yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8")
