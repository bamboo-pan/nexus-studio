import json

from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.capture import RequestCaptureService
from aistudio_api.infrastructure.gateway.request_rewriter import AistudioWireCodec, modify_body


def test_modify_body_updates_generation_config_and_prompt():
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16],"!snap",null,null]'
    rewritten = modify_body(
        original,
        model="models/new",
        prompt="new prompt",
        system_instruction="sys",
        max_tokens=256,
        temperature=0.2,
        top_p=0.9,
        top_k=32,
    )

    assert '"models/new"' in rewritten
    assert '"new prompt"' in rewritten
    assert '"sys"' in rewritten
    body = json.loads(rewritten)
    assert body[3][3] == 256
    assert body[3][4] == 0.2
    assert body[3][5] == 0.9
    assert body[3][6] == 32
    assert body[3][16] == [1, None, None, 3]
    assert body[3][17] == 1


def test_wire_codec_decodes_semantic_fields():
    codec = AistudioWireCodec()
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16],"!snap",[[[null,"sys"]],"user"],[[[]]]]'

    decoded = codec.decode(original)

    assert decoded.model == "models/original"
    assert decoded.snapshot == "!snap"
    assert decoded.contents[0].role == "user"
    assert decoded.contents[0].parts[-1].text == "old"
    assert decoded.system_instruction is not None
    assert decoded.system_instruction.parts[0].text == "sys"
    assert decoded.generation_config.max_tokens == 128


def test_modify_body_sanitizes_plain_text_generation_config():
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16,"application/json",[6],null,null,null,null,null,null,null,[1,null,null,3]],"!snap",null,null]'
    rewritten = modify_body(
        original,
        model="models/gemma-4-31b-it",
        prompt="hello",
    )

    assert '"text/plain"' in rewritten
    assert '"application/json"' not in rewritten
    assert '[6]' not in rewritten
    assert json.loads(rewritten)[3][16] == [1, None, None, 3]
    assert json.loads(rewritten)[3][17] == 1


def test_modify_body_enables_thinking_for_any_model():
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16],"!snap",null,null]'
    rewritten = modify_body(
        original,
        model="models/new-text-model",
        prompt="hello",
    )

    body = json.loads(rewritten)
    assert body[3][16] == [1, None, None, 3]
    assert body[3][17] == 1


def test_modify_body_keeps_structured_generation_config_for_gemini_mode():
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,["6"],null,128,0.5,0.8,16,"application/json",[6],0.1,0.2,true,5,null,[2,1],null,[1,null,null,3],1],"!snap",null,null,null,null,null,1,"cached",null,[[null,null,"Asia/Shanghai"]]]'
    rewritten = modify_body(
        original,
        model="models/gemini-2.5-pro-preview-05-06",
        prompt="hello",
        generation_config_overrides={
            "stop_sequences": ["STOP"],
            "response_mime_type": "application/json",
            "response_schema": [6],
            "presence_penalty": 0.3,
            "frequency_penalty": 0.4,
            "response_logprobs": True,
            "logprobs": 7,
            "media_resolution": [2, 1],
            "thinking_config": [1, None, None, 3],
            "request_flag": 1,
        },
        sanitize_plain_text=False,
    )

    body = json.loads(rewritten)
    assert body[3][1] == ["STOP"]
    assert body[3][7] == "application/json"
    assert body[3][8] == [6]
    assert body[3][9] == 0.3
    assert body[3][10] == 0.4
    assert body[3][11] is True
    assert body[3][12] == 7
    assert body[3][14] == [2, 1]
    assert body[3][16] == [1, None, None, 3]
    assert body[3][17] == 1


def test_modify_body_strips_unsupported_image_generation_config_and_sets_size():
    original = '["models/gemini-3.1-flash-image-preview",[[[[null,"old"]],"user"]],null,[null,["6"],null,65536,1,0.95,64,"application/json",[6],null,null,null,null,null,[2,1],null,[1,null,null,3],1,null,null,null,null,null,null,null,null,[null,"512"]],"!snap",null,null]'

    rewritten = modify_body(
        original,
        model="gemini-3.1-flash-image-preview",
        prompt="new image",
        generation_config_overrides={"output_image_size": [None, "1K"]},
        sanitize_plain_text=False,
        enable_thinking=False,
    )

    body = json.loads(rewritten)
    assert body[3][1] is None
    assert body[3][7] is None
    assert body[3][8] is None
    assert body[3][14] is None
    assert body[3][16] is None
    assert body[3][17] is None
    assert body[3][26] == [None, "1K"]
    assert body[10] is None


def test_modify_body_uses_requested_model_even_when_template_model_differs():
    original = '["models/gemini-3-flash-preview",[[[[null,"old"]],"user"]],null,[null,null,null,128],"!snap",null,null]'

    rewritten = modify_body(
        original,
        model="gemini-3.1-flash-image-preview",
        prompt="new image",
        sanitize_plain_text=False,
        enable_thinking=False,
    )

    body = json.loads(rewritten)
    assert body[0] == "models/gemini-3.1-flash-image-preview"


