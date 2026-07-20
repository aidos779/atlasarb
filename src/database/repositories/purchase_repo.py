"""Purchase repository — persistence for the purchase lifecycle."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Purchase as PurchaseRow
from src.domain.product import ProductCode
from src.domain.purchase import OPEN_STATUSES, Purchase, PurchaseStatus


def _ts(value: datetime | None) -> float | None:
    return value.timestamp() if value else None


def _to_domain(row: PurchaseRow) -> Purchase:
    return Purchase(
        id=row.id,
        user_id=row.user_id,
        product_code=ProductCode(row.product_code),
        currency=row.currency,
        amount=Decimal(str(row.amount)),
        status=PurchaseStatus(row.status),
        provider=row.provider,
        external_payment_id=row.external_payment_id,
        detail=row.detail,
        created_at=_ts(row.created_at),
        updated_at=_ts(row.updated_at),
        paid_at=_ts(row.paid_at),
    )


class PurchaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, purchase: Purchase) -> Purchase:
        self._session.add(PurchaseRow(
            id=purchase.id, user_id=purchase.user_id,
            product_code=purchase.product_code.value, currency=purchase.currency,
            amount=float(purchase.amount), status=purchase.status.value,
            provider=purchase.provider,
            external_payment_id=purchase.external_payment_id,
            detail=purchase.detail,
        ))
        await self._session.flush()
        return purchase

    async def _row(self, purchase_id: str) -> PurchaseRow | None:
        stmt = select(PurchaseRow).where(PurchaseRow.id == purchase_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get(self, purchase_id: str) -> Purchase | None:
        row = await self._row(purchase_id)
        return _to_domain(row) if row else None

    async def get_by_external_id(self, external_payment_id: str) -> Purchase | None:
        stmt = select(PurchaseRow).where(
            PurchaseRow.external_payment_id == external_payment_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def latest_open(self, user_id: int, product_code: ProductCode) -> Purchase | None:
        """Most recent resumable checkout, so re-tapping Buy does not litter rows."""
        stmt = (
            select(PurchaseRow)
            .where(PurchaseRow.user_id == user_id,
                   PurchaseRow.product_code == product_code.value,
                   PurchaseRow.status.in_([s.value for s in OPEN_STATUSES]))
            .order_by(PurchaseRow.created_at.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def history(self, user_id: int, limit: int = 20) -> list[Purchase]:
        stmt = (
            select(PurchaseRow)
            .where(PurchaseRow.user_id == user_id)
            .order_by(PurchaseRow.created_at.desc())
            .limit(limit)
        )
        return [_to_domain(r) for r in (await self._session.execute(stmt)).scalars()]

    async def save(self, purchase: Purchase) -> None:
        row = await self._row(purchase.id)
        if row is None:
            return
        row.status = purchase.status.value
        row.provider = purchase.provider
        row.external_payment_id = purchase.external_payment_id
        row.detail = purchase.detail
        row.paid_at = (
            datetime.fromtimestamp(purchase.paid_at, tz=UTC)
            if purchase.paid_at else None
        )
