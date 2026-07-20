"""Subscription service — the two-plan catalogue and the Pro Lifetime grant.

Pro is a single one-time purchase that never expires. There is consequently no
renewal, no proration, no retry ladder and no downgrade path: the monthly machinery
that used to live here was removed with the Basic/Pro tiers it served.

What remains is the one operation that matters — granting lifetime access — written
so that a payment webhook can call it safely.
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
from src.domain.enums import SubscriptionTier, UserRole
from src.domain.product import Product
from src.domain.user import UserProfile
from src.services.product_catalog import ProductCatalog

log = get_logger("services.subscription")


class SubscriptionService:
    def __init__(self, database: Database, settings: Settings,
                 catalog: ProductCatalog) -> None:
        self._db = database
        self._settings = settings
        self._catalog = catalog

    def pro_product(self) -> Product:
        """The purchasable Pro product — the one place a price comes from."""
        return self._catalog.pro_lifetime()

    def pricing(self) -> list[TierPricing]:
        """Plan comparison rows. Paid rows are derived from the product catalogue, so
        the compare screen can never quote a price the checkout does not charge."""
        quota = entitlements_for(SubscriptionTier.FREE).signal_quota
        rows = [TierPricing(SubscriptionTier.FREE, 0.0, one_time=False,
                            features=[f"{quota} signals total", "Full bot access"])]
        rows.extend(
            TierPricing(product.grants_tier, product.amount_float, one_time=True,
                        features=["Unlimited signals", "Lifetime access",
                                  "All future updates included"])
            for product in self._catalog.active()
        )
        return rows

    def entitlements(self, profile: UserProfile) -> Entitlements:
        return entitlements_for(profile.effective_tier)

    async def activate_lifetime(self, user_id: int, amount_paid: float,
                                external_payment_id: str | None = None,
                                now: float | None = None) -> UserProfile | None:
        """Grant Pro Lifetime after a confirmed payment.

        ``external_payment_id`` is the provider's payment reference. It is written under
        a unique constraint, so replaying the same webhook is a no-op that returns the
        already-Pro profile rather than granting (and recording revenue) twice. Pass it
        for anything money-backed; ``None`` is for admin comps, which are audited
        separately.

        Returns ``None`` only when the user does not exist.
        """
        now = now or time.time()
        async with self._db.session() as session:
            repo = UserRepository(session)
            profile = await repo.get(user_id)
            if profile is None:
                log.warning("activate_unknown_user", user_id=user_id)
                return None
            billing = BillingRepository(session)
            if external_payment_id and await billing.payment_recorded(external_payment_id):
                log.info("activate_replay_ignored", user_id=user_id,
                         payment_id=external_payment_id)
                return profile
            profile.subscription.tier = SubscriptionTier.PRO_LIFETIME
            profile.subscription.purchased_at = now
            if profile.role not in (UserRole.ADMIN, UserRole.SUPPORT):
                profile.role = UserRole.PAID
            await repo.save_profile(profile)
            await billing.record(user_id, "purchase", SubscriptionTier.PRO_LIFETIME.value,
                                 amount_paid, external_payment_id=external_payment_id)
            # Legacy rows frozen by the retired monthly-downgrade path are released;
            # nothing freezes favorites any more (both plans have unlimited caps).
            favs = FavoritesRepository(session)
            for kind in ("coin", "exchange", "signal"):
                await favs.unfreeze_all(user_id, kind)
            # If two deliveries of the same webhook race past the check above, the unique
            # index on external_payment_id makes one of them fail here and roll back
            # whole. The provider retries, the pre-check then sees the committed row, and
            # the retry is a clean no-op. Letting it raise is the safe outcome: the
            # alternative (swallowing it) would commit a second purchase row.
            log.info("pro_lifetime_activated", user_id=user_id, amount_usd=amount_paid,
                     payment_id=external_payment_id)
            return profile
