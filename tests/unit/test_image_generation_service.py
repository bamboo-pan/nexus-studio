import asyncio
import base64
import json
import tempfile

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from aistudio_api.config import settings
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.routes_openai import router as openai_router
from aistudio_api.api.routes_generated_images import register_generated_image_routes
from aistudio_api.api.schemas import ChatRequest, ImagePromptOptimizationRequest, ImageRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.application.account_service import AccountService
from aistudio_api.application.api_service import handle_chat, handle_image_generation, handle_image_prompt_optimization
from aistudio_api.domain.errors import AuthError, RequestError
from aistudio_api.domain.models import Candidate, GeneratedImage, ModelOutput
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.account.login_service import LoginService


SQUARE_PROMPT_SUFFIX = "Use a square 1:1 composition."
_UNSET = object()


class FakeImageClient:
    def __init__(self):
        self.calls = []

    async def generate_image(self, *, prompt, model, generation_config_overrides=None, images=None, timeout=_UNSET):
        call = {
            "prompt": prompt,
            "model": model,
            "generation_config_overrides": generation_config_overrides,
            "images": images,
        }
        if timeout is not _UNSET:
            call["timeout"] = timeout
        self.calls.append(call)
        image_bytes = f"image-{len(self.calls)}".encode("ascii")
        return ModelOutput(
            candidates=[
                Candidate(
                    text=f"revised-{len(self.calls)}",
                    images=[GeneratedImage(mime="image/png", data=image_bytes, size=len(image_bytes))],
                )
            ],
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )


class FakeEmptyImageClient(FakeImageClient):
    async def generate_image(self, *, prompt, model, generation_config_overrides=None, images=None, timeout=_UNSET):
        call = {
            "prompt": prompt,
            "model": model,
            "generation_config_overrides": generation_config_overrides,
            "images": images,
        }
        if timeout is not _UNSET:
            call["timeout"] = timeout
        self.calls.append(call)
        return ModelOutput(candidates=[Candidate(text="no image")])


class FakeErrorImageClient(FakeImageClient):
    async def generate_image(self, *, prompt, model, generation_config_overrides=None, images=None, timeout=_UNSET):
        call = {"prompt": prompt, "model": model, "images": images}
        if timeout is not _UNSET:
            call["timeout"] = timeout
        self.calls.append(call)
        raise RequestError(0, "capture failed")


class FakeSecondImageErrorClient(FakeImageClient):
    async def generate_image(self, *, prompt, model, generation_config_overrides=None, images=None, timeout=_UNSET):
        if self.calls:
            call = {"prompt": prompt, "model": model, "images": images}
            if timeout is not _UNSET:
                call["timeout"] = timeout
            self.calls.append(call)
            raise RequestError(0, "second capture failed")
        kwargs = {"prompt": prompt, "model": model, "generation_config_overrides": generation_config_overrides, "images": images}
        if timeout is not _UNSET:
            kwargs["timeout"] = timeout
        return await super().generate_image(**kwargs)


class FakeAuthThenSuccessImageClient(FakeImageClient):
    def __init__(self):
        super().__init__()
        self.clear_calls = 0

    def clear_capture_state(self):
        self.clear_calls += 1

    async def generate_image(self, *, prompt, model, generation_config_overrides=None, images=None, timeout=_UNSET):
        if not self.calls:
            call = {"prompt": prompt, "model": model, "generation_config_overrides": generation_config_overrides, "images": images}
            if timeout is not _UNSET:
                call["timeout"] = timeout
            self.calls.append(call)
            raise AuthError("stale capture auth")
        kwargs = {"prompt": prompt, "model": model, "generation_config_overrides": generation_config_overrides, "images": images}
        if timeout is not _UNSET:
            kwargs["timeout"] = timeout
        return await super().generate_image(**kwargs)


class FakeEmptyThenSuccessImageClient(FakeImageClient):
    def __init__(self):
        super().__init__()
        self.clear_calls = 0

    def clear_capture_state(self):
        self.clear_calls += 1

    async def generate_image(self, *, prompt, model, generation_config_overrides=None, images=None, timeout=_UNSET):
        if not self.calls:
            call = {"prompt": prompt, "model": model, "generation_config_overrides": generation_config_overrides, "images": images}
            if timeout is not _UNSET:
                call["timeout"] = timeout
            self.calls.append(call)
            return ModelOutput(candidates=[Candidate(text="empty image response")])
        kwargs = {"prompt": prompt, "model": model, "generation_config_overrides": generation_config_overrides, "images": images}
        if timeout is not _UNSET:
            kwargs["timeout"] = timeout
        return await super().generate_image(**kwargs)


