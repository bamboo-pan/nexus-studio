import asyncio

import httpx
from fastapi import FastAPI

from aistudio_api.api.routes_image_sessions import router as image_sessions_router
from aistudio_api.config import settings
from aistudio_api.infrastructure.image_sessions import ImageSessionStore


def request_app(app: FastAPI, method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(send())


def image_sessions_app(storage_dir):
    old_dir = settings.image_sessions_dir
    settings.image_sessions_dir = str(storage_dir)
    app = FastAPI()
    app.include_router(image_sessions_router)
    return app, old_dir


def test_image_session_store_persists_lightweight_snapshot(tmp_path):
    store = ImageSessionStore(tmp_path)

    session = store.save(
        {
            "prompt": "晴天草地",
            "model": "gemini-3-pro-image-preview",
            "size": "2048x2048",
            "count": 1,
            "results": [
                {
                    "url": "/generated-images/20260514/a.png",
                    "path": "20260514/a.png",
                    "b64_json": "large-payload",
                }
            ],
            "conversation": [
                {"role": "user", "prompt": "晴天草地", "images": []},
                {"role": "assistant", "images": [{"url": "/generated-images/20260514/a.png"}]},
            ],
        }
    )

    reloaded = ImageSessionStore(tmp_path).get(session["id"])

    assert reloaded["prompt"] == "晴天草地"
    assert reloaded["title"] == "晴天草地"
    assert reloaded["turn_count"] == 1
    assert reloaded["preview_url"] == "/generated-images/20260514/a.png"
    assert "b64_json" not in reloaded["results"][0]


def test_image_session_routes_round_trip_and_delete_without_image_deletion(tmp_path):
    app, old_dir = image_sessions_app(tmp_path)
    try:
        create = request_app(
            app,
            "POST",
            "/image-sessions",
            json={
                "prompt": "继续编辑",
                "model": "gemini-3-pro-image-preview",
                "size": "2048x2048",
                "results": [{"url": "/generated-images/keep.png", "path": "keep.png"}],
            },
        )
        assert create.status_code == 200
        session = create.json()

        listed = request_app(app, "GET", "/image-sessions")
        fetched = request_app(app, "GET", f"/image-sessions/{session['id']}")
        deleted = request_app(app, "DELETE", f"/image-sessions/{session['id']}")
        missing = request_app(app, "GET", f"/image-sessions/{session['id']}")

        assert listed.status_code == 200
        assert listed.json()["data"][0]["preview_url"] == "/generated-images/keep.png"
        assert fetched.json()["results"][0]["url"] == "/generated-images/keep.png"
        assert deleted.json() == {"ok": True, "id": session["id"]}
        assert missing.status_code == 404
    finally:
        settings.image_sessions_dir = old_dir


def test_image_session_route_rejects_invalid_id(tmp_path):
    app, old_dir = image_sessions_app(tmp_path)
    try:
        response = request_app(app, "PUT", "/image-sessions/../bad", json={"prompt": "x"})
    finally:
        settings.image_sessions_dir = old_dir

    assert response.status_code == 404