"""System and metadata routes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from dotenv import dotenv_values, set_key, unset_key

from aistudio_api.application.api_service import health_response, stats_response
from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.config import DEFAULT_IMAGE_MODEL, DEFAULT_RUNTIME_DATA_DIR, DEFAULT_TEXT_MODEL, DEFAULT_TMP_DIR, DEFAULT_WARMUP_TEXT_MODEL, settings

router = APIRouter()


@dataclass(frozen=True, slots=True)
class ConfigOption:
    key: str
    label: str
    category: str
    category_label: str
    value_type: str
    default: Any
    description: str
    settings_attr: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()
    restart_required: bool = True


class ConfigValueRequest(BaseModel):
    value: Any


CONFIG_CATEGORIES = (
    ("runtime", "运行模式"),
    ("server", "服务与浏览器"),
    ("timeouts", "超时与缓存"),
    ("identity", "浏览器身份"),
    ("storage", "存储路径"),
    ("accounts", "账号调度"),
)


CONFIG_OPTIONS: tuple[ConfigOption, ...] = (
    ConfigOption("AISTUDIO_USE_PURE_HTTP", "Pure HTTP 模式", "runtime", "运行模式", "bool", False, "启用实验性纯 HTTP 请求路径；开启后不启动浏览器，也会跳过账号浏览器预热。", "use_pure_http"),
    ConfigOption("AISTUDIO_DEFAULT_TEXT_MODEL", "默认文本模型", "runtime", "运行模式", "string", "gemma-4-31b-it", "CLI 和内部默认文本模型。"),
    ConfigOption("AISTUDIO_WARMUP_TEXT_MODEL", "预热文本模型", "runtime", "运行模式", "string", "gemini-3-flash-preview", "启动账号浏览器预热时用于捕获可复用文本请求模板的模型。"),
    ConfigOption("AISTUDIO_DEFAULT_IMAGE_MODEL", "默认图片模型", "runtime", "运行模式", "string", "gemini-3.1-flash-image-preview", "未显式指定时使用的图片模型。"),
    ConfigOption("AISTUDIO_MAX_CONCURRENCY", "最大并发", "runtime", "运行模式", "int", 3, "后端请求并发信号量大小。", "max_concurrency", minimum=1, maximum=100),
    ConfigOption("AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT", "账号 Native UI worker 数", "runtime", "运行模式", "int", 3, "每个账号可复用的独立 Native UI worker 进程数；用于账号态文本生成，保持干净进程隔离并支持同账号并发。", "native_ui_workers_per_account", minimum=1, maximum=20),
    ConfigOption("AISTUDIO_PORT", "API 服务端口", "server", "服务与浏览器", "int", 8080, "FastAPI 服务监听端口。", "port", minimum=1, maximum=65535),
    ConfigOption("AISTUDIO_CAMOUFOX_PORT", "Camoufox 调试端口", "server", "服务与浏览器", "int", 9222, "主浏览器调试端口。", "camoufox_port", minimum=1, maximum=65535),
    ConfigOption("AISTUDIO_LOGIN_CAMOUFOX_PORT", "登录浏览器端口", "server", "服务与浏览器", "int", 9223, "账号登录流程使用的浏览器调试端口。", "login_camoufox_port", minimum=1, maximum=65535),
    ConfigOption("AISTUDIO_CAMOUFOX_HEADLESS", "无头浏览器", "server", "服务与浏览器", "bool", True, "是否以 headless 模式启动 Camoufox。", "camoufox_headless"),
    ConfigOption("AISTUDIO_CAMOUFOX_PYTHON", "Camoufox Python", "server", "服务与浏览器", "path", "", "可选的 Camoufox Python 解释器路径。", "camoufox_python"),
    ConfigOption("AISTUDIO_TIMEOUT_REPLAY", "Replay 超时", "timeouts", "超时与缓存", "int", 120, "浏览器 replay 请求超时秒数。", "timeout_replay", minimum=1, maximum=3600),
    ConfigOption("AISTUDIO_TIMEOUT_STREAM", "Stream 超时", "timeouts", "超时与缓存", "int", 120, "流式请求超时秒数。", "timeout_stream", minimum=1, maximum=3600),
    ConfigOption("AISTUDIO_TIMEOUT_CAPTURE", "Capture 超时", "timeouts", "超时与缓存", "int", 30, "请求快照捕获超时秒数。", "timeout_capture", minimum=1, maximum=3600),
    ConfigOption("AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS", "预热 Probe 超时", "timeouts", "超时与缓存", "int", 30, "启动账号 native UI worker 预热 GenerateContent probe 的单次请求超时秒数。", "warmup_probe_timeout_seconds", minimum=1, maximum=3600),
    ConfigOption("AISTUDIO_SNAPSHOT_CACHE_TTL", "快照缓存 TTL", "timeouts", "超时与缓存", "int", 3600, "浏览器快照缓存保留秒数。", "snapshot_cache_ttl", minimum=0),
    ConfigOption("AISTUDIO_SNAPSHOT_CACHE_MAX", "快照缓存上限", "timeouts", "超时与缓存", "int", 100, "最多保留的浏览器快照数量。", "snapshot_cache_max", minimum=0),
    ConfigOption("AISTUDIO_CAMOUFOX_LOCALE", "浏览器 Locale", "identity", "浏览器身份", "string", "en-US", "代理浏览器使用的 locale。", "camoufox_locale"),
    ConfigOption("AISTUDIO_CAMOUFOX_TIMEZONE", "浏览器时区", "identity", "浏览器身份", "string", "America/Los_Angeles", "代理浏览器使用的 timezone。", "camoufox_timezone"),
    ConfigOption("AISTUDIO_CAMOUFOX_GEOLOCATION_LATITUDE", "地理位置纬度", "identity", "浏览器身份", "float", 37.7749, "代理浏览器暴露的纬度。", "camoufox_geolocation_latitude", minimum=-90, maximum=90),
    ConfigOption("AISTUDIO_CAMOUFOX_GEOLOCATION_LONGITUDE", "地理位置经度", "identity", "浏览器身份", "float", -122.4194, "代理浏览器暴露的经度。", "camoufox_geolocation_longitude", minimum=-180, maximum=180),
    ConfigOption("AISTUDIO_CAMOUFOX_GEOLOCATION_ACCURACY", "定位精度", "identity", "浏览器身份", "int", 100, "代理浏览器地理位置精度，单位米。", "camoufox_geolocation_accuracy", minimum=0),
    ConfigOption("AISTUDIO_CAMOUFOX_GEOIP", "代理自动地理位置", "identity", "浏览器身份", "bool", False, "代理浏览器自动按出口 IP 设置 timezone、locale 和地理位置；首次启用会下载 GeoIP 数据库，关闭后使用手动配置。", "camoufox_geoip"),
    ConfigOption("AISTUDIO_TMP_DIR", "临时目录", "storage", "存储路径", "path", str(DEFAULT_TMP_DIR), "上传、下载和中间文件临时目录。", "tmp_dir"),
    ConfigOption("AISTUDIO_REQUEST_LOGS_DIR", "请求记录目录", "storage", "存储路径", "path", str(DEFAULT_RUNTIME_DATA_DIR / "request-logs"), "请求记录 JSON 文件目录；开关已在请求记录页配置。", "request_logs_dir"),
    ConfigOption("AISTUDIO_GENERATED_IMAGES_DIR", "生成图片目录", "storage", "存储路径", "path", str(DEFAULT_RUNTIME_DATA_DIR / "generated-images"), "生成图片文件保存目录。", "generated_images_dir"),
    ConfigOption("AISTUDIO_GENERATED_IMAGES_ROUTE", "生成图片路由", "storage", "存储路径", "string", "/generated-images", "生成图片静态访问路由。", "generated_images_route"),
    ConfigOption("AISTUDIO_IMAGE_SESSIONS_DIR", "图片会话目录", "storage", "存储路径", "path", str(DEFAULT_RUNTIME_DATA_DIR / "image-sessions"), "图片 Studio 会话保存目录。", "image_sessions_dir"),
    ConfigOption("AISTUDIO_LOCAL_STUDIO_DIR", "Local Studio 目录", "storage", "存储路径", "path", str(DEFAULT_RUNTIME_DATA_DIR / "local-studio"), "Local Studio 会话、附件和缓存目录。", "local_studio_dir"),
    ConfigOption("AISTUDIO_PROVIDER_MANAGER_DIR", "Provider Manager 目录", "storage", "存储路径", "path", str(DEFAULT_RUNTIME_DATA_DIR / "provider-manager"), "Provider Manager provider registry、credential references、model catalog 和 audit 记录目录。", "provider_manager_dir"),
    ConfigOption("AISTUDIO_ACCOUNTS_DIR", "账号目录", "storage", "存储路径", "path", "", "账号元数据和授权状态目录。", "accounts_dir"),
    ConfigOption("AISTUDIO_DUMP_RAW_RESPONSE", "转储原始响应", "storage", "存储路径", "bool", False, "调试时保存上游原始响应。", "dump_raw_response"),
    ConfigOption("AISTUDIO_DUMP_RAW_RESPONSE_DIR", "原始响应目录", "storage", "存储路径", "path", "/tmp", "原始响应转储目录。", "dump_raw_response_dir"),
    ConfigOption("AISTUDIO_ACCOUNT_MAX_RETRIES", "账号最大重试", "accounts", "账号调度", "int", 3, "账号调度失败后的最大重试次数。", "account_max_retries", minimum=0, maximum=20),
    ConfigOption("AISTUDIO_ACCOUNT_WARMUP_LIMIT", "启动预热账号数", "accounts", "账号调度", "int", 2, "服务启动时预热的账号浏览器数量；仅在 Pure HTTP 关闭且已有账号时生效。", "account_warmup_limit", minimum=0, maximum=20),
)


CONFIG_OPTION_BY_KEY = {option.key: option for option in CONFIG_OPTIONS}


def _config_env_file() -> Path:
    override = os.getenv("AISTUDIO_CONFIG_ENV_FILE")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / ".env"


def _read_env_values() -> dict[str, str | None]:
    env_file = _config_env_file()
    if not env_file.exists():
        return {}
    return dict(dotenv_values(env_file))


def _error_detail(message: str, error_type: str = "bad_request") -> dict[str, str]:
    return {"message": message, "type": error_type}


def _coerce_config_value(option: ConfigOption, raw_value: Any) -> tuple[Any, str]:
    if raw_value is None:
        raise ValueError("value is required")
    if isinstance(raw_value, str) and ("\n" in raw_value or "\r" in raw_value):
        raise ValueError("value must be a single line")

    if option.value_type == "bool":
        if isinstance(raw_value, bool):
            value = raw_value
        else:
            normalized = str(raw_value).strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                value = True
            elif normalized in {"0", "false", "no", "off"}:
                value = False
            else:
                raise ValueError("value must be a boolean")
        return value, "1" if value else "0"

    if option.value_type == "int":
        if isinstance(raw_value, bool):
            raise ValueError("value must be an integer")
        value = int(str(raw_value).strip())
        if option.minimum is not None and value < option.minimum:
            raise ValueError(f"value must be >= {int(option.minimum)}")
        if option.maximum is not None and value > option.maximum:
            raise ValueError(f"value must be <= {int(option.maximum)}")
        return value, str(value)

    if option.value_type == "float":
        if isinstance(raw_value, bool):
            raise ValueError("value must be a number")
        value = float(str(raw_value).strip())
        if option.minimum is not None and value < option.minimum:
            raise ValueError(f"value must be >= {option.minimum:g}")
        if option.maximum is not None and value > option.maximum:
            raise ValueError(f"value must be <= {option.maximum:g}")
        return value, str(value)

    value = str(raw_value).strip()
    if option.options and value not in option.options:
        raise ValueError(f"value must be one of: {', '.join(option.options)}")
    return value, value


def _current_config_value(option: ConfigOption) -> Any:
    if option.key == "AISTUDIO_DEFAULT_TEXT_MODEL":
        return DEFAULT_TEXT_MODEL
    if option.key == "AISTUDIO_WARMUP_TEXT_MODEL":
        return DEFAULT_WARMUP_TEXT_MODEL
    if option.key == "AISTUDIO_DEFAULT_IMAGE_MODEL":
        return DEFAULT_IMAGE_MODEL
    if option.settings_attr:
        value = getattr(settings, option.settings_attr)
        return "" if value is None else value
    return option.default


def _configured_config_value(option: ConfigOption, env_values: dict[str, str | None]) -> tuple[Any | None, str | None, str | None]:
    if option.key not in env_values or env_values[option.key] is None:
        return None, None, None
    raw_value = env_values[option.key]
    try:
        value, serialized = _coerce_config_value(option, raw_value)
        return value, serialized, None
    except ValueError as exc:
        return raw_value, str(raw_value), str(exc)


def _config_item(option: ConfigOption, env_values: dict[str, str | None]) -> dict[str, Any]:
    current_value = _current_config_value(option)
    configured_value, serialized, configured_error = _configured_config_value(option, env_values)
    is_overridden = configured_value is not None
    value = configured_value if is_overridden else current_value
    pending_restart = bool(option.restart_required and is_overridden and configured_value != current_value and configured_error is None)
    item = {
        "key": option.key,
        "label": option.label,
        "category": option.category,
        "category_label": option.category_label,
        "type": option.value_type,
        "description": option.description,
        "default_value": option.default,
        "current_value": current_value,
        "configured_value": configured_value,
        "configured_raw": serialized,
        "configured_error": configured_error,
        "value": value,
        "is_overridden": is_overridden,
        "restart_required": option.restart_required,
        "pending_restart": pending_restart,
    }
    if option.minimum is not None:
        item["minimum"] = option.minimum
    if option.maximum is not None:
        item["maximum"] = option.maximum
    if option.options:
        item["options"] = list(option.options)
    return item


def _config_response() -> dict[str, Any]:
    env_values = _read_env_values()
    items = [_config_item(option, env_values) for option in CONFIG_OPTIONS]
    groups = []
    for category_id, category_label in CONFIG_CATEGORIES:
        group_items = [item for item in items if item["category"] == category_id]
        if group_items:
            groups.append({"id": category_id, "label": category_label, "items": group_items})
    return {
        "env_file": str(_config_env_file()),
        "groups": groups,
        "data": items,
    }


def _get_config_option(key: str) -> ConfigOption:
    option = CONFIG_OPTION_BY_KEY.get(key)
    if option is None:
        raise HTTPException(status_code=404, detail=_error_detail("configuration key is not editable", "not_found"))
    return option


def _write_config_value(option: ConfigOption, value: Any) -> dict[str, Any]:
    _, serialized = _coerce_config_value(option, value)
    env_file = _config_env_file()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.touch(exist_ok=True)
    set_key(str(env_file), option.key, serialized, quote_mode="auto")
    return _config_item(option, _read_env_values())


def _delete_config_value(option: ConfigOption) -> dict[str, Any]:
    env_file = _config_env_file()
    if env_file.exists():
        unset_key(str(env_file), option.key)
    return _config_item(option, _read_env_values())


@router.get("/health")
async def health():
    return health_response()


@router.get("/stats")
async def stats():
    return stats_response()


@router.get("/config")
async def get_config():
    return _config_response()


@router.put("/config/{key}")
async def set_config_value(key: str, req: ConfigValueRequest):
    option = _get_config_option(key)
    try:
        return _write_config_value(option, req.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.delete("/config/{key}")
async def reset_config_value(key: str):
    option = _get_config_option(key)
    return _delete_config_value(option)


# ========== 轮询管理 API ==========

class RotationModeRequest(BaseModel):
    mode: str  # round_robin(balanced), lru, least_rl, exhaustion
    cooldown_seconds: int | None = None


@router.get("/rotation")
async def get_rotation_status(runtime_state=Depends(get_runtime_state)):
    """获取轮询状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        return {"enabled": False, "message": "轮询器未初始化"}

    return {
        "enabled": True,
        "mode": rotator.mode.value,
        "cooldown_seconds": rotator.cooldown_seconds,
        "accounts": rotator.get_all_stats(),
    }