class FakeChatClient:
    def __init__(self):
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return ModelOutput(candidates=[Candidate(text='{"ok":true}')], usage={"total_tokens": 1})


class FakePromptOptimizerClient:
    def __init__(self):
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return ModelOutput(
            candidates=[
                Candidate(
                    text=json.dumps(
                        {
                            "options": [
                                {"title": "构图稳定版", "special": "主体和空间关系更明确", "prompt": "优化提示词一"},
                                {"title": "质感精修版", "special": "强化材质、光线和细节", "prompt": "优化提示词二"},
                                {"title": "氛围创意版", "special": "加入镜头感和情绪变化", "prompt": "优化提示词三"},
                            ]
                        },
                        ensure_ascii=False,
                    )
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )


def run_with_runtime(coro, *, generated_images_dir=None, account_service=None, rotator=None):
    old_busy_lock = runtime_state.busy_lock
    old_account_service = runtime_state.account_service
    old_rotator = runtime_state.rotator
    old_generated_images_dir = settings.generated_images_dir
    old_generated_images_route = settings.generated_images_route
    temp_dir = None
    if generated_images_dir is None:
        temp_dir = tempfile.TemporaryDirectory()
        generated_images_dir = temp_dir.name
    runtime_state.busy_lock = asyncio.Semaphore(3)
    runtime_state.account_service = account_service
    runtime_state.rotator = rotator
    settings.generated_images_dir = str(generated_images_dir)
    settings.generated_images_route = "/generated-images"
    try:
        return asyncio.run(coro)
    finally:
        runtime_state.busy_lock = old_busy_lock
        runtime_state.account_service = old_account_service
        runtime_state.rotator = old_rotator
        settings.generated_images_dir = old_generated_images_dir
        settings.generated_images_route = old_generated_images_route
        if temp_dir is not None:
            temp_dir.cleanup()


def storage_state(cookie_name="sid", cookie_value="1"):
    return {"cookies": [{"name": cookie_name, "value": cookie_value, "domain": ".google.com", "path": "/"}]}


def account_runtime(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state(), tier="pro")
    service = AccountService(store, LoginService())
    return account, service, AccountRotator(store)


def request_app(app: FastAPI, method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(send())


def openai_app(client) -> FastAPI:
    app = FastAPI()
    app.include_router(openai_router)
    app.dependency_overrides[get_client] = lambda: client
    return app


def generated_images_app(image_dir) -> FastAPI:
    app = FastAPI()
    register_generated_image_routes(app)
    app.mount("/generated-images", StaticFiles(directory=str(image_dir)), name="generated-images")
    return app


def run_stream_with_runtime(coro):
    async def collect():
        response = await coro
        assert isinstance(response, StreamingResponse)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        return response, "".join(chunks)

    return run_with_runtime(collect())


def test_image_generation_runs_n_sequential_calls_and_aggregates_images():
    client = FakeImageClient()
    req = ImageRequest(
        prompt="draw a city",
        model="gemini-3.1-flash-image-preview",
        n=2,
        size="1024x1024",
    )

    response = run_with_runtime(handle_image_generation(req, client))

    assert len(client.calls) == 2
    assert all(call["model"] == "gemini-3.1-flash-image-preview" for call in client.calls)
    assert all(call["generation_config_overrides"] == {"output_image_size": [None, "1K"]} for call in client.calls)
    assert all(call["prompt"] == f"draw a city\n\n{SQUARE_PROMPT_SUFFIX}" for call in client.calls)
    assert all(call["images"] is None for call in client.calls)
    assert all("timeout" not in call for call in client.calls)
    assert [base64.b64decode(item["b64_json"]) for item in response["data"]] == [b"image-1", b"image-2"]


def test_image_generation_forwards_explicit_timeout_to_client():
    client = FakeImageClient()
    req = ImageRequest(
        prompt="draw a city",
        model="gemini-3.1-flash-image-preview",
        timeout=241,
    )

    response = run_with_runtime(handle_image_generation(req, client))

    assert response["data"][0]["b64_json"]
    assert len(client.calls) == 1
    assert client.calls[0]["timeout"] == 241


@pytest.mark.parametrize("timeout", [0, -1])
def test_image_generation_rejects_invalid_timeout_before_client_call(timeout):
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", timeout=timeout)

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 400
    assert "timeout" in error.value.detail["message"]
    assert client.calls == []


def test_image_generation_passes_data_uri_images_to_client_and_cleans_temp_file():
    client = FakeImageClient()
    req = ImageRequest(
        prompt="make it painterly",
        model="gemini-3.1-flash-image-preview",
        images=[{"url": "data:image/png;base64,aGVsbG8="}],
    )

    response = run_with_runtime(handle_image_generation(req, client))

    assert response["data"][0]["b64_json"]
    image_paths = client.calls[0]["images"]
    assert image_paths and len(image_paths) == 1
    assert image_paths[0].endswith(".png")
    assert not __import__("pathlib").Path(image_paths[0]).exists()


def test_image_generation_rejects_invalid_edit_image_url_before_client_call():
    client = FakeImageClient()
    req = ImageRequest(prompt="edit", model="gemini-3.1-flash-image-preview", images=["/generated-images/a.png"])

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 400
    assert "images[0].url" in error.value.detail["message"]
    assert client.calls == []


def test_image_generation_rejects_unsupported_size_before_client_call():
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", size="256x256")

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 400
    assert "256x256" in error.value.detail["message"]
    assert client.calls == []


@pytest.mark.parametrize(
    ("size", "output_image_size"),
    [("2048x2048", "2K"), ("4096x4096", "4K")],
)
def test_image_generation_accepts_pro_high_resolution_sizes(size, output_image_size):
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3-pro-image-preview", size=size)

    response = run_with_runtime(handle_image_generation(req, client))

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "gemini-3-pro-image-preview"
    assert client.calls[0]["generation_config_overrides"] == {"output_image_size": [None, output_image_size]}
    assert client.calls[0]["prompt"] == f"draw\n\n{SQUARE_PROMPT_SUFFIX}"
    assert response["data"][0]["b64_json"]


def test_image_generation_rejects_non_image_model_before_client_call():
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3-flash-preview")

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 400
    assert "image generation" in error.value.detail["message"]
    assert client.calls == []


def test_image_generation_rejects_empty_prompt_before_client_call():
    client = FakeImageClient()
    req = ImageRequest(prompt="   ", model="gemini-3.1-flash-image-preview")

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 400
    assert "prompt" in error.value.detail["message"]
    assert client.calls == []


def test_image_generation_rejects_invalid_n_before_client_call():
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", n=0)

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 400
    assert "n" in error.value.detail["message"]
    assert client.calls == []


def test_image_generation_accepts_url_response_format_with_persisted_file_url(tmp_path):
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", response_format="url")

    response = run_with_runtime(handle_image_generation(req, client), generated_images_dir=tmp_path)

    assert len(client.calls) == 1
    item = response["data"][0]
    assert item["url"].startswith("/generated-images/")
    assert item["path"]
    assert item["delete_url"] == item["url"]
    assert item["mime_type"] == "image/png"
    assert item["size_bytes"] == len(b"image-1")
    assert base64.b64decode(item["b64_json"]) == b"image-1"
    assert (tmp_path / item["path"]).read_bytes() == b"image-1"


def test_generated_image_static_serving_and_delete_route(tmp_path):
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", response_format="url")

    old_dir = settings.generated_images_dir
    old_route = settings.generated_images_route
    settings.generated_images_dir = str(tmp_path)
    settings.generated_images_route = "/generated-images"
    try:
        response = run_with_runtime(handle_image_generation(req, client), generated_images_dir=tmp_path)
        item = response["data"][0]
        app = generated_images_app(tmp_path)

        static_response = request_app(app, "GET", item["url"])
        delete_response = request_app(app, "DELETE", item["delete_url"])
    finally:
        settings.generated_images_dir = old_dir
        settings.generated_images_route = old_route

    assert static_response.status_code == 200
    assert static_response.content == b"image-1"
    assert delete_response.status_code == 200
    assert delete_response.json() == {"ok": True, "path": item["path"]}
    assert not (tmp_path / item["path"]).exists()


def test_generated_image_delete_rejects_traversal(tmp_path):
    old_dir = settings.generated_images_dir
    old_route = settings.generated_images_route
    settings.generated_images_dir = str(tmp_path)
    settings.generated_images_route = "/generated-images"
    try:
        app = generated_images_app(tmp_path)
        response = request_app(app, "DELETE", "/generated-images/%2e%2e/secret.png")
    finally:
        settings.generated_images_dir = old_dir
        settings.generated_images_route = old_route

    assert response.status_code == 400
    assert "outside generated image storage" in response.json()["detail"]["message"]


def test_image_generation_rejects_unknown_response_format_before_client_call():
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", response_format="file")

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 400
    assert "response_format" in error.value.detail["message"]
    assert client.calls == []


def test_image_generation_rejects_unsupported_openai_image_fields_before_client_call():
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", quality="hd", style="vivid")

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 400
    assert "quality" in error.value.detail["message"]
    assert "style" in error.value.detail["message"]
    assert client.calls == []


def test_image_generation_accepts_compatibility_user_field_without_claiming_effect():
    client = FakeImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", user="client-trace")

    response = run_with_runtime(handle_image_generation(req, client))

    assert len(client.calls) == 1
    assert response["data"][0]["b64_json"]


def test_image_request_accepts_timeout_but_still_rejects_unknown_extra_fields():
    req = ImageRequest.model_validate({"prompt": "draw", "timeout": 180})

    assert req.timeout == 180


def test_image_request_rejects_unknown_extra_fields():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ImageRequest.model_validate({"prompt": "draw", "seed": 123})


def test_image_generation_route_rejects_unknown_extra_fields_as_bad_request():
    client = FakeImageClient()

    response = request_app(
        openai_app(client),
        "POST",
        "/v1/images/generations",
        json={"prompt": "draw", "model": "gemini-3.1-flash-image-preview", "seed": 123},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["type"] == "invalid_request_error"
    assert "seed" in response.json()["detail"]["message"]
    assert client.calls == []


def test_image_generation_returns_friendly_error_when_no_image_data():
    client = FakeEmptyImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview")

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 502
    assert error.value.detail["type"] == "upstream_error"
    assert "no image data" in error.value.detail["message"]


def test_image_generation_translates_low_level_request_error():
    client = FakeErrorImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview")

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client))

    assert error.value.status_code == 502
    assert error.value.detail["type"] == "upstream_error"


def test_image_generation_retries_auth_error_with_fresh_capture_without_counting_transient_error(tmp_path):
    account, service, rotator = account_runtime(tmp_path / "accounts")
    client = FakeAuthThenSuccessImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview")

    response = run_with_runtime(handle_image_generation(req, client), account_service=service, rotator=rotator)

    account_stats = rotator.get_all_stats()[account.id]
    assert response["data"][0]["b64_json"]
    assert client.clear_calls == 1
    assert len(client.calls) == 2
    assert account_stats["requests"] == 1
    assert account_stats["success"] == 1
    assert account_stats["errors"] == 0


def test_image_generation_retries_empty_image_response_with_fresh_capture(tmp_path):
    account, service, rotator = account_runtime(tmp_path / "accounts")
    client = FakeEmptyThenSuccessImageClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview")

    response = run_with_runtime(handle_image_generation(req, client), account_service=service, rotator=rotator)

    account_stats = rotator.get_all_stats()[account.id]
    assert response["data"][0]["b64_json"]
    assert client.clear_calls == 1
    assert len(client.calls) == 2
    assert account_stats["requests"] == 1
    assert account_stats["success"] == 1
    assert account_stats["errors"] == 0


def test_image_generation_cleans_up_persisted_files_when_later_generation_fails(tmp_path):
    client = FakeSecondImageErrorClient()
    req = ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", n=2)

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_generation(req, client), generated_images_dir=tmp_path)

    assert error.value.status_code == 502
    assert client.calls == [
        {
            "prompt": f"draw\n\n{SQUARE_PROMPT_SUFFIX}",
            "model": "gemini-3.1-flash-image-preview",
            "generation_config_overrides": {"output_image_size": [None, "1K"]},
            "images": None,
        },
        {"prompt": f"draw\n\n{SQUARE_PROMPT_SUFFIX}", "model": "gemini-3.1-flash-image-preview", "images": None},
    ]
    assert list(tmp_path.rglob("*")) == []


def test_chat_completion_with_image_model_returns_markdown_image():
    client = FakeImageClient()
    req = ChatRequest(
        model="gemini-3.1-flash-image-preview",
        messages=[{"role": "user", "content": "draw a city"}],
    )

    response = run_with_runtime(handle_chat(req, client))

    assert len(client.calls) == 1
    assert client.calls[0]["prompt"] == f"draw a city\n\n{SQUARE_PROMPT_SUFFIX}"
    content = response["choices"][0]["message"]["content"]
    assert content.startswith("![generated image 1](/generated-images/")
    assert content.endswith(".png)")


def test_chat_completion_with_image_model_rejects_unsupported_chat_fields():
    client = FakeImageClient()
    req = ChatRequest(
        model="gemini-3.1-flash-image-preview",
        messages=[{"role": "user", "content": "draw a city"}],
        temperature=0.5,
    )

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_chat(req, client))

    assert error.value.status_code == 400
    assert "temperature" in error.value.detail["message"]
    assert client.calls == []


