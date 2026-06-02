"""Provider Manager control-plane routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from aistudio_api.infrastructure.provider_manager import ProviderManagerStore


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


def _error_detail(message: str, error_type: str = "bad_request") -> dict[str, str]:
    return {"message": message, "type": error_type}


def _store() -> ProviderManagerStore:
    store = ProviderManagerStore()
    store.ensure_directory()
    return store


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


@router.get("/audit")
async def list_audit_events(limit: int = 100) -> dict[str, Any]:
    events = _store().list_audit_events(limit=limit)
    return {"object": "list", "total": len(events), "data": events}