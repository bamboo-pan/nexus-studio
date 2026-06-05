import base64
import asyncio
import json
import subprocess

import pytest

import aistudio_api.infrastructure.gateway.session as session_module
from aistudio_api.config import settings
from aistudio_api.domain.errors import ModelNotFoundError
from aistudio_api.infrastructure.account import tier_detector
from aistudio_api.infrastructure.account.tier_detector import AccountTier, TierResult
from aistudio_api.infrastructure.gateway.session import (
    AI_STUDIO_URL,
    AI_STUDIO_URL_FALLBACK,
    AI_STUDIO_URL_LEGACY_FALLBACK,
    BrowserSession,
    _aistudio_chat_urls,
)


class FakeRequest:
    def __init__(self, url: str, post_data: str, headers: dict[str, str]):
        self.url = url
        self.post_data = post_data
        self.headers = headers


class FakeRoute:
    def __init__(self, request: FakeRequest):
        self.request = request
        self.aborted = False
        self.continued = False
        self.continue_kwargs: dict[str, object] = {}

    def abort(self):
        self.aborted = True

    def continue_(self, **kwargs):
        self.continued = True
        self.continue_kwargs = dict(kwargs)
        post_data = kwargs.get("post_data")
        if post_data is not None:
            self.request.post_data = post_data


class FakeResponse:
    def __init__(self, *, url: str, status: int, body: bytes | str, request: FakeRequest):
        self.url = url
        self.status = status
        self.request = request
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def body(self):
        return self._body

    def text(self):
        return self._body.decode("utf-8", errors="replace")


class FakeTextArea:
    def __init__(self, page: "FakePage"):
        self.page = page

    def fill(self, value: str):
        if self.page.redirect_on_next_fill:
            self.page.redirect_on_next_fill = False
            self.page.url = "https://ai.google.dev/gemini-api/docs/available-regions"
            self.page.has_default_makersuite = False
            self.page.has_textarea = False
            raise RuntimeError("Element is not attached to the DOM")
        self.page.filled_texts.append(value)

    def click(self):
        self.page.textarea_clicks += 1

    def focus(self):
        self.page.textarea_focuses += 1


class FakeButton:
    def __init__(self, page: "FakePage"):
        self.page = page

    def click(self):
        self.page.clicks += 1
        self.page.trigger_generate_content_request()

    def evaluate(self, script: str, *args):
        return self.page.button_aria_disabled


class FakeKeyboard:
    def __init__(self, page: "FakePage"):
        self.page = page

    def press(self, key: str):
        self.page.keyboard_presses.append(key)
        if self.page.keyboard_triggers_send:
            self.page.trigger_generate_content_request()


class FakePage:
    def __init__(
        self,
        url: str = "about:blank",
        *,
        has_default_makersuite: bool = False,
        has_textarea: bool = False,
        title: str = "AI Studio",
        body: str = "",
        ready_after_waits: int | None = None,
        install_results: list[str] | None = None,
        button_selectors: set[str] | None = None,
        goto_redirect_url: str | None = None,
        goto_error: Exception | None = None,
        redirect_on_next_fill: bool = False,
        keyboard_triggers_send: bool = False,
        generate_content_url: str = "https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
        botguard_on_send: bool = False,
        button_aria_disabled: bool = False,
        js_send_result: dict[str, object] | None = None,
        js_body_hook_enabled: bool = True,
        text_model_open_result: dict[str, object] | None = None,
        text_model_select_result: dict[str, object] | None = None,
    ):
        self.url = url
        self.has_default_makersuite = has_default_makersuite
        self.has_textarea = has_textarea
        self.title_text = title
        self.body = body
        self.ready_after_waits = ready_after_waits
        self.install_results = list(install_results or [])
        self.button_selectors = {"button:has-text('Run')"} if button_selectors is None else button_selectors
        self.goto_redirect_url = goto_redirect_url
        self.goto_error = goto_error
        self.redirect_on_next_fill = redirect_on_next_fill
        self.keyboard_triggers_send = keyboard_triggers_send
        self.botguard_on_send = botguard_on_send
        self.button_aria_disabled = button_aria_disabled
        self.js_send_result = js_send_result
        self.js_body_hook_enabled = js_body_hook_enabled
        self.text_model_open_result = text_model_open_result or {"opened": True, "label": "Gemini 3.5 Flash"}
        self.text_model_select_result = text_model_select_result or {"selected": True, "label": "Gemini 3.5 Flash"}
        self.text_model_open_calls = 0
        self.text_model_select_calls = 0
        self.goto_urls: list[str] = []
        self.goto_kwargs: list[dict[str, object]] = []
        self.wait_calls: list[int] = []
        self.evaluate_calls: list[str] = []
        self.filled_texts: list[str] = []
        self.textarea_clicks = 0
        self.textarea_focuses = 0
        self.clicks = 0
        self.js_send_clicks = 0
        self.keyboard_presses: list[str] = []
        self.has_bg_service = False
        self.route_handlers: list[tuple[str, object]] = []
        self.unroute_calls: list[str] = []
        self.routed_requests: list[FakeRoute] = []
        self.response_handlers: list[object] = []
        self.pending_body: str | None = None
        self.hooked = False
        self.last_hook_url = ""
        self.generate_content_response_status = 200
        self.generate_content_response_body = b"native-ok"
        self.browser_fetch_requests: list[dict[str, object]] = []
        self.browser_fetch_response_status = 200
        self.browser_fetch_response_body: bytes | str = b"native-ok"
        self.browser_fetch_error: str | None = None
        self.generate_content_body = json.dumps(
            ["gemini-3-flash-preview", {"payload": "x" * 120}, None, None, "snapshot"],
            ensure_ascii=False,
        )
        self.generate_content_headers = {"authorization": "Bearer token", "content-type": "application/json"}
        self.generate_content_url = generate_content_url
        self.generate_content_response_sequence: list[tuple[int, bytes | str]] | None = None
        self.keyboard = FakeKeyboard(self)
        self.closed = False

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def goto(self, url: str, **kwargs):
        self.url = self.goto_redirect_url or url
        self.goto_urls.append(url)
        self.goto_kwargs.append(kwargs)
        if self.goto_error is not None:
            raise self.goto_error

    def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)
        if self.ready_after_waits is not None and len(self.wait_calls) >= self.ready_after_waits:
            self.has_default_makersuite = True
            self.has_textarea = True

    def evaluate(self, script: str, *args):
        self.evaluate_calls.append(script)
        if script == "mw:!!window.__bg_service":
            return self.has_bg_service
        if script == "mw:!!window.__hooked":
            return self.hooked
        if "__api_transport_hooked" in script:
            if "lastUrl" in script:
                return {
                    "hooked": self.hooked,
                    "transport": True,
                    "lastUrl": self.last_hook_url,
                    "lastTransport": "fetch" if self.hooked else "",
                }
            return "transport_hooked"
        if "__api_browser_fetch_replay" in script and args:
            request = dict(args[0])
            self.browser_fetch_requests.append(request)
            if self.browser_fetch_error:
                return {"error": self.browser_fetch_error}
            body = self.browser_fetch_response_body
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            return {
                "status": self.browser_fetch_response_status,
                "body": body,
                "url": request.get("url"),
                "headers": {"content-type": "application/json"},
            }
        if "__bg_hooked" in script and "snapKey" in script:
            return self.install_results.pop(0) if self.install_results else "hooked:snapshotKey"
        if "window.__pending_body" in script and args:
            self.pending_body = args[0]
            self.hooked = False
            self.last_hook_url = ""
            return None
        if "window.__pending_body = null" in script:
            self.pending_body = None
            return None
        if "model_picker_not_found" in script:
            self.text_model_open_calls += 1
            return self.text_model_open_result
        if "text_model_not_found" in script:
            self.text_model_select_calls += 1
            return self.text_model_select_result
        if "matchesSendIntent" in script:
            result = self.js_send_result
            if result is not None:
                if args and args[0] is True and result.get("found"):
                    self.js_send_clicks += 1
                    if result.get("clicked"):
                        self.trigger_generate_content_request()
                return result
            return {"found": False, "clicked": False, "label": "", "reason": "fake_no_js_button"}
        if "window.default_MakerSuite" in script:
            return self.has_default_makersuite
        if "document.body" in script:
            return self.body
        if "HTMLTextAreaElement.prototype" in script:
            return True
        return None

    def query_selector(self, selector: str):
        if selector == "textarea" and self.has_textarea:
            return FakeTextArea(self)
        if selector in self.button_selectors:
            return FakeButton(self)
        return None

    def title(self):
        return self.title_text

    def route(self, pattern: str, handler):
        self.route_handlers.append((pattern, handler))

    def unroute(self, pattern: str, handler):
        self.unroute_calls.append(pattern)
        self.route_handlers = [(p, h) for p, h in self.route_handlers if p != pattern or h is not handler]

    def on(self, event_name: str, handler):
        if event_name == "response":
            self.response_handlers.append(handler)

    def remove_listener(self, event_name: str, handler):
        if event_name == "response":
            self.response_handlers = [existing for existing in self.response_handlers if existing is not handler]

    def trigger_generate_content_request(self):
        if self.botguard_on_send:
            self.has_bg_service = True
        body = self.generate_content_body
        if self.pending_body is not None and self.js_body_hook_enabled:
            body = self.pending_body
            self.pending_body = None
            self.hooked = True
            self.last_hook_url = self.generate_content_url
        request = FakeRequest(
            self.generate_content_url,
            body,
            self.generate_content_headers,
        )
        route = FakeRoute(request)
        self.routed_requests.append(route)
        for _, handler in list(self.route_handlers):
            handler(route)
        if route.aborted:
            return
        responses = self.generate_content_response_sequence or [
            (self.generate_content_response_status, self.generate_content_response_body)
        ]
        for status, body in responses:
            response = FakeResponse(
                url=self.generate_content_url,
                status=status,
                body=body,
                request=request,
            )
            for handler in list(self.response_handlers):
                handler(response)


