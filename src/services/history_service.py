"""History service (PRD §7.2 /history, §17.4) — record interactions, serve lists.

Records a signal to the user's Viewed/Favorite lists on interaction and, on expiry,
into the Expired list. Each list caps at 50 (BR-HIST-2). Free tier is gated upstream
(BR-HIST-1) — this service is data-only.

Also hosts ReliabilityCache — the sync, TTL-cached facade that lets the scanner's
hot-path confidence factor (§11.5 historical reliability) read the rolling hit-rate
without ever awaiting a DB query per candidate.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from src.config import get_logger
from src.database.base import Database
from src.database.repositories.history_repo import HistoryRepository
from src.domain.signal import Signal

log = get_logger("services.history")


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


class ReliabilityCache:
    """Sync, bounded, TTL-cached provider of the §11.5 historical-reliability factor.

    The scanner assembler needs a *synchronous* callable in its per-candidate hot path,
    while the underlying hit-rate lives behind an async DB query. This cache returns the
    last known value immediately (default until first resolution) and refreshes stale
    keys via fire-and-forget tasks, so the scanner never blocks on the database.
    """

    def __init__(self, history: HistoryService, ttl_sec: float = 600.0,
                 default: float = 60.0, max_keys: int = 4096) -> None:
        self._history = history
        self._ttl = ttl_sec
        self._default = Decimal(str(default))
        self._max_keys = max_keys
        self._values: dict[tuple[str, str, str], tuple[Decimal, float]] = {}
        self._inflight: set[tuple[str, str, str]] = set()

    def __call__(self, arb_type: str, buy: str, sell: str) -> Decimal:
        key = (arb_type, buy, sell)
        now = time.monotonic()
        hit = self._values.get(key)
        if hit is not None and now - hit[1] < self._ttl:
            return hit[0]
        self._schedule_refresh(key)
        return hit[0] if hit is not None else self._default

    def _schedule_refresh(self, key: tuple[str, str, str]) -> None:
        if key in self._inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync context/tests) — keep serving the default
        self._inflight.add(key)
        task = loop.create_task(self._refresh(key))
        task.add_done_callback(lambda _t: self._inflight.discard(key))

    async def _refresh(self, key: tuple[str, str, str]) -> None:
        try:
            value = await self._history.reliability(*key)
        except Exception as exc:  # noqa: BLE001 — a failing lookup must never surface
            # into the scanner; the cached/default value keeps serving.
            log.debug("reliability_refresh_failed", key=key, error=str(exc))
            return
        if len(self._values) >= self._max_keys and key not in self._values:
            # Bounded: drop the stalest entry (rare; max_keys >> live route count).
            oldest = min(self._values, key=lambda k: self._values[k][1])
            del self._values[oldest]
        self._values[key] = (Decimal(str(value)), time.monotonic())
