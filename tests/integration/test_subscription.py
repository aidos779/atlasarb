import time

import pytest

from src.database.repositories.favorites_repo import FavoritesRepository
from src.database.repositories.user_repo import UserRepository
from src.domain.enums import SubscriptionTier
from src.services.subscription_service import SubscriptionService

pytestmark = pytest.mark.asyncio


async def _make_user(database, uid=1):
    async with database.session() as s:
        await UserRepository(s).get_or_create(uid, "u", "U")


async def test_upgrade_activates_immediately(database, settings):
    await _make_user(database)
    svc = SubscriptionService(database, settings)
    profile = await svc.activate(1, SubscriptionTier.PRO, 79.0)
    assert profile.effective_tier == SubscriptionTier.PRO
    assert profile.subscription.is_paid_active


async def test_proration_credits_remaining(database, settings):
    await _make_user(database)
    svc = SubscriptionService(database, settings)
    await svc.activate(1, SubscriptionTier.BASIC, 19.0)
    async with database.session() as s:
        profile = await UserRepository(s).get(1)
    # ~30 days remaining on Basic → Pro charge should be < full 79.
    charge = svc.prorated_charge(profile, SubscriptionTier.PRO, now=time.time())
    assert charge < settings.price_pro_usd


async def test_cancel_keeps_access_until_period_end(database, settings):
    await _make_user(database)
    svc = SubscriptionService(database, settings)
    await svc.activate(1, SubscriptionTier.PRO, 79.0)
    profile = await svc.cancel(1)
    assert profile.subscription.auto_renew is False
    # Still paid-active until period_end (not yet elapsed).
    assert profile.subscription.tier == SubscriptionTier.PRO


async def test_downgrade_freezes_excess_favorites(database, settings):
    await _make_user(database)
    svc = SubscriptionService(database, settings)
    await svc.activate(1, SubscriptionTier.PRO, 79.0)
    async with database.session() as s:
        favs = FavoritesRepository(s)
        for i in range(6):
            await favs.add(1, "coin", f"C{i}")
    # Exhaust retries → downgrade.
    for _ in range(3):
        await svc.process_renewal(1, charge_succeeds=False)
    async with database.session() as s:
        favs = FavoritesRepository(s)
        active = await favs.count(1, "coin", include_frozen=False)
        total = await favs.count(1, "coin", include_frozen=True)
    assert total == 6              # nothing deleted (BR-SUB-3)
    assert active == 3             # Free cap active, rest frozen


async def test_renewal_retries_then_downgrade(database, settings):
    await _make_user(database)
    svc = SubscriptionService(database, settings)
    await svc.activate(1, SubscriptionTier.BASIC, 19.0)
    assert await svc.process_renewal(1, charge_succeeds=False) == "retry"
    assert await svc.process_renewal(1, charge_succeeds=False) == "retry"
    assert await svc.process_renewal(1, charge_succeeds=False) == "downgraded"