class BrowserSessionForTest(BrowserSession):
    def __init__(self, page: FakePage):
        super().__init__(port=0)
        self._hook_page = page
        self.goto_calls = 0
        self.install_calls = 0

    def _ensure_browser_sync(self):
        return object()

    def _goto_aistudio_sync(self, page) -> None:
        self.goto_calls += 1
        page.url = AI_STUDIO_URL
        page.has_default_makersuite = True
        page.has_textarea = True

    def _install_hooks_sync(self, page) -> None:
        self.install_calls += 1


class FakeAPIResponse:
    def __init__(self, *, status: int = 200, body: bytes = b"context-ok"):
        self.status = status
        self._body = body

    def body(self):
        return self._body


class FakeAPIRequestContext:
    def __init__(self):
        self.posts: list[dict[str, object]] = []
        self.response = FakeAPIResponse()

    def post(self, url: str, *, data: str, headers: dict[str, str], timeout: int):
        self.posts.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return self.response


class FakeBrowserContext:
    def __init__(self, pages: list[FakePage] | None = None, new_pages: list[FakePage] | None = None):
        self.pages = list(pages or [])
        self.new_pages = list(new_pages or [])
        self.created_pages: list[FakePage] = []
        self.init_scripts: list[str] = []
        self.request = FakeAPIRequestContext()
        self.closed = False

    def add_init_script(self, *, script: str):
        self.init_scripts.append(script)

    def new_page(self):
        page = self.new_pages.pop(0) if self.new_pages else FakePage()
        self.created_pages.append(page)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True
        for page in self.pages:
            page.close()


class FakeBrowser:
    def __init__(self, contexts: list[FakeBrowserContext]):
        self.contexts = list(contexts)
        self.new_context_calls: list[dict[str, object]] = []

    def new_context(self, **kwargs):
        self.new_context_calls.append(dict(kwargs))
        return self.contexts.pop(0)


class BrowserSessionWithCleanProbePageForTest(BrowserSessionForTest):
    def __init__(self, hook_page: FakePage, probe_page: FakePage):
        super().__init__(hook_page)
        self._probe_page = probe_page
        self._ctx = FakeBrowserContext(pages=[hook_page], new_pages=[probe_page])
        self.goto_route_states: list[tuple[str | None, set[str]]] = []

    def _goto_aistudio_sync(self, page) -> None:
        self.goto_route_states.append((self._preferred_chat_url, set(self._failed_chat_urls)))
        super()._goto_aistudio_sync(page)

    def _ensure_browser_sync(self, navigation_timeout_ms: int | None = None, chat_ready_timeout_ms: int | None = None):
        return self._ctx


class BrowserSessionWithIsolatedProbeContextForTest(BrowserSessionForTest):
    def __init__(self, hook_page: FakePage, probe_context: FakeBrowserContext):
        super().__init__(hook_page)
        self._browser = FakeBrowser([probe_context])

    def _ensure_browser_process_sync(self):
        return self._browser


class SnapshotRetrySessionForTest(BrowserSession):
    def __init__(self, snapshots):
        super().__init__(port=0)
        self.snapshots = list(snapshots)
        self.wait_calls = []

    def _generate_snapshot_once_sync(self, contents):
        return self.snapshots.pop(0)

    def _ensure_botguard_service_sync(self):
        return self

    def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)


class FakeTierContext:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeTierBrowser:
    def __init__(self):
        self.storage_states = []
        self.contexts = []

    def new_context(self, **kwargs):
        self.storage_states.append(kwargs.get("storage_state"))
        context = FakeTierContext()
        self.contexts.append(context)
        return context


class NewContextBrowser:
    def __init__(self):
        self.calls = []
        self.contexts = []

    def new_context(self, **kwargs):
        self.calls.append(dict(kwargs))
        context = FakeBrowserContext()
        self.contexts.append(context)
        return context


class TierDetectionSessionForTest(BrowserSession):
    def __init__(self):
        super().__init__(port=0)
        self.browser = FakeTierBrowser()
        self.ensure_process_calls = 0

    def _ensure_browser_process_sync(self):
        self.ensure_process_calls += 1
        self._browser = self.browser
        return self.browser

    def _ensure_browser_sync(self):
        raise AssertionError("tier detection for an explicit auth file must not preheat the active browser context")


class TemplateCaptureSessionForTest(BrowserSessionForTest):
    def __init__(self, page: FakePage):
        super().__init__(page)
        self.wait_until_idle_calls = 0

    def _ensure_botguard_service_sync(self):
        return self._hook_page

    def _wait_until_idle_sync(self, page) -> None:
        self.wait_until_idle_calls += 1


