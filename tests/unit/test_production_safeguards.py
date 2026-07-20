"""Production preflight — fail fast on dev defaults (issue #9).

Booting production on development defaults is worse than not booting: SQLite discards
every user on restart and a "change-me" webhook secret accepts forged callbacks. Both
surface weeks later as application bugs rather than the deploy mistake they are.
"""
from __future__ import annotations

import pytest

from src.config.settings import Settings
from src.domain.dev_mode import set_unlimited_access


def _prod(**overrides) -> Settings:
    base = dict(
        environment="production",
        bot_token="123456:REAL-TOKEN",
        database_url="postgresql+asyncpg://u:p@db/arb",
        telegram_webhook_secret="real-webhook-secret",
        payment_webhook_secret="real-payment-secret",
        admin_telegram_ids=[999],
    )
    return Settings(**{**base, **overrides})


@pytest.fixture(autouse=True)
def _paywall_on():
    # The preflight asserts the dev bypass is off; default it off per test and restore.
    set_unlimited_access(False)
    yield
    set_unlimited_access(False)


def test_fully_configured_production_passes():
    assert _prod().production_config_errors() == []


def test_non_production_is_never_checked():
    # Every dev default at once — still fine outside production.
    assert Settings(environment="development").production_config_errors() == []


@pytest.mark.parametrize("overrides,marker", [
    ({"bot_token": "TEST:TOKEN"}, "BOT_TOKEN"),
    ({"bot_token": "no-colon"}, "malformed"),
    ({"database_url": "sqlite+aiosqlite:///./arb.db"}, "SQLite"),
    ({"telegram_webhook_secret": "change-me"}, "TELEGRAM_WEBHOOK_SECRET"),
    ({"payment_webhook_secret": "change-me-payment"}, "PAYMENT_WEBHOOK_SECRET"),
    ({"admin_telegram_ids": []}, "ADMIN_TELEGRAM_IDS"),
])
def test_each_dev_default_is_rejected(overrides, marker):
    errors = _prod(**overrides).production_config_errors()
    assert any(marker in e for e in errors), errors


def test_all_errors_reported_at_once():
    """One restart per broken key would make a bad deploy an N-restart loop."""
    errors = _prod(bot_token="TEST:TOKEN", database_url="sqlite+aiosqlite:///./a.db",
                   admin_telegram_ids=[]).production_config_errors()
    assert len(errors) >= 3


def test_paywall_bypass_conflicts_with_production():
    set_unlimited_access(True)
    errors = _prod().production_config_errors()
    assert any("paywall-free" in e for e in errors), errors


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
