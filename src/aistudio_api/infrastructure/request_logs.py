"""Outbound AI Studio request log persistence helpers."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aistudio_api.config import settings


_REQUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_CURRENT_CHAIN_ID: ContextVar[str | None] = ContextVar("aistudio_request_log_chain_id", default=None)
_PHASE_ORDER = {
    "client_request": 10,
    "upstream_request": 20,
    "upstream_response": 30,
    "client_response": 40,
}


def new_request_chain_id() -> str:
    return uuid.uuid4().hex


def current_request_chain_id() -> str | None:
    return _CURRENT_CHAIN_ID.get()


def set_request_chain_id(chain_id: str) -> Token[str | None]:
    return _CURRENT_CHAIN_ID.set(str(chain_id or ""))


def reset_request_chain_id(token: Token[str | None]) -> None:
    _CURRENT_CHAIN_ID.reset(token)


class RequestLogStore:
    """Store complete outbound AI Studio requests as JSON files."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        self.root = Path(storage_dir or settings.request_logs_dir).expanduser().resolve()
        self.entries_dir = self.root / "entries"
        self.state_path = self.root / "state.json"
        self._lock = threading.RLock()

    def ensure_directory(self) -> None:
        self.entries_dir.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        return {"enabled": self.is_enabled(), "count": self.count(), "group_count": self.group_count()}

    def is_enabled(self) -> bool:
        return bool(self._read_state().get("enabled", False))

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self.ensure_directory()
            state = {"enabled": bool(enabled), "updated_at": self._now_iso()}
            self._write_json(self.state_path, state)
            return self.status()

    def count(self) -> int:
        self.ensure_directory()
        return sum(1 for path in self.entries_dir.glob("*.json") if path.is_file())

    def list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        self.ensure_directory()
        items = []
        for path in self.entries_dir.glob("*.json"):
            try:
                items.append(self._summary(self._read_entry(path)))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        items.sort(key=lambda item: (item.get("created_at_unix") or 0, item.get("id") or ""), reverse=True)
        if limit is not None:
            return items[: max(0, limit)]
        return items

    def group_count(self) -> int:
        return len(self._group_entries().keys())

    def list_groups(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        groups = [self._group_summary(chain_id, entries) for chain_id, entries in self._group_entries().items()]
        groups.sort(key=lambda item: (item.get("updated_at_unix") or 0, item.get("id") or ""), reverse=True)
        if limit is not None:
            return groups[: max(0, limit)]
        return groups

    def get_group(self, chain_id: str) -> dict[str, Any]:
        normalized_id = self._normalize_chain_id(chain_id)
        entries = self._group_entries().get(normalized_id)
        if not entries:
            raise FileNotFoundError(normalized_id)
        return self._group_detail(normalized_id, entries)

    def export_groups(self, chain_ids: list[str]) -> dict[str, Any]:
        requested_ids = self._normalize_chain_ids(chain_ids)
        groups = self._group_entries()
        data = [self._group_detail(chain_id, groups[chain_id]) for chain_id in requested_ids if chain_id in groups]
        found_ids = {item["id"] for item in data}
        return {
            "data": data,
            "missing": [chain_id for chain_id in requested_ids if chain_id not in found_ids],
        }

    def delete_group(self, chain_id: str) -> dict[str, Any]:
        result = self.delete_groups([chain_id])
        if not result["deleted_entries"]:
            raise FileNotFoundError(chain_id)
        return result

    def delete_groups(self, chain_ids: list[str]) -> dict[str, Any]:
        requested_ids = self._normalize_chain_ids(chain_ids)
        deleted_entries = 0
        deleted_groups: list[str] = []
        with self._lock:
            grouped_paths = self._group_entry_paths()
            for chain_id in requested_ids:
                paths = grouped_paths.get(chain_id, [])
                if not paths:
                    continue
                for path in paths:
                    try:
                        path.unlink()
                        deleted_entries += 1
                    except FileNotFoundError:
                        continue
                deleted_groups.append(chain_id)
        return {
            "requested": len(requested_ids),
            "deleted_groups": len(deleted_groups),
            "deleted_entries": deleted_entries,
            "groups": deleted_groups,
            "missing": [chain_id for chain_id in requested_ids if chain_id not in set(deleted_groups)],
        }

    def get(self, request_id: str) -> dict[str, Any]:
        path = self._path_for(request_id)
        if not path.is_file():
            raise FileNotFoundError(request_id)
        return self._read_entry(path)

    def save(
        self,
        *,
        kind: str,
        model: str | None,
        method: str,
        url: str,
        headers: Mapping[str, Any],
        body: str | bytes,
        captured_headers: Mapping[str, Any] | None = None,
        transport: str = "",
        chain_id: str | None = None,
        direction: str = "outbound",
        phase: str | None = None,
        status_code: int | None = None,
        response_headers: Mapping[str, Any] | None = None,
        response_body: str | bytes | None = None,
        elapsed_ms: float | None = None,
    ) -> dict[str, Any] | None:
        if not self.is_enabled():
            return None

        request_id = uuid.uuid4().hex
        body_raw = self._body_to_text(body)
        body_json, body_parse_error = self._parse_body(body_raw)
        created_at_unix = time.time()
        response_fields = self._response_fields(response_headers=response_headers, response_body=response_body)
        entry = {
            "id": request_id,
            "chain_id": str(chain_id or current_request_chain_id() or request_id),
            "created_at": self._now_iso(created_at_unix),
            "created_at_unix": created_at_unix,
            "kind": str(kind or "request"),
            "model": str(model or ""),
            "transport": str(transport or ""),
            "direction": str(direction or "outbound"),
            "phase": str(phase or self._default_phase(direction)),
            "method": str(method or "POST").upper(),
            "url": str(url or ""),
            "headers": self._string_mapping(headers),
            "captured_headers": self._string_mapping(captured_headers or headers),
            "body_size": len(body_raw.encode("utf-8")),
            "body_raw": body_raw,
            "body_json": body_json,
            "body_parse_error": body_parse_error,
        }
        if status_code is not None:
            entry["status_code"] = int(status_code)
        if elapsed_ms is not None:
            entry["elapsed_ms"] = round(float(elapsed_ms), 3)
        entry.update(response_fields)
        with self._lock:
            self.ensure_directory()
            self._write_json(self._path_for(request_id), entry)
        return entry

    def attach_response(
        self,
        request_id: str,
        *,
        status_code: int | None = None,
        response_headers: Mapping[str, Any] | None = None,
        response_body: str | bytes | None = None,
        elapsed_ms: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            path = self._path_for(request_id)
            if not path.is_file():
                raise FileNotFoundError(request_id)
            entry = self._read_entry(path)
            if status_code is not None:
                entry["status_code"] = int(status_code)
            if elapsed_ms is not None:
                entry["elapsed_ms"] = round(float(elapsed_ms), 3)
            entry.update(self._response_fields(response_headers=response_headers, response_body=response_body))
            self._write_json(path, entry)
            return entry

    def _path_for(self, request_id: str) -> Path:
        value = str(request_id or "").strip()
        if not _REQUEST_ID_RE.fullmatch(value):
            raise ValueError("request log id is invalid")
        return self.entries_dir / f"{value}.json"

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"enabled": False}
        return data if isinstance(data, dict) else {"enabled": False}

    def _read_entry(self, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request log file must contain an object")
        return data

    def _group_entries(self) -> dict[str, list[dict[str, Any]]]:
        self.ensure_directory()
        groups: dict[str, list[dict[str, Any]]] = {}
        for path in self.entries_dir.glob("*.json"):
            try:
                entry = self._read_entry(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            groups.setdefault(self._chain_key(entry), []).append(entry)
        return groups

    def _group_entry_paths(self) -> dict[str, list[Path]]:
        self.ensure_directory()
        groups: dict[str, list[Path]] = {}
        for path in self.entries_dir.glob("*.json"):
            try:
                entry = self._read_entry(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            groups.setdefault(self._chain_key(entry), []).append(path)
        return groups

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def _summary(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": entry.get("id", ""),
            "created_at": entry.get("created_at"),
            "created_at_unix": entry.get("created_at_unix"),
            "kind": entry.get("kind", ""),
            "model": entry.get("model", ""),
            "transport": entry.get("transport", ""),
            "chain_id": entry.get("chain_id", ""),
            "direction": entry.get("direction", ""),
            "phase": entry.get("phase", ""),
            "status_code": entry.get("status_code"),
            "elapsed_ms": entry.get("elapsed_ms"),
            "method": entry.get("method", "POST"),
            "url": entry.get("url", ""),
            "body_size": entry.get("body_size", 0),
            "response_body_size": entry.get("response_body_size", 0),
        }

    def _group_summary(self, chain_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        sorted_entries = self._sort_group_entries(entries)
        time_entries = sorted(entries, key=lambda entry: (entry.get("created_at_unix") or 0, entry.get("id") or ""))
        first_time_entry = time_entries[0]
        last_time_entry = time_entries[-1]
        representative = self._representative_entry(sorted_entries)
        body_size = sum(int(entry.get("body_size") or 0) for entry in entries)
        response_body_size = sum(int(entry.get("response_body_size") or 0) for entry in entries)
        return {
            "id": chain_id,
            "chain_id": chain_id,
            "created_at": first_time_entry.get("created_at"),
            "created_at_unix": first_time_entry.get("created_at_unix"),
            "updated_at": last_time_entry.get("created_at"),
            "updated_at_unix": last_time_entry.get("created_at_unix"),
            "entry_count": len(entries),
            "phase_count": len({str(entry.get("phase") or "") for entry in entries if entry.get("phase")}),
            "phases": [self._summary(entry) for entry in sorted_entries],
            "kind": representative.get("kind", ""),
            "model": self._first_non_empty(sorted_entries, "model"),
            "transport": representative.get("transport", ""),
            "direction": representative.get("direction", ""),
            "phase": representative.get("phase", ""),
            "status_code": self._last_non_empty(sorted_entries, "status_code"),
            "elapsed_ms": self._last_non_empty(sorted_entries, "elapsed_ms"),
            "method": representative.get("method", "POST"),
            "url": representative.get("url", ""),
            "body_size": body_size,
            "response_body_size": response_body_size,
            "total_size": body_size + response_body_size,
        }

    def _group_detail(self, chain_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        summary = self._group_summary(chain_id, entries)
        summary["entries"] = self._sort_group_entries(entries)
        return summary

    def _sort_group_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            entries,
            key=lambda entry: (
                _PHASE_ORDER.get(str(entry.get("phase") or ""), 100),
                entry.get("created_at_unix") or 0,
                entry.get("id") or "",
            ),
        )

    def _representative_entry(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        for phase in ("client_request", "upstream_request", "client_response"):
            for entry in entries:
                if entry.get("phase") == phase:
                    return entry
        return entries[0] if entries else {}

    def _chain_key(self, entry: dict[str, Any]) -> str:
        return str(entry.get("chain_id") or entry.get("id") or "").strip()

    def _normalize_chain_id(self, chain_id: str) -> str:
        normalized = str(chain_id or "").strip()
        if not normalized:
            raise ValueError("request log group id is required")
        return normalized

    def _normalize_chain_ids(self, chain_ids: list[str]) -> list[str]:
        normalized_ids: list[str] = []
        seen: set[str] = set()
        for chain_id in chain_ids:
            normalized = self._normalize_chain_id(chain_id)
            if normalized in seen:
                continue
            seen.add(normalized)
            normalized_ids.append(normalized)
        return normalized_ids

    def _first_non_empty(self, entries: list[dict[str, Any]], key: str) -> Any:
        for entry in entries:
            value = entry.get(key)
            if value not in (None, ""):
                return value
        return ""

    def _last_non_empty(self, entries: list[dict[str, Any]], key: str) -> Any:
        for entry in reversed(entries):
            value = entry.get(key)
            if value not in (None, ""):
                return value
        return None

    def _body_to_text(self, body: str | bytes) -> str:
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="replace")
        return str(body)

    def _parse_body(self, body: str) -> tuple[Any, str | None]:
        if body == "":
            return None, None
        try:
            return json.loads(body), None
        except json.JSONDecodeError as exc:
            return None, str(exc)

    def _response_fields(
        self,
        *,
        response_headers: Mapping[str, Any] | None,
        response_body: str | bytes | None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if response_headers is not None:
            fields["response_headers"] = self._string_mapping(response_headers)
        if response_body is not None:
            response_body_raw = self._body_to_text(response_body)
            response_body_json, response_body_parse_error = self._parse_body(response_body_raw)
            fields.update(
                {
                    "response_body_size": len(response_body_raw.encode("utf-8")),
                    "response_body_raw": response_body_raw,
                    "response_body_json": response_body_json,
                    "response_body_parse_error": response_body_parse_error,
                }
            )
        return fields

    def _string_mapping(self, mapping: Mapping[str, Any]) -> dict[str, str]:
        return {str(key): str(value) for key, value in mapping.items()}

    def _default_phase(self, direction: str) -> str:
        return "upstream_request" if str(direction or "").lower() == "outbound" else "request"

    def _now_iso(self, value: float | None = None) -> str:
        return datetime.fromtimestamp(value or time.time(), UTC).isoformat().replace("+00:00", "Z")