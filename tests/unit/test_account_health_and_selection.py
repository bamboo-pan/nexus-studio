import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aistudio_api.api.app import (
    ACCOUNT_WARMUP_FAILURE_HEALTH_REASON,
    GENERATE_CONTENT_AUTH_HEALTH_REASON,
    _account_pool_warmup_account_ids,
    _account_pool_warmup_required_success_count,
    _account_pool_warmup_status,
    _is_transient_warmup_error,
    _record_account_warmup_failure,
    _run_account_startup_preflight,
    _should_start_account_pool_warmup,
    _should_start_background_warmup,
    _warmup_with_retries,
)
from aistudio_api.api.dependencies import get_account_service
from aistudio_api.api.routes_accounts import router as accounts_router
from aistudio_api.api.schemas import ChatRequest, GeminiContent, GeminiGenerateContentRequest, GeminiPart, ImageRequest, Message
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_client_pool import AccountClientPool
from aistudio_api.config import settings
from aistudio_api.application.account_rotator import AccountRotator, RotationMode
from aistudio_api.application.account_service import AccountService
from aistudio_api.application.api_service import handle_chat, handle_gemini_generate_content, handle_image_generation, health_response
from aistudio_api.domain.errors import AuthError, UsageLimitExceeded
from aistudio_api.domain.models import Candidate, GeneratedImage, ModelOutput
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.account.login_service import LoginService
from aistudio_api.infrastructure.account.tier_detector import AccountTier, TierResult


def storage_state(cookie_name="sid", cookie_value="1", domain=".google.com", expires=None, email=None, indexed_db=False):
    cookie = {
        "name": cookie_name,
        "value": cookie_value,
        "domain": domain,
        "path": "/",
    }
    if expires is not None:
        cookie["expires"] = expires
    origins = []
    if email:
        origin = {
            "origin": "https://aistudio.google.com",
            "localStorage": [{"name": "account_email", "value": email}],
        }
        if indexed_db:
            origin["indexedDB"] = [{"name": "firebaseLocalStorageDb", "version": 1, "stores": []}]
        origins = [origin]
    return {"cookies": [cookie], "origins": origins}


