import pytest

from aistudio_api.infrastructure.account.tier_detector import AccountTier, _complete_onboarding_sync, parse_account_tier_from_text


@pytest.mark.parametrize(
    ("text", "email", "expected"),
    [
        (
            "Settings\nuser@example.com PRO\nAdvanced settings",
            "user@example.com",
            AccountTier.PRO,
        ),
        (
            "Account\nuser@example.com\nAI Ultra\nSwitch account\nSign out",
            "user@example.com",
            AccountTier.ULTRA,
        ),
        (
            "close User user@example.com PRO Link an API key Manage membership Switch account Sign out",
            "user@example.com",
            AccountTier.PRO,
        ),
        (
            "Google AI Pro\nManage membership\nSwitch account",
            None,
            AccountTier.PRO,
        ),
    ],
)
def test_parse_account_tier_detects_premium_account_context(text, email, expected):
    assert parse_account_tier_from_text(text, email=email) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Upgrade to Google AI Pro to unlock more features",
        "Test out our most advanced and newest models. Advanced settings",
        "Free plan available. Upgrade to get more.",
        "Google AI Pro is available for purchase",
    ],
)
def test_parse_account_tier_keeps_upgrade_marketing_free(text):
    assert parse_account_tier_from_text(text, email="user@example.com") == AccountTier.FREE


def test_parse_account_tier_prefers_ultra_when_both_badges_are_visible():
    text = "user@example.com\nPRO\nGoogle AI Ultra\nManage membership"

    assert parse_account_tier_from_text(text, email="user@example.com") == AccountTier.ULTRA


class FakeOnboardingPage:
    def __init__(self, results):
        self.results = list(results)
        self.wait_calls = []

    def evaluate(self, script: str):
        assert "google apis terms" in script.lower()
        return self.results.pop(0) if self.results else {"needed": False}

    def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)


def test_tier_detection_onboarding_completion_handles_required_terms():
    page = FakeOnboardingPage([
        {"needed": True, "checked": True, "submitted": False, "remaining": True},
        {"needed": True, "checked": False, "submitted": True, "remaining": False},
    ])

    assert _complete_onboarding_sync(page) is True
    assert page.wait_calls == [1200, 1200]