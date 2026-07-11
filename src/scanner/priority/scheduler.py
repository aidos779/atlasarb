"""Scanning Priority System (Scanner §1.6).

Assigns each asset a priority tier (1: majors, 2: top-100, 3: long-tail) governing
event-queue ordering. Priority 1 never batches/queues; 2/3 may micro-batch. Priority
never affects the tier (⭐/🟢/🟡/⚪) a signal receives — input-scheduling only.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from src.config.scanner_config import ScannerConfig


class PriorityClassifier:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config
        self._top100: set[str] = set()

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def set_top100(self, assets: set[str]) -> None:
        self._top100 = {a.upper() for a in assets}

    def priority(self, base_asset: str) -> int:
        asset = base_asset.upper()
        if asset in self._config.priority1_assets:
            return 1
        if asset in self._top100:
            return 2
        return 3


class PriorityEventQueue:
    """Priority queue feeding the Signal Generator (§1.6 queue prioritization).

    Priority 1 events are always dequeued before 2, before 3. Under sustained load
    Priority 3 may queue briefly; Priority 1 never does.
    """

    def __init__(self) -> None:
        self._queues: dict[int, asyncio.Queue] = {
            1: asyncio.Queue(), 2: asyncio.Queue(), 3: asyncio.Queue()
        }
        # Coalesce by (base,quote): a symbol already queued is not re-queued. Without
        # this, high-frequency majors (BTC/ETH/SOL) flooded the priority-1 queue with
        # tens of thousands of duplicate events, starving priorities 2/3 (every
        # mid-cap) so they were never processed -> no candidates for profitable alts.
        self._pending: dict[tuple[str, str], float] = {}  # key -> monotonic enqueue time
        # Round-robin cursor so priority 1 cannot indefinitely precede 2/3.
        self._starts = 0
        self._event = asyncio.Event()
        self._enqueued_total = 0
        self._dequeued_total = 0

    def put(self, priority: int, item: tuple[str, str, str]) -> None:
        key = (item[0], item[1])
        if key in self._pending:
            return  # already queued — coalesce (latest cache state read at process time)
        self._pending[key] = time.monotonic()
        self._enqueued_total += 1
        self._queues[priority].put_nowait(item)
        self._event.set()

    async def get(self) -> tuple[str, str, str]:
        while True:
            # Weighted-fair order: rotate the starting tier so 2/3 are not starved by
            # a never-empty priority-1 queue, while still favouring 1 most cycles.
            order = (1, 2, 3) if self._starts % 4 else (2, 3, 1)
            self._starts += 1
            for p in order:
                q = self._queues[p]
                if not q.empty():
                    item = q.get_nowait()
                    self._pending.pop((item[0], item[1]), None)
                    self._dequeued_total += 1
                    return item
            self._event.clear()
            await self._event.wait()

    def pending(self) -> int:
        """Number of distinct (base, quote) symbols currently buffered awaiting a
        detection pass. Coalesced: a symbol already queued is never re-queued, so this is
        bounded by the count of tracked symbols (NOT a notification/signal backlog) and a
        stable non-zero value is normal steady state, not a leak. Each pending key maps to
        exactly one queued item, so ``len(self._pending) == sum(qsize)`` by construction.
        """
        return sum(q.qsize() for q in self._queues.values())

    def depth_report(self, now: float | None = None) -> dict[str, object]:
        """Composition of the event queue for observability: per-priority-tier depth, the
        coalesced pending count, lifetime enqueued/dequeued counters, and the age of the
        oldest/mean currently-queued item. A growing oldest-age on tier 3 is the signature
        of tier starvation (the bug the weighted-fair `get()` order guards against).
        """
        now = time.monotonic() if now is None else now
        ages = [now - t for t in self._pending.values()]
        return {
            "pending": sum(q.qsize() for q in self._queues.values()),
            "by_priority": {p: q.qsize() for p, q in self._queues.items()},
            "enqueued_total": self._enqueued_total,
            "dequeued_total": self._dequeued_total,
            "oldest_age_sec": round(max(ages), 2) if ages else 0.0,
            "avg_age_sec": round(sum(ages) / len(ages), 2) if ages else 0.0,
        }


def profit_reference_for(priority: int) -> Decimal:
    """Per-tier normalization reference for profit/liquidity scaling (§9.4/§11.2)."""
    return {1: Decimal(500), 2: Decimal(200), 3: Decimal(50)}.get(priority, Decimal(100))