def test_modify_body_maps_discovered_gemini_35_flash_to_stable_wire_model():
    original = '["models/gemini-3-flash-preview",[[[[null,"old"]],"user"]],null,[null,null,null,128],"!snap",null,null]'

    rewritten = modify_body(
        original,
        model="gemini-3.5-flash",
        prompt="hello",
    )

    body = json.loads(rewritten)
    assert body[0] == "models/gemini-3-flash-preview"


def test_clear_capture_state_clears_session_template_cache():
    class FakeCaptureService:
        def __init__(self):
            self.cleared = False

        def clear_templates(self):
            self.cleared = True

    class FakeSession:
        def __init__(self):
            self.cleared = False

        def clear_templates(self):
            self.cleared = True

    client = AIStudioClient(use_pure_http=True)
    capture_service = FakeCaptureService()
    session = FakeSession()
    client._capture_service = capture_service
    client._session = session

    client.clear_capture_state()

    assert capture_service.cleared is True
    assert session.cleared is True


def test_capture_resolves_model_alias_before_template_and_cache():
    class FakeSnapshotCache:
        def __init__(self):
            self.get_models = []
            self.put_models = []

        def get(self, prompt, model=None):
            self.get_models.append(model)
            return None

        def put(self, prompt, snapshot, url, headers, body, model=None):
            self.put_models.append(model)

    class FakeSession:
        def __init__(self):
            self.template_models = []

        async def capture_template(self, model):
            self.template_models.append(model)
            return {
                "url": "https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
                "headers": {"content-type": "application/json"},
                "body": '["models/gemini-3-flash-preview",[[[[null,"old"]],"user"]],null,[null,null,null,64],"template-snapshot",null,null]',
            }

        async def generate_snapshot(self, contents):
            return "fresh-snapshot"

    cache = FakeSnapshotCache()
    session = FakeSession()
    service = RequestCaptureService(session, cache)

    import asyncio

    captured = asyncio.run(service.capture("hello", model="gemini-3.5-flash"))

    assert session.template_models == ["models/gemini-3-flash-preview"]
    assert cache.get_models == ["models/gemini-3-flash-preview"]
    assert cache.put_models == ["models/gemini-3-flash-preview"]
    assert json.loads(captured.body)[0] == "models/gemini-3-flash-preview"


def test_capture_with_images_obtains_template_before_rewrite():
    class FakeSnapshotCache:
        def __init__(self):
            self.get_calls = 0
            self.put_calls = 0

        def get(self, prompt, model=None):
            self.get_calls += 1
            return None

        def put(self, prompt, snapshot, url, headers, body, model=None):
            self.put_calls += 1

    class FakeSession:
        def __init__(self):
            self.template_models = []

        async def capture_template(self, model):
            self.template_models.append(model)
            return {
                "url": "https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
                "headers": {"content-type": "application/json"},
                "body": '["models/gemini-3-flash-preview",[[[[null,"old"]],"user"]],null,[null,null,null,64],"template-snapshot",null,null]',
            }

        async def generate_snapshot(self, contents):
            return "fresh-snapshot"

    cache = FakeSnapshotCache()
    session = FakeSession()
    service = RequestCaptureService(session, cache)

    import asyncio

    captured = asyncio.run(service.capture("hello", model="gemini-3.5-flash", images=["fake.png"]))

    assert session.template_models == ["models/gemini-3-flash-preview"]
    assert cache.get_calls == 0
    assert cache.put_calls == 0
    assert json.loads(captured.body)[0] == "models/gemini-3-flash-preview"


def test_capture_force_refresh_obtains_template_before_rewrite():
    class FakeSnapshotCache:
        def get(self, prompt, model=None):
            return ("cached", "https://cached.example", {}, '["models/cached",[],null,null,"cached"]')

        def put(self, prompt, snapshot, url, headers, body, model=None):
            self.put_model = model

    class FakeSession:
        def __init__(self):
            self.template_models = []

        async def capture_template(self, model):
            self.template_models.append(model)
            return {
                "url": "https://aistudio.google.com/_/BardChatUi/data/batchexecute/GenerateContent",
                "headers": {"content-type": "application/json"},
                "body": '["models/gemini-3-flash-preview",[[[[null,"old"]],"user"]],null,[null,null,null,64],"template-snapshot",null,null]',
            }

        async def generate_snapshot(self, contents):
            return "fresh-snapshot"

    cache = FakeSnapshotCache()
    session = FakeSession()
    service = RequestCaptureService(session, cache)

    import asyncio

    captured = asyncio.run(service.capture("hello", model="gemini-3.5-flash", force_refresh=True))

    assert session.template_models == ["models/gemini-3-flash-preview"]
    assert cache.put_model == "models/gemini-3-flash-preview"
    assert json.loads(captured.body)[4] == "fresh-snapshot"
