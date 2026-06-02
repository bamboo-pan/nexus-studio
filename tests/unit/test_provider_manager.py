import asyncio
import json

import httpx
from fastapi import FastAPI

from aistudio_api.api.routes_provider_manager import router as provider_manager_router
from aistudio_api.config import settings
from aistudio_api.infrastructure.provider_manager import ProviderManagerStore


def request_app(app: FastAPI, method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(send())


def provider_manager_app(storage_dir, monkeypatch):
    monkeypatch.setattr(settings, "provider_manager_dir", str(storage_dir))
    app = FastAPI()
    app.include_router(provider_manager_router)
    return app


def test_provider_manager_lists_builtin_google_without_credential_surface(tmp_path, monkeypatch):
    app = provider_manager_app(tmp_path, monkeypatch)

    response = request_app(app, "GET", "/api/provider-manager/providers")

    assert response.status_code == 200
    body = response.json()
    google = body["data"][0]
    assert google["id"] == "google-ai-studio"
    assert google["type"] == "google-ai-studio"
    assert google["enabled"] is True
    assert google["built_in"] is True
    assert google["deletable"] is False
    assert google["credential"] is None
    assert google["credential_ref"] is None
    assert "base_url" not in google
    assert "token" not in json.dumps(google).lower()
    assert google["health"]["status"] == "ready"

    model = google["model_catalog"][0]
    assert model["provider_id"] == "google-ai-studio"
    assert model["external_model_id"]
    assert model["display_name"]
    assert isinstance(model["capabilities"], dict)
    assert isinstance(model["modalities"], list)
    assert model["source"] == "discovered"


def test_provider_manager_custom_provider_crud_redacts_token_and_audits(tmp_path, monkeypatch):
    app = provider_manager_app(tmp_path, monkeypatch)
    secret = "unit-provider-token-value"
    create_payload = {
        "name": "Unit OpenAI",
        "base_url": "https://api.example.test/v1/",
        "timeout": 45,
        "token": secret,
        "aliases": {"fast": "unit-chat"},
        "defaults": {"text_model": "unit-chat"},
        "model_catalog": [
            {
                "external_model_id": "unit-chat",
                "display_name": "Unit Chat",
                "aliases": ["fast"],
                "defaults": {"text": True},
                "capabilities": {"text_output": True, "streaming": True, "tools": True},
                "modalities": ["text", "streaming", "tools"],
            }
        ],
    }

    created = request_app(app, "POST", "/api/provider-manager/providers", json=create_payload)

    assert created.status_code == 200
    provider = created.json()
    provider_id = provider["id"]
    credential_ref = provider["credential_ref"]
    assert provider["base_url"] == "https://api.example.test/v1"
    assert provider["credential"]["ref"] == credential_ref
    assert provider["credential"]["has_token"] is True
    assert provider["credential"]["masked"].startswith("***")
    assert secret not in created.text
    assert provider["model_catalog"][0]["provider_id"] == provider_id
    assert provider["model_catalog"][0]["source"] == "manual"
    assert provider["model_catalog"][0]["aliases"] == ["fast"]
    assert provider["health"]["status"] == "unknown"

    store_credentials = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))["data"]
    assert store_credentials[credential_ref]["token"] == secret

    disabled = request_app(app, "POST", f"/api/provider-manager/providers/{provider_id}/enabled", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["health"]["status"] == "disabled"
    assert secret not in disabled.text

    updated = request_app(
        app,
        "PATCH",
        f"/api/provider-manager/providers/{provider_id}",
        json={
            "name": "Renamed Provider",
            "enabled": True,
            "health": {"status": "ready", "message": "manual ok"},
            "model_catalog": [
                {
                    "external_model_id": "unit-chat-2",
                    "display_name": "Unit Chat 2",
                    "source": "manual",
                }
            ],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed Provider"
    assert updated.json()["health"]["status"] == "ready"
    assert [model["external_model_id"] for model in updated.json()["model_catalog"]] == ["unit-chat-2"]
    assert secret not in updated.text

    catalog = request_app(app, "GET", f"/api/provider-manager/model-catalog?provider_id={provider_id}").json()
    assert catalog["data"][0]["external_model_id"] == "unit-chat-2"
    assert catalog["data"][0]["provider_id"] == provider_id
    assert "token" not in json.dumps(catalog).lower()

    audit = request_app(app, "GET", "/api/provider-manager/audit").json()["data"]
    actions = [event["action"] for event in audit]
    assert "provider.created" in actions
    assert "provider.disabled" in actions
    assert "provider.enabled" in actions
    assert "provider.updated" in actions
    assert "provider.model_catalog.updated" in actions
    assert secret not in json.dumps(audit)
    assert all("token" not in json.dumps(event.get("summary", {})).lower() for event in audit)

    deleted = request_app(app, "DELETE", f"/api/provider-manager/providers/{provider_id}")
    assert deleted.status_code == 200
    listed_ids = [item["id"] for item in request_app(app, "GET", "/api/provider-manager/providers").json()["data"]]
    assert provider_id not in listed_ids
    assert provider_id in [event["target_id"] for event in request_app(app, "GET", "/api/provider-manager/audit").json()["data"]]


def test_provider_manager_rejects_builtin_mutation_and_multiline_token_without_leak(tmp_path, monkeypatch):
    app = provider_manager_app(tmp_path, monkeypatch)
    secret = "unit-multiline-token"

    delete_google = request_app(app, "DELETE", "/api/provider-manager/providers/google-ai-studio")
    assert delete_google.status_code == 403

    create_google = request_app(
        app,
        "POST",
        "/api/provider-manager/providers",
        json={"name": "Google Clone", "provider_type": "google-ai-studio", "base_url": "https://api.example.test/v1"},
    )
    assert create_google.status_code == 403

    invalid = request_app(
        app,
        "POST",
        "/api/provider-manager/providers",
        json={"name": "Bad", "base_url": "https://api.example.test/v1", "token": f"{secret}\nextra"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["message"] == "API token must be a single line"
    assert secret not in invalid.text

    audit = request_app(app, "GET", "/api/provider-manager/audit").json()["data"]
    assert any(event["action"] == "provider.created" and event["status"] == "failed" for event in audit)
    assert any(event["action"] == "provider.deleted" and event["status"] == "failed" for event in audit)
    assert secret not in json.dumps(audit)


def test_provider_manager_short_token_mask_does_not_expose_complete_token(tmp_path, monkeypatch):
    app = provider_manager_app(tmp_path, monkeypatch)
    secret = "abcd"

    created = request_app(
        app,
        "POST",
        "/api/provider-manager/providers",
        json={"name": "Short Secret", "base_url": "https://api.example.test/v1", "token": secret},
    )

    assert created.status_code == 200
    assert created.json()["credential"]["masked"] == "***"
    assert secret not in created.text

    credential_ref = created.json()["credential_ref"]
    credentials_path = tmp_path / "credentials.json"
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    credentials["data"][credential_ref]["masked"] = f"***{secret}"
    credentials_path.write_text(json.dumps(credentials), encoding="utf-8")

    fetched = request_app(app, "GET", f"/api/provider-manager/providers/{created.json()['id']}")

    assert fetched.status_code == 200
    assert fetched.json()["credential"]["masked"] == "***"
    assert secret not in fetched.text


def test_provider_manager_store_round_trips_custom_provider(tmp_path):
    store = ProviderManagerStore(tmp_path)
    created = store.create_provider(
        {
            "name": "Persisted",
            "base_url": "https://api.persisted.test/v1",
            "token": "unit-persisted-token",
            "model_catalog": [{"external_model_id": "persisted-model"}],
        }
    )

    reloaded = ProviderManagerStore(tmp_path).get_provider(created["id"])

    assert reloaded["id"] == created["id"]
    assert reloaded["credential_ref"] == created["credential_ref"]
    assert reloaded["credential"]["masked"].startswith("***")
    assert reloaded["model_catalog"][0]["external_model_id"] == "persisted-model"


def test_provider_manager_router_is_registered_on_main_app(tmp_path, monkeypatch):
    from aistudio_api.api.app import app as main_app

    monkeypatch.setattr(settings, "provider_manager_dir", str(tmp_path))

    response = request_app(main_app, "GET", "/api/provider-manager/providers")

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "google-ai-studio"


def test_provider_manager_save_is_not_captured_by_request_logs(tmp_path, monkeypatch):
    from aistudio_api.api.app import app as main_app
    from aistudio_api.api.state import runtime_state
    from aistudio_api.infrastructure.request_logs import RequestLogStore

    secret = "unit-request-log-secret"
    request_log_store = RequestLogStore(tmp_path / "request-logs")
    request_log_store.set_enabled(True)
    monkeypatch.setattr(settings, "provider_manager_dir", str(tmp_path / "provider-manager"))
    old_store = runtime_state.request_log_store
    runtime_state.request_log_store = request_log_store
    try:
        response = request_app(
            main_app,
            "POST",
            "/api/provider-manager/providers",
            json={"name": "No Log Secret", "base_url": "https://api.example.test/v1", "token": secret},
        )
    finally:
        runtime_state.request_log_store = old_store

    assert response.status_code == 200
    assert secret not in response.text
    assert request_log_store.count() == 0