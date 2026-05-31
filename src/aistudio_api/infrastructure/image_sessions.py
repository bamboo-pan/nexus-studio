"""Image generation session persistence helpers."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aistudio_api.config import settings


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_HEAVY_IMAGE_FIELDS = {"b64", "b64_json"}


class ImageSessionStore:
    """Store lightweight image conversation snapshots as JSON files."""

    def __init__(self, storage_dir: str | Path | None = None, *, max_sessions: int = 100) -> None:
        self.root = Path(storage_dir or settings.image_sessions_dir).expanduser().resolve()
        self.max_sessions = max_sessions

    def ensure_directory(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict[str, Any]]:
        self.ensure_directory()
        sessions = []
        for path in self.root.glob("*.json"):
            try:
                sessions.append(self._summary(self._read_json(path)))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        sessions.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or 0, reverse=True)
        return sessions[: self.max_sessions]

    def get(self, session_id: str) -> dict[str, Any]:
        path = self._path_for(session_id)
        if not path.is_file():
            raise FileNotFoundError(session_id)
        return self._read_json(path)

    def save(self, payload: Mapping[str, Any], session_id: str | None = None) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("image session payload must be an object")
        incoming = self._strip_heavy_fields(dict(payload))
        session_id = session_id or str(incoming.get("id") or "") or uuid.uuid4().hex
        session_id = self._validate_session_id(session_id)
        path = self._path_for(session_id)
        existing = self._read_json(path) if path.is_file() else {}
        now = int(time.time())
        created_at = incoming.get("created_at") or existing.get("created_at") or now
        session = {
            **incoming,
            "id": session_id,
            "created_at": created_at,
            "updated_at": now,
        }
        session["title"] = self._title_for(session)
        session["turn_count"] = self._turn_count(session)
        session["preview_url"] = self._preview_url(session)
        self.ensure_directory()
        self._write_json(path, session)
        self._prune_old_sessions()
        return session

    def delete(self, session_id: str) -> bool:
        path = self._path_for(session_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def _path_for(self, session_id: str) -> Path:
        return self.root / f"{self._validate_session_id(session_id)}.json"

    def _validate_session_id(self, session_id: str) -> str:
        value = str(session_id or "").strip()
        if not _SESSION_ID_RE.fullmatch(value):
            raise ValueError("image session id is invalid")
        return value

    def _read_json(self, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("image session file must contain an object")
        return data

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def _prune_old_sessions(self) -> None:
        files = []
        for path in self.root.glob("*.json"):
            try:
                data = self._read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            files.append((data.get("updated_at") or data.get("created_at") or 0, path.name, path))
        files.sort(reverse=True)
        for _, _, path in files[self.max_sessions :]:
            try:
                path.unlink()
            except FileNotFoundError:
                continue

    def _strip_heavy_fields(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._strip_heavy_fields(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): self._strip_heavy_fields(item)
                for key, item in value.items()
                if str(key) not in _HEAVY_IMAGE_FIELDS
            }
        return value

    def _summary(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": session.get("id", ""),
            "title": self._title_for(session),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "model": session.get("model", ""),
            "size": session.get("size", ""),
            "count": session.get("count") or session.get("image_count") or 0,
            "turn_count": self._turn_count(session),
            "preview_url": self._preview_url(session),
        }

    def _title_for(self, session: dict[str, Any]) -> str:
        title = str(session.get("title") or "").strip()
        if title:
            return title[:80]
        prompt = str(session.get("prompt") or "").strip()
        if not prompt:
            prompt = self._last_prompt(session)
        return (prompt or "Untitled image session")[:80]

    def _last_prompt(self, session: dict[str, Any]) -> str:
        conversation = session.get("conversation")
        if not isinstance(conversation, list):
            return ""
        for turn in reversed(conversation):
            if isinstance(turn, dict):
                prompt = str(turn.get("prompt") or "").strip()
                if prompt:
                    return prompt
        return ""

    def _turn_count(self, session: dict[str, Any]) -> int:
        conversation = session.get("conversation")
        if not isinstance(conversation, list):
            return 0
        user_turns = sum(1 for turn in conversation if isinstance(turn, dict) and turn.get("role") == "user")
        return user_turns or max(1, len(conversation) // 2) if conversation else 0

    def _preview_url(self, session: dict[str, Any]) -> str:
        for key in ("results", "base_image", "references"):
            value = session.get(key)
            url = self._first_image_url(value)
            if url:
                return url
        return ""

    def _first_image_url(self, value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("url") or "")
        if isinstance(value, list):
            for item in value:
                url = self._first_image_url(item)
                if url:
                    return url
        return ""