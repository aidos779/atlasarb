"""History service (PRD §7.2 /history, §17.4) — record interactions, serve lists.

Records a signal to the user's Viewed/Favorite lists on interaction and, on expiry,
into the Expired list. Each list caps at 50 (BR-HIST-2). Free tier is gated upstream
(BR-HIST-1) — this service is data-only.
"""
from __future__ import annotations

from src.database.base import Database
from src.database.repositories.history_repo import HistoryRepository
from src.domain.signal import Signal


class HistoryService:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def record_view(self, user_id: int, signal: Signal) -> None:
        async with self._db.session() as session:
            await HistoryRepository(session).record_interaction(user_id, signal, "viewed")

    async def record_favorite(self, user_id: int, signal: Signal) -> None:
        async with self._db.session() as session:
            await HistoryRepository(session).record_interaction(user_id, signal, "favorited")

    async def viewed(self, user_id: int) -> list:
        async with self._db.session() as session:
            return await HistoryRepository(session).list_interactions(user_id, "viewed")

    async def favorites_history(self, user_id: int) -> list:
        async with self._db.session() as session:
            return await HistoryRepository(session).list_interactions(user_id, "favorited")

    async def expired(self, user_id: int) -> list:
        async with self._db.session() as session:
            return await HistoryRepository(session).list_interactions(user_id, "expired")

    async def reliability(self, arb_type: str, buy: str, sell: str) -> float:
        async with self._db.session() as session:
            return await HistoryRepository(session).type_reliability(arb_type, buy, sell)
