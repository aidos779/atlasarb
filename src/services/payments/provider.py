"""The crypto-payment port and its not-yet-wired placeholder.

Integration contract for a real provider (CryptoBot, NOWPayments, …):

1. Implement :class:`PaymentProvider`.

   * ``create_invoice`` returns a URL the user is sent to. ``payload`` is opaque to the
     provider and must come back on the webhook — put the telegram user id in it.
   * ``verify`` is handed the raw webhook body plus its signature header and returns a
     :class:`PaymentReceipt` **only** for a payment it has cryptographically verified
     as settled. Returning ``None`` must be the behaviour for anything unverified;
     never trust an amount or a user id that was not signed.

2. Register the adapter at the composition root (``src/app.py``) in place of
   :class:`PlaceholderPaymentProvider`.

3. Route the webhook to
   :meth:`src.services.subscription_service.SubscriptionService.activate_lifetime`,
   passing ``receipt.payment_id``. That call is idempotent on the payment id, which is
   the property that makes at-least-once webhook delivery safe.

Nothing in the bot's paywall or entitlement code needs to change for any of this: the
Pro grant already flows through ``activate_lifetime``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Invoice:
    """A hosted checkout the user is redirected to."""

    payment_id: str      # provider-scoped id, echoed back on the webhook
    pay_url: str
    amount_usd: float


@dataclass(frozen=True)
class PaymentReceipt:
    """A payment the provider has verified as settled."""

    payment_id: str
    user_id: int
    amount_usd: float


@runtime_checkable
class PaymentProvider(Protocol):
    """Crypto checkout port. See the module docstring for the integration contract."""

    id: str

    async def create_invoice(self, user_id: int, amount_usd: float,
                             payload: str) -> Invoice: ...

    async def verify(self, raw_body: bytes, signature: str) -> PaymentReceipt | None: ...


class PaymentProviderUnavailable(RuntimeError):
    """Raised when checkout is requested before a provider has been wired up."""


class PlaceholderPaymentProvider:
    """Stand-in used until a real processor is integrated.

    It deliberately fails loudly rather than pretending to succeed: a checkout stub
    that silently "worked" is exactly how an unpaid user ends up with Pro. Handlers
    catch this and show the "coming soon" screen.
    """

    id = "placeholder"

    async def create_invoice(self, user_id: int, amount_usd: float,
                             payload: str) -> Invoice:
        raise PaymentProviderUnavailable(
            "no crypto payment provider is configured (see services/payments/provider.py)")

    async def verify(self, raw_body: bytes, signature: str) -> PaymentReceipt | None:
        return None
