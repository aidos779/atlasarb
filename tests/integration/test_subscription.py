"""Two-plan model: the Free signal quota, the paywall, and the Pro Lifetime grant."""
from decimal import Decimal

import pytest

from src.database.repositories.user_repo import UserRepository
from src.domain.enums import ArbitrageType, RiskScore, SubscriptionTier, UserRole
from src.domain.signal import Signal
from src.services.admin_service import AdminService
from src.services.signal_access_service import (
    CHANNEL_ALERT,
    CHANNEL_LIST,
    SignalAccessService,
)
from src.services.subscription_service import SubscriptionService
from src.services.user_service import UserService

pytestmark = pytest.mark.asyncio

FREE_QUOTA = 5


async def _make_user(database, uid=1):
    async with database.session() as s:
        return await UserRepository(s).get_or_create(uid, "u", "U")


async def _reload(database, uid=1):
    async with database.session() as s:
        return await UserRepository(s).get(uid)


def _signal(**kw) -> Signal:
    base = dict(coin="ETH", trading_pair="ETH/USDT", net_profit_pct=Decimal("1.5"),
                liquidity_usd=Decimal("50000"), risk_score=RiskScore.LOW,
                buy_exchange="binance", sell_exchange="okx",
                arb_type=ArbitrageType.CEX_CEX)
    base.update(kw)
    return Signal(**base)


# ── purchase ──

async def test_purchase_grants_lifetime_pro(database, settings, catalog):
    await _make_user(database)
    svc = SubscriptionService(database, settings, catalog)
    profile = await svc.activate_lifetime(1, settings.price_pro_lifetime_usd,
                                          external_payment_id="cryptobot:1")
    assert profile.subscription.tier == SubscriptionTier.PRO_LIFETIME
    assert profile.subscription.is_pro
    assert profile.effective_tier == SubscriptionTier.PRO_LIFETIME
    assert profile.role == UserRole.PAID
    assert profile.subscription.purchased_at is not None
    # Survives a round trip through the DB — the grant is persisted, not in-memory.
    assert (await _reload(database)).subscription.is_pro


async def test_purchase_never_expires(database, settings, catalog):
    """There is no period_end to elapse; a reloaded profile is Pro indefinitely."""
    await _make_user(database)
    await SubscriptionService(database, settings, catalog).activate_lifetime(1, 20.0)
    reloaded = await _reload(database)
    assert reloaded.effective_tier == SubscriptionTier.PRO_LIFETIME
    assert not hasattr(reloaded.subscription, "period_end")


async def test_replayed_webhook_does_not_grant_twice(database, settings, catalog):
    """Providers deliver at-least-once; a duplicate must not double-count revenue."""
    await _make_user(database)
    svc = SubscriptionService(database, settings, catalog)
    await svc.activate_lifetime(1, 20.0, external_payment_id="nowpayments:abc")
    profile = await svc.activate_lifetime(1, 20.0, external_payment_id="nowpayments:abc")
    assert profile.subscription.is_pro
    admin = AdminService(database, None)
    assert await admin.revenue_usd() == 20.0     # charged once, not twice


async def test_activate_unknown_user_returns_none(database, settings, catalog):
    svc = SubscriptionService(database, settings, catalog)
    assert await svc.activate_lifetime(404, 20.0) is None


async def test_pricing_is_a_single_one_time_plan(database, settings, catalog):
    plans = SubscriptionService(database, settings, catalog).pricing()
    assert [p.tier for p in plans] == [SubscriptionTier.FREE, SubscriptionTier.PRO_LIFETIME]
    pro = plans[1]
    assert pro.price_usd == 20.0
    assert pro.one_time is True


# ── free quota ──

async def test_free_user_gets_exactly_five_signals(database, settings, catalog):
    profile = await _make_user(database)
    access = SignalAccessService(database, catalog)
    signals = [_signal() for _ in range(10)]

    allowance = await access.allowance(profile, signals)
    assert allowance.remaining == FREE_QUOTA
    assert len(allowance.visible) == FREE_QUOTA      # the other 5 are withheld
    assert not allowance.exhausted

    await access.record_deliveries(profile, [s.id for s in allowance.visible], CHANNEL_LIST)

    after = await access.allowance(profile, [_signal() for _ in range(3)])
    assert after.delivered == FREE_QUOTA
    assert after.remaining == 0
    assert after.exhausted is True                   # paywall from here on
    assert after.visible == []


async def test_redelivering_the_same_signal_is_free(database, settings, catalog):
    """A re-render or a retry must not burn a second slot."""
    profile = await _make_user(database)
    access = SignalAccessService(database, catalog)
    signal = _signal()
    for _ in range(4):
        await access.record_delivery(profile, signal.id, CHANNEL_LIST)
    assert (await access.allowance(profile)).delivered == 1


