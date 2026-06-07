import base64
import json

import pytest

from aistudio_api.infrastructure.gateway import native_ui_sender
from aistudio_api.infrastructure.gateway.native_ui_sender import _send_on_page


class FakeRequest:
    def __init__(self, body: str):
        self.post_data = body


class FakeResponse:
    def __init__(self, *, url: str, status: int, body: bytes, request: FakeRequest):
        self.url = url
        self.status = status
        self._body = body
        self.request = request

    def body(self):
        return self._body


class FakeTextArea:
    def __init__(self, page: "FakePage"):
        self.page = page

    def fill(self, value: str):
        self.page.filled_prompts.append(value)


class FakePage:
    def __init__(self):
        self.response_handlers = []
        self.filled_prompts = []
        self.goto_urls = []
        self.goto_timeouts = []
        self.goto_failures = []
        self.wait_calls = []
        self.model_selection_results = []
        self.model_selection_calls = 0
        self.cleanup_calls = 0
        self.open_picker_results = []
        self.open_picker_calls = 0

    def goto(self, url: str, **kwargs):
        self.goto_urls.append(url)
        self.goto_timeouts.append(kwargs.get("timeout"))
        if self.goto_failures:
            failure = self.goto_failures.pop(0)
            if failure is not None:
                raise failure
        self.url = url

    def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)

    def query_selector(self, selector: str):
        if selector == "textarea":
            return FakeTextArea(self)
        return None

    def evaluate(self, script: str, *args):
        if script == "mw:!!window.default_MakerSuite":
            return True
        if script == native_ui_sender.DIALOG_CLEANUP_JS:
            self.cleanup_calls += 1
            return None
        if "text_model_not_found" in script:
            self.model_selection_calls += 1
            if self.model_selection_results:
                return self.model_selection_results.pop(0)
            return {"selected": True, "label": "Gemini 3.5 Flash"}
        if "model_picker_not_found" in script:
            self.open_picker_calls += 1
            if self.open_picker_results:
                return self.open_picker_results.pop(0)
            return {"opened": True, "label": "Gemini 3 Flash Preview"}
        if args and args[0] is True and "matchesSendIntent" in script:
            self._emit_responses()
            return {"found": True, "clicked": True, "label": "Run"}
        return None

    def on(self, event_name: str, handler):
        if event_name == "response":
            self.response_handlers.append(handler)

    def remove_listener(self, event_name: str, handler):
        if event_name == "response":
            self.response_handlers = [existing for existing in self.response_handlers if existing is not handler]

    def _emit_responses(self):
        body = json.dumps(["models/gemini-3.5-flash", {"text": "warmup"}], ensure_ascii=False)
        request = FakeRequest(body)
        responses = [
            FakeResponse(
                url=(
                    "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
                    "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/"
                    "StreamGenerateContentPerUserQuota"
                ),
                status=404,
                body=b"quota helper failed",
                request=request,
            ),
            FakeResponse(
                url=(
                    "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
                    "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent"
                ),
                status=200,
                body=b"main generate ok",
                request=request,
            ),
        ]
        for response in responses:
            for handler in list(self.response_handlers):
                handler(response)


def test_send_on_page_ignores_per_user_quota_generate_content_helper_response():
    page = FakePage()

    result = _send_on_page(page, model="gemini-3.5-flash", prompt="warmup", timeout_ms=1000)

    assert result["status"] == 200
    assert base64.b64decode(str(result["body_b64"])) == b"main generate ok"
    assert result["wire_model"] == "models/gemini-3.5-flash"
    assert result["url_path"].endswith("/GenerateContent")


def test_send_on_page_retries_text_model_selection_until_available():
    page = FakePage()
    page.model_selection_results = [
        {"selected": False, "reason": "text_model_not_found", "current": "chat spark playground"},
        {"selected": True, "label": "Gemini 3.5 Flash"},
    ]

    result = _send_on_page(page, model="gemini-3.5-flash", prompt="warmup", timeout_ms=1000)

    assert result["status"] == 200
    assert page.filled_prompts == ["warmup"]
    assert 1250 in page.wait_calls


def test_send_on_page_keeps_waiting_for_cold_text_model_picker():
    page = FakePage()
    page.model_selection_results = [
        {"selected": False, "reason": "text_model_not_found", "current": "chat spark playground"},
        {"selected": False, "reason": "text_model_not_found", "current": "chat spark playground"},
        {"selected": False, "reason": "text_model_not_found", "current": "chat spark playground"},
        {"selected": True, "label": "Gemini 3.5 Flash"},
    ]

    result = _send_on_page(page, model="gemini-3.5-flash", prompt="warmup", timeout_ms=60_000)

    assert result["status"] == 200
    assert page.model_selection_calls == 4


