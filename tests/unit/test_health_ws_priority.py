"""WebSocket-first health precedence (§1.1) + dev-build access override."""
import time

from src.config.scanner_config import ScannerConfig
from src.domain.dev_mode import set_unlimited_access, unlimited_access_enabled
from src.domain.entitlements import entitlements_for
from src.domain.enums import ExchangeStatus, SubscriptionTier
from src.domain.user import UserProfile
from src.scanner.status.health_registry import HealthRegistry


def test_rest_failure_does_not_offline_streaming_venue():
    # OKX flap root cause: a REST health-check timeout must not offline a venue
    # whose WS is actively streaming.
    r = HealthRegistry(ScannerConfig())
    r.register("okx")
    r.record_success("okx", stream=True)          # WS delivering data -> Online
    assert r.status("okx") == ExchangeStatus.ONLINE
    for _ in range(ScannerConfig().ws_fast_reconnect_max + 3):
        r.record_failure("okx")                    # REST probe keeps timing out
    assert r.status("okx") == ExchangeStatus.ONLINE  # stays Online, WS is alive


def test_rest_failure_offlines_when_ws_also_stale():
    r = HealthRegistry(ScannerConfig())
    r.register("okx")
    r.record_success("okx", stream=True)
    # Age the WS data past the idle window so REST failures are authoritative again.
    r.health("okx").last_ws_data_at = time.time() - 999
    for _ in range(ScannerConfig().ws_fast_reconnect_max):
        r.record_failure("okx")
    assert r.status("okx") == ExchangeStatus.API_OFFLINE


def test_dev_unlimited_access_grants_pro():
    try:
        set_unlimited_access(True)
        assert unlimited_access_enabled()
        u = UserProfile(telegram_user_id=1)   # default FREE subscription
        assert u.effective_tier == SubscriptionTier.PRO
        ent = entitlements_for(SubscriptionTier.FREE)
        assert ent.tier == SubscriptionTier.PRO
        assert ent.history_enabled and ent.signal_delay_sec == 0
    finally:
        set_unlimited_access(False)


def test_paywall_restored_when_disabled():
    set_unlimited_access(False)
    u = UserProfile(telegram_user_id=1)
    assert u.effective_tier == SubscriptionTier.FREE
    assert entitlements_for(SubscriptionTier.FREE).tier == SubscriptionTier.FREE
