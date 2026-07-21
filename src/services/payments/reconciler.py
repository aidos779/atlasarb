"""Payment reconciler — polls Crypto Pay for the status of open invoices.

A long-polling deployment has no inbound web server, so a webhook cannot always be
delivered. The reconciler is the pull-side counterpart: on each scheduler tick it asks
Crypto Pay for the current status of every PENDING invoice and drives the purchase to its
terminal state.

It settles through the *same* idempotent ``PurchaseService.mark_paid`` the webhook uses,
so the two mechanisms are safe to run together — whichever observes the payment first
grants access, and the other is a no-op. A ``paid`` reconciliation is also written to the
``payment_events`` audit trail (with no ``update_id`` — it is a poll, not a callback), so
the settlement's provenance is never lost.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.config import describe_exc, get_logger
from src.database.base import Database
from src.database.repositories.payment_event_repo import PaymentEventRepository
from src.database.repositories.purchase_repo import PurchaseRepository
from src.domain.purchase import IllegalPurchaseTransition, Purchase
from src.services.payments.cryptopay import CryptoPayClient
from src.services.purchase_service import PurchaseService

log = get_logger("services.payments.reconciler")

PROVIDER = "cryptobot"

PaidCallback = Callable[[Purchase], Awaitable[None]]


class PaymentReconciler:
    def __init__(self, database: Database, client: CryptoPayClient,
                 purchases: PurchaseService, *,
                 on_paid: PaidCallback | None = None, batch: int = 100) -> None:
        self._db = database
        self._client = client
        self._purchases = purchases
        self._on_paid = on_paid
        self._batch = batch

    async def run_once(self) -> int:
        """Poll every open invoice once; return how many purchases were settled."""
        async with self._db.session() as session:
            pending = await PurchaseRepository(session).pending_with_invoice(
                PROVIDER, self._batch)
        if not pending:
            return 0
        by_invoice = {p.external_payment_id: p for p in pending
                      if p.external_payment_id}
        try:
            invoices = await self._client.get_invoices(list(by_invoice))
        except Exception as exc:  # noqa: BLE001 — a provider hiccup must not kill the tick
            log.warning("reconcile_poll_failed", error=describe_exc(exc))
            return 0

        settled = 0
        for invoice in invoices:
            purchase = by_invoice.get(str(invoice.get("invoice_id")))
            if purchase is None:
                continue
            status = invoice.get("status")
            if status == "paid":
                if await self._settle(purchase, invoice):
                    settled += 1
            elif status == "expired":
                await self._purchases.mark_expired(purchase.id, "invoice expired")
                log.info("reconcile_invoice_expired", purchase_id=purchase.id,
                         invoice_id=purchase.external_payment_id)
        return settled

    async def _settle(self, purchase: Purchase, invoice: dict) -> bool:
        # If a webhook already settled this invoice between the poll and now, do nothing —
        # activation and the confirmation message have already happened once.
        fresh = await self._purchases.get(purchase.id)
        if fresh is None or fresh.is_paid:
            return False
        async with self._db.session() as session:
            await PaymentEventRepository(session).record(
                provider=PROVIDER, update_id=None,
                invoice_id=purchase.external_payment_id, purchase_id=purchase.id,
                event_type="reconcile_paid", signature_valid=True, processed=True,
                payload=invoice)
        asset = invoice.get("paid_asset") or invoice.get("asset")
        try:
            result = await self._purchases.mark_paid(
                purchase.id, external_payment_id=purchase.external_payment_id,
                asset=asset, provider_payload=invoice)
        except IllegalPurchaseTransition:
            return False
        if result is None or not result.is_paid:
            return False
        if self._on_paid is not None:
            try:
                await self._on_paid(result)
            except Exception as exc:  # noqa: BLE001
                log.error("reconcile_confirm_failed", purchase_id=result.id,
                          error=str(exc))
        log.info("reconcile_invoice_settled", purchase_id=purchase.id,
                 invoice_id=purchase.external_payment_id, asset=asset)
        return True
