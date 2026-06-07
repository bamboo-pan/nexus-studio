import asyncio
from pathlib import Path

import httpx

from aistudio_api.api.app import app


def request(method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(send())


def test_config_route_lists_allowlisted_runtime_settings(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("AISTUDIO_CONFIG_ENV_FILE", str(env_file))

    response = request("GET", "/config")

    assert response.status_code == 200
    body = response.json()
    keys = {item["key"] for item in body["data"]}
    pure_http = next(item for item in body["data"] if item["key"] == "AISTUDIO_USE_PURE_HTTP")
    warmup_model = next(item for item in body["data"] if item["key"] == "AISTUDIO_WARMUP_TEXT_MODEL")
    assert body["env_file"] == str(env_file.resolve())
    assert "AISTUDIO_USE_PURE_HTTP" in keys
    assert pure_http["default_value"] is False
    assert warmup_model["default_value"] == "gemini-3-flash-preview"
    assert "跳过账号浏览器预热" in pure_http["description"]
    assert "AISTUDIO_DEFAULT_TEXT_MODEL" in keys
    assert "AISTUDIO_WARMUP_TEXT_MODEL" in keys
    assert "AISTUDIO_NATIVE_UI_WORKERS_PER_ACCOUNT" in keys
    assert "AISTUDIO_WARMUP_PROBE_TIMEOUT_SECONDS" in keys
    assert "AISTUDIO_PROVIDER_MANAGER_DIR" in keys
    assert "AISTUDIO_ACCOUNT_ROTATION_MODE" not in keys
    assert "AISTUDIO_AUTH_FILE" not in keys
    assert "AISTUDIO_PROXY_SERVER" not in keys


def test_config_route_saves_and_resets_boolean_value(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("AISTUDIO_CONFIG_ENV_FILE", str(env_file))

    response = request("PUT", "/config/AISTUDIO_USE_PURE_HTTP", json={"value": True})

    assert response.status_code == 200
    body = response.json()
    assert body["key"] == "AISTUDIO_USE_PURE_HTTP"
    assert body["configured_value"] is True
    assert body["configured_raw"] == "1"
    assert body["is_overridden"] is True
    assert "AISTUDIO_USE_PURE_HTTP=1" in env_file.read_text(encoding="utf-8")

    response = request("DELETE", "/config/AISTUDIO_USE_PURE_HTTP")

    assert response.status_code == 200
    body = response.json()
    assert body["configured_value"] is None
    assert body["is_overridden"] is False
    assert "AISTUDIO_USE_PURE_HTTP" not in env_file.read_text(encoding="utf-8")


def test_config_route_rejects_invalid_or_unknown_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("AISTUDIO_CONFIG_ENV_FILE", str(env_file))

    invalid = request("PUT", "/config/AISTUDIO_MAX_CONCURRENCY", json={"value": 0})
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["message"] == "value must be >= 1"
    assert not env_file.exists()

    multiline = request("PUT", "/config/AISTUDIO_DEFAULT_TEXT_MODEL", json={"value": "gemini\nsecret"})
    assert multiline.status_code == 400
    assert multiline.json()["detail"]["message"] == "value must be a single line"

    unknown = request("PUT", "/config/AISTUDIO_AUTH_FILE", json={"value": "x"})
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["type"] == "not_found"