def test_chat_url_detection_requires_aistudio_chat_route():
    session = BrowserSession(port=0)

    assert session._is_aistudio_chat_url("https://aistudio.google.com/u/0/prompts/new_chat")
    assert session._is_aistudio_chat_url("https://aistudio.google.com/u/2/prompts/new_chat")
    assert session._is_aistudio_chat_url("https://aistudio.google.com/prompts/new_chat")
    assert session._is_aistudio_chat_url("https://aistudio.google.com/app/prompts/new_chat")
    assert session._is_aistudio_chat_url("https://aistudio.google.com/prompts/abc123")
    assert not session._is_aistudio_chat_url("https://aistudio.google.com/app/apikey")
    assert not session._is_aistudio_chat_url("https://accounts.google.com/signin")


def test_chat_url_candidates_are_authuser_scoped_and_configurable(monkeypatch):
    monkeypatch.setattr(settings, "ai_studio_authuser_candidates", "1, 2, 1, bad")

    assert _aistudio_chat_urls()[:4] == (
        "https://aistudio.google.com/u/1/prompts/new_chat",
        "https://aistudio.google.com/u/2/prompts/new_chat",
        "https://aistudio.google.com/u/0/prompts/new_chat",
        "https://aistudio.google.com/prompts/new_chat",
    )


def test_ensure_hook_page_navigates_wrong_aistudio_route_before_install():
    page = FakePage(url="https://aistudio.google.com/app/apikey")
    session = BrowserSessionForTest(page)

    assert session._ensure_hook_page_sync() is page

    assert session.goto_calls == 1
    assert session.install_calls == 1


def test_ensure_hook_page_reuses_ready_chat_route():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    session = BrowserSessionForTest(page)

    assert session._ensure_hook_page_sync() is page

    assert session.goto_calls == 0
    assert session.install_calls == 1


def test_generate_snapshot_retries_short_cold_start_snapshot():
    session = SnapshotRetrySessionForTest(["x" * 1390, "y" * 1600])

    snapshot = session._generate_snapshot_sync([])

    assert snapshot == "y" * 1600
    assert session.wait_calls == [1500]


def test_generate_snapshot_returns_short_snapshot_after_retry_budget():
    session = SnapshotRetrySessionForTest(["x" * 1390, "y" * 1400, "z" * 1450])

    snapshot = session._generate_snapshot_sync([])

    assert snapshot == "z" * 1450
    assert session.wait_calls == [1500, 1500]


def test_goto_waits_until_chat_runtime_and_input_are_ready():
    page = FakePage(ready_after_waits=2)
    session = BrowserSession(port=0)

    session._goto_aistudio_sync(page)

    assert page.goto_urls == [AI_STUDIO_URL]
    assert page.goto_kwargs[0]["wait_until"] == "commit"
    assert page.has_default_makersuite is True
    assert page.has_textarea is True


def test_goto_skips_auth_failed_chat_route_after_warmup_probe_failure(monkeypatch):
    monkeypatch.setattr(settings, "ai_studio_authuser_candidates", "0,1,2")
    page = FakePage(ready_after_waits=1)
    session = BrowserSession(port=0)
    session._preferred_chat_url = "https://aistudio.google.com/u/0/prompts/new_chat"
    session._failed_chat_urls.add("https://aistudio.google.com/u/0/prompts/new_chat")

    session._goto_aistudio_sync(page)

    assert page.goto_urls == ["https://aistudio.google.com/u/1/prompts/new_chat"]
    assert session._preferred_chat_url == "https://aistudio.google.com/u/1/prompts/new_chat"


def test_goto_skips_redirect_to_failed_chat_route(monkeypatch):
    monkeypatch.setattr(settings, "ai_studio_authuser_candidates", "2,0,1")
    page = FakePage()
    session = BrowserSession(port=0)
    session._failed_chat_urls.add("https://aistudio.google.com/u/0/prompts/new_chat")

    def goto(url: str, **kwargs):
        page.goto_urls.append(url)
        page.url = "https://aistudio.google.com/u/0/prompts/new_chat" if "/u/2/" in url else url
        page.has_default_makersuite = True
        page.has_textarea = True

    page.goto = goto

    session._goto_aistudio_sync(page)

    assert page.goto_urls == [
        "https://aistudio.google.com/u/2/prompts/new_chat",
        "https://aistudio.google.com/u/1/prompts/new_chat",
    ]
    assert "https://aistudio.google.com/u/2/prompts/new_chat" in session._failed_chat_urls
    assert session._preferred_chat_url == "https://aistudio.google.com/u/1/prompts/new_chat"
    assert session._last_requested_chat_url == "https://aistudio.google.com/u/1/prompts/new_chat"


def test_advance_chat_route_marks_requested_and_redirected_routes_failed():
    page = FakePage(url="https://aistudio.google.com/u/0/prompts/new_chat")
    session = BrowserSessionForTest(page)
    session._preferred_chat_url = "https://aistudio.google.com/u/0/prompts/new_chat"
    session._last_requested_chat_url = "https://aistudio.google.com/u/2/prompts/new_chat"

    assert session._advance_chat_route_after_auth_failure_sync() is True

    assert "https://aistudio.google.com/u/0/prompts/new_chat" in session._failed_chat_urls
    assert "https://aistudio.google.com/u/2/prompts/new_chat" in session._failed_chat_urls
    assert session._preferred_chat_url is None
    assert session._last_requested_chat_url is None


def test_goto_recovers_from_ai_developers_docs_redirect():
    page = FakePage(title="Available regions", body="Google AI for Developers")

    def goto(url: str, **kwargs):
        page.goto_urls.append(url)
        if len(page.goto_urls) == 1:
            page.url = "https://ai.google.dev/gemini-api/docs/available-regions"
            return
        page.url = url
        page.has_default_makersuite = True
        page.has_textarea = True

    page.goto = goto
    session = BrowserSession(port=0)

    session._goto_aistudio_sync(page)

    assert page.goto_urls[:2] == [AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK]
    assert page.url == AI_STUDIO_URL_FALLBACK


def test_goto_failure_reports_readiness_diagnostics():
    page = FakePage(title="Account page", body="Login completed but chat shell is missing")
    session = BrowserSession(port=0)

    with pytest.raises(RuntimeError) as exc_info:
        session._goto_aistudio_sync(page)

    message = str(exc_info.value)
    assert "AI Studio chat runtime not ready" in message
    assert "url=https://aistudio.google.com/" in message
    assert "title=Account page" in message
    assert "default_MakerSuite=False" in message
    assert "textarea=False" in message
    assert "body=Login completed but chat shell is missing" in message


def test_goto_google_signin_reports_auth_state_diagnostics(tmp_path):
    auth_file = tmp_path / "missing-auth.json"
    page = FakePage(
        title="Sign in - Google Accounts",
        body="Sign in Use your Google Account",
        goto_redirect_url="https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Faistudio.google.com",
    )
    session = BrowserSession(port=0)
    session._auth_file = str(auth_file)

    with pytest.raises(RuntimeError) as exc_info:
        session._goto_aistudio_sync(page)

    message = str(exc_info.value)
    assert "AI Studio redirected to Google sign-in" in message
    assert "browser auth state is missing or invalid" in message
    assert f"auth_file={auth_file}" in message
    assert "exists=False" in message
    assert "title=Sign in - Google Accounts" in message
    assert "body=Sign in Use your Google Account" in message


