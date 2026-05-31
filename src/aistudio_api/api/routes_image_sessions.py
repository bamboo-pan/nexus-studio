"""Image generation session history routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from aistudio_api.infrastructure.image_sessions import ImageSessionStore


router = APIRouter(prefix="/image-sessions")


def _error_detail(message: str, error_type: str = "bad_request") -> dict[str, str]:
    return {"message": message, "type": error_type}


@router.get("")
async def list_image_sessions() -> dict[str, list[dict[str, Any]]]:
    return {"data": ImageSessionStore().list()}


@router.post("")
async def create_image_session(payload: Any = Body(...)) -> dict[str, Any]:
    try:
        return ImageSessionStore().save(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.get("/{session_id}")
async def get_image_session(session_id: str) -> dict[str, Any]:
    try:
        return ImageSessionStore().get(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_detail("image session not found", "not_found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.put("/{session_id}")
async def update_image_session(session_id: str, payload: Any = Body(...)) -> dict[str, Any]:
    try:
        return ImageSessionStore().save(payload, session_id=session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.delete("/{session_id}")
async def delete_image_session(session_id: str) -> dict[str, Any]:
    store = ImageSessionStore()
    try:
        deleted = store.delete(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=_error_detail("image session not found", "not_found"))
    return {"ok": True, "id": session_id}