def test_chat_completion_with_image_model_ignores_stream_true_and_returns_sse_image():
    client = FakeImageClient()
    req = ChatRequest(
        model="gemini-3.1-flash-image-preview",
        messages=[{"role": "user", "content": "draw a city"}],
        stream=True,
    )

    response, stream = run_stream_with_runtime(handle_chat(req, client))

    assert response.media_type == "text/event-stream"
    assert len(client.calls) == 1
    assert "streaming responses" not in stream
    payload = json.loads(stream.split("\n\n", 1)[0].removeprefix("data: "))
    content = payload["choices"][0]["delta"]["content"]
    assert content.startswith("![generated image 1](/generated-images/")
    assert content.endswith(".png)")
    assert "data: [DONE]" in stream


def test_chat_completion_rejects_empty_messages():
    client = FakeChatClient()
    req = ChatRequest(model="gemini-3-flash-preview", messages=[])

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_chat(req, client))

    assert error.value.status_code == 400
    assert "messages" in error.value.detail["message"]
    assert client.calls == []


def test_chat_completion_rejects_illegal_role():
    client = FakeChatClient()
    req = ChatRequest(model="gemini-3-flash-preview", messages=[{"role": "alien", "content": "hello"}])

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_chat(req, client))

    assert error.value.status_code == 400
    assert "role" in error.value.detail["message"]
    assert client.calls == []


def test_chat_completion_rejects_malformed_image_url_part_before_client_call():
    client = FakeChatClient()
    req = ChatRequest(
        model="gemini-3-flash-preview",
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"name": "missing-url"}}]}],
    )

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_chat(req, client))

    assert error.value.status_code == 400
    assert "image_url.url" in error.value.detail["message"]
    assert client.calls == []


def test_chat_completion_forwards_inline_file_attachment_to_client():
    client = FakeChatClient()
    req = ChatRequest(
        model="gemini-3-flash-preview",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "summarize"},
                    {
                        "type": "file",
                        "file": {
                            "filename": "note.txt",
                            "mime_type": "text/plain",
                            "file_data": "data:text/plain;base64,aGVsbG8=",
                        },
                    },
                ],
            }
        ],
    )

    response = run_with_runtime(handle_chat(req, client))

    assert response["choices"][0]["message"]["content"] == '{"ok":true}'
    parts = client.calls[0]["contents"][0].parts
    assert parts[0].text == "summarize"
    assert parts[1].inline_data == ("text/plain", "aGVsbG8=")
    assert client.calls[0]["capture_images"] is None


def test_chat_completion_rejects_file_attachment_for_model_without_file_input():
    client = FakeChatClient()
    req = ChatRequest(
        model="gemma-4-31b-it",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "file", "file": {"mime_type": "text/plain", "file_data": "data:text/plain;base64,aGVsbG8="}}
                ],
            }
        ],
    )

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_chat(req, client))

    assert error.value.status_code == 400
    assert "file input" in error.value.detail["message"]
    assert client.calls == []


