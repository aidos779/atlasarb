"""Payment event repository — append-only webhook history for the crypto provider.

The table is never updated except to flip ``processed`` once, and never deleted: a
payment dispute is resolved from here, so no inbound callback may be lost. The unique
``(provider, update_id)`` constraint is the duplicate-webhook guard — ``already_seen``
short-circuits a redelivered signed update before it can drive a second activation.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import PaymentEvent


class PaymentEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def already_seen(self, provider: str, update_id: int) -> bool:
        """Has this provider update id already been recorded? (webhook idempotency)."""
        stmt = select(PaymentEvent.id).where(
            PaymentEvent.provider == provider,
            PaymentEvent.update_id == update_id).limit(1)
        return (await self._session.execute(stmt)).first() is not None

    async def record(self, *, provider: str, update_id: int | None,
                     invoice_id: str | None, purchase_id: str | None,
                     event_type: str, signature_valid: bool, processed: bool,
                     payload: dict) -> PaymentEvent:
        event = PaymentEvent(
            provider=provider, update_id=update_id, invoice_id=invoice_id,
            purchase_id=purchase_id, event_type=event_type,
            signature_valid=signature_valid, processed=processed, payload=payload)
        self._session.add(event)
        await self._session.flush()
        return event

    async def mark_processed(self, event_id: int) -> None:
        event = await self._session.get(PaymentEvent, event_id)
        if event is not None:
            event.processed = True

    async def for_invoice(self, invoice_id: str, limit: int = 50) -> list[PaymentEvent]:
        stmt = select(PaymentEvent).where(
            PaymentEvent.invoice_id == invoice_id
        ).order_by(PaymentEvent.created_at.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())
