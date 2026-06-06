import base64
import json

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
        self.wait_calls = []
        self.model_selection_results = []
        self.model_selection_calls = 0

    def goto(self, url: str, **kwargs):
        self.goto_urls.append(url)
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
        if "text_model_not_found" in script:
            self.model_selection_calls += 1
            if self.model_selection_results:
                return self.model_selection_results.pop(0)
            return {"selected": True, "label": "Gemini 3.5 Flash"}
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