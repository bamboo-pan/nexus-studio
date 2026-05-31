"""Request log management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.infrastructure.request_logs import RequestLogStore


router = APIRouter(prefix="/request-logs")


class RequestLogStatusRequest(BaseModel):
    enabled: bool


class RequestLogGroupsRequest(BaseModel):
    ids: list[str]


def _error_detail(message: str, error_type: str = "bad_request") -> dict[str, str]:
    return {"message": message, "type": error_type}


def _store(runtime_state) -> RequestLogStore:
    store = getattr(runtime_state, "request_log_store", None)
    if store is None:
        store = RequestLogStore()
        runtime_state.request_log_store = store
    return store


@router.get("/status")
async def get_request_log_status(runtime_state=Depends(get_runtime_state)) -> dict:
    return _store(runtime_state).status()


@router.put("/status")
async def set_request_log_status(
    req: RequestLogStatusRequest,
    runtime_state=Depends(get_runtime_state),
) -> dict:
    return _store(runtime_state).set_enabled(req.enabled)


@router.get("")
async def list_request_logs(limit: int = 200, runtime_state=Depends(get_runtime_state)) -> dict:
    normalized_limit = max(1, min(int(limit), 1000))
    store = _store(runtime_state)
    return {
        "enabled": store.is_enabled(),
        "total": store.group_count(),
        "entry_total": store.count(),
        "data": store.list_groups(limit=normalized_limit),
    }


@router.post("/export")
async def export_request_log_groups(req: RequestLogGroupsRequest, runtime_state=Depends(get_runtime_state)) -> dict:
    try:
        return _store(runtime_state).export_groups(req.ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.post("/groups/delete")
async def delete_request_log_groups(req: RequestLogGroupsRequest, runtime_state=Depends(get_runtime_state)) -> dict:
    try:
        return _store(runtime_state).delete_groups(req.ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.get("/groups/{chain_id}")
async def get_request_log_group(chain_id: str, runtime_state=Depends(get_runtime_state)) -> dict:
    try:
        return _store(runtime_state).get_group(chain_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_detail("request log group not found", "not_found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.delete("/groups/{chain_id}")
async def delete_request_log_group(chain_id: str, runtime_state=Depends(get_runtime_state)) -> dict:
    try:
        return _store(runtime_state).delete_group(chain_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_detail("request log group not found", "not_found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.get("/{request_id}")
async def get_request_log(request_id: str, runtime_state=Depends(get_runtime_state)) -> dict:
    try:
        return _store(runtime_state).get(request_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_detail("request log not found", "not_found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc