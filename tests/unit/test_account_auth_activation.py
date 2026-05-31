import asyncio
from types import SimpleNamespace

from aistudio_api.api.routes_accounts import login_status
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.account.login_service import LoginSession, LoginStatus


def test_save_account_makes_saved_account_active(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    first = store.save_account("first", None, {"cookies": [{"name": "a", "value": "1", "domain": ".google.com", "path": "/"}]})
    second = store.save_account("second", None, {"cookies": [{"name": "b", "value": "2", "domain": ".google.com", "path": "/"}]})

    assert store.get_active_account().id == second.id
    assert store.get_active_auth_path() == tmp_path / second.id / "auth.json"
    assert first.id != second.id


def test_completed_login_status_activates_saved_account_auth_once():
    session = LoginSession(
        session_id="login_1",
        status=LoginStatus.COMPLETED,
        account_id="acc_1",
    )
    account = SimpleNamespace(id="acc_1", name="Account", email=None, created_at="now", last_used="now")

    class FakeAccountService:
        def __init__(self):
            self.calls = 0

        def get_login_status(self, session_id):
            assert session_id == "login_1"
            return session

        async def activate_account(self, account_id, browser_session, snapshot_cache, busy_lock):
            self.calls += 1
            assert account_id == "acc_1"
            assert browser_session == "client"
            assert snapshot_cache == "snapshot-cache"
            assert busy_lock == "busy-lock"
            return account

    service = FakeAccountService()
    runtime_state = SimpleNamespace(
        client="client",
        snapshot_cache="snapshot-cache",
        busy_lock="busy-lock",
    )

    async def run_check():
        response = await login_status("login_1", account_service=service, runtime_state=runtime_state)
        response_again = await login_status("login_1", account_service=service, runtime_state=runtime_state)
        return response, response_again

    response, response_again = asyncio.run(run_check())

    assert response.status == LoginStatus.COMPLETED.value
    assert response_again.status == LoginStatus.COMPLETED.value
    assert session.auth_activated is True
    assert service.calls == 1