def request_app(app: FastAPI, method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(send())


def test_background_warmup_skips_redundant_account_pool_navigation():
    assert _should_start_background_warmup(use_pure_http=False, account_count=0) is True
    assert _should_start_background_warmup(use_pure_http=False, account_count=1) is False
    assert _should_start_background_warmup(use_pure_http=True, account_count=0) is False


def test_account_pool_warmup_starts_only_for_browser_account_pool():
    assert _should_start_account_pool_warmup(use_pure_http=False, account_count=1, warmup_limit=1) is True
    assert _should_start_account_pool_warmup(use_pure_http=False, account_count=0, warmup_limit=1) is False
    assert _should_start_account_pool_warmup(use_pure_http=True, account_count=1, warmup_limit=1) is False
    assert _should_start_account_pool_warmup(use_pure_http=False, account_count=1, warmup_limit=0) is False


def test_account_pool_warmup_candidates_cover_balanced_and_premium(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    free = store.save_account("free", None, storage_state(cookie_name="free"), tier="free")
    premium = store.save_account("premium", None, storage_state(cookie_name="premium"), activate=False, tier="pro")

    assert _account_pool_warmup_account_ids(
        store.list_accounts(),
        active_account_id=free.id,
        rotation_mode="round_robin",
        warmup_limit=2,
    ) == [free.id, premium.id]
    assert _account_pool_warmup_account_ids(
        store.list_accounts(),
        active_account_id=free.id,
        rotation_mode="round_robin",
        warmup_limit=1,
    ) == [free.id, premium.id]


def test_account_pool_warmup_candidates_prefer_active_in_round_robin(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    first = store.save_account("first", None, storage_state(cookie_name="first"), activate=False)
    active = store.save_account("active", None, storage_state(cookie_name="active"), activate=True)

    assert _account_pool_warmup_account_ids(
        store.list_accounts(),
        active_account_id=active.id,
        rotation_mode="round_robin",
        warmup_limit=2,
    ) == [active.id, first.id]


def test_account_pool_warmup_candidates_prefer_active_for_exhaustion(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    first = store.save_account("first", None, storage_state(cookie_name="first"), activate=False)
    active = store.save_account("active", None, storage_state(cookie_name="active"), activate=True)

    assert _account_pool_warmup_account_ids(
        store.list_accounts(),
        active_account_id=active.id,
        rotation_mode="exhaustion",
        warmup_limit=2,
    ) == [active.id, first.id]


def test_account_pool_warmup_candidates_skip_isolated_accounts(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    isolated = store.save_account("isolated", None, storage_state(cookie_name="isolated"), activate=True)
    premium = store.save_account("premium", None, storage_state(cookie_name="premium"), activate=False, tier="pro")
    store.isolate_account(isolated.id, "test isolation")

    assert _account_pool_warmup_account_ids(
        store.list_accounts(),
        active_account_id=isolated.id,
        rotation_mode="exhaustion",
        warmup_limit=2,
    ) == [premium.id]


def test_account_pool_warmup_status_requires_success_for_current_candidates(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    failed = store.save_account("failed", None, storage_state(cookie_name="sid1"), activate=True)
    ready = store.save_account("ready", None, storage_state(cookie_name="sid2"), activate=False)

    required = _account_pool_warmup_required_success_count(store.list_accounts(), warmup_limit=2)

    assert required == 2
    assert _account_pool_warmup_status(
        completed_accounts=[ready.id],
        failed_accounts=[failed.id],
        required_success_count=required,
    ) == "partial"


def test_account_pool_warmup_status_completes_after_failed_account_is_isolated(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    failed = store.save_account("failed", None, storage_state(cookie_name="sid1"), activate=True)
    ready = store.save_account("ready", None, storage_state(cookie_name="sid2"), activate=False)
    store.isolate_account(failed.id, "GenerateContent permission failed")

    required = _account_pool_warmup_required_success_count(store.list_accounts(), warmup_limit=2)

    assert required == 1
    assert _account_pool_warmup_status(
        completed_accounts=[ready.id],
        failed_accounts=[failed.id],
        required_success_count=required,
    ) == "complete"


def test_warmup_retry_classifies_navigation_timeout_only_as_transient():
    timeout = RuntimeError(
        'Page.goto: Timeout 60000ms exceeded. navigating to "https://aistudio.google.com/", waiting until "commit"'
    )

    assert _is_transient_warmup_error(timeout) is True
    assert _is_transient_warmup_error(RuntimeError("template capture timeout for model=gemini-3.1-flash-lite")) is True
    assert _is_transient_warmup_error(RuntimeError("failed to trigger send during template capture")) is True
    assert _is_transient_warmup_error(RuntimeError("BotGuardService capture timeout")) is True
    assert _is_transient_warmup_error(RuntimeError("AI Studio chat runtime not ready after navigating to https://aistudio.google.com/")) is True
    assert _is_transient_warmup_error(RuntimeError("Google sign-in auth state is missing or invalid")) is False
    assert _is_transient_warmup_error(RuntimeError("GenerateContent permission denied. Please try again.")) is False
    assert _is_transient_warmup_error(RuntimeError("invalid account auth message")) is False
    assert _is_transient_warmup_error(ValueError("validation failed")) is False


def test_warmup_with_retries_succeeds_after_transient_navigation_timeout():
    calls = 0
    sleeps = []

    async def warmup():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError(
                'Page.goto: Timeout 60000ms exceeded. navigating to "https://aistudio.google.com/", waiting until "commit"'
            )

    async def sleep(delay):
        sleeps.append(delay)

    asyncio.run(_warmup_with_retries(warmup, label="test", attempts=3, backoff_seconds=(0.1, 0.2), sleep=sleep))

    assert calls == 3
    assert sleeps == [0.1, 0.2]


def test_warmup_with_retries_keeps_auth_failure_hard():
    calls = 0

    async def warmup():
        nonlocal calls
        calls += 1
        raise RuntimeError("AI Studio redirected to Google sign-in; browser auth state is missing or invalid")

    async def sleep(delay):
        raise AssertionError(f"unexpected retry sleep: {delay}")

    with pytest.raises(RuntimeError, match="Google sign-in"):
        asyncio.run(_warmup_with_retries(warmup, label="test", attempts=3, backoff_seconds=(0.1, 0.2), sleep=sleep))

    assert calls == 1


def test_account_warmup_auth_failure_isolates_account_with_actionable_reason(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state(cookie_name="sid1"))

    _record_account_warmup_failure(store, account.id, AuthError("GenerateContent permission check failed"))

    refreshed = store.get_account(account.id)
    assert refreshed.health_status == "isolated"
    assert refreshed.is_isolated is True
    assert refreshed.health_reason == GENERATE_CONTENT_AUTH_HEALTH_REASON
    assert "sid1" not in refreshed.health_reason


def test_account_warmup_hard_failure_isolates_account_with_actionable_reason(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state(cookie_name="sid1"))

    _record_account_warmup_failure(store, account.id, RuntimeError("AI Studio text model not selected: text_model_not_found"))

    refreshed = store.get_account(account.id)
    assert refreshed.health_status == "isolated"
    assert refreshed.is_isolated is True
    assert refreshed.isolated_until is not None
    assert datetime.fromisoformat(refreshed.isolated_until) > datetime.now(timezone.utc)
    assert refreshed.health_reason == ACCOUNT_WARMUP_FAILURE_HEALTH_REASON
    assert "sid1" not in refreshed.health_reason


def test_startup_preflight_isolates_cookie_only_accounts_before_warmup(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    stale = store.save_account("stale", None, storage_state())
    ready = store.save_account(
        "ready",
        None,
        storage_state(cookie_name="sid2", email="ready@example.com"),
        activate=False,
    )

    failed = _run_account_startup_preflight(store, store.list_accounts())

    assert failed == [stale.id]
    assert store.get_account(stale.id).health_status == "isolated"
    assert store.get_account(stale.id).is_isolated is True
    assert store.get_account(ready.id).health_status == "healthy"
    assert _account_pool_warmup_account_ids(
        store.list_accounts(),
        active_account_id=stale.id,
        rotation_mode="round_robin",
        warmup_limit=2,
    ) == [ready.id]


def test_warmup_with_retries_raises_after_transient_attempts_exhausted():
    calls = 0
    sleeps = []

    async def warmup():
        nonlocal calls
        calls += 1
        raise RuntimeError(
            'Page.goto: Timeout 60000ms exceeded. navigating to "https://aistudio.google.com/", waiting until "commit"'
        )

    async def sleep(delay):
        sleeps.append(delay)

    with pytest.raises(RuntimeError, match="Page.goto"):
        asyncio.run(_warmup_with_retries(warmup, label="test", attempts=3, backoff_seconds=(0.1, 0.2), sleep=sleep))

    assert calls == 3
    assert sleeps == [0.1, 0.2]


def test_health_response_exposes_warmup_status():
    previous_status = runtime_state.warmup_status
    previous_targets = list(runtime_state.warmup_target_accounts)
    previous_completed = list(runtime_state.warmup_completed_accounts)
    previous_failed = list(runtime_state.warmup_failed_accounts)
    try:
        runtime_state.warmup_status = "running"
        runtime_state.warmup_target_accounts = ["acc_active"]
        runtime_state.warmup_completed_accounts = []
        runtime_state.warmup_failed_accounts = []

        response = health_response()

        assert response["warmup"] == {
            "status": "running",
            "target_accounts": ["acc_active"],
            "completed_accounts": [],
            "failed_accounts": [],
        }
    finally:
        runtime_state.warmup_status = previous_status
        runtime_state.warmup_target_accounts = previous_targets
        runtime_state.warmup_completed_accounts = previous_completed
        runtime_state.warmup_failed_accounts = previous_failed


def accounts_app(service: AccountService) -> FastAPI:
    app = FastAPI()
    app.include_router(accounts_router)
    app.dependency_overrides[get_account_service] = lambda: service
    return app


def test_account_health_check_marks_valid_account_healthy_and_keeps_tier(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state(email="user@example.com"), tier="pro")

    result = store.test_account_health(account.id)

    refreshed = store.get_account(account.id)
    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert result["tier"] == "pro"
    assert refreshed.email == "user@example.com"
    assert refreshed.health_status == "healthy"
    assert refreshed.is_isolated is False


def test_account_health_check_isolates_cookie_only_state(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state(), tier="pro")

    result = store.test_account_health(account.id)

    refreshed = store.get_account(account.id)
    assert result["ok"] is False
    assert result["status"] == "isolated"
    assert "AI Studio browser storage" in result["reason"]
    assert refreshed.is_isolated is True


def test_account_health_check_marks_expired_google_cookie_as_isolated(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    expired = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
    account = store.save_account("expired", None, storage_state(expires=expired))

    result = store.test_account_health(account.id)

    refreshed = store.get_account(account.id)
    assert result["ok"] is False
    assert result["status"] == "expired"
    assert "expired" in result["reason"]
    assert refreshed.is_isolated is True


def test_account_health_check_marks_missing_auth_as_isolated(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("missing", None, storage_state())
    (tmp_path / account.id / "auth.json").unlink()

    result = store.test_account_health(account.id)

    refreshed = store.get_account(account.id)
    assert result["ok"] is False
    assert result["status"] == "missing_auth"
    assert refreshed.is_isolated is True


def test_account_health_route_returns_sanitized_status_payload(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state(cookie_value="synthetic-secret", email="route@example.com"), tier="ultra")
    app = accounts_app(AccountService(store, LoginService()))

    response = request_app(app, "POST", f"/accounts/{account.id}/test")

    body_text = response.text.lower()
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["tier"] == "ultra"
    assert '"cookies"' not in body_text
    assert "synthetic-secret" not in body_text


def test_account_tier_check_can_update_free_account_to_pro(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state(cookie_value="synthetic-secret", email="route@example.com"), tier="free")
    service = AccountService(store, LoginService())

    async def fake_detector(auth_path):
        assert auth_path == tmp_path / account.id / "auth.json"
        return TierResult(tier=AccountTier.PRO, email="route@example.com", raw_header="route@example.com\nPRO")

    result = asyncio.run(service.test_account_with_tier(account.id, tier_detector=fake_detector))

    body_text = json.dumps(result).lower()
    assert result["ok"] is True
    assert result["tier"] == "pro"
    assert store.get_account(account.id).tier == "pro"
    assert '"cookies"' not in body_text
    assert "synthetic-secret" not in body_text


def test_account_tier_check_does_not_downgrade_stored_premium_to_free(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state(cookie_value="synthetic-secret", email="route@example.com"), tier="pro")
    service = AccountService(store, LoginService())

    async def fake_detector(auth_path):
        assert auth_path == tmp_path / account.id / "auth.json"
        return TierResult(tier=AccountTier.FREE, email="route@example.com", raw_header="Upgrade to Google AI Pro")

    result = asyncio.run(service.test_account_with_tier(account.id, tier_detector=fake_detector))

    assert result["ok"] is True
    assert result["tier"] == "pro"
    assert store.get_account(account.id).tier == "pro"
    assert "keeping stored pro" in result["reason"]


def test_account_update_route_accepts_tier_and_rejects_unknown_tier(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state())
    app = accounts_app(AccountService(store, LoginService()))

    update = request_app(app, "PUT", f"/accounts/{account.id}", json={"tier": "ultra"})
    invalid = request_app(app, "PUT", f"/accounts/{account.id}", json={"tier": "enterprise"})

    assert update.status_code == 200
    assert update.json()["tier"] == "ultra"
    assert store.get_account(account.id).tier == "ultra"
    assert invalid.status_code == 400
    assert "free, pro, ultra" in invalid.json()["detail"]["message"]


def test_rotator_prefers_premium_account_for_image_models(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    store.save_account("free", None, storage_state(), tier="free")
    pro = store.save_account("pro", None, storage_state(cookie_name="sid2"), activate=False, tier="pro")
    rotator = AccountRotator(store)

    picked = asyncio.run(rotator.get_next_account("gemini-3.1-flash-image-preview"))

    assert picked.id == pro.id
    assert rotator.last_selection_reason == "premium-preferred model selected a Pro/Ultra account"


def test_rotator_prefers_premium_account_for_pro_text_models(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    store.save_account("free", None, storage_state(), tier="free")
    pro = store.save_account("pro", None, storage_state(cookie_name="sid2"), activate=False, tier="pro")
    rotator = AccountRotator(store)

    picked = asyncio.run(rotator.get_next_account("gemini-3.1-pro-preview"))

    assert rotator.model_prefers_premium("gemini-3.1-pro-preview") is True
    assert rotator.model_prefers_premium("models/gemini-3.1-pro-preview") is True
    assert rotator.model_prefers_premium("gemini-pro-latest") is True
    assert rotator.model_prefers_premium("gemini-3.1-flash-lite") is False
    assert picked.id == pro.id
    assert rotator.last_selection_reason == "premium-preferred model selected a Pro/Ultra account"


def test_rotator_uses_free_account_for_text_and_logs_image_fallback(tmp_path, caplog):
    store = AccountStore(accounts_dir=tmp_path)
    free = store.save_account("free", None, storage_state(), tier="free")
    rotator = AccountRotator(store)

    text_pick = asyncio.run(rotator.get_next_account("gemini-3-flash-preview"))
    with caplog.at_level(logging.WARNING, logger="aistudio.rotator"):
        image_pick = asyncio.run(rotator.get_next_account("gemini-3.1-flash-image-preview"))

    assert text_pick.id == free.id
    assert image_pick.id == free.id
    assert "fell back" in rotator.last_selection_reason
    assert any("fallback" in record.message.lower() for record in caplog.records)


def test_rotator_rate_limit_and_error_isolation_update_health_and_availability(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    limited = store.save_account("limited", None, storage_state(cookie_name="sid1"))
    failing = store.save_account("failing", None, storage_state(cookie_name="sid2"), activate=False)
    healthy = store.save_account("healthy", None, storage_state(cookie_name="sid3"), activate=False)
    rotator = AccountRotator(store, cooldown_seconds=30, error_isolation_threshold=2)

    rotator.record_rate_limited(limited.id)
    rotator.record_error(failing.id)
    rotator.record_error(failing.id)
    picked = asyncio.run(rotator.get_next_account("gemini-3-flash-preview"))
    stats = rotator.get_all_stats()

    assert store.get_account(limited.id).health_status == "rate_limited"
    assert store.get_account(limited.id).is_isolated is True
    assert store.get_account(failing.id).health_status == "isolated"
    assert store.get_account(failing.id).is_isolated is True
    assert stats[limited.id]["is_available"] is False
    assert stats[failing.id]["is_available"] is False
    assert picked.id == healthy.id


def test_lru_selection_prefers_never_used_accounts(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    used = store.save_account("used", None, storage_state(cookie_name="sid1"))
    unused = store.save_account("unused", None, storage_state(cookie_name="sid2"), activate=False)
    rotator = AccountRotator(store, mode=RotationMode.LEAST_RECENTLY_USED)
    rotator.record_success(used.id)

    picked = asyncio.run(rotator.get_next_account("gemini-3-flash-preview"))

    assert picked.id == unused.id


def test_exhaustion_selection_keeps_active_account_until_rate_limited(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    active = store.save_account("active", None, storage_state(cookie_name="sid1"))
    standby = store.save_account("standby", None, storage_state(cookie_name="sid2"), activate=False)
    rotator = AccountRotator(store, mode=RotationMode.EXHAUSTION, cooldown_seconds=30)

    first = asyncio.run(rotator.get_next_account("gemini-3-flash-preview"))
    second = asyncio.run(rotator.get_next_account("gemini-3-flash-preview"))
    rotator.record_rate_limited(active.id)
    after_limit = asyncio.run(rotator.get_next_account("gemini-3-flash-preview"))

    assert first.id == active.id
    assert second.id == active.id
    assert after_limit.id == standby.id


def test_force_next_can_exclude_active_account_in_exhaustion_mode(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    active = store.save_account("active", None, storage_state(cookie_name="sid1"))
    standby = store.save_account("standby", None, storage_state(cookie_name="sid2"), activate=False)
    rotator = AccountRotator(store, mode=RotationMode.EXHAUSTION)

    picked = asyncio.run(rotator.get_next_account(exclude_account_id=active.id))

    assert picked.id == standby.id


def test_balanced_selection_spreads_concurrent_leases(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    first = store.save_account("first", None, storage_state(cookie_name="sid1"))
    second = store.save_account("second", None, storage_state(cookie_name="sid2"), activate=False)
    rotator = AccountRotator(store)

    async def acquire_two():
        lease_one = await rotator.acquire_account("gemini-3-flash-preview")
        lease_two = await rotator.acquire_account("gemini-3-flash-preview")
        try:
            return lease_one.account.id, lease_two.account.id, rotator.get_all_stats()
        finally:
            await lease_two.release()
            await lease_one.release()

    first_id, second_id, stats = asyncio.run(acquire_two())

    assert {first_id, second_id} == {first.id, second.id}
    assert stats[first.id]["in_flight"] == 1
    assert stats[second.id]["in_flight"] == 1


def test_balanced_selection_keeps_affinity_when_not_overloaded(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    first = store.save_account("first", None, storage_state(cookie_name="sid1"))
    store.save_account("second", None, storage_state(cookie_name="sid2"), activate=False)
    rotator = AccountRotator(store)

    async def acquire_same_affinity_twice():
        lease_one = await rotator.acquire_account("gemini-3-flash-preview", affinity_key="conversation-a")
        await lease_one.release()
        lease_two = await rotator.acquire_account("gemini-3-flash-preview", affinity_key="conversation-a")
        try:
            return lease_one.account.id, lease_two.account.id
        finally:
            await lease_two.release()

    first_pick, second_pick = asyncio.run(acquire_same_affinity_twice())

    assert first_pick == first.id
    assert second_pick == first.id


def test_affinity_load_expires_after_ttl(tmp_path, monkeypatch):
    store = AccountStore(accounts_dir=tmp_path)
    first = store.save_account("first", None, storage_state(cookie_name="sid1"))
    second = store.save_account("second", None, storage_state(cookie_name="sid2"), activate=False)
    current_time = 1000.0
    monkeypatch.setattr("aistudio_api.application.account_rotator.time.time", lambda: current_time)
    rotator = AccountRotator(store, affinity_ttl_seconds=3600)

    async def lease_with_affinity():
        lease_one = await rotator.acquire_account("gemini-3-flash-preview", affinity_key="user-a")
        await lease_one.release()
        lease_two = await rotator.acquire_account("gemini-3-flash-preview", affinity_key="user-a")
        await lease_two.release()
        return lease_one.account.id, lease_two.account.id

    first_pick, second_pick = asyncio.run(lease_with_affinity())
    stats_before_expiry = rotator.get_all_stats()
    current_time = 4601.0
    stats_after_expiry = rotator.get_all_stats()
    lease_after_expiry = asyncio.run(rotator.acquire_account("gemini-3-flash-preview", affinity_key="user-a"))
    try:
        expired_pick = lease_after_expiry.account.id
    finally:
        asyncio.run(lease_after_expiry.release())

    assert first_pick == first.id
    assert second_pick == first.id
    assert stats_before_expiry[first.id]["affinity_load"] == 1
    assert stats_before_expiry[first.id]["bound_users"] == 1
    assert stats_before_expiry[first.id]["affinity_ttl_seconds"] == 3600
    assert stats_before_expiry[second.id]["affinity_load"] == 0
    assert stats_after_expiry[first.id]["affinity_load"] == 0
    assert expired_pick == second.id


def test_account_lease_log_includes_bound_account_and_load(tmp_path, caplog):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state())
    rotator = AccountRotator(store, affinity_ttl_seconds=3600)

    async def lease_once():
        lease = await rotator.acquire_account("gemini-3-flash-preview", affinity_key="user-a")
        await lease.release()

    with caplog.at_level(logging.INFO, logger="aistudio.rotator"):
        asyncio.run(lease_once())

    assert any(
        f"account={account.id}" in record.message
        and "affinity_load=1" in record.message
        and "affinity_ttl_seconds=3600" in record.message
        for record in caplog.records
    )


def test_account_stats_track_image_usage_by_resolution(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("main", None, storage_state())
    rotator = AccountRotator(store)

    rotator.record_success(account.id, image_size="1024x1024", image_count=2)
    rotator.record_success(account.id, image_size="1024x1792", image_count=1)
    stats = rotator.get_all_stats()[account.id]

    assert stats["image_sizes"] == {"1024x1024": 2, "1024x1792": 1}
    assert stats["image_total"] == 3


class FakeImageClient:
    def __init__(self):
        self.calls = []

    async def generate_image(self, *, prompt, model, generation_config_overrides=None):
        self.calls.append({"prompt": prompt, "model": model, "generation_config_overrides": generation_config_overrides})
        return ModelOutput(
            candidates=[Candidate(text="ok", images=[GeneratedImage(mime="image/png", data=b"image", size=5)])],
            usage={"total_tokens": 1},
        )


class FakeChatClient:
    def __init__(self):
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return ModelOutput(
            candidates=[Candidate(text="ok")],
            usage={"total_tokens": 1},
        )


class FakePooledChatClient:
    clients = []
    fail_for_accounts: set[str] = set()
    auth_fail_for_accounts: set[str] = set()

    def __init__(self, **kwargs):
        self.auth_path = ""
        self.calls = []
        self.closed = False
        FakePooledChatClient.clients.append(self)

    async def switch_auth(self, auth_path):
        self.auth_path = auth_path

    async def close(self):
        self.closed = True

    @property
    def account_id(self):
        return self.auth_path.replace("\\", "/").split("/")[-2]

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.account_id in FakePooledChatClient.auth_fail_for_accounts:
            FakePooledChatClient.auth_fail_for_accounts.remove(self.account_id)
            raise AuthError("GenerateContent permission check failed")
        if self.account_id in FakePooledChatClient.fail_for_accounts:
            FakePooledChatClient.fail_for_accounts.remove(self.account_id)
            raise UsageLimitExceeded("quota exhausted")
        return ModelOutput(
            candidates=[Candidate(text=f"ok:{self.account_id}")],
            usage={"total_tokens": 1},
        )

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        if self.account_id in FakePooledChatClient.auth_fail_for_accounts:
            FakePooledChatClient.auth_fail_for_accounts.remove(self.account_id)
            raise AuthError("GenerateContent permission check failed")
        return ModelOutput(
            candidates=[Candidate(text=f"ok:{self.account_id}", images=[GeneratedImage(mime="image/png", data=b"image", size=5)])],
            usage={"total_tokens": 1},
        )


class FakeBrowserSession:
    def __init__(self):
        self.auth_paths = []

    async def switch_auth(self, auth_path):
        self.auth_paths.append(auth_path)


class FakeAuthClient:
    def __init__(self, browser_session):
        self._session = browser_session
        self.clear_calls = 0

    async def switch_auth(self, auth_path):
        await self._session.switch_auth(auth_path)
        self.clear_capture_state()

    def clear_capture_state(self):
        self.clear_calls += 1


class FakeSnapshotCache:
    def __init__(self):
        self.clear_calls = 0

    def clear(self):
        self.clear_calls += 1


def run_with_account_runtime(coro, *, account_service, rotator, browser_session, snapshot_cache, generated_images_dir=None):
    old_busy_lock = runtime_state.busy_lock
    old_account_service = runtime_state.account_service
    old_rotator = runtime_state.rotator
    old_client = runtime_state.client
    old_snapshot_cache = runtime_state.snapshot_cache
    old_account_client_pool = runtime_state.account_client_pool
    old_generated_images_dir = settings.generated_images_dir
    old_generated_images_route = settings.generated_images_route
    runtime_state.busy_lock = asyncio.Semaphore(3)
    runtime_state.account_service = account_service
    runtime_state.rotator = rotator
    runtime_state.client = FakeAuthClient(browser_session)
    runtime_state.snapshot_cache = snapshot_cache
    if generated_images_dir is not None:
        settings.generated_images_dir = str(generated_images_dir)
    settings.generated_images_route = "/generated-images"
    try:
        return asyncio.run(coro)
    finally:
        runtime_state.busy_lock = old_busy_lock
        runtime_state.account_service = old_account_service
        runtime_state.rotator = old_rotator
        runtime_state.client = old_client
        runtime_state.snapshot_cache = old_snapshot_cache
        runtime_state.account_client_pool = old_account_client_pool
        settings.generated_images_dir = old_generated_images_dir
        settings.generated_images_route = old_generated_images_route


def run_with_account_pool(coro, *, account_service, rotator, account_client_pool):
    old_busy_lock = runtime_state.busy_lock
    old_account_service = runtime_state.account_service
    old_rotator = runtime_state.rotator
    old_client = runtime_state.client
    old_account_client_pool = runtime_state.account_client_pool
    runtime_state.busy_lock = asyncio.Semaphore(3)
    runtime_state.account_service = account_service
    runtime_state.rotator = rotator
    runtime_state.client = FakeChatClient()
    runtime_state.account_client_pool = account_client_pool
    try:
        return asyncio.run(coro)
    finally:
        runtime_state.busy_lock = old_busy_lock
        runtime_state.account_service = old_account_service
        runtime_state.rotator = old_rotator
        runtime_state.client = old_client
        runtime_state.account_client_pool = old_account_client_pool


def test_chat_uses_pooled_account_client_without_switching_active_account(tmp_path):
    FakePooledChatClient.clients = []
    FakePooledChatClient.fail_for_accounts = set()
    store = AccountStore(accounts_dir=tmp_path)
    first = store.save_account("first", None, storage_state(cookie_name="sid1"))
    second = store.save_account("second", None, storage_state(cookie_name="sid2"), activate=False)
    account_service = AccountService(store, LoginService())
    rotator = AccountRotator(store)
    pool = AccountClientPool(store, client_factory=FakePooledChatClient)

    async def call_twice():
        response_one = await handle_chat(
            ChatRequest(model="gemini-3-flash-preview", messages=[Message(role="user", content="one")]),
            runtime_state.client,
        )
        response_two = await handle_chat(
            ChatRequest(model="gemini-3-flash-preview", messages=[Message(role="user", content="two")]),
            runtime_state.client,
        )
        return response_one, response_two

    response_one, response_two = run_with_account_pool(
        call_twice(),
        account_service=account_service,
        rotator=rotator,
        account_client_pool=pool,
    )

    assert store.get_active_account().id == first.id
    assert response_one["choices"][0]["message"]["content"] == f"ok:{first.id}"
    assert response_two["choices"][0]["message"]["content"] == f"ok:{second.id}"
    assert {client.account_id for client in FakePooledChatClient.clients} == {first.id, second.id}


def test_chat_user_affinity_balances_different_users(tmp_path):
    FakePooledChatClient.clients = []
    FakePooledChatClient.fail_for_accounts = set()
    store = AccountStore(accounts_dir=tmp_path)
    first = store.save_account("first", None, storage_state(cookie_name="sid1"))
    second = store.save_account("second", None, storage_state(cookie_name="sid2"), activate=False)
    account_service = AccountService(store, LoginService())
    rotator = AccountRotator(store)
    pool = AccountClientPool(store, client_factory=FakePooledChatClient)

    async def call_two_users():
        response_one = await handle_chat(
            ChatRequest(model="gemini-3-flash-preview", user="user-a", messages=[Message(role="user", content="same")]),
            runtime_state.client,
        )
        response_two = await handle_chat(
            ChatRequest(model="gemini-3-flash-preview", user="user-b", messages=[Message(role="user", content="same")]),
            runtime_state.client,
        )
        return response_one, response_two

    response_one, response_two = run_with_account_pool(
        call_two_users(),
        account_service=account_service,
        rotator=rotator,
        account_client_pool=pool,
    )

    assert response_one["choices"][0]["message"]["content"] == f"ok:{first.id}"
    assert response_two["choices"][0]["message"]["content"] == f"ok:{second.id}"


def test_chat_pool_retry_excludes_rate_limited_account(tmp_path):
    FakePooledChatClient.clients = []
    store = AccountStore(accounts_dir=tmp_path)
    first = store.save_account("first", None, storage_state(cookie_name="sid1"))
    second = store.save_account("second", None, storage_state(cookie_name="sid2"), activate=False)
    FakePooledChatClient.fail_for_accounts = {first.id}
    account_service = AccountService(store, LoginService())
    rotator = AccountRotator(store)
    pool = AccountClientPool(store, client_factory=FakePooledChatClient)

    async def call_chat_once():
        return await handle_chat(
            ChatRequest(model="gemini-3-flash-preview", messages=[Message(role="user", content="hello")]),
            runtime_state.client,
        )

    response = run_with_account_pool(
        call_chat_once(),
        account_service=account_service,
        rotator=rotator,
        account_client_pool=pool,
    )

    stats = rotator.get_all_stats()
    assert response["choices"][0]["message"]["content"] == f"ok:{second.id}"
    assert stats[first.id]["rate_limited"] == 1
    assert stats[second.id]["requests"] == 1
    assert stats[first.id]["in_flight"] == 0
    assert stats[second.id]["in_flight"] == 0


def test_chat_pool_preserves_auth_error_when_retry_has_no_account(tmp_path):
    FakePooledChatClient.clients = []
    FakePooledChatClient.fail_for_accounts = set()
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("first", None, storage_state(cookie_name="sid1"))
    FakePooledChatClient.auth_fail_for_accounts = {account.id}
    account_service = AccountService(store, LoginService())
    rotator = AccountRotator(store)
    pool = AccountClientPool(store, client_factory=FakePooledChatClient)

    async def call_chat_once():
        return await handle_chat(
            ChatRequest(model="gemini-3-flash-preview", messages=[Message(role="user", content="hello")]),
            runtime_state.client,
        )

    with pytest.raises(HTTPException) as exc_info:
        run_with_account_pool(
            call_chat_once(),
            account_service=account_service,
            rotator=rotator,
            account_client_pool=pool,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["type"] == "authentication_error"
    assert "GenerateContent permission check failed" in exc_info.value.detail["message"]


def test_image_pool_preserves_auth_error_when_retry_has_no_account(tmp_path):
    FakePooledChatClient.clients = []
    FakePooledChatClient.fail_for_accounts = set()
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("first", None, storage_state(cookie_name="sid1"), tier="pro")
    FakePooledChatClient.auth_fail_for_accounts = {account.id}
    account_service = AccountService(store, LoginService())
    rotator = AccountRotator(store)
    pool = AccountClientPool(store, client_factory=FakePooledChatClient)

    async def call_image_once():
        return await handle_image_generation(
            ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview"),
            runtime_state.client,
        )

    with pytest.raises(HTTPException) as exc_info:
        run_with_account_pool(
            call_image_once(),
            account_service=account_service,
            rotator=rotator,
            account_client_pool=pool,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["type"] == "authentication_error"
    assert "GenerateContent permission check failed" in exc_info.value.detail["message"]


def test_gemini_pool_preserves_auth_error_when_retry_has_no_account(tmp_path):
    FakePooledChatClient.clients = []
    FakePooledChatClient.fail_for_accounts = set()
    store = AccountStore(accounts_dir=tmp_path)
    account = store.save_account("first", None, storage_state(cookie_name="sid1"))
    FakePooledChatClient.auth_fail_for_accounts = {account.id}
    account_service = AccountService(store, LoginService())
    rotator = AccountRotator(store)
    pool = AccountClientPool(store, client_factory=FakePooledChatClient)

    async def call_gemini_once():
        return await handle_gemini_generate_content(
            "gemini-3-flash-preview",
            GeminiGenerateContentRequest(
                contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
            ),
            runtime_state.client,
            stream=False,
        )

    with pytest.raises(HTTPException) as exc_info:
        run_with_account_pool(
            call_gemini_once(),
            account_service=account_service,
            rotator=rotator,
            account_client_pool=pool,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["type"] == "authentication_error"
    assert "GenerateContent permission check failed" in exc_info.value.detail["message"]


def test_image_generation_switches_from_free_active_account_to_available_premium(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    free = store.save_account("free", None, storage_state(cookie_name="sid1"), tier="free")
    pro = store.save_account("pro", None, storage_state(cookie_name="sid2"), activate=False, tier="pro")
    account_service = AccountService(store, LoginService())
    rotator = AccountRotator(store)
    browser_session = FakeBrowserSession()
    snapshot_cache = FakeSnapshotCache()
    client = FakeImageClient()

    response = run_with_account_runtime(
        handle_image_generation(ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview"), client),
        account_service=account_service,
        rotator=rotator,
        browser_session=browser_session,
        snapshot_cache=snapshot_cache,
        generated_images_dir=tmp_path / "generated-images",
    )

    assert store.get_active_account().id == pro.id
    assert store.get_active_account().id != free.id
    assert browser_session.auth_paths == [str(tmp_path / pro.id / "auth.json")]
    assert snapshot_cache.clear_calls == 1
    assert len(client.calls) == 1
    assert response["data"][0]["b64_json"]


def test_chat_pro_text_model_switches_from_free_active_account_to_available_premium(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    free = store.save_account("free", None, storage_state(cookie_name="sid1"), tier="free")
    pro = store.save_account("pro", None, storage_state(cookie_name="sid2"), activate=False, tier="pro")
    account_service = AccountService(store, LoginService())
    rotator = AccountRotator(store)
    browser_session = FakeBrowserSession()
    snapshot_cache = FakeSnapshotCache()
    client = FakeChatClient()

    response = run_with_account_runtime(
        handle_chat(
            ChatRequest(
                model="gemini-3.1-pro-preview",
                messages=[Message(role="user", content="hello")],
            ),
            client,
        ),
        account_service=account_service,
        rotator=rotator,
        browser_session=browser_session,
        snapshot_cache=snapshot_cache,
    )

    assert store.get_active_account().id == pro.id
    assert store.get_active_account().id != free.id
    assert browser_session.auth_paths == [str(tmp_path / pro.id / "auth.json")]
    assert snapshot_cache.clear_calls == 1
    assert client.calls[0]["model"] == "gemini-3.1-pro-preview"
    assert response["choices"][0]["message"]["content"] == "ok"


def test_image_generation_records_resolution_usage(tmp_path):
    store = AccountStore(accounts_dir=tmp_path)
    pro = store.save_account("pro", None, storage_state(cookie_name="sid1"), tier="pro")
    account_service = AccountService(store, LoginService())
    rotator = AccountRotator(store)
    browser_session = FakeBrowserSession()
    snapshot_cache = FakeSnapshotCache()
    client = FakeImageClient()

    response = run_with_account_runtime(
        handle_image_generation(
            ImageRequest(prompt="draw", model="gemini-3.1-flash-image-preview", size="1024x1024", n=1),
            client,
        ),
        account_service=account_service,
        rotator=rotator,
        browser_session=browser_session,
        snapshot_cache=snapshot_cache,
        generated_images_dir=tmp_path / "generated-images",
    )

    stats = rotator.get_all_stats()[pro.id]
    assert response["data"][0]["b64_json"]
    assert stats["image_sizes"] == {"1024x1024": 1}
    assert stats["image_total"] == 1