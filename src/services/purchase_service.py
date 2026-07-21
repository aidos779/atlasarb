"""Purchase service — drives the purchase lifecycle and hands settled payments to
subscription activation.

This is the layer a crypto provider plugs into. The full intended flow:

    start_checkout()            CREATED -> invoice requested
      -> mark_pending()         PENDING  (invoice issued, awaiting confirmations)
      -> settle()               PAID     (verified receipt) -> Pro Lifetime granted
    or mark_failed/cancelled/expired()   terminal, nothing granted

Two invariants hold the money side together:

* **Activation happens in exactly one place.** ``settle`` never writes a tier; it calls
  ``SubscriptionService.activate_lifetime``, the same entry point an admin comp and any
  future product use. There is no second way to become Pro.
* **Settling is idempotent, and activation precedes the state write.** A duplicate
  callback on an already-PAID purchase returns silently. A crash between activating and
  recording PAID leaves the purchase re-settleable, and the retry is a no-op at the
  activation layer (unique external_payment_id) — so the failure mode is "runs again
  harmlessly", never "charged but not granted".

Nothing here can mark a purchase PAID on its own: only a caller holding a verified
provider receipt does that. The placeholder provider cannot produce one.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from src.config import get_logger
from src.database.base import Database
from src.database.repositories.purchase_repo import PurchaseRepository
from src.domain.product import Product, ProductCode
from src.domain.purchase import IllegalPurchaseTransition, Purchase, PurchaseStatus
from src.services.payments import PaymentProvider
from src.services.payments.provider import PaymentProviderUnavailable, PaymentReceipt
from src.services.subscription_service import SubscriptionService

log = get_logger("services.purchase")


@dataclass(frozen=True)
class Checkout:
    """Outcome of asking for a payment link."""

    purchase: Purchase
    pay_url: str | None = None

    @property
    def available(self) -> bool:
        """False when no provider is wired up yet — the caller shows "coming soon"."""
        return self.pay_url is not None


class PurchaseService:
    def __init__(self, database: Database, subscriptions: SubscriptionService,
                 provider: PaymentProvider) -> None:
        self._db = database
        self._subscriptions = subscriptions
        self._provider = provider

    # ── lifecycle ──

    async def start_checkout(self, user_id: int, product: Product) -> Checkout:
        """Open (or resume) a checkout for this user and product.

        An existing CREATED/PENDING purchase is reused rather than duplicated, so
        tapping Buy repeatedly does not litter the ledger with abandoned rows.
        """
        async with self._db.session() as session:
            repo = PurchaseRepository(session)
            purchase = await repo.latest_open(user_id, product.code)
            if purchase is None:
                purchase = await repo.add(Purchase(
                    id=str(uuid.uuid4()), user_id=user_id, product_code=product.code,
                    currency=product.currency, amount=product.amount,
                    status=PurchaseStatus.CREATED, provider=self._provider.id,
                ))
                log.info("purchase_created", purchase_id=purchase.id, user_id=user_id,
                         product=product.code.value, amount=product.amount_float)

        try:
            invoice = await self._provider.create_invoice(
                user_id, product.amount_float, payload=purchase.id)
        except PaymentProviderUnavailable as exc:
            # Expected until a processor is wired up. The purchase is closed out rather
            # than left dangling, and nothing is granted.
            await self._close(purchase.id, PurchaseStatus.CANCELLED, str(exc))
            log.info("checkout_unavailable", purchase_id=purchase.id, user_id=user_id)
            return Checkout(purchase=purchase)

        await self.mark_pending(purchase.id, invoice.payment_id)
        return Checkout(purchase=purchase, pay_url=invoice.pay_url)

    async def mark_pending(self, purchase_id: str, external_payment_id: str) -> Purchase | None:
        """Invoice issued — awaiting settlement."""
        async with self._db.session() as session:
            repo = PurchaseRepository(session)
            purchase = await repo.get(purchase_id)
            if purchase is None:
                return None
            if purchase.status == PurchaseStatus.PENDING:
                return purchase                      # already pending; nothing to do
            purchase.transition_to(PurchaseStatus.PENDING)
            purchase.external_payment_id = external_payment_id
            await repo.save(purchase)
            log.info("purchase_pending", purchase_id=purchase_id,
                     payment_id=external_payment_id)
            return purchase

    async def settle(self, receipt: PaymentReceipt) -> Purchase | None:
        """Apply a **verified** provider receipt: mark PAID and grant the entitlement.

        The receipt's payload must carry our purchase id. Callers must only reach this
        with a receipt their provider has cryptographically verified — this method
        trusts it.
        """
        async with self._db.session() as session:
            purchase = await PurchaseRepository(session).get(receipt.payment_id)
            if purchase is None:
                purchase = await PurchaseRepository(session).get_by_external_id(
                    receipt.payment_id)
        if purchase is None:
            log.warning("settle_unknown_purchase", payment_id=receipt.payment_id)
            return None
        return await self.mark_paid(
            purchase.id, external_payment_id=receipt.invoice_id or receipt.payment_id,
            asset=receipt.asset, provider_payload=receipt.raw)

    async def mark_paid(self, purchase_id: str,
                        external_payment_id: str | None = None,
                        asset: str | None = None,
                        provider_payload: dict | None = None) -> Purchase | None:
        """Settle a purchase and grant its product. Idempotent on replay."""
        async with self._db.session() as session:
            purchase = await PurchaseRepository(session).get(purchase_id)
        if purchase is None:
            return None
        if purchase.is_paid:
            # Duplicate callback — the provider delivered the same settlement twice.
            log.info("purchase_settle_replay_ignored", purchase_id=purchase_id)
            return purchase
        if not purchase.is_open:
            raise IllegalPurchaseTransition(
                f"purchase {purchase_id}: {purchase.status.value} -> paid")

        # Grant first, record second: activation is idempotent (unique payment id), the
        # state write is not retry-sensitive, and this ordering means a crash in between
        # can only ever repeat harmless work — never leave a payer without access.
        product_ref = f"purchase:{purchase.id}"
        activated = await self._subscriptions.activate_lifetime(
            purchase.user_id, float(purchase.amount),
            external_payment_id=product_ref)
        if activated is None:
            await self._close(purchase.id, PurchaseStatus.FAILED, "user not found")
            log.error("purchase_activation_failed", purchase_id=purchase_id,
                      user_id=purchase.user_id)
            return None

        async with self._db.session() as session:
            repo = PurchaseRepository(session)
            fresh = await repo.get(purchase_id)
            if fresh is None or fresh.is_paid:
                return fresh
            fresh.transition_to(PurchaseStatus.PAID)
            fresh.paid_at = time.time()
            if external_payment_id:
                fresh.external_payment_id = external_payment_id
            if asset:
                fresh.asset = asset
            if provider_payload:
                fresh.provider_payload = provider_payload
            await repo.save(fresh)
            log.info("purchase_paid", purchase_id=purchase_id, user_id=fresh.user_id,
                     amount=float(fresh.amount), payment_id=external_payment_id)
            return fresh

    async def mark_failed(self, purchase_id: str, detail: str = "") -> Purchase | None:
        return await self._close(purchase_id, PurchaseStatus.FAILED, detail)

    async def mark_cancelled(self, purchase_id: str, detail: str = "") -> Purchase | None:
        return await self._close(purchase_id, PurchaseStatus.CANCELLED, detail)

    async def mark_expired(self, purchase_id: str, detail: str = "") -> Purchase | None:
        return await self._close(purchase_id, PurchaseStatus.EXPIRED, detail)

    async def _close(self, purchase_id: str, status: PurchaseStatus,
                     detail: str = "") -> Purchase | None:
        async with self._db.session() as session:
            repo = PurchaseRepository(session)
            purchase = await repo.get(purchase_id)
            if purchase is None:
                return None
            if purchase.status == status:
                return purchase
            purchase.transition_to(status)
            if detail:
                purchase.detail = detail[:500]
            await repo.save(purchase)
            log.info("purchase_closed", purchase_id=purchase_id, status=status.value)
            return purchase

    # ── reads ──

    async def history(self, user_id: int, limit: int = 20) -> list[Purchase]:
        async with self._db.session() as session:
            return await PurchaseRepository(session).history(user_id, limit)

    async def latest(self, user_id: int) -> Purchase | None:
        history = await self.history(user_id, limit=1)
        return history[0] if history else None

    async def get(self, purchase_id: str) -> Purchase | None:
        async with self._db.session() as session:
            return await PurchaseRepository(session).get(purchase_id)

    async def latest_open(self, user_id: int, product_code: ProductCode) -> Purchase | None:
        async with self._db.session() as session:
            return await PurchaseRepository(session).latest_open(user_id, product_code)
