"""Subscription service — plans, upgrade/downgrade/cancel, renewal, proration, freeze.

Implements PRD §15: immediate upgrades with pro-rata credit (R-SUB-1), end-of-period
downgrade keeping paid entitlements (R-SUB-2), data freeze not delete on downgrade
(R-SUB-3), self-service cancel (R-SUB-4), and 2 automatic retries over 72h (§15.4).
"""
from __future__ import annotations

import time

from src.config import get_logger
from src.config.settings import Settings
from src.database.base import Database
from src.database.repositories.favorites_repo import FavoritesRepository
from src.database.repositories.misc_repos import BillingRepository
from src.database.repositories.user_repo import UserRepository
from src.domain.entitlements import Entitlements, TierPricing, entitlements_for
from src.domain.enums import SubscriptionStatus, SubscriptionTier, UserRole
from src.domain.user import UserProfile

log = get_logger("services.subscription")

_PERIOD_SEC = 30 * 24 * 3600
_MAX_RENEWAL_RETRIES = 2


class SubscriptionService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self._db = database
        self._settings = settings

    def pricing(self) -> list[TierPricing]:
        return [
            TierPricing(SubscriptionTier.FREE, 0.0, ["Discovery tier"]),
            TierPricing(SubscriptionTier.BASIC, self._settings.price_basic_usd,
                        ["10s delay", "Top 20/refresh", "+CEX-DEX/DEX-DEX",
                         "20 alerts/hr", "History", "Network/Liquidity filters"]),
            TierPricing(SubscriptionTier.PRO, self._settings.price_pro_usd,
                        ["Real-time", "Unlimited signals", "+Funding/Cross-Chain",
                         "Unlimited alerts", "Risk/Age filters", "Priority support"]),
        ]

    def entitlements(self, profile: UserProfile) -> Entitlements:
        return entitlements_for(profile.effective_tier)

    def _price_for(self, tier: SubscriptionTier) -> float:
        return {SubscriptionTier.FREE: 0.0,
                SubscriptionTier.BASIC: self._settings.price_basic_usd,
                SubscriptionTier.PRO: self._settings.price_pro_usd}[tier]

    def prorated_charge(self, profile: UserProfile, target: SubscriptionTier,
                        now: float | None = None) -> float:
        """R-SUB-1 — credit remaining paid time toward the new tier's first invoice."""
        now = now or time.time()
        new_price = self._price_for(target)
        sub = profile.subscription
        if not sub.is_paid_active or not sub.period_end or sub.period_end <= now:
            return new_price
        remaining_frac = max(0.0, (sub.period_end - now) / _PERIOD_SEC)
        credit = self._price_for(sub.tier) * remaining_frac
        return round(max(0.0, new_price - credit), 2)

    async def activate(self, user_id: int, target: SubscriptionTier,
                       amount_paid: float, now: float | None = None) -> UserProfile:
        """Apply a successful payment immediately (R-SUB-1)."""
        now = now or time.time()
        async with self._db.session() as session:
            repo = UserRepository(session)
            profile = await repo.get(user_id)
            if profile is None:
                raise ValueError("user not found")
            profile.subscription.tier = target
            profile.subscription.status = SubscriptionStatus.ACTIVE
            profile.subscription.period_end = now + _PERIOD_SEC
            profile.subscription.retries_used = 0
            profile.subscription.auto_renew = True
            if profile.role not in (UserRole.ADMIN, UserRole.SUPPORT):
                profile.role = UserRole.PAID
            await repo.save_profile(profile)
            await BillingRepository(session).record(
                user_id, "purchase", target.value, amount_paid)
            await self._unfreeze(session, user_id)
            return profile

    async def cancel(self, user_id: int) -> UserProfile:
        """R-SUB-4 self-service; R-SUB-2 stays active until period end."""
        async with self._db.session() as session:
            repo = UserRepository(session)
            profile = await repo.get(user_id)
            if profile is None:
                raise ValueError("user not found")
            profile.subscription.auto_renew = False
            profile.subscription.status = SubscriptionStatus.CANCELLED
            await repo.save_profile(profile)
            await BillingRepository(session).record(
                user_id, "cancel", profile.subscription.tier.value)
            return profile

    async def process_renewal(self, user_id: int, charge_succeeds: bool,
                              now: float | None = None) -> str:
        """Renewal attempt with up to 2 retries then downgrade (§15.4 / FR-SUB-02).

        Returns one of: renewed | retry | downgraded.
        """
        now = now or time.time()
        async with self._db.session() as session:
            repo = UserRepository(session)
            profile = await repo.get(user_id)
            if profile is None:
                return "downgraded"
            sub = profile.subscription
            billing = BillingRepository(session)
            if charge_succeeds:
                sub.status = SubscriptionStatus.ACTIVE
                sub.period_end = (sub.period_end or now) + _PERIOD_SEC
                sub.retries_used = 0
                await repo.save_profile(profile)
                await billing.record(user_id, "renewal", sub.tier.value,
                                     self._price_for(sub.tier))
                return "renewed"
            if sub.retries_used < _MAX_RENEWAL_RETRIES:
                sub.retries_used += 1
                sub.status = SubscriptionStatus.PAST_DUE
                await repo.save_profile(profile)
                await billing.record(user_id, "failure", sub.tier.value)
                return "retry"
            # Exhausted retries → downgrade at end of paid period (R-SUB-2).
            await self._downgrade_to_free(session, profile, repo)
            await billing.record(user_id, "downgrade", "free")
            return "downgraded"

    async def _downgrade_to_free(self, session, profile: UserProfile,
                                 repo: UserRepository) -> None:
        old_tier = profile.subscription.tier
        profile.subscription.tier = SubscriptionTier.FREE
        profile.subscription.status = SubscriptionStatus.EXPIRED
        profile.subscription.auto_renew = False
        if profile.role == UserRole.PAID:
            profile.role = UserRole.FREE
        await repo.save_profile(profile)
        # BR-SUB-3 — freeze (never delete) data exceeding Free limits.
        free = entitlements_for(SubscriptionTier.FREE)
        favs = FavoritesRepository(session)
        await favs.freeze_excess(profile.telegram_user_id, "coin", free.max_favorite_coins)
        await favs.freeze_excess(profile.telegram_user_id, "exchange",
                                 free.max_favorite_exchanges)
        await favs.freeze_excess(profile.telegram_user_id, "signal", free.max_favorite_signals)
        log.info("downgraded", user_id=profile.telegram_user_id, from_tier=old_tier.value)

    async def _unfreeze(self, session, user_id: int) -> None:
        favs = FavoritesRepository(session)
        for kind in ("coin", "exchange", "signal"):
            await favs.unfreeze_all(user_id, kind)

    async def expire_due(self, now: float | None = None) -> None:
        """Downgrade accounts whose paid period has ended (called by scheduler)."""
        # Implemented via process_renewal in the billing scheduler; kept as hook.
