"""Signal access service — the Free-tier delivery quota and the paywall decision.

Business rule: a Free user may be delivered exactly ``FREE_SIGNAL_QUOTA`` arbitrage
signals, ever. Pro Lifetime (and staff, and dev builds) are unlimited. Every other
feature of the bot is open on both plans, so this service is the *only* thing standing
between a Free user and the product.

Counting contract — a slot is consumed only by a signal the user genuinely received:

* the ledger row is written **after** the delivery is confirmed (a returned Telegram
  send / a completed render), never before, so a failed send costs nothing;
* the row is an idempotent UPSERT keyed on (user, signal), so a re-render, a retry, or
  the same opportunity arriving on two channels reconciles instead of double-charging;
* signals that were filtered, deduplicated, throttled or never reached the user are
  never recorded, because ``record`` is not reached on those paths.

Pro users are never written to the ledger at all — it exists solely to enforce the Free
cap, and there is nothing to count once the cap does not apply.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.config import get_logger
from src.database.base import Database
from src.database.repositories.delivery_repo import SignalDeliveryRepository
from src.domain.entitlements import UNLIMITED, entitlements_for
from src.domain.enums import SubscriptionTier
from src.domain.signal import Signal
from src.domain.user import UserProfile
from src.i18n import t
from src.services.product_catalog import ProductCatalog

log = get_logger("services.signal_access")

CHANNEL_LIST = "list"
CHANNEL_ALERT = "alert"


@dataclass(frozen=True)
class Allowance:
    """What this user may currently be shown, and how much of the quota is left."""

    visible: list[Signal]
    delivered: int
    quota: int
    remaining: int

    @property
    def unlimited(self) -> bool:
        return self.quota == UNLIMITED

    @property
    def exhausted(self) -> bool:
        """True when the paywall should replace the signal list."""
        return not self.unlimited and self.remaining <= 0


class SignalAccessService:
    def __init__(self, database: Database, catalog: ProductCatalog) -> None:
        self._db = database
        self._catalog = catalog

    def paywall_text(self, profile: UserProfile) -> str:
        """The paywall copy. Lives here so the pull (signal list) and push (instant
        alert) surfaces cannot drift into two different offers."""
        lang = profile.settings.language.value
        quota = entitlements_for(SubscriptionTier.FREE).signal_quota
        return "\n\n".join([
            t("paywall.title", lang),
            t("paywall.used_all", lang, quota=quota),
            t("paywall.pitch", lang, price=self._catalog.pro_lifetime().format_amount()),
            t("paywall.updates", lang),
        ])

    async def allowance(self, profile: UserProfile,
                        signals: list[Signal] | None = None) -> Allowance:
        """Resolve how many signals this user may still receive, and which of the
        supplied candidates fit inside that budget (original ordering preserved).
        """
        signals = signals or []
        ent = entitlements_for(profile.effective_tier)
        if ent.unlimited_signals:
            return Allowance(visible=list(signals), delivered=0,
                             quota=UNLIMITED, remaining=UNLIMITED)

        uid = profile.telegram_user_id
        async with self._db.session() as session:
            repo = SignalDeliveryRepository(session)
            delivered = await repo.count(uid)
            already = await repo.delivered_ids(uid, [s.id for s in signals])

        remaining = max(0, ent.signal_quota - delivered)
        budget = remaining
        visible: list[Signal] = []
        for signal in signals:
            if signal.id in already:
                visible.append(signal)   # already paid for — re-showing is free
            elif budget > 0:
                visible.append(signal)
                budget -= 1
        return Allowance(visible=visible, delivered=delivered,
                         quota=ent.signal_quota, remaining=remaining)

    async def may_deliver(self, profile: UserProfile) -> bool:
        """Cheap pre-send gate for the push (instant alert) channel."""
        allowance = await self.allowance(profile)
        return not allowance.exhausted

    async def record_delivery(self, profile: UserProfile, signal_id: str,
                              channel: str) -> bool:
        """Charge one confirmed delivery. Returns True iff a new slot was consumed."""
        return bool(await self.record_deliveries(profile, [signal_id], channel))

    async def record_deliveries(self, profile: UserProfile, signal_ids: list[str],
                                channel: str) -> int:
        """Charge a batch of confirmed deliveries; returns how many slots were consumed.

        Called only once delivery is known to have succeeded (see module docstring).
        """
        if not signal_ids:
            return 0
        if entitlements_for(profile.effective_tier).unlimited_signals:
            return 0  # nothing to meter for Pro/staff — keep the ledger Free-only
        uid = profile.telegram_user_id
        async with self._db.session() as session:
            repo = SignalDeliveryRepository(session)
            consumed = 0
            for signal_id in signal_ids:
                if await repo.record(uid, signal_id, channel):
                    consumed += 1
        if consumed:
            log.info("free_signals_consumed", user_id=uid, channel=channel,
                     consumed=consumed)
        return consumed
