import json

from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache


def test_snapshot_cache_is_scoped_by_model():
    cache = SnapshotCache(ttl=60, max_size=10)
    headers = {"content-type": "application/json"}
    text_body = json.dumps(["models/text-model", [], None, [], "text-snapshot"])
    image_body = json.dumps(["models/image-model", [], None, [], "image-snapshot"])

    cache.put("same prompt", "text-snapshot", "https://example.test/text", headers, text_body, model="text-model")
    cache.put("same prompt", "image-snapshot", "https://example.test/image", headers, image_body, model="image-model")

    assert cache.get("same prompt", model="text-model") == (
        "text-snapshot",
        "https://example.test/text",
        headers,
        text_body,
    )
    assert cache.get("same prompt", model="image-model") == (
        "image-snapshot",
        "https://example.test/image",
        headers,
        image_body,
    )
    assert cache.get("same prompt", model="other-model") is None