def test_goto_google_signin_after_navigation_error_reports_auth_state(tmp_path):
    auth_file = tmp_path / "missing-auth.json"
    page = FakePage(
        title="Sign in - Google Accounts",
        body="Sign in Use your Google Account",
        goto_redirect_url="https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Faistudio.google.com",
        goto_error=TimeoutError("navigation timed out"),
    )
    session = BrowserSession(port=0)
    session._auth_file = str(auth_file)

    with pytest.raises(RuntimeError) as exc_info:
        session._goto_aistudio_sync(page)

    message = str(exc_info.value)
    assert "AI Studio redirected to Google sign-in" in message
    assert "browser auth state is missing or invalid" in message
    assert "navigation timed out" not in message
    assert f"auth_file={auth_file}" in message
    assert "exists=False" in message


def test_new_context_missing_auth_file_fails_before_unauthenticated_context(tmp_path):
    session = BrowserSession(port=0)
    session._auth_file = str(tmp_path / "missing-auth.json")
    session._browser = object()

    with pytest.raises(FileNotFoundError) as exc_info:
        session._new_context_sync()

    assert "Browser auth state file is missing" in str(exc_info.value)


def test_new_context_blocks_service_workers_for_route_replay(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    browser = NewContextBrowser()
    session = BrowserSession(port=0)
    session._auth_file = str(auth_file)
    session._browser = browser

    context = session._new_context_sync()

    assert context is browser.contexts[0]
    assert browser.calls == [{"storage_state": str(auth_file), "service_workers": "block"}]
    assert len(context.init_scripts) == 1
    assert "__api_transport_hooked" in context.init_scripts[0]


def test_browser_options_set_proxy_identity_when_proxy_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "proxy_server", "http://127.0.0.1:7890")
    monkeypatch.setattr(settings, "camoufox_geoip", True)
    session = BrowserSession(port=0)

    options = session._browser_options_sync()

    assert options["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert options["geoip"] is True
    assert options["i_know_what_im_doing"] is True


def test_browser_options_can_use_manual_proxy_identity_when_geoip_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "proxy_server", "http://127.0.0.1:7890")
    monkeypatch.setattr(settings, "camoufox_geoip", False)
    session = BrowserSession(port=0)

    options = session._browser_options_sync()

    assert options["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert options["locale"] == settings.camoufox_locale
    assert options["config"]["timezone"] == settings.camoufox_timezone
    assert options["config"]["geolocation:latitude"] == settings.camoufox_geolocation_latitude
    assert options["config"]["geolocation:longitude"] == settings.camoufox_geolocation_longitude
    assert options["i_know_what_im_doing"] is True


def test_browser_options_disable_ipv6_and_http3_for_wsl_navigation(monkeypatch):
    monkeypatch.setattr(settings, "proxy_server", "")
    session = BrowserSession(port=0)

    options = session._browser_options_sync()

    assert options["firefox_user_prefs"]["network.dns.disableIPv6"] is True
    assert options["firefox_user_prefs"]["network.http.http3.enable"] is False


def test_install_hook_failure_reports_page_diagnostics():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_textarea=True,
        title="AI Studio Chat",
        body="Chat shell visible without runtime",
        install_results=["no_default_MakerSuite"] * 4,
    )
    session = BrowserSession(port=0)

    with pytest.raises(RuntimeError) as exc_info:
        session._install_hooks_sync(page)

    message = str(exc_info.value)
    assert "Hook install failed: no_default_MakerSuite" in message
    assert f"url={AI_STUDIO_URL}" in message
    assert "title=AI Studio Chat" in message
    assert "default_MakerSuite=False" in message
    assert "textarea=True" in message
    assert "body=Chat shell visible without runtime" in message


def test_click_run_button_uses_alternate_aria_selector():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_textarea=True,
        button_selectors={"button[aria-label*='Send' i]"},
    )
    session = BrowserSession(port=0)

    assert session._click_run_button_sync(page) is True

    assert page.clicks == 1
    assert len(page.routed_requests) == 1


def test_click_run_button_prefers_composer_scoped_js_button_over_global_selector():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_textarea=True,
        button_selectors={"button:has-text('Generate')"},
        js_send_result={"found": True, "clicked": True, "label": "send"},
    )
    session = BrowserSession(port=0)

    assert session._click_run_button_sync(page) is True

    assert page.js_send_clicks == 1
    assert page.clicks == 0
    assert len(page.routed_requests) == 1


def test_click_run_button_falls_back_to_keyboard_shortcut():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_textarea=True,
        button_selectors=set(),
        keyboard_triggers_send=True,
    )
    session = BrowserSession(port=0)

    assert session._click_run_button_sync(page) is False

    assert page.textarea_clicks == 1
    assert page.keyboard_presses == ["Control+Enter"]
    assert len(page.routed_requests) == 1


def test_click_run_button_skips_aria_disabled_button():
    page = FakePage(url=AI_STUDIO_URL, has_textarea=True, button_aria_disabled=True)
    session = BrowserSession(port=0)

    assert session._click_run_button_sync(page) is False

    assert page.clicks == 0
    assert page.keyboard_presses == ["Control+Enter"]


def test_has_run_button_accepts_alternate_selector():
    page = FakePage(
        url=AI_STUDIO_URL,
        button_selectors={"button[aria-label*='Run' i]"},
    )
    session = BrowserSession(port=0)

    assert session._has_run_button_sync(page) is True


def test_send_hooked_request_uses_browser_fetch_with_captured_request():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    session = BrowserSessionForTest(page)

    status, raw = session._send_hooked_request_sync(
        body="rewritten-body",
        url="https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
        headers={"content-type": "application/json"},
        timeout_ms=1000,
    )

    assert status == 200
    assert raw == b"native-ok"
    assert page.filled_texts == []
    assert page.routed_requests == []
    assert page.browser_fetch_requests == [
        {
            "url": "https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
            "headers": {"content-type": "application/json"},
            "body": "rewritten-body",
            "timeoutMs": 1000,
        }
    ]
    assert page.response_handlers == []


def test_send_hooked_request_filters_browser_forbidden_headers():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_default_makersuite=True,
        has_textarea=True,
    )
    session = BrowserSessionForTest(page)

    status, raw = session._send_hooked_request_sync(
        body="rewritten-body",
        url="https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent",
        headers={
            "content-type": "application/json+protobuf",
            "cookie": "SID=secret",
            "host": "alkalimakersuite-pa.clients6.google.com",
            "origin": "https://aistudio.google.com",
            "referer": "https://aistudio.google.com/",
            "sec-fetch-mode": "cors",
            "user-agent": "browser",
            "x-goog-api-key": "AIza-test",
            "x-goog-authuser": "0",
        },
        timeout_ms=1000,
    )

    assert status == 200
    assert raw == b"native-ok"
    assert page.browser_fetch_requests[0]["headers"] == {
        "content-type": "application/json+protobuf",
        "x-goog-api-key": "AIza-test",
        "x-goog-authuser": "0",
    }


