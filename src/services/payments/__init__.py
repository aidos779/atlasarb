"""Payment provider boundary (crypto checkout).

The live provider is Telegram Crypto Pay (:mod:`cryptopay`); when no ``CRYPTO_PAY_TOKEN``
is configured the composition root falls back to :class:`PlaceholderPaymentProvider`
(checkout shows "coming soon"). Everything a provider plugs into is defined here, so
wiring a different processor is an adapter plus one line at the composition root — never a
change to subscription or paywall logic.
"""
from src.services.payments.cryptopay import (
    CryptoPayClient,
    CryptoPayError,
    CryptoPayProvider,
)
from src.services.payments.provider import (
    Invoice,
    PaymentProvider,
    PaymentProviderUnavailable,
    PaymentReceipt,
    PlaceholderPaymentProvider,
)
from src.services.payments.reconciler import PaymentReconciler
from src.services.payments.webhook import CryptoPayWebhookHandler, WebhookResult

__all__ = [
    "CryptoPayClient",
    "CryptoPayError",
    "CryptoPayProvider",
    "CryptoPayWebhookHandler",
    "Invoice",
    "PaymentProvider",
    "PaymentProviderUnavailable",
    "PaymentReceipt",
    "PaymentReconciler",
    "PlaceholderPaymentProvider",
    "WebhookResult",
]