def test_send_on_page_keeps_open_model_picker_while_options_load():
    page = FakePage()
    page.model_selection_results = [
        {"selected": False, "reason": "text_model_not_found", "current": "chat spark playground", "visible": []},
        {"selected": True, "label": "Gemini 3.5 Flash"},
    ]

    result = _send_on_page(page, model="gemini-3.5-flash", prompt="warmup", timeout_ms=60_000)

    assert result["status"] == 200
    assert page.cleanup_calls == 2
    assert page.open_picker_calls == 1
    assert page.model_selection_calls == 2


def test_send_on_page_uses_extended_open_chat_budget(monkeypatch):
    page = FakePage()
    open_chat_timeouts = []

    def open_chat(_page, timeout_ms: int):
        open_chat_timeouts.append(timeout_ms)

    monkeypatch.setattr(native_ui_sender, "_open_chat", open_chat)

    result = _send_on_page(page, model="gemini-3.5-flash", prompt="warmup", timeout_ms=300_000)

    assert result["status"] == 200
    assert open_chat_timeouts == [180_000]


def test_open_chat_shares_timeout_across_candidate_urls(monkeypatch):
    page = FakePage()
    now = {"value": 100.0}
    ready_timeouts = []

    monkeypatch.setattr(
        native_ui_sender,
        "_aistudio_chat_urls",
        lambda: (
            "https://aistudio.google.com/u/2/prompts/new_chat",
            "https://aistudio.google.com/u/0/prompts/new_chat",
        ),
    )
    monkeypatch.setattr(native_ui_sender.time, "monotonic", lambda: now["value"])

    def wait_for_chat_ready(_page, timeout_ms: int) -> bool:
        ready_timeouts.append(timeout_ms)
        now["value"] += (timeout_ms / 1000.0) + 0.001
        return False

    monkeypatch.setattr(native_ui_sender, "_wait_for_chat_ready", wait_for_chat_ready)

    with pytest.raises(RuntimeError, match="AI Studio chat runtime not ready"):
        native_ui_sender._open_chat(page, 3000)

    assert page.goto_urls == ["https://aistudio.google.com/", "https://aistudio.google.com/u/2/prompts/new_chat"]
    assert ready_timeouts == [3000]


def test_open_chat_accepts_ready_page_after_interrupted_goto(monkeypatch):
    page = FakePage()
    page.goto_failures = [None, TimeoutError("first authuser route interrupted")]

    monkeypatch.setattr(
        native_ui_sender,
        "_aistudio_chat_urls",
        lambda: (
            "https://aistudio.google.com/u/2/prompts/new_chat",
            "https://aistudio.google.com/u/0/prompts/new_chat",
        ),
    )
    monkeypatch.setattr(native_ui_sender, "_wait_for_chat_ready", lambda _page, _timeout_ms: True)

    native_ui_sender._open_chat(page, 90_000)

    assert page.goto_urls == [
        "https://aistudio.google.com/",
        "https://aistudio.google.com/u/2/prompts/new_chat",
    ]
    assert page.goto_timeouts[1] == 45_000


def test_open_chat_tries_next_candidate_when_interrupted_page_is_not_ready(monkeypatch):
    page = FakePage()
    page.goto_failures = [None, TimeoutError("first authuser route stalled"), None]
    ready_results = [False, True]

    monkeypatch.setattr(
        native_ui_sender,
        "_aistudio_chat_urls",
        lambda: (
            "https://aistudio.google.com/u/2/prompts/new_chat",
            "https://aistudio.google.com/u/0/prompts/new_chat",
        ),
    )
    monkeypatch.setattr(native_ui_sender, "_wait_for_chat_ready", lambda _page, _timeout_ms: ready_results.pop(0))

    native_ui_sender._open_chat(page, 90_000)

    assert page.goto_urls == [
        "https://aistudio.google.com/",
        "https://aistudio.google.com/u/2/prompts/new_chat",
        "https://aistudio.google.com/u/0/prompts/new_chat",
    ]
    assert page.goto_timeouts[1] == 45_000

def test_open_chat_continues_when_home_prime_fails(monkeypatch):
    page = FakePage()
    page.goto_failures = [TimeoutError("home route stalled"), None]

    monkeypatch.setattr(
        native_ui_sender,
        "_aistudio_chat_urls",
        lambda: ("https://aistudio.google.com/u/0/prompts/new_chat",),
    )
    monkeypatch.setattr(native_ui_sender, "_wait_for_chat_ready", lambda _page, _timeout_ms: True)

    native_ui_sender._open_chat(page, 90_000)

    assert page.goto_urls == ["https://aistudio.google.com/", "https://aistudio.google.com/u/0/prompts/new_chat"]