def test_send_hooked_request_prefers_context_request_when_context_exists():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    context = FakeBrowserContext(pages=[page])
    session = BrowserSessionForTest(page)
    session._ctx = context

    status, raw = session._send_hooked_request_sync(
        body="rewritten-body",
        url="https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent",
        headers={
            "content-type": "application/json+protobuf",
            "connection": "keep-alive",
            "content-length": "999",
            "host": "alkalimakersuite-pa.clients6.google.com",
            "cookie": "SID=secret",
            "origin": "https://aistudio.google.com",
            "sec-fetch-mode": "cors",
            "x-goog-api-key": "AIza-test",
        },
        timeout_ms=2500,
    )

    assert status == 200
    assert raw == b"context-ok"
    assert page.browser_fetch_requests == []
    assert context.request.posts == [
        {
            "url": "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent",
            "data": "rewritten-body",
            "headers": {
                "content-type": "application/json+protobuf",
                "cookie": "SID=secret",
                "origin": "https://aistudio.google.com",
                "sec-fetch-mode": "cors",
                "x-goog-api-key": "AIza-test",
            },
            "timeout": 2500,
        }
    ]


def test_send_hooked_request_uses_native_ui_before_context_replay_for_text_body():
    hook_page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    probe_page = FakePage(url="about:blank", has_default_makersuite=False, has_textarea=False)
    context = FakeBrowserContext(pages=[hook_page], new_pages=[probe_page])
    context.request.response = FakeAPIResponse(status=403, body=b'[,[7,"The caller does not have permission"]]')
    session = BrowserSessionWithCleanProbePageForTest(hook_page, probe_page)
    session._ctx = context
    session._preferred_chat_url = AI_STUDIO_URL_LEGACY_FALLBACK
    session._failed_chat_urls.add(AI_STUDIO_URL)
    body = json.dumps(
        [
            "models/gemini-3.5-flash",
            [[[[None, "Reply with exactly: nexus-permission-api-ok"]], "user"]],
            None,
            [],
            "snapshot",
        ],
        ensure_ascii=False,
    )
    probe_page.generate_content_body = body

    status, raw = session._send_hooked_request_sync(
        body=body,
        url="https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent",
        headers={"content-type": "application/json+protobuf"},
        timeout_ms=2500,
    )

    assert status == 200
    assert raw == b"native-ok"
    assert context.request.posts == []
    assert probe_page.url == AI_STUDIO_URL
    assert probe_page.filled_texts == ["Reply with exactly: nexus-permission-api-ok"]
    assert probe_page.routed_requests[-1].continue_kwargs == {}
    assert hook_page.routed_requests == []
    assert session.goto_route_states[-1] == (None, set())
    assert probe_page.closed is True


def test_send_hooked_request_native_ui_fallback_uses_unhooked_isolated_context():
    hook_page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    hook_page.browser_fetch_response_status = 403
    hook_page.browser_fetch_response_body = b'[, [7, "The caller does not have permission"]]'
    probe_page = FakePage(url="about:blank", has_default_makersuite=False, has_textarea=False)
    body = json.dumps(
        [
            "models/gemini-3.5-flash",
            [[[[None, "Reply with exactly: nexus-permission-api-ok"]], "user"]],
            None,
            [],
            "snapshot",
        ],
        ensure_ascii=False,
    )
    probe_page.generate_content_body = body
    probe_context = FakeBrowserContext(new_pages=[probe_page])
    session = BrowserSessionWithIsolatedProbeContextForTest(hook_page, probe_context)
    session._auth_file = None

    status, raw = session._send_hooked_request_sync(
        body=body,
        url="https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent",
        headers={"content-type": "application/json+protobuf"},
        timeout_ms=2500,
    )

    assert status == 200
    assert raw == b"native-ok"
    assert probe_context.init_scripts == []
    assert probe_context.closed is True
    assert session._browser.new_context_calls == [{"service_workers": "block"}]
    assert probe_page.filled_texts == ["Reply with exactly: nexus-permission-api-ok"]


def test_send_native_generate_content_uses_subprocess_when_auth_file_available(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    session = BrowserSession(port=0)
    session._auth_file = str(auth_file)
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, text, capture_output, timeout, env):
        payload = json.loads(input)
        calls.append(
            {
                "command": command,
                "payload": payload,
                "text": text,
                "capture_output": capture_output,
                "timeout": timeout,
                "env": env,
            }
        )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "body_b64": base64.b64encode(b"subprocess-ok").decode("ascii"),
                    "wire_model": "models/gemini-3.5-flash",
                    "url_path": "/u/0/prompts/new_chat",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(session_module.subprocess, "run", fake_run)
    body = json.dumps(
        [
            "models/gemini-3.5-flash",
            [[[[None, "Reply with exactly: nexus-permission-api-ok"]], "user"]],
            None,
            [],
            "snapshot",
        ],
        ensure_ascii=False,
    )

    status, raw = session._send_native_generate_content_body_sync(body=body, timeout_ms=2500)

    assert status == 200
    assert raw == b"subprocess-ok"
    assert calls[0]["command"][-2:] == ["-m", "aistudio_api.infrastructure.gateway.native_ui_sender"]
    assert calls[0]["payload"] == {
        "auth_file": str(auth_file),
        "model": "models/gemini-3.5-flash",
        "prompt": "Reply with exactly: nexus-permission-api-ok",
        "timeout_ms": 2500,
    }
    assert calls[0]["text"] is True
    assert calls[0]["capture_output"] is True


def test_send_hooked_request_reports_browser_fetch_error():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    page.browser_fetch_error = "TypeError: Failed to fetch"
    session = BrowserSessionForTest(page)

    with pytest.raises(RuntimeError, match="browser fetch replay failed"):
        session._send_hooked_request_sync(
            body="rewritten-body",
            url="https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
            headers={"content-type": "application/json"},
            timeout_ms=1000,
        )


def test_native_generate_content_probe_observes_real_body_without_rewrite():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    native_body = json.dumps(
        ["models/gemini-3.5-flash", {"text": "1", "payload": "x" * 120}, None, None, "snapshot"],
        ensure_ascii=False,
    )
    page.generate_content_body = native_body
    session = BrowserSessionForTest(page)

    status, raw, wire_model = session._probe_native_generate_content_sync("gemini-3.5-flash", 1000)

    assert status == 200
    assert raw == b"native-ok"
    assert wire_model == "models/gemini-3.5-flash"
    assert page.filled_texts == ["1"]
    assert page.routed_requests[-1].request.post_data == native_body
    assert page.routed_requests[-1].continue_kwargs == {}
    assert page.hooked is False
    assert page.response_handlers == []


def test_native_generate_content_probe_uses_clean_temporary_page_when_context_exists():
    hook_page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    probe_page = FakePage(url="about:blank", has_default_makersuite=False, has_textarea=False)
    native_body = json.dumps(
        ["models/gemini-3.5-flash", {"text": "1", "payload": "x" * 120}, None, None, "snapshot"],
        ensure_ascii=False,
    )
    probe_page.generate_content_body = native_body
    session = BrowserSessionWithCleanProbePageForTest(hook_page, probe_page)

    status, raw, wire_model = session._probe_native_generate_content_sync("gemini-3.5-flash", 1000)

    assert status == 200
    assert raw == b"native-ok"
    assert wire_model == "models/gemini-3.5-flash"
    assert session.goto_calls == 1
    assert probe_page.url == AI_STUDIO_URL
    assert probe_page.filled_texts == ["1"]
    assert probe_page.closed is True
    assert hook_page.routed_requests == []


