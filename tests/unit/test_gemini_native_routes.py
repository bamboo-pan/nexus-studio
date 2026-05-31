import asyncio

import httpx

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.state import runtime_state
from aistudio_api.domain.model_capabilities import clear_dynamic_model_capabilities


class UnusedClient:
    async def generate_content(self, **kwargs):
        raise AssertionError("downstream client should not be called")


class ModelListClient(UnusedClient):
    def __init__(self, models: list[str]):
        self.models = models
        self.list_calls = 0

    async def list_available_models(self):
        self.list_calls += 1
        return self.models


def request_with_client(client, method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            return await http_client.request(method, url, **kwargs)

    old_busy_lock = runtime_state.busy_lock
    old_account_service = runtime_state.account_service
    old_rotator = runtime_state.rotator
    app.dependency_overrides[get_client] = lambda: client
    runtime_state.busy_lock = asyncio.Semaphore(3)
    runtime_state.account_service = None
    runtime_state.rotator = None
    try:
        return asyncio.run(send())
    finally:
        runtime_state.busy_lock = old_busy_lock
        runtime_state.account_service = old_account_service
        runtime_state.rotator = old_rotator
        app.dependency_overrides.pop(get_client, None)


def test_gemini_models_route_exposes_supported_generation_methods():
    response = request_with_client(UnusedClient(), "GET", "/v1beta/models")

    assert response.status_code == 200
    models = response.json()["models"]
    flash = next(model for model in models if model["name"] == "models/gemini-3-flash-preview")
    assert "generateContent" in flash["supportedGenerationMethods"]
    assert "countTokens" in flash["supportedGenerationMethods"]
    assert "streamGenerateContent" in flash["supportedGenerationMethods"]


def test_gemini_models_refresh_registers_discovered_models():
    client = ModelListClient(["models/gemini-dynamic-preview"])
    old_client = runtime_state.client
    runtime_state.client = client
    clear_dynamic_model_capabilities()
    try:
        response = request_with_client(UnusedClient(), "GET", "/v1beta/models?refresh=true")
    finally:
        runtime_state.client = old_client
        clear_dynamic_model_capabilities()

    assert response.status_code == 200
    assert client.list_calls == 1
    models = response.json()["models"]
    dynamic = next(model for model in models if model["name"] == "models/gemini-dynamic-preview")
    assert "generateContent" in dynamic["supportedGenerationMethods"]
    assert "streamGenerateContent" in dynamic["supportedGenerationMethods"]


def test_gemini_count_tokens_accepts_generate_content_request_wrapper():
    response = request_with_client(
        UnusedClient(),
        "POST",
        "/v1beta/models/gemini-3-flash-preview:countTokens",
        json={
            "generateContentRequest": {
                "contents": [{"role": "user", "parts": [{"text": "hello world"}]}],
                "systemInstruction": {"parts": [{"text": "system"}]},
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["totalTokens"] >= 3


def test_gemini_embedding_routes_return_clear_unsupported_errors():
    embed = request_with_client(
        UnusedClient(),
        "POST",
        "/v1beta/models/gemini-3-flash-preview:embedContent",
        json={"content": {"parts": [{"text": "hello"}]}},
    )
    batch = request_with_client(
        UnusedClient(),
        "POST",
        "/v1beta/models/gemini-3-flash-preview:batchEmbedContents",
        json={"requests": [{"content": {"parts": [{"text": "hello"}]}}]},
    )

    assert embed.status_code == 501
    assert embed.json()["detail"]["type"] == "unsupported_feature"
    assert "embeddings" in embed.json()["detail"]["message"]
    assert batch.status_code == 501
    assert batch.json()["detail"]["type"] == "unsupported_feature"
    assert "batch embeddings" in batch.json()["detail"]["message"]


def test_gemini_generate_content_rejects_safety_settings_with_clear_error():
    response = request_with_client(
        UnusedClient(),
        "POST",
        "/v1beta/models/gemini-3-flash-preview:generateContent",
        json={
            "contents": [{"parts": [{"text": "hello"}]}],
            "safetySettings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["type"] == "bad_request"
    assert "safetySettings" in response.json()["detail"]["message"]


def test_gemini_generate_content_rejects_cached_content_with_clear_error():
    response = request_with_client(
        UnusedClient(),
        "POST",
        "/v1beta/models/gemini-3-flash-preview:generateContent",
        json={
            "contents": [{"parts": [{"text": "hello"}]}],
            "cachedContent": "cachedContents/example",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["type"] == "bad_request"
    assert "cachedContent" in response.json()["detail"]["message"]


def test_gemini_generate_content_rejects_file_data_with_clear_error():
    response = request_with_client(
        UnusedClient(),
        "POST",
        "/v1beta/models/gemini-3-flash-preview:generateContent",
        json={
            "contents": [
                {
                    "parts": [
                        {
                            "fileData": {
                                "mimeType": "image/png",
                                "fileUri": "gs://bucket/image.png",
                            }
                        }
                    ]
                }
            ]
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["type"] == "bad_request"
    assert "fileData" in response.json()["detail"]["message"]