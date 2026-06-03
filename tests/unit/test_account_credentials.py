import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from aistudio_api.api.dependencies import get_account_service
from aistudio_api.api.routes_accounts import router as accounts_router
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account import account_store as account_store_module
from aistudio_api.infrastructure.account.login_service import LoginService

from aistudio_api.infrastructure.account.account_store import BACKUP_FORMAT, AccountStore


def storage_state(cookie_name="sid", cookie_value="1", domain=".google.com", expires=None, email=None):
    cookie = {
        "name": cookie_name,
        "value": cookie_value,
        "domain": domain,
        "path": "/",
    }
    if expires is not None:
        cookie["expires"] = expires
    origins = []
    if email:
        origins = [
            {
                "origin": "https://aistudio.google.com",
                "localStorage": [{"name": "account_email", "value": email}],
            }
        ]
    return {
        "cookies": [cookie],
        "origins": origins,
    }


def request_app(app: FastAPI, method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(send())


def test_export_credentials_builds_project_backup_package(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", "main@example.com", storage_state())

    backup = store.export_credentials(account.id)

    assert backup["format"] == BACKUP_FORMAT
    assert backup["manifest"]["account_count"] == 1
    assert "sensitive" in backup["manifest"]["warning"]
    assert backup["manifest"]["accounts"][0]["email"] == "main@example.com"
    assert backup["accounts"][0]["auth"]["cookies"][0]["name"] == "sid"


def test_import_credentials_accepts_single_storage_state(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)

    imported = store.import_credentials(storage_state("sid", "2"), name="Imported")

    assert len(imported) == 1
    assert imported[0].name == "Imported"
    assert store.get_active_account().id == imported[0].id
    assert store.export_credentials(imported[0].id)["accounts"][0]["auth"]["cookies"][0]["value"] == "2"


def test_account_store_does_not_auto_migrate_legacy_root_auth_file(tmp_path, monkeypatch):
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "auth.json").write_text(json.dumps(storage_state()), encoding="utf-8")
    monkeypatch.setattr(account_store_module, "_SEARCH_ROOTS", [root])

    store = AccountStore(accounts_dir=data_dir / "accounts")

    assert store.list_accounts() == []
    assert not (data_dir / "accounts" / "acc_migrated").exists()


def test_import_credentials_infers_email_from_storage_state(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)

    imported = store.import_credentials(storage_state(email="user@example.com"))

    assert imported[0].email == "user@example.com"
    assert imported[0].name == "user@example.com"


def test_import_credentials_preserves_playwright_indexed_db_storage_state(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    state = storage_state(email="user@example.com")
    state["origins"][0]["indexedDB"] = [{"name": "oauth", "version": 1, "stores": []}]

    imported = store.import_credentials(state)

    exported = store.export_credentials(imported[0].id)
    assert exported["accounts"][0]["auth"]["origins"][0]["indexedDB"] == state["origins"][0]["indexedDB"]


def test_import_credentials_restores_backup_metadata_when_possible(tmp_path):
    source = AccountStore(accounts_dir=tmp_path / "source")
    account = source.save_account("main", "main@example.com", storage_state())
    backup = source.export_credentials(account.id)

    target = AccountStore(accounts_dir=tmp_path / "target")
    imported = target.import_credentials(backup)

    assert imported[0].id == account.id
    assert imported[0].name == "main"
    assert imported[0].email == "main@example.com"
    assert target.get_active_account().id == account.id


def test_import_credentials_rejects_legacy_backup_auth_field_aliases(tmp_path):
    source = AccountStore(accounts_dir=tmp_path / "source")
    account = source.save_account("main", "main@example.com", storage_state())

    for alias in ("storage_state", "storageState"):
        backup = source.export_credentials(account.id)
        storage = backup["accounts"][0].pop("auth")
        backup["accounts"][0][alias] = storage
        target = AccountStore(accounts_dir=tmp_path / f"target-{alias}")

        with pytest.raises(ValueError, match="account auth"):
            target.import_credentials(backup)

        assert target.list_accounts() == []


def test_import_credentials_rejects_malformed_storage_state(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)

    with pytest.raises(ValueError, match="cookies"):
        store.import_credentials({"origins": []})


def test_import_credentials_rejects_non_google_storage_state(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)

    with pytest.raises(ValueError, match="Google cookie"):
        store.import_credentials(storage_state(domain="example.com"))


def test_import_credentials_rejects_expired_google_cookie(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    expired = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()

    with pytest.raises(ValueError, match="expired"):
        store.import_credentials(storage_state(expires=expired))


def test_import_credentials_rejects_malformed_backup_package(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)

    with pytest.raises(ValueError, match="version"):
        store.import_credentials({"format": BACKUP_FORMAT, "version": 999, "accounts": []})


def test_import_credentials_validates_backup_before_saving_any_accounts(tmp_path):
    source = AccountStore(accounts_dir=tmp_path / "source")
    source.save_account("main", "main@example.com", storage_state())
    backup = source.export_credentials()
    backup["accounts"].append({"meta": {"name": "bad"}, "auth": {"origins": []}})

    target = AccountStore(accounts_dir=tmp_path / "target")

    with pytest.raises(ValueError, match="cookies"):
        target.import_credentials(backup)

    assert target.list_accounts() == []


def test_import_credentials_validates_backup_email_metadata_before_saving(tmp_path):
    source = AccountStore(accounts_dir=tmp_path / "source")
    source.save_account("main", "main@example.com", storage_state(email="main@example.com"))
    backup = source.export_credentials()
    backup["accounts"][0]["auth"] = storage_state(email="other@example.com")

    target = AccountStore(accounts_dir=tmp_path / "target")

    with pytest.raises(ValueError, match="email"):
        target.import_credentials(backup)

    assert target.list_accounts() == []


def test_export_credentials_route_marks_sensitive_response_no_store(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    store.save_account("main", "main@example.com", storage_state())
    service = AccountService(store, LoginService())
    app = FastAPI()
    app.include_router(accounts_router)
    app.dependency_overrides[get_account_service] = lambda: service

    response = request_app(app, "GET", "/accounts/export")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_import_credentials_route_rejects_invalid_json(tmp_path):
    service = AccountService(AccountStore(accounts_dir=tmp_path), LoginService())
    app = FastAPI()
    app.include_router(accounts_router)
    app.dependency_overrides[get_account_service] = lambda: service

    response = request_app(
        app,
        "POST",
        "/accounts/import",
        content="{bad json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {"message": "无效的 JSON 凭证内容", "type": "bad_request"}