"""Generated image management routes."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from aistudio_api.config import settings
from aistudio_api.infrastructure.generated_images import GeneratedImageStore, normalize_generated_images_route


def _error_detail(message: str, error_type: str = "bad_request") -> dict[str, str]:
    return {"message": message, "type": error_type}


async def delete_generated_image(image_path: str):
    store = GeneratedImageStore()
    try:
        deleted = store.delete(image_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=_error_detail("generated image not found", "not_found"))
    return {"ok": True, "path": image_path}


def register_generated_image_routes(app: FastAPI) -> None:
    route = normalize_generated_images_route(settings.generated_images_route)
    app.add_api_route(
        f"{route}/{{image_path:path}}",
        delete_generated_image,
        methods=["DELETE"],
        name="delete_generated_image",
    )