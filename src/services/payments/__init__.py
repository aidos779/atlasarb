"""Payment provider boundary (crypto checkout).

No provider is integrated yet. This package defines the contract a real one plugs
into so that wiring CryptoBot / NOWPayments / any other processor is an adapter plus
one line at the composition root — never a change to subscription or paywall logic.
"""
from src.services.payments.provider import (
    Invoice,
    PaymentProvider,
    PaymentReceipt,
    PlaceholderPaymentProvider,
)

__all__ = [
    "Invoice",
    "PaymentProvider",
    "PaymentReceipt",
    "PlaceholderPaymentProvider",
]