async def test_only_recorded_deliveries_are_counted(database, settings, catalog):
    """Nothing is charged until record_* is called — the failure paths never call it."""
    profile = await _make_user(database)
    access = SignalAccessService(database, catalog)
    # Merely resolving an allowance (what a filtered-out or failed send would do) is free.
    await access.allowance(profile, [_signal() for _ in range(5)])
    assert (await access.allowance(profile)).delivered == 0
    assert (await access.may_deliver(profile)) is True


async def test_alert_and_list_channels_share_one_quota(database, settings, catalog):
    profile = await _make_user(database)
    access = SignalAccessService(database, catalog)
    pushed, pulled = _signal(), _signal()
    await access.record_delivery(profile, pushed.id, CHANNEL_ALERT)
    await access.record_delivery(profile, pulled.id, CHANNEL_LIST)
    assert (await access.allowance(profile)).remaining == FREE_QUOTA - 2


async def test_same_signal_on_both_channels_charges_once(database, settings, catalog):
    profile = await _make_user(database)
    access = SignalAccessService(database, catalog)
    signal = _signal()
    assert await access.record_delivery(profile, signal.id, CHANNEL_ALERT) is True
    assert await access.record_delivery(profile, signal.id, CHANNEL_LIST) is False
    assert (await access.allowance(profile)).delivered == 1


async def test_pro_user_is_never_metered(database, settings, catalog):
    await _make_user(database)
    await SubscriptionService(database, settings, catalog).activate_lifetime(1, 20.0)
    profile = await _reload(database)
    access = SignalAccessService(database, catalog)
    signals = [_signal() for _ in range(50)]
    allowance = await access.allowance(profile, signals)
    assert allowance.unlimited and not allowance.exhausted
    assert len(allowance.visible) == 50
    await access.record_deliveries(profile, [s.id for s in signals], CHANNEL_LIST)
    assert (await access.allowance(profile)).exhausted is False


async def test_buying_pro_lifts_an_exhausted_paywall(database, settings, catalog):
    profile = await _make_user(database)
    access = SignalAccessService(database, catalog)
    await access.record_deliveries(
        profile, [_signal().id for _ in range(FREE_QUOTA)], CHANNEL_LIST)
    assert (await access.allowance(profile)).exhausted is True

    await SubscriptionService(database, settings, catalog).activate_lifetime(1, 20.0)
    upgraded = await _reload(database)
    assert (await access.allowance(upgraded)).exhausted is False


async def test_paywall_text_names_the_offer(database, settings, catalog):
    profile = await _make_user(database)
    text = SignalAccessService(database, catalog).paywall_text(profile)
    assert "20" in text and str(FREE_QUOTA) in text


# ── admin override ──

async def test_configured_admin_is_always_pro(database, settings, catalog):
    """settings.admin_telegram_ids -> Administrator role -> permanent Pro, with no
    Telegram id compared anywhere in domain logic."""
    admin_id = settings.admin_telegram_ids[0]
    profile = await UserService(database, settings).get_or_create(admin_id, "a", "A")
    assert profile.role == UserRole.ADMIN
    assert profile.subscription.tier == SubscriptionTier.FREE   # never bought anything
    assert profile.effective_tier == SubscriptionTier.PRO_LIFETIME

    access = SignalAccessService(database, catalog)
    await access.record_deliveries(
        profile, [_signal().id for _ in range(FREE_QUOTA + 3)], CHANNEL_LIST)
    allowance = await access.allowance(profile, [_signal() for _ in range(10)])
    assert allowance.unlimited and not allowance.exhausted
    assert len(allowance.visible) == 10


async def test_support_staff_also_bypass_the_paywall(database, settings, catalog):
    support_id = settings.support_user_ids[0]
    profile = await UserService(database, settings).get_or_create(support_id, "s", "S")
    assert profile.role == UserRole.SUPPORT
    assert profile.effective_tier == SubscriptionTier.PRO_LIFETIME


async def test_non_admin_is_not_promoted(database, settings, catalog):
    profile = await UserService(database, settings).get_or_create(12345, "u", "U")
    assert profile.effective_tier == SubscriptionTier.FREE


async def test_admin_can_comp_pro_without_revenue(database, settings, catalog):
    await _make_user(database, uid=7)
    admin = AdminService(database, None)
    ok = await admin.override_tier(999, 7, SubscriptionTier.PRO_LIFETIME, "vip")
    assert ok
    granted = await _reload(database, uid=7)
    assert granted.subscription.is_pro
    assert granted.subscription.purchased_at is not None
    assert await admin.revenue_usd() == 0.0   # comped, not sold