def test_native_generate_content_probe_uses_isolated_context_when_browser_exists():
    hook_page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    probe_page = FakePage(url="about:blank", has_default_makersuite=False, has_textarea=False)
    native_body = json.dumps(
        ["models/gemini-3.5-flash", {"text": "1", "payload": "x" * 120}, None, None, "snapshot"],
        ensure_ascii=False,
    )
    probe_page.generate_content_body = native_body
    probe_context = FakeBrowserContext(new_pages=[probe_page])
    session = BrowserSessionWithIsolatedProbeContextForTest(hook_page, probe_context)
    session._auth_file = None

    status, raw, wire_model = session._probe_native_generate_content_sync("gemini-3.5-flash", 1000)

    assert status == 200
    assert raw == b"native-ok"
    assert wire_model == "models/gemini-3.5-flash"
    assert probe_page.url == AI_STUDIO_URL
    assert probe_page.filled_texts == ["1"]
    assert probe_context.closed is True
    assert hook_page.routed_requests == []
    assert session._browser.new_context_calls == [{"service_workers": "block"}]


def test_native_generate_content_probe_skips_empty_204_before_native_response():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    page.generate_content_body = json.dumps(
        ["models/gemini-3.5-flash", {"text": "1", "payload": "x" * 120}, None, None, "snapshot"],
        ensure_ascii=False,
    )
    page.generate_content_response_sequence = [(204, b""), (200, b"native-ok")]
    session = BrowserSessionForTest(page)

    status, raw, wire_model = session._probe_native_generate_content_sync("gemini-3.5-flash", 1000)

    assert status == 200
    assert raw == b"native-ok"
    assert wire_model == "models/gemini-3.5-flash"
    assert page.routed_requests[-1].continue_kwargs == {}
    assert page.response_handlers == []


def test_native_generate_content_probe_does_not_send_when_text_model_unavailable():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_default_makersuite=True,
        has_textarea=True,
        text_model_select_result={"selected": False, "reason": "text_model_not_found"},
    )
    session = BrowserSessionForTest(page)

    with pytest.raises(ModelNotFoundError, match="text model not selected"):
        session._probe_native_generate_content_sync("gemini-3.5-flash", 1000)

    assert page.filled_texts == []
    assert page.routed_requests == []
    assert page.text_model_select_calls == 2
    assert page.response_handlers == []


def test_send_streaming_request_uses_browser_fetch_with_captured_request():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    page.browser_fetch_response_body = b'[[[[[[[[null,"hello"]]]]]]]]'
    session = BrowserSessionForTest(page)

    async def collect():
        events = []
        async for event in session.send_streaming_request(
            body="stream-body",
            url="https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
            headers={"content-type": "application/json"},
            timeout_ms=1000,
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())

    assert events == [("status", 200), ("chunk", b'[[[[[[[[null,"hello"]]]]]]]]')]
    assert page.routed_requests == []
    assert page.browser_fetch_requests == [
        {
            "url": "https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
            "headers": {"content-type": "application/json"},
            "body": "stream-body",
            "timeoutMs": 1000,
        }
    ]
    assert page.response_handlers == []


def test_detect_tier_for_auth_file_does_not_require_active_auth(monkeypatch):
    session = TierDetectionSessionForTest()

    def fake_detect_tier_sync(context, timeout_ms):
        assert timeout_ms == 12345
        return TierResult(tier=AccountTier.PRO, email="user@example.com", raw_header="user@example.com PRO")

    monkeypatch.setattr(tier_detector, "detect_tier_sync", fake_detect_tier_sync)

    result = session._detect_tier_for_auth_file_sync("/tmp/auth.json", timeout_ms=12345)

    assert result.tier == AccountTier.PRO
    assert session.ensure_process_calls == 1
    assert session.browser.storage_states == ["/tmp/auth.json"]
    assert session.browser.contexts[0].closed is True


class FakeOnboardingPage:
    def __init__(self, results):
        self.results = list(results)
        self.wait_calls = []

    def evaluate(self, script: str, *args):
        assert "google apis terms" in script.lower()
        return self.results.pop(0) if self.results else {"needed": False}

    def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)


class FakeImageOnboardingPage:
    def __init__(self, *, initial_consent: bool = False, selection_result: dict | None = None, trigger_result: dict | None = None):
        self.wait_calls = []
        self.evaluate_calls = []
        self.goto_urls = []
        self.url = AI_STUDIO_URL
        self.has_default_makersuite = True
        self.has_textarea = True
        self.initial_consent = initial_consent
        self.selection_result = selection_result
        self.trigger_result = trigger_result
        self.consent_calls = 0
        self.trigger_calls = 0
        self.open_picker_calls = 0
        self.model_picker_selection_calls = 0

    def goto(self, url: str, **kwargs):
        self.goto_urls.append(url)
        self.url = url

    def evaluate(self, script: str, *args):
        self.evaluate_calls.append(script)
        if script == "mw:!!window.default_MakerSuite":
            return self.has_default_makersuite
        if "image_entry_not_found" in script:
            self.trigger_calls += 1
            if self.trigger_result is not None:
                return self.trigger_result
            if self.initial_consent and self.trigger_calls == 1:
                return {"triggered": False, "reason": "already_visible"}
            return {"triggered": True, "label": "image Image Generation"}
        if "image_model_picker_not_found" in script:
            self.open_picker_calls += 1
            return {"opened": True, "label": "Nano Banana"}
        if "model_card_not_found" in script:
            if "openedLabel" in script and "pickerLike" in script:
                self.model_picker_selection_calls += 1
            return self.selection_result or {"selected": True, "label": "Nano Banana Pro"}
        if "google apis terms" in script.lower():
            self.consent_calls += 1
            if self.initial_consent and self.consent_calls == 1:
                return {"needed": True, "checked": True, "submitted": False, "remaining": True}
            if self.initial_consent and self.consent_calls == 2:
                return {"needed": True, "checked": False, "submitted": True, "remaining": False}
            return {"needed": False, "checked": False, "submitted": False, "remaining": False}
        return None

    def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)

    def query_selector(self, selector: str):
        return object() if selector == "textarea" and self.has_textarea else None

    def title(self):
        return "AI Studio"


class FakeModelListPage:
    def __init__(self, models):
        self.models = models
        self.open_calls = 0
        self.wait_calls = []

    def evaluate(self, script: str, *args):
        if "model_picker_not_found" in script:
            self.open_calls += 1
            return {"opened": True, "label": "Gemini 3.5 Flash"}
        assert "matchAll" in script
        return self.models

    def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)


class FakeTextModelSelectionPage:
    def __init__(self, selection_result: dict | None = None):
        self.selection_result = selection_result or {"selected": True, "label": "Gemini 3.5 Flash"}
        self.evaluate_calls = []
        self.wait_calls = []
        self.open_calls = 0
        self.select_calls = 0

    def evaluate(self, script: str, *args):
        self.evaluate_calls.append(script)
        if "model_picker_not_found" in script:
            self.open_calls += 1
            return {"opened": True, "label": "Gemini 3.5 Flash"}
        if "text_model_not_found" in script:
            self.select_calls += 1
            return self.selection_result
        return None

    def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)


