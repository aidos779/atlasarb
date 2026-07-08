"""Engine bridge — the hand-off boundary between the scanner and the bot (Scanner §0/§16).

Implements the NotificationQueue and SignalHistoryStore ports the engine depends on:
  - publish(): update the live SignalRegistry and enqueue the signal for the
    NotificationService to evaluate per-user eligibility.
  - archive(): move the expired signal out of the registry and persist it to history.

This keeps the engine ignorant of the bot/DB (Dependency Inversion / ARCH-1).
"""
from __future__ import annotations

import asyncio

from src.config import get_logger
from src.domain.signal import Signal
from src.services.signal_registry import SignalRegistry

log = get_logger("services.bridge")


class EngineBridge:
    def __init__(self, registry: SignalRegistry, database, history_repo_factory) -> None:
        self._registry = registry
        self._database = database
        self._history_repo_factory = history_repo_factory
        self._queue: asyncio.Queue[tuple[str, Signal]] = asyncio.Queue(maxsize=10000)

    # ── NotificationQueue port ──
    async def publish(self, signal: Signal, event: str) -> None:
        self._registry.upsert(signal)
        try:
            self._queue.put_nowait((event, signal))
        except asyncio.QueueFull:
            log.warning("notification_queue_full", signal_id=signal.id)

    # ── SignalHistoryStore port ──
    async def archive(self, signal: Signal) -> None:
        self._registry.expire(signal)
        try:
            async with self._database.session() as session:
                await self._history_repo_factory(session).archive_signal(signal)
        except Exception as exc:  # noqa: BLE001 — never let archival break the engine
            log.warning("archive_failed", signal_id=signal.id, error=str(exc))

    # ── consumed by NotificationService ──
    async def next_event(self) -> tuple[str, Signal]:
        return await self._queue.get()

    def pending(self) -> int:
        return self._queue.qsize()
