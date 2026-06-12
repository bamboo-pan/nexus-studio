import base64
import json

import pytest

from aistudio_api.infrastructure.gateway import native_ui_sender
from aistudio_api.infrastructure.gateway.session import AI_STUDIO_CURRENT_TEXT_MODEL_JS, AI_STUDIO_SELECT_TEXT_MODEL_JS
from aistudio_api.infrastructure.gateway.native_ui_sender import NativeUiSenderWorker, _send_on_page


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
        self.response_model = "gemini-3.5-flash"
        self.response_sequence = None
        self.current_model_read_calls = 0
        self.current_model_matches = False
        self.current_model_label = "Chat Spark Playground"
        self.current_model_match_after_success = True
        self.closed = False

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

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def evaluate(self, script: str, *args):
        if script == "mw:!!window.default_MakerSuite":
            return True
        if script == native_ui_sender.DIALOG_CLEANUP_JS:
            self.cleanup_calls += 1
            return None
        if script == AI_STUDIO_CURRENT_TEXT_MODEL_JS:
            self.current_model_read_calls += 1
            return {"matches": self.current_model_matches, "label": self.current_model_label}
        if "text_model_not_found" in script:
            self.model_selection_calls += 1
            if self.model_selection_results:
                result = self.model_selection_results.pop(0)
                if self.current_model_match_after_success and (result.get("selected") is True or result.get("reason") == "already_selected"):
                    self.current_model_matches = True
                    self.current_model_label = str(result.get("label") or self.response_model)
                return result
            if self.current_model_match_after_success:
                self.current_model_matches = True
                self.current_model_label = "Gemini 3.5 Flash"
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
        body = json.dumps([f"models/{self.response_model}", {"text": "warmup"}], ensure_ascii=False)
        request = FakeRequest(body)
        generate_url = (
            "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
            "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent"
        )
        response_specs = self.response_sequence or [
            (
                "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
                "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/"
                "StreamGenerateContentPerUserQuota",
                404,
                b"quota helper failed",
            ),
            (generate_url, 200, b"main generate ok"),
        ]
        responses = [FakeResponse(url=url, status=status, body=response_body, request=request) for url, status, response_body in response_specs]
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


def test_send_on_page_ignores_per_user_quota_ambiguous_body_on_generate_url():
    page = FakePage()
    generate_url = (
        "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
        "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent"
    )
    page.response_sequence = [
        (
            generate_url,
            404,
            (
                "[, [5, \"Ambiguous request for service '' and method "
                "'/GenerativeService.StreamGenerateContentPerUserQuota'. Please use fully qualified names.\"]]"
            ).encode("utf-8"),
        ),
        (generate_url, 200, b"main generate ok after ambiguous helper"),
    ]

    result = _send_on_page(page, model="gemini-3.5-flash", prompt="warmup", timeout_ms=1000)

    assert result["status"] == 200
    assert base64.b64decode(str(result["body_b64"])) == b"main generate ok after ambiguous helper"
    assert result["wire_model"] == "models/gemini-3.5-flash"


def test_text_model_selector_scans_model_selector_cards():
    assert ".model-selector-card" in AI_STUDIO_SELECT_TEXT_MODEL_JS


def test_text_model_picker_opener_recognizes_chat_spark_control():
    assert "chat spark playground" in native_ui_sender.AI_STUDIO_OPEN_MODEL_PICKER_JS
    assert "code and chat" in native_ui_sender.AI_STUDIO_OPEN_MODEL_PICKER_JS
    assert "text_category" in native_ui_sender.AI_STUDIO_OPEN_MODEL_PICKER_JS
    assert "system instructions" in native_ui_sender.AI_STUDIO_OPEN_MODEL_PICKER_JS
    assert "[tabindex]" in native_ui_sender.AI_STUDIO_OPEN_MODEL_PICKER_JS
    assert "nearestPickerControl" in native_ui_sender.AI_STUDIO_OPEN_MODEL_PICKER_JS


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


def test_send_on_page_reopens_picker_after_target_card_fallback():
    page = FakePage()
    page.response_model = "gemini-3-flash-preview"
    page.open_picker_results = [
        {"opened": True, "type": "target_card", "label": "Gemini 3 Flash Preview"},
        {"opened": True, "type": "picker_control", "label": "Chat Spark Playground"},
    ]
    page.model_selection_results = [
        {"selected": False, "reason": "text_model_not_found", "current": "chat spark playground", "visible": []},
        {"selected": True, "label": "Gemini 3 Flash Preview"},
    ]

    result = _send_on_page(page, model="gemini-3-flash-preview", prompt="warmup", timeout_ms=60_000)

    assert result["status"] == 200
    assert page.open_picker_calls == 2
    assert page.model_selection_calls == 2


def test_send_on_page_reopens_picker_after_text_category_navigation():
    page = FakePage()
    page.response_model = "gemini-3-flash-preview"
    page.open_picker_results = [
        {"opened": True, "type": "text_category", "label": "Code and Chat Build chatbots, agents, and code with Gemini 3."},
        {"opened": True, "type": "picker_control", "label": "Chat Spark Playground"},
    ]
    page.model_selection_results = [
        {"selected": False, "reason": "text_model_not_found", "current": "chat spark playground", "visible": []},
        {"selected": True, "label": "Gemini 3 Flash Preview"},
    ]

    result = _send_on_page(page, model="gemini-3-flash-preview", prompt="warmup", timeout_ms=60_000)

    assert result["status"] == 200
    assert page.open_picker_calls == 2
    assert page.model_selection_calls == 2


