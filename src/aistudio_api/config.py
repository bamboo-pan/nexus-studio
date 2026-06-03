"""Runtime configuration helpers."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件（如果存在）
load_dotenv()

DEFAULT_TEXT_MODEL = os.getenv("AISTUDIO_DEFAULT_TEXT_MODEL", "gemma-4-31b-it")
DEFAULT_WARMUP_TEXT_MODEL = os.getenv("AISTUDIO_WARMUP_TEXT_MODEL", "gemini-3-flash-preview")
DEFAULT_IMAGE_MODEL = os.getenv("AISTUDIO_DEFAULT_IMAGE_MODEL", "gemini-3.1-flash-image-preview")
DEFAULT_CAMOUFOX_PORT = 9222
DEFAULT_RUNTIME_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_TMP_DIR = tempfile.gettempdir()


def _proxy_server() -> str | None:
    value = os.getenv("AISTUDIO_PROXY_SERVER")
    return value.strip() if value else None

_AUTH_SEARCH_ROOTS = [
    Path(__file__).resolve().parents[2] / "data",  # 项目内 data/ 目录
]


def discover_auth_file() -> str | None:
    override = os.getenv("AISTUDIO_AUTH_FILE")
    if override:
        return override

    for root in _AUTH_SEARCH_ROOTS:
        if not root.is_dir():
            continue
        for file in root.iterdir():
            if file.suffix == ".json":
                return str(file)
    return None


@dataclass(slots=True)
class Settings:
    port: int = int(os.getenv("AISTUDIO_PORT", "8080"))
    camoufox_port: int = int(os.getenv("AISTUDIO_CAMOUFOX_PORT", DEFAULT_CAMOUFOX_PORT))
    auth_file: str | None = discover_auth_file()
    tmp_dir: str = os.getenv("AISTUDIO_TMP_DIR") or DEFAULT_TMP_DIR
    camoufox_headless: bool = os.getenv("AISTUDIO_CAMOUFOX_HEADLESS", "1") not in ("0", "false", "False")
    camoufox_python: str | None = os.getenv("AISTUDIO_CAMOUFOX_PYTHON")
    proxy_server: str | None = _proxy_server()
    camoufox_locale: str = os.getenv("AISTUDIO_CAMOUFOX_LOCALE", "en-US")
    camoufox_timezone: str = os.getenv("AISTUDIO_CAMOUFOX_TIMEZONE", "America/Los_Angeles")
    camoufox_geolocation_latitude: float = float(os.getenv("AISTUDIO_CAMOUFOX_GEOLOCATION_LATITUDE", "37.7749"))
    camoufox_geolocation_longitude: float = float(os.getenv("AISTUDIO_CAMOUFOX_GEOLOCATION_LONGITUDE", "-122.4194"))
    camoufox_geolocation_accuracy: int = int(os.getenv("AISTUDIO_CAMOUFOX_GEOLOCATION_ACCURACY", "100"))
    ai_studio_authuser_candidates: str = os.getenv("AISTUDIO_AUTHUSER_CANDIDATES", os.getenv("AISTUDIO_AUTHUSER", "2,0"))
    timeout_replay: int = int(os.getenv("AISTUDIO_TIMEOUT_REPLAY", "120"))
    timeout_stream: int = int(os.getenv("AISTUDIO_TIMEOUT_STREAM", "120"))
    timeout_capture: int = int(os.getenv("AISTUDIO_TIMEOUT_CAPTURE", "30"))
    snapshot_cache_ttl: int = int(os.getenv("AISTUDIO_SNAPSHOT_CACHE_TTL", "3600"))
    snapshot_cache_max: int = int(os.getenv("AISTUDIO_SNAPSHOT_CACHE_MAX", "100"))
    dump_raw_response: bool = os.getenv("AISTUDIO_DUMP_RAW_RESPONSE", "0") in ("1", "true", "True")
    dump_raw_response_dir: str = os.getenv("AISTUDIO_DUMP_RAW_RESPONSE_DIR", "/tmp")
    request_logs_dir: str = os.getenv("AISTUDIO_REQUEST_LOGS_DIR", str(DEFAULT_RUNTIME_DATA_DIR / "request-logs"))
    generated_images_dir: str = os.getenv("AISTUDIO_GENERATED_IMAGES_DIR", str(DEFAULT_RUNTIME_DATA_DIR / "generated-images"))
    image_sessions_dir: str = os.getenv("AISTUDIO_IMAGE_SESSIONS_DIR", str(DEFAULT_RUNTIME_DATA_DIR / "image-sessions"))
    local_studio_dir: str = os.getenv("AISTUDIO_LOCAL_STUDIO_DIR", str(DEFAULT_RUNTIME_DATA_DIR / "local-studio"))
    provider_manager_dir: str = os.getenv("AISTUDIO_PROVIDER_MANAGER_DIR", str(DEFAULT_RUNTIME_DATA_DIR / "provider-manager"))
    generated_images_route: str = os.getenv("AISTUDIO_GENERATED_IMAGES_ROUTE", "/generated-images")
    accounts_dir: str = os.getenv("AISTUDIO_ACCOUNTS_DIR", "")
    login_camoufox_port: int = int(os.getenv("AISTUDIO_LOGIN_CAMOUFOX_PORT", "9223"))
    # 账号轮询配置
    account_rotation_mode: str = os.getenv("AISTUDIO_ACCOUNT_ROTATION_MODE", "round_robin")  # round_robin, lru, least_rl, exhaustion
    account_cooldown_seconds: int = int(os.getenv("AISTUDIO_ACCOUNT_COOLDOWN_SECONDS", "60"))
    account_max_retries: int = int(os.getenv("AISTUDIO_ACCOUNT_MAX_RETRIES", "3"))
    account_warmup_limit: int = int(os.getenv("AISTUDIO_ACCOUNT_WARMUP_LIMIT", "2"))
    max_concurrency: int = int(os.getenv("AISTUDIO_MAX_CONCURRENCY", "3"))
    # Pure HTTP mode: no browser needed for snapshot generation
    use_pure_http: bool = os.getenv("AISTUDIO_USE_PURE_HTTP", "0") in ("1", "true", "True")


settings = Settings()


def camoufox_proxy_identity_options() -> dict[str, object]:
    """Return stable browser identity hints for proxied Camoufox sessions."""
    return {
        "config": {
            "geolocation:latitude": settings.camoufox_geolocation_latitude,
            "geolocation:longitude": settings.camoufox_geolocation_longitude,
            "geolocation:accuracy": settings.camoufox_geolocation_accuracy,
            "timezone": settings.camoufox_timezone,
        },
        "locale": settings.camoufox_locale,
        "i_know_what_im_doing": True,
    }