def test_aistudio_onboarding_completion_clicks_required_consent_until_submitted():
    page = FakeOnboardingPage([
        {"needed": True, "checked": True, "submitted": False, "remaining": True},
        {"needed": True, "checked": False, "submitted": True, "remaining": False},
    ])
    session = BrowserSession(port=0)

    assert session._complete_aistudio_onboarding_sync(page) is True

    assert page.wait_calls == [1200, 1200]


def test_aistudio_onboarding_completion_noops_when_not_needed():
    page = FakeOnboardingPage([{"needed": False, "checked": False, "submitted": False, "remaining": False}])
    session = BrowserSession(port=0)

    assert session._complete_aistudio_onboarding_sync(page) is False

    assert page.wait_calls == []


def test_image_model_capture_prepares_image_onboarding(monkeypatch):
    page = FakeImageOnboardingPage()
    session = BrowserSession(port=0)

    assert session._prepare_model_onboarding_sync(page, "gemini-3-pro-image-preview") is True

    assert page.wait_calls == [1200, 800, 1200]
    assert page.open_picker_calls == 1
    assert page.model_picker_selection_calls == 1
    assert any("model_card_not_found" in script for script in page.evaluate_calls)


def test_image_model_capture_reopens_image_entry_after_initial_terms():
    page = FakeImageOnboardingPage(initial_consent=True)
    session = BrowserSession(port=0)

    assert session._prepare_model_onboarding_sync(page, "gemini-3-pro-image-preview") is True

    assert page.trigger_calls == 1
    assert page.consent_calls >= 2
    assert any("model_card_not_found" in script for script in page.evaluate_calls)


def test_image_model_capture_continues_when_picker_cards_are_hidden_after_entry_opens():
    page = FakeImageOnboardingPage(selection_result={"selected": False, "reason": "model_card_not_found", "visible": ["Nano Banana"]})
    session = BrowserSession(port=0)

    assert session._prepare_model_onboarding_sync(page, "gemini-3-pro-image-preview") is True

    assert page.trigger_calls == 1
    assert page.open_picker_calls == 1
    assert page.model_picker_selection_calls == 1
    assert any("model_card_not_found" in script for script in page.evaluate_calls)


def test_image_model_capture_navigates_to_image_route_when_entry_missing():
    page = FakeImageOnboardingPage(trigger_result={"triggered": False, "reason": "image_entry_not_found"})
    session = BrowserSession(port=0)

    assert session._prepare_model_onboarding_sync(page, "gemini-3-pro-image-preview") is True

    assert page.goto_urls == ["https://aistudio.google.com/u/2/prompts/new_image?model=gemini-3-pro-image-preview"]
    assert page.open_picker_calls == 1
    assert page.model_picker_selection_calls == 1


def test_image_model_capture_continues_on_image_route_when_picker_cards_are_hidden():
    page = FakeImageOnboardingPage(selection_result={"selected": False, "reason": "model_card_not_found", "opened": "image_edit_auto"})
    page.url = "https://aistudio.google.com/u/2/prompts/new_image?model=gemini-3-pro-image-preview"
    session = BrowserSession(port=0)

    assert session._prepare_model_onboarding_sync(page, "gemini-3-pro-image-preview") is True

    assert page.goto_urls == []
    assert page.open_picker_calls == 1
    assert page.model_picker_selection_calls == 1


def test_image_model_capture_continues_when_image_controls_are_visible_after_picker_open():
    page = FakeImageOnboardingPage(
        selection_result={"selected": False, "reason": "model_card_not_found", "opened": "image_edit_auto", "visible": ["tune", "image_edit_auto"]}
    )
    page.url = AI_STUDIO_URL
    session = BrowserSession(port=0)

    assert session._prepare_model_onboarding_sync(page, "gemini-3-pro-image-preview") is True

    assert page.goto_urls == []
    assert page.open_picker_calls == 1
    assert page.model_picker_selection_calls == 1


def test_text_model_capture_skips_image_onboarding():
    page = FakeImageOnboardingPage()
    session = BrowserSession(port=0)

    assert session._prepare_model_onboarding_sync(page, "gemini-3-flash-preview") is False
    assert page.evaluate_calls == []


def test_text_model_selection_opens_picker_and_clicks_requested_model():
    page = FakeTextModelSelectionPage()
    session = BrowserSession(port=0)

    assert session._select_text_model_sync(page, "gemini-3.5-flash") is True

    assert page.open_calls == 1
    assert page.select_calls == 1
    assert page.wait_calls == [1000, 1200]


def test_text_model_selection_treats_already_selected_as_ready():
    page = FakeTextModelSelectionPage({"selected": False, "reason": "already_selected", "label": "gemini 3.5 flash"})
    session = BrowserSession(port=0)

    assert session._select_text_model_sync(page, "models/gemini-3.5-flash") is True

    assert page.open_calls == 1
    assert page.select_calls == 1
    assert page.wait_calls == [1000]


def test_text_model_selection_rejects_non_boolean_selected_payload():
    page = FakeTextModelSelectionPage({"selected": "chat spark playground high", "reason": "text_model_not_found"})
    session = BrowserSession(port=0)

    assert session._select_text_model_sync(page, "gemini-3.5-flash") is False

    assert page.open_calls == 2
    assert page.select_calls == 2
    assert page.wait_calls == [1000, 1000, 1000]


def test_list_available_models_normalizes_and_deduplicates_page_results():
    page = FakeModelListPage(["models/Gemini-Dynamic-Preview", "gemini-dynamic-preview", "deep-research-max-preview-04-2026", "", None])
    session = BrowserSession(port=0)
    session._hook_page = page
    session._ensure_hook_page_sync = lambda: page

    assert session._list_available_models_sync() == ["gemini-dynamic-preview", "deep-research-max-preview-04-2026"]
    assert page.open_calls == 1
    assert page.wait_calls == [800]


class TemplateCaptureImageSessionForTest(TemplateCaptureSessionForTest):
    def __init__(self, page):
        super().__init__(page)
        self.ensure_hook_calls = 0
        self.botguard_calls = 0
        self.prepare_calls = []
        self.text_select_calls = []
        self.text_select_result = False
        self.install_calls = 0
        self.goto_calls = 0

    def _ensure_hook_page_sync(self):
        self.ensure_hook_calls += 1
        return self._hook_page

    def _ensure_botguard_service_sync(self):
        self.botguard_calls += 1
        return self._hook_page

    def _prepare_model_onboarding_sync(self, page, model: str) -> bool:
        self.prepare_calls.append((page, model, self.botguard_calls))
        return "image" in model

    def _select_text_model_sync(self, page, model: str) -> bool:
        self.text_select_calls.append((page, model))
        return self.text_select_result

    def _install_hooks_sync(self, page) -> None:
        self.install_calls += 1

    def _goto_aistudio_sync(self, page) -> None:
        self.goto_calls += 1
        page.url = AI_STUDIO_URL
        page.has_default_makersuite = True
        page.has_textarea = True