@router.post("/rotation/mode")
async def set_rotation_mode(
    req: RotationModeRequest,
    runtime_state=Depends(get_runtime_state),
):
    """设置轮询模式。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="轮询器未初始化")

    try:
        from aistudio_api.application.account_rotator import RotationMode
        rotator.mode = RotationMode(req.mode)
        if req.cooldown_seconds is not None:
            rotator.cooldown_seconds = req.cooldown_seconds
        return {
            "ok": True,
            "mode": rotator.mode.value,
            "cooldown_seconds": rotator.cooldown_seconds,
        }
    except ValueError:
        raise HTTPException(400, detail=f"无效的轮询模式: {req.mode}，可选: round_robin(均衡模式), lru, least_rl, exhaustion")


@router.get("/rotation/accounts")
async def get_rotation_accounts(runtime_state=Depends(get_runtime_state)):
    """获取所有账号的轮询统计。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="轮询器未初始化")

    return rotator.get_all_stats()


@router.post("/rotation/next")
async def force_next_account(runtime_state=Depends(get_runtime_state)):
    """强制切换到下一个可用账号。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="轮询器未初始化")

    # 获取下一个账号
    active = runtime_state.account_service.get_active_account() if runtime_state.account_service else None
    next_account = await rotator.get_next_account(exclude_account_id=active.id if active else None)
    if next_account is None:
        raise HTTPException(404, detail="没有可用的账号")

    # 切换账号
    account_service = runtime_state.account_service
    client = runtime_state.client
    busy_lock = runtime_state.busy_lock

    if not all([account_service, client, busy_lock]):
        raise HTTPException(503, detail="服务未就绪")

    result = await account_service.activate_account(
        next_account.id,
        client,
        runtime_state.snapshot_cache,
        busy_lock,
        keep_snapshot_cache=False,
    )

    if result is None:
        raise HTTPException(500, detail="切换失败")

    return {
        "ok": True,
        "account": {
            "id": result.id,
            "name": result.name,
            "email": result.email,
        },
    }

