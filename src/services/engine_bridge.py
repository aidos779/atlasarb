"""Engine bridge — the hand-off boundary between the scanner and the bot (Scanner §0/§16).

Implements the NotificationQueue and SignalHistoryStore ports the engine depends on:
  - publish(): update the live SignalRegistry and enqueue the signal for the
    NotificationService to evaluate per-user eligibility.
  - archive(): move the expired signal out of the registry and persist it to history.

This keeps the engine ignorant of the bot/DB (Dependency Inversion / ARCH-1).
"""
from __future__ import annotations

import asyncio
import time

from src.config import describe_exc, get_logger
from src.domain.signal import Signal
from src.services.signal_registry import SignalRegistry

log = get_logger("services.bridge")

# Upper bound on the outbound notification backlog. Deliberately far below the old
# 10_000: an arbitrage alert is only actionable while the spread is live, so a backlog
# of thousands is not "work to catch up on", it is memory holding signals that will be
# stale before they are read. At the cap the OLDEST entry is evicted, not the newest —
# during a surge the freshest signals are the ones worth delivering.
MAX_PENDING = 1000

# Hard freshness budget. A signal that has waited longer than this in the queue is
# dropped instead of dispatched: the price it quotes no longer exists, and sending it
# costs a Telegram write plus a user's trust. Tier delivery delays (0/10/60s) are
# applied *after* the dequeue, so this bounds queue latency only.
MAX_QUEUE_AGE_SEC = 20.0


class EngineBridge:
    def __init__(self, registry: SignalRegistry, database, history_repo_factory) -> None:
        self._registry = registry
        self._database = database
        self._history_repo_factory = history_repo_factory
        self._queue: asyncio.Queue[tuple[float, str, Signal]] = asyncio.Queue(
            maxsize=MAX_PENDING)
        self._dropped_full = 0
        self._dropped_stale = 0

    # ── NotificationQueue port ──
    async def publish(self, signal: Signal, event: str) -> None:
        self._registry.upsert(signal)
        item = (time.monotonic(), event, signal)
        while True:
            try:
                self._queue.put_nowait(item)
                break
            except asyncio.QueueFull:
                # Evict the oldest to make room. Drop-oldest (rather than reject-newest)
                # keeps the backlog fresh under sustained overload instead of pinning it
                # to whatever arrived first and starving every later signal.
                try:
                    _, _, evicted = self._queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — drained concurrently
                    continue
                self._dropped_full += 1
                log.warning("notification_queue_full", dropped_signal_id=evicted.id,
                            incoming_signal_id=signal.id, cap=MAX_PENDING,
                            dropped_total=self._dropped_full)
        log.info("notification_queued", signal_id=signal.id, admit_event=event,
                 arb_type=signal.arb_type.value, coin=signal.coin,
                 net_profit_pct=float(round(signal.net_profit_pct, 4)),
                 queue_pending=self._queue.qsize())

    # ── SignalHistoryStore port ──
    async def archive(self, signal: Signal) -> None:
        self._registry.expire(signal)
        try:
            async with self._database.session() as session:
                await self._history_repo_factory(session).archive_signal(signal)
        except Exception as exc:  # noqa: BLE001 — never let archival break the engine
            log.error("archive_failed", signal_id=signal.id, error=describe_exc(exc),
                      exc_info=exc)

    # ── consumed by NotificationService ──
    async def next_event(self) -> tuple[str, Signal]:
        """Pop the next signal fresh enough to be worth delivering.

        Stale entries are discarded here rather than at enqueue time because staleness is
        a function of how long the *consumers* took — the queue only backs up when the
        dispatcher is behind, which is exactly when the head is oldest.
        """
        while True:
            queued_at, event, signal = await self._queue.get()
            age = time.monotonic() - queued_at
            if age > MAX_QUEUE_AGE_SEC:
                self._dropped_stale += 1
                log.warning("notification_dropped_stale", signal_id=signal.id,
                            age_sec=round(age, 2), max_age_sec=MAX_QUEUE_AGE_SEC,
                            queue_pending=self._queue.qsize(),
                            dropped_total=self._dropped_stale)
                continue
            return event, signal

    def pending(self) -> int:
        return self._queue.qsize()

    def drop_stats(self) -> dict[str, int]:
        """Backlog-health counters for the ENGINE STATS report / admin panel."""
        return {"dropped_full": self._dropped_full, "dropped_stale": self._dropped_stale}
