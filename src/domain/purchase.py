"""Purchase lifecycle — the state machine a payment moves through.

A purchase is created before the user is sent to a provider and is the record the
provider's callback resolves against. Its terminal PAID transition is the *only*
trigger for granting an entitlement, which is what makes duplicate callbacks harmless:
the second one finds the purchase already PAID and does nothing.

Transitions are declared, not implied. An illegal move (PAID -> PENDING, a second
PAID) raises rather than silently overwriting, because every one of those would be a
provider or integration bug that must surface loudly rather than corrupt billing state.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from src.domain.product import ProductCode


class PurchaseStatus(StrEnum):
    CREATED = "created"      # row exists; no invoice requested yet
    PENDING = "pending"      # invoice issued, awaiting settlement/confirmations
    PAID = "paid"            # settled and verified — grants the entitlement
    FAILED = "failed"        # provider reported failure
    CANCELLED = "cancelled"  # abandoned by the user, or checkout never opened
    EXPIRED = "expired"      # invoice window elapsed unpaid


#: Legal transitions. PAID/FAILED/CANCELLED/EXPIRED are terminal.
_TRANSITIONS: dict[PurchaseStatus, frozenset[PurchaseStatus]] = {
    PurchaseStatus.CREATED: frozenset({
        PurchaseStatus.PENDING, PurchaseStatus.PAID, PurchaseStatus.FAILED,
        PurchaseStatus.CANCELLED, PurchaseStatus.EXPIRED,
    }),
    PurchaseStatus.PENDING: frozenset({
        PurchaseStatus.PAID, PurchaseStatus.FAILED,
        PurchaseStatus.CANCELLED, PurchaseStatus.EXPIRED,
    }),
    PurchaseStatus.PAID: frozenset(),
    PurchaseStatus.FAILED: frozenset(),
    PurchaseStatus.CANCELLED: frozenset(),
    PurchaseStatus.EXPIRED: frozenset(),
}

#: States in which a checkout can still be resumed rather than started over.
OPEN_STATUSES = frozenset({PurchaseStatus.CREATED, PurchaseStatus.PENDING})


class IllegalPurchaseTransition(RuntimeError):
    """Raised on a move the lifecycle does not allow."""


def can_transition(current: PurchaseStatus, target: PurchaseStatus) -> bool:
    return target in _TRANSITIONS[current]


@dataclass
class Purchase:
    """One attempt to buy one product."""

    id: str                       # our id; travels to the provider as the invoice payload
    user_id: int
    product_code: ProductCode
    currency: str
    amount: Decimal
    status: PurchaseStatus = PurchaseStatus.CREATED
    provider: str = ""
    external_payment_id: str | None = None   # provider-scoped id (invoice id), unique when set
    asset: str | None = None                 # crypto asset actually paid (USDT/TON/BTC…)
    provider_payload: dict | None = None     # last provider snapshot of the invoice
    detail: str | None = None
    created_at: float | None = None
    updated_at: float | None = None
    paid_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_paid(self) -> bool:
        return self.status == PurchaseStatus.PAID

    def transition_to(self, target: PurchaseStatus) -> None:
        if not can_transition(self.status, target):
            raise IllegalPurchaseTransition(
                f"purchase {self.id}: {self.status.value} -> {target.value}")
        self.status = target
