import json

import pytest

from aistudio_api.config import settings
from aistudio_api.infrastructure.account import tier_detector
from aistudio_api.infrastructure.account.tier_detector import AccountTier, TierResult
from aistudio_api.infrastructure.gateway.session import AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK, BrowserSession, _aistudio_chat_urls


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

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


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
        self.goto_urls: list[str] = []
        self.goto_kwargs: list[dict[str, object]] = []
        self.wait_calls: list[int] = []
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
        self.generate_content_body = json.dumps(
            ["gemini-3-flash-preview", {"payload": "x" * 120}, None, None, "snapshot"],
            ensure_ascii=False,
        )
        self.generate_content_headers = {"authorization": "Bearer token", "content-type": "application/json"}
        self.generate_content_url = generate_content_url
        self.keyboard = FakeKeyboard(self)

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
        if script == "mw:!!window.__bg_service":
            return self.has_bg_service
        if "matchesSendIntent" in script:
            result = self.js_send_result
            if result is not None:
                if args and args[0] is True and result.get("found"):
                    self.js_send_clicks += 1
                    if result.get("clicked"):
                        self.trigger_generate_content_request()
                return result
            return {"found": False, "clicked": False, "label": "", "reason": "fake_no_js_button"}
        if "__bg_hooked" in script and "snapKey" in script:
            return self.install_results.pop(0) if self.install_results else "hooked:snapshotKey"
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

    def trigger_generate_content_request(self):
        if self.botguard_on_send:
            self.has_bg_service = True
        request = FakeRequest(
            self.generate_content_url,
            self.generate_content_body,
            self.generate_content_headers,
        )
        route = FakeRoute(request)
        self.routed_requests.append(route)
        for _, handler in list(self.route_handlers):
            handler(route)


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


def test_browser_options_set_proxy_identity_when_proxy_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "proxy_server", "http://127.0.0.1:7890")
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
    assert page.routed_requests[0].continued is True
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
    assert session.install_calls == 1
    assert page.unroute_calls == ["**/*", "**/*"]
