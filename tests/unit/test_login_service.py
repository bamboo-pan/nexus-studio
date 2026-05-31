import asyncio
import sys
from types import SimpleNamespace

from aistudio_api.infrastructure.account import login_service as login_module
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.account.login_service import LOGIN_IDENTITY_ERROR, LoginService, LoginSession, LoginStatus


def storage_state(email=None):
    origins = []
    if email:
        origins = [
            {
                "origin": "https://aistudio.google.com",
                "localStorage": [{"name": "account_email", "value": email}],
            }
        ]
    return {
        "cookies": [{"name": "sid", "value": "1", "domain": ".google.com", "path": "/"}],
        "origins": origins,
    }


class FakeManager:
    def __init__(self, port, headless):
        self.port = port
        self.headless = headless
        self.stopped = False

    async def start(self):
        return "ws://fake-browser"

    async def stop(self):
        self.stopped = True


class FakePage:
    def __init__(self, email=None):
        self.email = email
        self.handlers = {}
        self.urls = []

    def on(self, event, handler):
        self.handlers[event] = handler

    async def goto(self, url, **kwargs):
        self.urls.append((url, kwargs))
        if "ServiceLogin" in url and "framenavigated" in self.handlers:
            await self.handlers["framenavigated"](SimpleNamespace(url="https://aistudio.google.com/app"))

    async def evaluate(self, script):
        return self.email


class FakeContext:
    def __init__(self, page, state):
        self.page = page
        self.state = state

    async def new_page(self):
        return self.page

    async def storage_state(self):
        return self.state


class FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.closed = False

    async def new_context(self):
        return self.context

    async def close(self):
        self.closed = True


class FakeFirefox:
    def __init__(self, browser):
        self.browser = browser

    async def connect(self, ws_endpoint):
        assert ws_endpoint == "ws://fake-browser"
        return self.browser


class FakePlaywright:
    def __init__(self, browser):
        self.firefox = FakeFirefox(browser)
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakePlaywrightStarter:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


def install_browser_fakes(monkeypatch, page, state):
    context = FakeContext(page, state)
    browser = FakeBrowser(context)
    playwright = FakePlaywright(browser)
    original_sleep = asyncio.sleep
    monkeypatch.setattr(login_module, "CamoufoxManager", FakeManager)
    monkeypatch.setattr(login_module.asyncio, "sleep", lambda delay: original_sleep(0))
    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(async_playwright=lambda: FakePlaywrightStarter(playwright)),
    )


def run_login_worker(service, session_id, store):
    asyncio.run(service._login_worker(session_id, store, None))


def test_login_worker_rejects_google_cookies_without_account_identity(tmp_path, monkeypatch):
    service = LoginService()
    session_id = "login_no_identity"
    service._sessions[session_id] = LoginSession(session_id=session_id)
    store = AccountStore(accounts_dir=tmp_path)
    install_browser_fakes(monkeypatch, FakePage(email=None), storage_state())

    run_login_worker(service, session_id, store)

    session = service.get_status(session_id)
    assert session.status == LoginStatus.FAILED
    assert session.error == LOGIN_IDENTITY_ERROR
    assert session.account_id is None
    assert store.list_accounts() == []


def test_login_worker_saves_verified_identity_without_immediate_activation(tmp_path, monkeypatch):
    service = LoginService()
    session_id = "login_verified"
    service._sessions[session_id] = LoginSession(session_id=session_id)
    store = AccountStore(accounts_dir=tmp_path)
    install_browser_fakes(monkeypatch, FakePage(email="user@example.com"), storage_state())

    run_login_worker(service, session_id, store)

    session = service.get_status(session_id)
    assert session.status == LoginStatus.COMPLETED
    assert session.account_id is not None
    assert session.email == "user@example.com"
    account = store.get_account(session.account_id)
    assert account is not None
    assert account.email == "user@example.com"
    assert store.get_active_account() is None
