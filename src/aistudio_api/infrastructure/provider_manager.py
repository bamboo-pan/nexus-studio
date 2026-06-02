"""Provider Manager registry, credential, model catalog, and audit persistence."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aistudio_api.config import DEFAULT_IMAGE_MODEL, DEFAULT_TEXT_MODEL, settings
from aistudio_api.domain.model_capabilities import get_model_capabilities, list_model_metadata
from aistudio_api.infrastructure.local_studio import normalize_openai_base_url


PROVIDER_GOOGLE_AI_STUDIO = "google-ai-studio"
PROVIDER_OPENAI_COMPATIBLE = "openai-compatible"
PROVIDER_TYPES = (PROVIDER_GOOGLE_AI_STUDIO, PROVIDER_OPENAI_COMPATIBLE)
BUILT_IN_GOOGLE_PROVIDER_ID = PROVIDER_GOOGLE_AI_STUDIO

HEALTH_READY = "ready"
HEALTH_DISABLED = "disabled"
HEALTH_UNKNOWN = "unknown"
HEALTH_AUTH_FAILED = "auth_failed"
HEALTH_QUOTA_EXHAUSTED = "quota_exhausted"
HEALTH_DEGRADED = "degraded"
HEALTH_STATUSES = (
    HEALTH_READY,
    HEALTH_DISABLED,
    HEALTH_UNKNOWN,
    HEALTH_AUTH_FAILED,
    HEALTH_QUOTA_EXHAUSTED,
    HEALTH_DEGRADED,
)

MODEL_SOURCES = ("manual", "discovered")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def _now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), UTC).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any, *, max_length: int = 240) -> str:
    text = str(value or "").strip()
    text = text.replace("\x00", "")
    return text[:max_length]


def _validate_id(value: str, *, label: str) -> str:
    identifier = _clean_text(value, max_length=80)
    if not _ID_RE.fullmatch(identifier):
        raise ValueError(f"{label} is invalid")
    return identifier


def _normalize_provider_type(value: Any) -> str:
    normalized = str(value or PROVIDER_OPENAI_COMPATIBLE).strip().lower().replace("_", "-")
    if normalized in {"google", "google-ai", "google-aistudio", "google-ai-studio"}:
        return PROVIDER_GOOGLE_AI_STUDIO
    if normalized in {"openai", "open-ai", "openai-compatible", "openai-compat"}:
        return PROVIDER_OPENAI_COMPATIBLE
    raise ValueError(f"provider type must be one of: {', '.join(PROVIDER_TYPES)}")


def _normalize_timeout(value: Any, *, default: int = 120) -> int:
    if value is None or value == "":
        return default
    timeout = int(value)
    return max(1, min(timeout, 600))


def _normalize_token(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if "\n" in token or "\r" in token:
        raise ValueError("API token must be a single line")
    return token


def _mask_token(token: str) -> str:
    value = str(token or "")
    if not value:
        return ""
    suffix = value[-4:] if len(value) > 4 else ""
    return f"***{suffix}" if suffix else "***"


def _normalize_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        key_text = _clean_text(key, max_length=80)
        if key_text:
            result[key_text] = item
    return result


def _normalize_aliases(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    aliases = []
    for item in value:
        alias = _clean_text(item, max_length=120)
        if alias and alias not in aliases:
            aliases.append(alias)
    return aliases


def _modalities_from_capabilities(capabilities: Mapping[str, Any]) -> list[str]:
    modalities: list[str] = []
    if capabilities.get("text_output"):
        modalities.append("text")
    if capabilities.get("image_input"):
        modalities.append("image_input")
    if capabilities.get("file_input"):
        modalities.append("file_input")
    if capabilities.get("image_output"):
        modalities.append("image_generation")
    if capabilities.get("search"):
        modalities.append("search")
    if capabilities.get("tools") or capabilities.get("tool_calls"):
        modalities.append("tools")
    if capabilities.get("streaming"):
        modalities.append("streaming")
    return modalities


def _default_capabilities(model_id: str) -> dict[str, Any]:
    return get_model_capabilities(model_id).to_model_dict().get("capabilities", {})


def _normalize_health(value: Any, *, enabled: bool) -> dict[str, Any]:
    health = value if isinstance(value, Mapping) else {}
    status = _clean_text(health.get("status"), max_length=40) or (HEALTH_UNKNOWN if enabled else HEALTH_DISABLED)
    if not enabled:
        status = HEALTH_DISABLED
    if status not in HEALTH_STATUSES:
        raise ValueError(f"health status must be one of: {', '.join(HEALTH_STATUSES)}")
    normalized: dict[str, Any] = {
        "status": status,
        "message": _clean_text(health.get("message"), max_length=240),
        "checked_at": _clean_text(health.get("checked_at"), max_length=80),
        "last_success_at": _clean_text(health.get("last_success_at"), max_length=80),
    }
    return normalized


def _model_entry_id(provider_id: str, external_model_id: str) -> str:
    return f"{provider_id}:{external_model_id}"


def _normalize_model_entry(raw: Mapping[str, Any], *, provider_id: str, default_source: str = "manual") -> dict[str, Any]:
    external_model_id = _clean_text(raw.get("external_model_id") or raw.get("model") or raw.get("id"), max_length=180)
    if not external_model_id:
        raise ValueError("model external_model_id is required")
    source = _clean_text(raw.get("source") or default_source, max_length=40) or default_source
    if source not in MODEL_SOURCES:
        raise ValueError(f"model source must be one of: {', '.join(MODEL_SOURCES)}")
    capabilities = raw.get("capabilities") if isinstance(raw.get("capabilities"), Mapping) else _default_capabilities(external_model_id)
    capabilities = dict(capabilities)
    modalities = raw.get("modalities")
    if not isinstance(modalities, list) or not modalities:
        modalities = _modalities_from_capabilities(capabilities)
    metadata = _normalize_mapping(raw.get("metadata"))
    context_window = raw.get("context_window")
    if context_window is not None:
        try:
            context_window = int(context_window)
        except (TypeError, ValueError):
            context_window = None
    return {
        "id": _model_entry_id(provider_id, external_model_id),
        "provider_id": provider_id,
        "external_model_id": external_model_id,
        "display_name": _clean_text(raw.get("display_name") or raw.get("name") or external_model_id, max_length=180),
        "capabilities": capabilities,
        "modalities": [_clean_text(item, max_length=80) for item in modalities if _clean_text(item, max_length=80)],
        "aliases": _normalize_aliases(raw.get("aliases")),
        "defaults": _normalize_mapping(raw.get("defaults")),
        "context_window": context_window,
        "source": source,
        "metadata": metadata,
    }


def _google_model_catalog() -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for model in list_model_metadata():
        model_id = str(model.get("id") or "")
        if not model_id:
            continue
        aliases: list[str] = []
        defaults: dict[str, Any] = {}
        if model_id == DEFAULT_TEXT_MODEL:
            aliases.append("default_text")
            defaults["text"] = True
        if model_id == DEFAULT_IMAGE_MODEL:
            aliases.append("default_image")
            defaults["image"] = True
        catalog.append(
            _normalize_model_entry(
                {
                    "external_model_id": model_id,
                    "display_name": model.get("display_name") or model_id,
                    "capabilities": model.get("capabilities") or {},
                    "aliases": aliases,
                    "defaults": defaults,
                    "source": "discovered",
                    "metadata": {"owned_by": model.get("owned_by") or "google", "built_in": True},
                },
                provider_id=BUILT_IN_GOOGLE_PROVIDER_ID,
                default_source="discovered",
            )
        )
    return catalog


class ProviderManagerStore:
    """Store Provider Manager records in a dedicated JSON-backed data directory."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        self.root = Path(storage_dir or settings.provider_manager_dir).expanduser().resolve()
        self.providers_path = self.root / "providers.json"
        self.credentials_path = self.root / "credentials.json"
        self.audit_path = self.root / "audit.jsonl"
        self._lock = threading.RLock()

    def ensure_directory(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        providers = self.list_providers()
        return {
            "ok": True,
            "storage": str(self.root),
            "provider_count": len(providers),
            "custom_provider_count": sum(1 for item in providers if not item.get("built_in")),
        }

    def list_providers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._built_in_google_provider(), *[self._public_provider(item) for item in self._read_custom_providers()]]

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        provider_id = _validate_id(provider_id, label="provider id")
        if provider_id == BUILT_IN_GOOGLE_PROVIDER_ID:
            return self._built_in_google_provider()
        provider = self._find_custom_provider(provider_id)
        if provider is None:
            raise FileNotFoundError(provider_id)
        return self._public_provider(provider)

    def create_provider(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        provider_type = _normalize_provider_type(payload.get("type") or payload.get("provider_type"))
        if provider_type != PROVIDER_OPENAI_COMPATIBLE:
            raise PermissionError("built-in providers are managed by Nexus Studio")

        provider_id = _validate_id(_clean_text(payload.get("id"), max_length=80) or f"provider-{uuid.uuid4().hex[:16]}", label="provider id")
        if provider_id == BUILT_IN_GOOGLE_PROVIDER_ID:
            raise ValueError("provider id is reserved")
        name = _clean_text(payload.get("name"), max_length=120)
        if not name:
            raise ValueError("provider name is required")
        base_url = normalize_openai_base_url(str(payload.get("base_url") or payload.get("baseUrl") or ""))
        enabled = bool(payload.get("enabled", True))
        token = _normalize_token(payload.get("token"))
        credential_ref = _clean_text(payload.get("credential_ref") or payload.get("credentialRef"), max_length=80)
        now = _now_iso()
        provider = {
            "id": provider_id,
            "type": provider_type,
            "name": name,
            "enabled": enabled,
            "base_url": base_url,
            "timeout": _normalize_timeout(payload.get("timeout"), default=120),
            "credential_ref": credential_ref or None,
            "model_catalog": self._normalize_model_catalog(payload.get("model_catalog") or payload.get("models") or [], provider_id=provider_id),
            "aliases": _normalize_mapping(payload.get("aliases")),
            "defaults": _normalize_mapping(payload.get("defaults")),
            "health": _normalize_health(payload.get("health"), enabled=enabled),
            "created_at": now,
            "updated_at": now,
        }

        with self._lock:
            providers = self._read_custom_providers()
            if any(item.get("id") == provider_id for item in providers):
                raise ValueError("provider id already exists")
            credentials = self._read_credentials()
            if token:
                provider["credential_ref"] = self._upsert_credential(credentials, provider_id, token)
                self._write_credentials(credentials)
            elif credential_ref and credential_ref not in credentials:
                raise ValueError("credential reference was not found")
            providers.append(provider)
            self._write_custom_providers(providers)
            self._append_audit(
                action="provider.created",
                target_id=provider_id,
                status="success",
                summary=self._provider_audit_summary(provider),
            )
            if provider["model_catalog"]:
                self._append_audit(
                    action="provider.model_catalog.updated",
                    target_id=provider_id,
                    status="success",
                    summary={"model_count": len(provider["model_catalog"]), "source": "manual"},
                )
        return self.get_provider(provider_id)

    def update_provider(self, provider_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        provider_id = _validate_id(provider_id, label="provider id")
        if provider_id == BUILT_IN_GOOGLE_PROVIDER_ID:
            raise PermissionError("built-in providers cannot be modified")
        changed_fields: list[str] = []
        model_catalog_changed = False
        enabled_action: str | None = None
        with self._lock:
            providers = self._read_custom_providers()
            index = self._custom_provider_index(providers, provider_id)
            if index < 0:
                raise FileNotFoundError(provider_id)
            provider = dict(providers[index])
            old_enabled = bool(provider.get("enabled", True))

            if "type" in payload or "provider_type" in payload:
                provider_type = _normalize_provider_type(payload.get("type") or payload.get("provider_type"))
                if provider_type != PROVIDER_OPENAI_COMPATIBLE:
                    raise PermissionError("custom provider type cannot be changed to a built-in provider")
                provider["type"] = provider_type
                changed_fields.append("type")
            if "name" in payload:
                name = _clean_text(payload.get("name"), max_length=120)
                if not name:
                    raise ValueError("provider name is required")
                provider["name"] = name
                changed_fields.append("name")
            if "base_url" in payload or "baseUrl" in payload:
                provider["base_url"] = normalize_openai_base_url(str(payload.get("base_url") or payload.get("baseUrl") or ""))
                changed_fields.append("base_url")
            if "timeout" in payload:
                provider["timeout"] = _normalize_timeout(payload.get("timeout"), default=int(provider.get("timeout") or 120))
                changed_fields.append("timeout")
            if "enabled" in payload:
                provider["enabled"] = bool(payload.get("enabled"))
                if provider["enabled"] != old_enabled:
                    enabled_action = "provider.enabled" if provider["enabled"] else "provider.disabled"
                changed_fields.append("enabled")
            if "aliases" in payload:
                provider["aliases"] = _normalize_mapping(payload.get("aliases"))
                changed_fields.append("aliases")
            if "defaults" in payload:
                provider["defaults"] = _normalize_mapping(payload.get("defaults"))
                changed_fields.append("defaults")
            if "health" in payload:
                provider["health"] = _normalize_health(payload.get("health"), enabled=bool(provider.get("enabled", True)))
                changed_fields.append("health")
            elif "enabled" in payload:
                provider["health"] = _normalize_health(provider.get("health"), enabled=bool(provider.get("enabled", True)))
            if "model_catalog" in payload or "models" in payload:
                provider["model_catalog"] = self._normalize_model_catalog(payload.get("model_catalog") if "model_catalog" in payload else payload.get("models"), provider_id=provider_id)
                model_catalog_changed = True
                changed_fields.append("model_catalog")
            if "credential_ref" in payload or "credentialRef" in payload:
                credential_ref = _clean_text(payload.get("credential_ref") or payload.get("credentialRef"), max_length=80)
                if credential_ref:
                    credentials = self._read_credentials()
                    if credential_ref not in credentials:
                        raise ValueError("credential reference was not found")
                    provider["credential_ref"] = credential_ref
                else:
                    provider["credential_ref"] = None
                changed_fields.append("credential_ref")
            if "token" in payload:
                token = _normalize_token(payload.get("token"))
                if token:
                    credentials = self._read_credentials()
                    provider["credential_ref"] = self._upsert_credential(credentials, provider_id, token, credential_id=provider.get("credential_ref"))
                    self._write_credentials(credentials)
                    changed_fields.append("credential_ref")

            provider["updated_at"] = _now_iso()
            providers[index] = provider
            self._write_custom_providers(providers)
            action = enabled_action or "provider.updated"
            unique_changed_fields = sorted(set(changed_fields))
            self._append_audit(
                action=action,
                target_id=provider_id,
                status="success",
                summary={**self._provider_audit_summary(provider), "changed_fields": unique_changed_fields},
            )
            non_enabled_fields = [field for field in unique_changed_fields if field != "enabled"]
            if enabled_action and non_enabled_fields:
                self._append_audit(
                    action="provider.updated",
                    target_id=provider_id,
                    status="success",
                    summary={**self._provider_audit_summary(provider), "changed_fields": non_enabled_fields},
                )
            if model_catalog_changed:
                self._append_audit(
                    action="provider.model_catalog.updated",
                    target_id=provider_id,
                    status="success",
                    summary={"model_count": len(provider.get("model_catalog") or []), "source": "manual"},
                )
        return self.get_provider(provider_id)

    def set_enabled(self, provider_id: str, enabled: bool) -> dict[str, Any]:
        return self.update_provider(provider_id, {"enabled": bool(enabled)})

    def delete_provider(self, provider_id: str) -> dict[str, Any]:
        provider_id = _validate_id(provider_id, label="provider id")
        if provider_id == BUILT_IN_GOOGLE_PROVIDER_ID:
            raise PermissionError("built-in providers cannot be deleted")
        with self._lock:
            providers = self._read_custom_providers()
            index = self._custom_provider_index(providers, provider_id)
            if index < 0:
                raise FileNotFoundError(provider_id)
            provider = providers.pop(index)
            credentials = self._read_credentials()
            removed_credentials = [credential_id for credential_id, item in credentials.items() if item.get("provider_id") == provider_id]
            for credential_id in removed_credentials:
                credentials.pop(credential_id, None)
            self._write_custom_providers(providers)
            self._write_credentials(credentials)
            self._append_audit(
                action="provider.deleted",
                target_id=provider_id,
                status="success",
                summary={"name": provider.get("name"), "model_count": len(provider.get("model_catalog") or []), "credential_removed": bool(removed_credentials)},
            )
        return {"ok": True, "deleted": provider_id}

    def list_model_catalog(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        providers = self.list_providers()
        if provider_id:
            provider_id = _validate_id(provider_id, label="provider id")
            providers = [provider for provider in providers if provider.get("id") == provider_id]
            if not providers:
                raise FileNotFoundError(provider_id)
        models: list[dict[str, Any]] = []
        for provider in providers:
            models.extend(provider.get("model_catalog") or [])
        return models

    def update_model_catalog(self, provider_id: str, entries: list[Mapping[str, Any]]) -> dict[str, Any]:
        return self.update_provider(provider_id, {"model_catalog": entries})

    def list_audit_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self.ensure_directory()
        events = []
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
        events.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""), reverse=True)
        return events[: max(1, min(int(limit), 1000))]

    def record_failed_event(self, *, action: str, target_id: str, exc: Exception) -> None:
        self._append_audit(
            action=action,
            target_id=_clean_text(target_id or "unknown", max_length=120) or "unknown",
            status="failed",
            summary={"error": _clean_text(str(exc), max_length=240), "error_type": type(exc).__name__},
        )

    def _built_in_google_provider(self) -> dict[str, Any]:
        catalog = _google_model_catalog()
        return {
            "id": BUILT_IN_GOOGLE_PROVIDER_ID,
            "type": PROVIDER_GOOGLE_AI_STUDIO,
            "provider_type": PROVIDER_GOOGLE_AI_STUDIO,
            "name": "Google AI Studio",
            "enabled": True,
            "built_in": True,
            "deletable": False,
            "credential_ref": None,
            "credential": None,
            "timeout": 300,
            "model_catalog": catalog,
            "aliases": {"default_text": DEFAULT_TEXT_MODEL, "default_image": DEFAULT_IMAGE_MODEL},
            "defaults": {"text_model": DEFAULT_TEXT_MODEL, "image_model": DEFAULT_IMAGE_MODEL},
            "health": {"status": HEALTH_READY, "message": "Built-in provider", "checked_at": "", "last_success_at": ""},
            "created_at": "builtin",
            "updated_at": "builtin",
        }

    def _public_provider(self, provider: Mapping[str, Any]) -> dict[str, Any]:
        credential_ref = provider.get("credential_ref")
        credential = None
        if credential_ref:
            credential = self._credential_summary(str(credential_ref))
        enabled = bool(provider.get("enabled", True))
        provider_id = str(provider.get("id") or "")
        public = {
            "id": provider_id,
            "type": PROVIDER_OPENAI_COMPATIBLE,
            "provider_type": PROVIDER_OPENAI_COMPATIBLE,
            "name": provider.get("name") or provider_id,
            "enabled": enabled,
            "built_in": False,
            "deletable": True,
            "base_url": provider.get("base_url") or "",
            "timeout": int(provider.get("timeout") or 120),
            "credential_ref": credential_ref,
            "credential": credential,
            "model_catalog": [self._public_model_entry(item, provider_id=provider_id) for item in provider.get("model_catalog") or [] if isinstance(item, Mapping)],
            "aliases": _normalize_mapping(provider.get("aliases")),
            "defaults": _normalize_mapping(provider.get("defaults")),
            "health": _normalize_health(provider.get("health"), enabled=enabled),
            "created_at": provider.get("created_at") or "",
            "updated_at": provider.get("updated_at") or "",
        }
        return public

    def _public_model_entry(self, raw: Mapping[str, Any], *, provider_id: str) -> dict[str, Any]:
        return _normalize_model_entry(raw, provider_id=provider_id, default_source=str(raw.get("source") or "manual"))

    def _normalize_model_catalog(self, value: Any, *, provider_id: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("model_catalog must be a list")
        models = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("model catalog entries must be objects")
            model = _normalize_model_entry(item, provider_id=provider_id)
            if model["external_model_id"] in seen:
                continue
            seen.add(model["external_model_id"])
            models.append(model)
        return models

    def _provider_audit_summary(self, provider: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": provider.get("name"),
            "provider_type": provider.get("type") or PROVIDER_OPENAI_COMPATIBLE,
            "enabled": bool(provider.get("enabled", True)),
            "credential_ref": provider.get("credential_ref"),
            "model_count": len(provider.get("model_catalog") or []),
            "health_status": (provider.get("health") or {}).get("status") if isinstance(provider.get("health"), Mapping) else None,
        }

    def _credential_summary(self, credential_ref: str) -> dict[str, Any] | None:
        credentials = self._read_credentials()
        credential = credentials.get(credential_ref)
        if not isinstance(credential, Mapping):
            return None
        token = str(credential.get("token") or "")
        return {
            "id": credential_ref,
            "ref": credential_ref,
            "kind": credential.get("kind") or "bearer_token",
            "has_token": bool(token),
            "masked": _mask_token(token) if token else str(credential.get("masked") or ""),
            "updated_at": credential.get("updated_at") or "",
        }

    def _upsert_credential(self, credentials: dict[str, Any], provider_id: str, token: str, credential_id: Any | None = None) -> str:
        credential_ref = _clean_text(credential_id, max_length=80)
        if credential_ref and credential_ref not in credentials:
            credential_ref = ""
        if not credential_ref:
            credential_ref = f"cred-{uuid.uuid4().hex[:16]}"
        existing = credentials.get(credential_ref) if isinstance(credentials.get(credential_ref), Mapping) else {}
        now = _now_iso()
        credentials[credential_ref] = {
            "id": credential_ref,
            "provider_id": provider_id,
            "kind": "bearer_token",
            "token": token,
            "masked": _mask_token(token),
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
        }
        return credential_ref

    def _append_audit(self, *, action: str, target_id: str, status: str, summary: Mapping[str, Any] | None = None) -> None:
        self.ensure_directory()
        event = {
            "id": uuid.uuid4().hex,
            "created_at": _now_iso(),
            "actor": "local-user",
            "action": action,
            "target_type": "provider",
            "target_id": target_id,
            "status": status,
            "summary": dict(summary or {}),
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def _find_custom_provider(self, provider_id: str) -> dict[str, Any] | None:
        for provider in self._read_custom_providers():
            if provider.get("id") == provider_id:
                return provider
        return None

    def _custom_provider_index(self, providers: list[dict[str, Any]], provider_id: str) -> int:
        for index, provider in enumerate(providers):
            if provider.get("id") == provider_id:
                return index
        return -1

    def _read_custom_providers(self) -> list[dict[str, Any]]:
        data = self._read_json(self.providers_path, default={"data": []})
        raw_items = data.get("data") if isinstance(data, Mapping) else data
        if not isinstance(raw_items, list):
            return []
        providers = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            try:
                providers.append(self._normalize_stored_provider(item))
            except ValueError:
                continue
        providers.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""))
        return providers

    def _normalize_stored_provider(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        provider_id = _validate_id(str(raw.get("id") or ""), label="provider id")
        enabled = bool(raw.get("enabled", True))
        return {
            "id": provider_id,
            "type": PROVIDER_OPENAI_COMPATIBLE,
            "name": _clean_text(raw.get("name") or provider_id, max_length=120),
            "enabled": enabled,
            "base_url": normalize_openai_base_url(str(raw.get("base_url") or "")),
            "timeout": _normalize_timeout(raw.get("timeout"), default=120),
            "credential_ref": _clean_text(raw.get("credential_ref"), max_length=80) or None,
            "model_catalog": self._normalize_model_catalog(raw.get("model_catalog") or [], provider_id=provider_id),
            "aliases": _normalize_mapping(raw.get("aliases")),
            "defaults": _normalize_mapping(raw.get("defaults")),
            "health": _normalize_health(raw.get("health"), enabled=enabled),
            "created_at": _clean_text(raw.get("created_at"), max_length=80),
            "updated_at": _clean_text(raw.get("updated_at"), max_length=80),
        }

    def _write_custom_providers(self, providers: list[dict[str, Any]]) -> None:
        self.ensure_directory()
        self._write_json(self.providers_path, {"data": providers})

    def _read_credentials(self) -> dict[str, Any]:
        data = self._read_json(self.credentials_path, default={"data": {}})
        if isinstance(data, Mapping) and isinstance(data.get("data"), Mapping):
            return dict(data["data"])
        if isinstance(data, Mapping):
            return dict(data)
        return {}

    def _write_credentials(self, credentials: dict[str, Any]) -> None:
        self.ensure_directory()
        self._write_json(self.credentials_path, {"data": credentials})

    def _read_json(self, path: Path, *, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)