class ImageTemplateRetrySessionForTest(TemplateCaptureImageSessionForTest):
    def __init__(self, page):
        super().__init__(page)
        self.capture_calls = 0

    def _capture_template_request_sync(self, page, model: str) -> dict:
        self.capture_calls += 1
        if self.capture_calls == 1:
            raise RuntimeError(f"template capture timeout for model={model}")
        return {"url": page.generate_content_url, "headers": page.generate_content_headers, "body": page.generate_content_body}


class TextTemplateTimeoutRetrySessionForTest(TemplateCaptureImageSessionForTest):
    def __init__(self, page):
        super().__init__(page)
        self.capture_calls = 0

    def _capture_template_request_sync(self, page, model: str) -> dict:
        self.capture_calls += 1
        if self.capture_calls == 1:
            raise RuntimeError(f"template capture timeout for model={model}")
        return {"url": page.generate_content_url, "headers": page.generate_content_headers, "body": page.generate_content_body}


def test_capture_template_prepares_image_model_before_botguard_snapshot_ready():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    session = TemplateCaptureImageSessionForTest(page)

    captured = session._capture_template_sync("gemini-3-pro-image-preview")

    assert captured["url"].endswith("GenerateContent")
    assert session.ensure_hook_calls == 1
    assert session.botguard_calls == 1
    assert session.prepare_calls == [(page, "gemini-3-pro-image-preview", 0)]
    assert session.install_calls == 2
    assert session.goto_calls == 1


def test_image_template_capture_reselects_image_model_after_timeout():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    session = ImageTemplateRetrySessionForTest(page)

    captured = session._capture_template_sync("gemini-3-pro-image-preview")

    assert captured["url"].endswith("GenerateContent")
    assert session.capture_calls == 2
    assert session.prepare_calls == [
        (page, "gemini-3-pro-image-preview", 0),
        (page, "gemini-3-pro-image-preview", 0),
    ]
    assert session.install_calls == 3


def test_text_template_capture_reopens_page_after_timeout():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    session = TextTemplateTimeoutRetrySessionForTest(page)

    captured = session._capture_template_sync("gemini-3-flash-preview")

    assert captured["url"].endswith("GenerateContent")
    assert session.capture_calls == 2
    assert session.goto_calls == 1
    assert session.install_calls == 1


def test_text_template_capture_selects_requested_text_model_before_capture():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    page.generate_content_body = json.dumps(
        ["models/gemini-3.5-flash", {"text": "template", "payload": "x" * 120}, None, None, "snapshot"],
        ensure_ascii=False,
    )
    session = TemplateCaptureImageSessionForTest(page)
    session.text_select_result = True

    captured = session._capture_template_sync("gemini-3.5-flash")

    assert captured["url"].endswith("GenerateContent")
    assert session.text_select_calls == [(page, "gemini-3.5-flash")]
    assert session.install_calls == 1


def test_capture_template_uses_request_route_and_aborts_dummy_generation():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True)
    session = TemplateCaptureSessionForTest(page)

    captured = session._capture_template_sync("gemini-3-flash-preview")

    assert captured == {
        "url": "https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
        "headers": page.generate_content_headers,
        "body": page.generate_content_body,
    }
    assert page.filled_texts == ["template"]
    assert len(page.routed_requests) == 1
    assert page.routed_requests[0].aborted is True
    assert page.routed_requests[0].continued is False
    assert page.unroute_calls == ["**/*"]
    assert session.wait_until_idle_calls == 1


def test_capture_template_accepts_batchexecute_url_without_generatecontent_marker():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_default_makersuite=True,
        has_textarea=True,
        generate_content_url="https://aistudio.google.com/_/BardChatUi/data/batchexecute?rpcids=abc123",
    )
    page.generate_content_body = json.dumps(
        ["models/gemini-3.1-flash-lite", {"text": "template", "payload": "x" * 120}, None, None, "snapshot"],
        ensure_ascii=False,
    )
    session = TemplateCaptureSessionForTest(page)

    captured = session._capture_template_sync("gemini-3.1-flash-lite")

    assert captured["url"] == "https://aistudio.google.com/_/BardChatUi/data/batchexecute?rpcids=abc123"
    assert captured["body"] == page.generate_content_body
    assert page.routed_requests[0].aborted is True
    assert page.unroute_calls == ["**/*"]


def test_capture_template_accepts_current_generatecontent_rpc_host():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_default_makersuite=True,
        has_textarea=True,
        generate_content_url="https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent",
    )
    session = TemplateCaptureSessionForTest(page)

    captured = session._capture_template_sync("gemini-3-flash-preview")

    assert captured["url"] == page.generate_content_url
    assert captured["body"] == page.generate_content_body
    assert page.routed_requests[0].aborted is True


def test_text_capture_template_accepts_batchexecute_body_markers_without_generatecontent_url():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_default_makersuite=True,
        has_textarea=True,
        generate_content_url="https://aistudio.google.com/_/BardChatUi/data/batchexecute?rpcids=abc123",
    )
    page.generate_content_body = json.dumps([
        "models/gemini-3-flash-preview",
        {"snapshot": "x" * 120},
        None,
    ])
    session = TemplateCaptureSessionForTest(page)

    captured = session._capture_template_sync("gemini-3.1-flash-lite")

    assert captured["url"] == page.generate_content_url
    assert captured["body"] == page.generate_content_body
    assert page.routed_requests[0].aborted is True


def test_capture_template_ignores_generatecontent_url_with_non_json_body():
    session = BrowserSession(port=0)

    assert not session._is_template_capture_request(
        url="https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent",
        body="x" * 160,
        model_marker="gemini-3-flash-preview",
    )


def test_capture_template_accepts_generatecontent_body_with_model_and_snapshot_markers():
    session = BrowserSession(port=0)

    body = json.dumps([
        "models/gemini-3-flash-preview",
        [["snapshot", {"value": "x" * 120}]],
        None,
    ])

    assert session._is_template_capture_request(
        url="https://aistudio.google.com/_/BardChatUi/data/batchexecute?rpcids=abc123",
        body=body,
        model_marker="gemini-3-flash-preview",
        allow_text_markers=True,
    )


def test_text_capture_does_not_reuse_botguard_warmup_request():
    page = FakePage(url=AI_STUDIO_URL, has_default_makersuite=True, has_textarea=True, botguard_on_send=True)
    session = BrowserSessionForTest(page)

    captured = session._capture_template_sync("gemini-3.1-flash-lite")

    assert captured == {
        "url": "https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
        "headers": page.generate_content_headers,
        "body": page.generate_content_body,
    }
    assert page.filled_texts == ["1", "template"]
    assert len(page.routed_requests) == 2
    assert page.routed_requests[0].aborted is True
    assert page.routed_requests[0].continued is False
    assert page.routed_requests[1].aborted is True
    assert page.unroute_calls == ["**/*", "**/*"]


def test_capture_template_recovers_when_page_redirects_to_docs_during_fill():
    page = FakePage(
        url=AI_STUDIO_URL,
        has_default_makersuite=True,
        has_textarea=True,
        redirect_on_next_fill=True,
    )
    session = TemplateCaptureSessionForTest(page)

    captured = session._capture_template_sync("gemini-3-flash-preview")

    assert captured["url"].endswith("GenerateContent")
    assert page.filled_texts == ["template"]
    assert session.goto_calls == 1
    assert session.install_calls == 3
    assert page.unroute_calls == ["**/*", "**/*"]
