"""Signal delivery ledger — the Free-tier quota counter.

The ledger is the single source of truth for "how many signals has this user actually
received". There is deliberately no denormalized counter column alongside it: two
sources would drift, and the whole point of the quota is that it is exact.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import SignalDelivery


class SignalDeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _insert(self):
        """Dialect-aware INSERT supporting ON CONFLICT (PostgreSQL and SQLite)."""
        dialect = self._session.get_bind().dialect.name
        return pg_insert(SignalDelivery) if dialect == "postgresql" else sqlite_insert(
            SignalDelivery)

    async def count(self, user_id: int) -> int:
        stmt = select(func.count()).select_from(SignalDelivery).where(
            SignalDelivery.user_id == user_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def delivered_ids(self, user_id: int, signal_ids: list[str]) -> set[str]:
        """Which of these signals this user has already been charged for."""
        if not signal_ids:
            return set()
        stmt = select(SignalDelivery.signal_id).where(
            SignalDelivery.user_id == user_id,
            SignalDelivery.signal_id.in_(signal_ids))
        return set((await self._session.execute(stmt)).scalars())

    async def record(self, user_id: int, signal_id: str, channel: str) -> bool:
        """Record one confirmed delivery. Returns True iff it consumed a new slot.

        ``ON CONFLICT DO NOTHING`` on (user_id, signal_id) makes this idempotent and
        race-free: the signal list and an instant alert can deliver the same signal
        concurrently, and exactly one of them consumes the slot.
        """
        stmt = (
            self._insert()
            .values(user_id=user_id, signal_id=signal_id, channel=channel)
            .on_conflict_do_nothing(index_elements=["user_id", "signal_id"])
        )
        result = await self._session.execute(stmt)
        return bool(result.rowcount)
