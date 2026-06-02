"""Provider Manager control-plane routes."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from aistudio_api.infrastructure.local_studio import local_studio_models_path, normalize_interface_mode, normalize_openai_base_url, upstream_url
from aistudio_api.infrastructure.provider_manager import ProviderManagerStore, normalize_discovered_model_catalog


router = APIRouter(prefix="/api/provider-manager")


class ModelCatalogEntryRequest(BaseModel):
    external_model_id: str | None = None
    id: str | None = None
    model: str | None = None
    display_name: str | None = None
    name: str | None = None
    capabilities: dict[str, Any] | None = None
    modalities: list[str] | None = None
    aliases: list[str] | None = None
    defaults: dict[str, Any] | None = None
    context_window: int | None = None
    source: str | None = None
    metadata: dict[str, Any] | None = None


class ProviderCreateRequest(BaseModel):
    id: str | None = None
    name: str
    type: str | None = None
    provider_type: str | None = None
    enabled: bool = True
    base_url: str
    timeout: int = 120
    token: str | None = None
    credential_ref: str | None = None
    model_catalog: list[ModelCatalogEntryRequest] = Field(default_factory=list)
    aliases: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    health: dict[str, Any] | None = None


class ProviderUpdateRequest(BaseModel):
    name: str | None = None
    type: str | None = None
    provider_type: str | None = None
    enabled: bool | None = None
    base_url: str | None = None
    timeout: int | None = None
    token: str | None = None
    credential_ref: str | None = None
    model_catalog: list[ModelCatalogEntryRequest] | None = None
    aliases: dict[str, Any] | None = None
    defaults: dict[str, Any] | None = None
    health: dict[str, Any] | None = None


class ProviderEnabledRequest(BaseModel):
    enabled: bool


class ModelCatalogUpdateRequest(BaseModel):
    data: list[ModelCatalogEntryRequest] = Field(default_factory=list)


class ModelCatalogDiscoveryRequest(BaseModel):
    provider_id: str | None = None
    base_url: str | None = None
    timeout: int = 120
    token: str | None = None
    credential_ref: str | None = None
    interface_mode: str = "responses"


def _error_detail(message: str, error_type: str = "bad_request") -> dict[str, str]:
    return {"message": message, "type": error_type}


def _store() -> ProviderManagerStore:
    store = ProviderManagerStore()
    store.ensure_directory()
    return store


def _new_http_client(timeout: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _auth_headers(token: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _safe_error_message(message: str, *secrets: str | None) -> str:
    cleaned = str(message or "").strip()
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned[:500]


def _handle_store_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=_error_detail("provider not found", "not_found"))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=_error_detail(str(exc), "forbidden"))
    return HTTPException(status_code=400, detail=_error_detail(str(exc)))


def _record_failure(store: ProviderManagerStore, *, action: str, target_id: str | None, exc: Exception) -> None:
    try:
        store.record_failed_event(action=action, target_id=target_id or "unknown", exc=exc)
    except Exception:
        pass


@router.get("/health")
async def provider_manager_health() -> dict[str, Any]:
    return _store().status()


@router.get("/providers")
async def list_providers() -> dict[str, Any]:
    providers = _store().list_providers()
    return {"object": "list", "total": len(providers), "data": providers}


@router.post("/providers")
async def create_provider(req: ProviderCreateRequest) -> dict[str, Any]:
    store = _store()
    try:
        return store.create_provider(req.model_dump(exclude_none=True))
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _record_failure(store, action="provider.created", target_id=None, exc=exc)
        raise _handle_store_error(exc) from exc


@router.get("/providers/{provider_id}")
async def get_provider(provider_id: str) -> dict[str, Any]:
    try:
        return _store().get_provider(provider_id)
    except (FileNotFoundError, ValueError) as exc:
        raise _handle_store_error(exc) from exc


@router.patch("/providers/{provider_id}")
@router.put("/providers/{provider_id}")
async def update_provider(provider_id: str, req: ProviderUpdateRequest) -> dict[str, Any]:
    store = _store()
    try:
        return store.update_provider(provider_id, req.model_dump(exclude_unset=True, exclude_none=True))
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _record_failure(store, action="provider.updated", target_id=provider_id, exc=exc)
        raise _handle_store_error(exc) from exc


@router.post("/providers/{provider_id}/enabled")
async def set_provider_enabled(provider_id: str, req: ProviderEnabledRequest) -> dict[str, Any]:
    store = _store()
    try:
        return store.set_enabled(provider_id, req.enabled)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _record_failure(store, action="provider.enabled" if req.enabled else "provider.disabled", target_id=provider_id, exc=exc)
        raise _handle_store_error(exc) from exc


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str) -> dict[str, Any]:
    store = _store()
    try:
        return store.delete_provider(provider_id)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _record_failure(store, action="provider.deleted", target_id=provider_id, exc=exc)
        raise _handle_store_error(exc) from exc


@router.get("/model-catalog")
async def list_model_catalog(provider_id: str | None = None) -> dict[str, Any]:
    try:
        models = _store().list_model_catalog(provider_id=provider_id)
    except (FileNotFoundError, ValueError) as exc:
        raise _handle_store_error(exc) from exc
    return {"object": "list", "total": len(models), "data": models}


@router.put("/providers/{provider_id}/model-catalog")
async def update_model_catalog(provider_id: str, req: ModelCatalogUpdateRequest) -> dict[str, Any]:
    store = _store()
    try:
        return store.update_model_catalog(provider_id, [item.model_dump(exclude_none=True) for item in req.data])
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        _record_failure(store, action="provider.model_catalog.updated", target_id=provider_id, exc=exc)
        raise _handle_store_error(exc) from exc


@router.post("/model-catalog/discover")
async def discover_model_catalog(req: ModelCatalogDiscoveryRequest) -> dict[str, Any]:
    store = _store()
    provider_id = req.provider_id or "provider_preview"
    token = str(req.token or "").strip()
    try:
        mode = normalize_interface_mode(req.interface_mode)
        base_url = req.base_url
        if req.provider_id:
            provider = store.get_provider(req.provider_id)
            base_url = base_url or provider.get("base_url")
            if not token:
                token = store.get_provider_token(req.provider_id, credential_ref=req.credential_ref)
        resolved_base_url = normalize_openai_base_url(str(base_url or ""))
        timeout = max(1, min(int(req.timeout or 120), 600))
        url = upstream_url(resolved_base_url, local_studio_models_path(mode))
        async with _new_http_client(timeout) as client:
            response = await client.get(url, headers=_auth_headers(token))
            response.raise_for_status()
        data = response.json()
        raw_models = data.get("models") if mode == "gemini" and isinstance(data, dict) else data.get("data") if isinstance(data, dict) else []
        model_list = raw_models if isinstance(raw_models, list) else []
        models = normalize_discovered_model_catalog(model_list, provider_id=provider_id, mode=mode)
        store.record_model_discovery(provider_id=provider_id, model_count=len(models), status="success")
        return {"object": "list", "total": len(models), "data": models, "interface_mode": mode}
    except httpx.HTTPStatusError as exc:
        message = _safe_error_message(exc.response.text or exc.response.reason_phrase or str(exc), token)
        store.record_model_discovery(provider_id=provider_id, model_count=0, status="failed", error=message)
        status_code = exc.response.status_code if 400 <= exc.response.status_code < 500 else 502
        raise HTTPException(status_code=status_code, detail=_error_detail(f"HTTP {exc.response.status_code}: {message}", "upstream_error")) from exc
    except httpx.TimeoutException as exc:
        store.record_model_discovery(provider_id=provider_id, model_count=0, status="failed", error="Model discovery request timed out")
        raise HTTPException(status_code=504, detail=_error_detail("Model discovery request timed out", "upstream_timeout")) from exc
    except httpx.HTTPError as exc:
        message = _safe_error_message(str(exc), token)
        store.record_model_discovery(provider_id=provider_id, model_count=0, status="failed", error=message)
        raise HTTPException(status_code=502, detail=_error_detail(message, "upstream_error")) from exc
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        store.record_model_discovery(provider_id=provider_id, model_count=0, status="failed", error=str(exc))
        raise _handle_store_error(exc) from exc


@router.get("/audit")
async def list_audit_events(limit: int = 100) -> dict[str, Any]:
    events = _store().list_audit_events(limit=limit)
    return {"object": "list", "total": len(events), "data": events}