def test_chat_completion_rejects_invalid_numeric_params():
    client = FakeChatClient()
    req = ChatRequest(model="gemini-3-flash-preview", messages=[{"role": "user", "content": "hello"}], top_p=1.5)

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_chat(req, client))

    assert error.value.status_code == 400
    assert "top_p" in error.value.detail["message"]
    assert client.calls == []


def test_chat_completion_rejects_invalid_model_before_client_call():
    client = FakeChatClient()
    req = ChatRequest(model="not-registered", messages=[{"role": "user", "content": "hello"}])

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_chat(req, client))

    assert error.value.status_code == 400
    assert "not registered" in error.value.detail["message"]
    assert client.calls == []


def test_chat_completion_passes_structured_response_format_to_gateway():
    client = FakeChatClient()
    req = ChatRequest(
        model="gemini-3-flash-preview",
        messages=[{"role": "user", "content": "return json"}],
        response_format={
            "type": "json_schema",
            "json_schema": {"schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}},
        },
    )

    response = run_with_runtime(handle_chat(req, client))

    assert response["choices"][0]["message"]["content"] == '{"ok":true}'
    call = client.calls[0]
    assert call["generation_config_overrides"]["response_mime_type"] == "application/json"
    assert call["generation_config_overrides"]["response_schema"] == [6, None, None, None, None, None, [["ok", [4]]]]
    assert call["sanitize_plain_text"] is False


def test_image_prompt_optimization_returns_three_options_and_forwards_thinking():
    client = FakePromptOptimizerClient()
    req = ImagePromptOptimizationRequest(
        prompt="一张产品海报",
        model="gemini-3-flash-preview",
        style_template="photorealistic",
        thinking="low",
    )

    response = run_with_runtime(handle_image_prompt_optimization(req, client))

    assert response["object"] == "image_prompt_optimization"
    assert response["model"] == "gemini-3-flash-preview"
    assert response["style_template"] == "photorealistic"
    assert response["style_label"] == "写实摄影"
    assert [item["title"] for item in response["options"]] == ["构图稳定版", "质感精修版", "氛围创意版"]
    call = client.calls[0]
    assert call["model"] == "gemini-3-flash-preview"
    assert call["generation_config_overrides"]["request_flag"] == 1
    assert call["generation_config_overrides"]["response_mime_type"] == "application/json"
    assert "写实摄影" in call["capture_prompt"]
    assert "一张产品海报" in call["capture_prompt"]
    assert call["sanitize_plain_text"] is False


def test_image_prompt_optimization_forwards_reference_images_to_optimizer_context():
    client = FakePromptOptimizerClient()
    image_data = "data:image/png;base64," + base64.b64encode(b"reference-image").decode("ascii")
    req = ImagePromptOptimizationRequest(
        prompt="让产品保持参考图里的玻璃质感",
        model="gemini-3-flash-preview",
        style_template="photorealistic",
        images=[image_data],
    )

    response = run_with_runtime(handle_image_prompt_optimization(req, client))

    assert response["options"][0]["prompt"] == "优化提示词一"
    call = client.calls[0]
    assert len(call["capture_images"]) == 1
    assert "用户同时提供了 1 张图片素材" in call["capture_prompt"]
    user_content = call["contents"][-1]
    assert user_content.role == "user"
    assert user_content.parts[0].text.startswith("原始提示词")
    assert user_content.parts[1].inline_data == ("image/png", base64.b64encode(b"reference-image").decode("ascii"))


def test_image_prompt_optimization_normalizes_thinking_for_unsupported_model():
    client = FakePromptOptimizerClient()
    req = ImagePromptOptimizationRequest(
        prompt="一张产品海报",
        model="gemini-3.1-flash-tts-preview",
        style_template="comic",
        thinking="high",
    )

    response = run_with_runtime(handle_image_prompt_optimization(req, client))

    assert response["options"][0]["prompt"] == "优化提示词一"
    call = client.calls[0]
    assert call["model"] == "gemini-3.1-flash-tts-preview"
    assert call["generation_config_overrides"] is None


def test_image_prompt_optimization_rejects_reference_images_for_non_image_input_model():
    client = FakePromptOptimizerClient()
    image_data = "data:image/png;base64," + base64.b64encode(b"reference-image").decode("ascii")
    req = ImagePromptOptimizationRequest(
        prompt="一张产品海报",
        model="gemini-3.1-flash-tts-preview",
        style_template="none",
        images=[image_data],
    )

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_prompt_optimization(req, client))

    assert error.value.status_code == 400
    assert "does not support image input" in error.value.detail["message"]
    assert client.calls == []


def test_image_prompt_optimization_rejects_image_model_before_client_call():
    client = FakePromptOptimizerClient()
    req = ImagePromptOptimizationRequest(
        prompt="一张产品海报",
        model="gemini-3.1-flash-image-preview",
        style_template="none",
    )

    with pytest.raises(HTTPException) as error:
        run_with_runtime(handle_image_prompt_optimization(req, client))

    assert error.value.status_code == 400
    assert "text prompt optimization model" in error.value.detail["message"]
    assert client.calls == []


def test_image_prompt_optimization_route_rejects_unknown_style_template():
    client = FakePromptOptimizerClient()

    response = request_app(
        openai_app(client),
        "POST",
        "/v1/images/prompt-optimizations",
        json={"prompt": "一张产品海报", "model": "gemini-3-flash-preview", "style_template": "unknown"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["type"] == "invalid_request_error"
    assert "style_template" in response.json()["detail"]["message"]
    assert client.calls == []