"""Generated image file persistence helpers."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from aistudio_api.config import settings


_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/avif": ".avif",
}


def normalize_generated_images_route(value: str | None) -> str:
    route = (value or "/generated-images").strip() or "/generated-images"
    if not route.startswith("/"):
        route = f"/{route}"
    route = route.rstrip("/")
    return route or "/generated-images"


def _extension_for_mime(mime_type: str | None) -> str:
    return _MIME_EXTENSIONS.get((mime_type or "").split(";", 1)[0].strip().lower(), ".bin")


@dataclass(frozen=True)
class PersistedGeneratedImage:
    id: str
    path: str
    url: str
    delete_url: str
    mime_type: str
    size: int


class GeneratedImageStore:
    """Store generated images under one configured root with safe deletion."""

    def __init__(self, storage_dir: str | Path | None = None, public_route: str | None = None):
        self.root = Path(storage_dir or settings.generated_images_dir).expanduser().resolve()
        self.public_route = normalize_generated_images_route(public_route or settings.generated_images_route)

    def ensure_directory(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, mime_type: str | None, *, created_at: int | None = None) -> PersistedGeneratedImage:
        created = int(created_at or time.time())
        day = datetime.fromtimestamp(created, UTC).strftime("%Y%m%d")
        image_id = uuid.uuid4().hex
        extension = _extension_for_mime(mime_type)
        relative_path = Path(day) / f"{image_id}{extension}"
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        path = relative_path.as_posix()
        url = self.public_url(path)
        return PersistedGeneratedImage(
            id=image_id,
            path=path,
            url=url,
            delete_url=url,
            mime_type=mime_type or "application/octet-stream",
            size=len(data),
        )

    def public_url(self, relative_path: str) -> str:
        encoded = "/".join(quote(part) for part in relative_path.replace("\\", "/").split("/") if part)
        return f"{self.public_route}/{encoded}"

    def delete(self, image_path_or_url: str) -> bool:
        target = self._resolve_safe_path(image_path_or_url)
        if not target.is_file():
            return False
        target.unlink()
        self._remove_empty_parents(target.parent)
        return True

    def _resolve_safe_path(self, image_path_or_url: str) -> Path:
        raw = (image_path_or_url or "").strip()
        if not raw:
            raise ValueError("generated image path is required")
        if "\x00" in raw:
            raise ValueError("generated image path is invalid")

        parsed = urlparse(raw)
        path = parsed.path if parsed.scheme or parsed.netloc else raw
        path = unquote(path).replace("\\", "/")
        route_prefix = f"{self.public_route}/"
        if path == self.public_route:
            raise ValueError("generated image path is required")
        if path.startswith(route_prefix):
            path = path[len(route_prefix):]
        elif path.startswith("/"):
            raise ValueError("generated image path is outside generated image storage")

        candidate = (self.root / path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("generated image path is outside generated image storage") from exc
        if candidate == self.root:
            raise ValueError("generated image path is required")
        return candidate

    def _remove_empty_parents(self, start: Path) -> None:
        current = start
        while current != self.root and current.is_dir():
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent