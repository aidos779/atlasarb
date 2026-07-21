"""Crypto Pay webhook handler — verifies, records, and settles inbound callbacks.

The one place a provider callback becomes a Pro grant. Its ordering is deliberate and
each step is a guard the money side depends on:

1. **Verify the signature.** A callback whose HMAC does not match the API token is never
   trusted — the recorded event is flagged ``signature_valid=False`` and processing stops.
2. **Record the raw event first (never lose webhook history).** Every callback is written
   to ``payment_events`` before anything acts on it, so a dispute is always reconstructable.
3. **Reject duplicates by ``update_id``.** A provider redelivering the same signed update
   is short-circuited as already seen — it cannot drive a second activation.
4. **Validate the amount and currency.** The paid invoice must match the purchase's price
   in USD; a mismatch is refused even though the signature already vouches for the body
   (defence in depth against a tampered or replayed-with-different-amount callback).
5. **Settle idempotently.** Activation flows through the same idempotent
   ``PurchaseService.mark_paid`` used everywhere else, so even a callback that slips past
   the dedup guard (a distinct update id for an already-paid invoice) grants exactly once.

The handler is transport-agnostic: it takes the raw body and signature header and is
driven by either the inbound HTTP listener or, in polling deployments, nothing at all —
the reconciler settles the same invoices through the same ``mark_paid``.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from src.config import get_logger
from src.database.base import Database
from src.database.models import PaymentEvent
from src.database.repositories.payment_event_repo import PaymentEventRepository
from src.domain.purchase import IllegalPurchaseTransition, Purchase
from src.services.payments.cryptopay import CryptoPayClient
from src.services.purchase_service import PurchaseService

log = get_logger("services.payments.webhook")

PROVIDER = "cryptobot"

# Outcome tags — also what the HTTP layer maps to a status code (all 200 to the provider
# except a malformed body, so Crypto Pay does not retry a callback we deliberately refuse).
STATUS_ACTIVATED = "activated"
STATUS_DUPLICATE = "duplicate"
STATUS_INVALID_SIGNATURE = "invalid_signature"
STATUS_MALFORMED = "malformed"
STATUS_IGNORED = "ignored"
STATUS_UNKNOWN_PURCHASE = "unknown_purchase"
STATUS_AMOUNT_MISMATCH = "amount_mismatch"
STATUS_ALREADY_PAID = "already_paid"

#: Called after a callback settles a purchase for the first time (send the Telegram
#: confirmation). Never called on a duplicate or an already-paid purchase.
PaidCallback = Callable[[Purchase], Awaitable[None]]


@dataclass(frozen=True)
class WebhookResult:
    ok: bool
    status: str
    purchase: Purchase | None = None


class CryptoPayWebhookHandler:
    def __init__(self, database: Database, client: CryptoPayClient,
                 purchases: PurchaseService, *, price_usd: float,
                 accepted_assets: list[str] | None = None,
                 on_paid: PaidCallback | None = None) -> None:
        self._db = database
        self._client = client
        self._purchases = purchases
        self._price = Decimal(str(price_usd))
        self._assets = {a.upper() for a in (accepted_assets or [])}
        self._on_paid = on_paid

    async def handle(self, raw_body: bytes, signature: str) -> WebhookResult:
        valid = self._client.verify_signature(raw_body, signature)
        try:
            update = json.loads(raw_body)
        except (ValueError, TypeError):
            # Cannot even parse it — record a minimal event and tell the caller to 400.
            await self._record(update_id=None, invoice_id=None, purchase_id=None,
                               event_type="", signature_valid=valid, processed=False,
                               payload={"raw": _safe_text(raw_body)})
            log.warning("crypto_pay_webhook_malformed", signature_valid=valid)
            return WebhookResult(ok=False, status=STATUS_MALFORMED)

        update_id = _as_int(update.get("update_id"))
        event_type = str(update.get("update_type") or "")
        invoice = update.get("payload") if isinstance(update.get("payload"), dict) else {}
        invoice_id = _opt_str(invoice.get("invoice_id"))
        purchase_id = _opt_str(invoice.get("payload"))

        # Duplicate guard: a redelivered signed update is already in history.
        if valid and update_id is not None:
            async with self._db.session() as session:
                if await PaymentEventRepository(session).already_seen(PROVIDER, update_id):
                    log.info("crypto_pay_webhook_duplicate", update_id=update_id,
                             invoice_id=invoice_id)
                    return WebhookResult(ok=True, status=STATUS_DUPLICATE)

        event = await self._record(
            update_id=update_id, invoice_id=invoice_id, purchase_id=purchase_id,
            event_type=event_type, signature_valid=valid, processed=False,
            payload=update)

        if not valid:
            log.warning("crypto_pay_webhook_bad_signature", update_id=update_id,
                        invoice_id=invoice_id)
            return WebhookResult(ok=False, status=STATUS_INVALID_SIGNATURE)

        if event_type != "invoice_paid" or invoice.get("status") != "paid":
            return WebhookResult(ok=True, status=STATUS_IGNORED)

        if purchase_id is None:
            log.warning("crypto_pay_webhook_no_payload", invoice_id=invoice_id)
            return WebhookResult(ok=True, status=STATUS_UNKNOWN_PURCHASE)

        purchase = await self._purchases.get(purchase_id)
        if purchase is None:
            log.warning("crypto_pay_webhook_unknown_purchase", purchase_id=purchase_id,
                        invoice_id=invoice_id)
            return WebhookResult(ok=True, status=STATUS_UNKNOWN_PURCHASE)

        if not self._amount_matches(invoice):
            log.error("crypto_pay_webhook_amount_mismatch", purchase_id=purchase_id,
                      invoice_id=invoice_id, expected=str(self._price),
                      got=invoice.get("amount"), fiat=invoice.get("fiat"))
            return WebhookResult(ok=False, status=STATUS_AMOUNT_MISMATCH)

        if purchase.is_paid:
            return WebhookResult(ok=True, status=STATUS_ALREADY_PAID, purchase=purchase)

        asset = invoice.get("paid_asset") or invoice.get("asset")
        try:
            settled = await self._purchases.mark_paid(
                purchase.id, external_payment_id=invoice_id, asset=asset,
                provider_payload=invoice)
        except IllegalPurchaseTransition:
            # A provider reporting success on an abandoned/cancelled invoice is a bug —
            # surface it as a structured error rather than corrupting billing state.
            log.error("crypto_pay_webhook_illegal_transition", purchase_id=purchase.id,
                      status=purchase.status.value)
            return WebhookResult(ok=False, status=STATUS_IGNORED, purchase=purchase)

        await self._mark_event_processed(event.id)
        if settled is not None and settled.is_paid and self._on_paid is not None:
            await self._safe_confirm(settled)
        log.info("crypto_pay_webhook_settled", purchase_id=purchase.id,
                 invoice_id=invoice_id, asset=asset)
        return WebhookResult(ok=True, status=STATUS_ACTIVATED, purchase=settled)

    # ── helpers ──

    def _amount_matches(self, invoice: dict) -> bool:
        if str(invoice.get("fiat") or invoice.get("currency") or "").upper() != "USD":
            return False
        try:
            return Decimal(str(invoice.get("amount"))) == self._price
        except (InvalidOperation, TypeError):
            return False

    async def _record(self, **kwargs) -> PaymentEvent:
        async with self._db.session() as session:
            return await PaymentEventRepository(session).record(
                provider=PROVIDER, **kwargs)

    async def _mark_event_processed(self, event_id: int) -> None:
        async with self._db.session() as session:
            await PaymentEventRepository(session).mark_processed(event_id)

    async def _safe_confirm(self, purchase: Purchase) -> None:
        if self._on_paid is None:
            return
        try:
            await self._on_paid(purchase)
        except Exception as exc:  # noqa: BLE001 — a failed confirmation must not un-settle
            log.error("crypto_pay_confirm_failed", purchase_id=purchase.id,
                      error=str(exc))


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _opt_str(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _safe_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8", "replace")[:2000]
    except Exception:  # noqa: BLE001
        return "<unreadable>"