def test_send_on_page_rejects_selected_click_when_current_model_readback_mismatches():
    page = FakePage()
    page.current_model_match_after_success = False
    page.model_selection_results = [
        {"selected": True, "label": "Gemini 3 Flash Preview"},
        {"selected": True, "label": "Gemini 3 Flash Preview"},
        {"selected": True, "label": "Gemini 3 Flash Preview"},
    ]

    with pytest.raises(RuntimeError, match="current="):
        _send_on_page(page, model="gemini-3.5-flash", prompt="warmup", timeout_ms=60_000)

    assert page.current_model_read_calls >= 2
    assert page.model_selection_calls >= 1


def test_send_on_page_uses_extended_open_chat_budget(monkeypatch):
    page = FakePage()
    open_chat_calls = []

    def open_chat(_page, timeout_ms: int, model: str = "", *, prime_home: bool = True):
        open_chat_calls.append((timeout_ms, model, prime_home))

    monkeypatch.setattr(native_ui_sender, "_open_chat", open_chat)

    result = _send_on_page(page, model="gemini-3.5-flash", prompt="warmup", timeout_ms=300_000)

    assert result["status"] == 200
    assert open_chat_calls == [(180_000, "gemini-3.5-flash", True)]


def test_send_on_page_can_skip_home_prime_for_warmed_worker_page(monkeypatch):
    page = FakePage()
    open_chat_calls = []

    def open_chat(_page, timeout_ms: int, model: str = "", *, prime_home: bool = True):
        open_chat_calls.append((timeout_ms, model, prime_home))

    monkeypatch.setattr(native_ui_sender, "_open_chat", open_chat)

    result = _send_on_page(page, model="gemini-3.5-flash", prompt="warmup", timeout_ms=300_000, prime_home=False)

    assert result["status"] == 200
    assert open_chat_calls == [(180_000, "gemini-3.5-flash", False)]


class FakeContext:
    def __init__(self):
        self.pages = []

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self):
        for page in self.pages:
            page.close()


def test_native_sender_worker_reuses_warmed_page(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    context = FakeContext()

    worker = NativeUiSenderWorker()
    monkeypatch.setattr(worker, "_ensure_context", lambda auth_file: context)

    first = worker.send({"auth_file": str(auth_file), "model": "gemini-3.5-flash", "prompt": "warmup", "timeout_ms": 1000})
    second = worker.send({"auth_file": str(auth_file), "model": "gemini-3.5-flash", "prompt": "warmup", "timeout_ms": 1000})

    assert first["status"] == 200
    assert second["status"] == 200
    assert len(context.pages) == 1
    assert context.pages[0].filled_prompts == ["warmup", "warmup"]


def test_native_sender_worker_closes_warmed_page_after_request_failure(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    context = FakeContext()

    worker = NativeUiSenderWorker()
    monkeypatch.setattr(worker, "_ensure_context", lambda auth_file: context)
    original_send_on_page = native_ui_sender._send_on_page
    calls = {"count": 0}

    def flaky_send_on_page(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("native UI sender timeout")
        return original_send_on_page(*args, **kwargs)

    monkeypatch.setattr(native_ui_sender, "_send_on_page", flaky_send_on_page)

    with pytest.raises(RuntimeError, match="native UI sender timeout"):
        worker.send({"auth_file": str(auth_file), "model": "gemini-3.5-flash", "prompt": "warmup", "timeout_ms": 1000})

    assert context.pages[0].closed is True

    result = worker.send({"auth_file": str(auth_file), "model": "gemini-3.5-flash", "prompt": "warmup", "timeout_ms": 1000})

    assert result["status"] == 200
    assert len(context.pages) == 2


def test_open_chat_shares_timeout_across_candidate_urls(monkeypatch):
    page = FakePage()
    now = {"value": 100.0}
    ready_timeouts = []

    monkeypatch.setattr(
        native_ui_sender,
        "_aistudio_chat_urls",
        lambda model=None: (
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
        lambda model=None: (
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
        lambda model=None: (
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
        lambda model=None: ("https://aistudio.google.com/u/0/prompts/new_chat",),
    )
    monkeypatch.setattr(native_ui_sender, "_wait_for_chat_ready", lambda _page, _timeout_ms: True)

    native_ui_sender._open_chat(page, 90_000)

    assert page.goto_urls == ["https://aistudio.google.com/", "https://aistudio.google.com/u/0/prompts/new_chat"]


def test_open_chat_passes_target_model_to_chat_url_candidates(monkeypatch):
    page = FakePage()
    requested_models = []

    def chat_urls(model=None):
        requested_models.append(model)
        return ("https://aistudio.google.com/u/0/prompts/new_chat?model=gemini-3.5-flash",)

    monkeypatch.setattr(native_ui_sender, "_aistudio_chat_urls", chat_urls)
    monkeypatch.setattr(native_ui_sender, "_wait_for_chat_ready", lambda _page, _timeout_ms: True)

    native_ui_sender._open_chat(page, 90_000, "gemini-3.5-flash")

    assert requested_models == ["gemini-3.5-flash"]
    assert page.goto_urls == [
        "https://aistudio.google.com/",
        "https://aistudio.google.com/u/0/prompts/new_chat?model=gemini-3.5-flash",
    ]
