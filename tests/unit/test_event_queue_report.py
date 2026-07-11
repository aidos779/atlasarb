"""Event-queue semantics + composition report (audit item 7).

`queue_pending` is the coalesced market-event queue depth, not a notification backlog.
These lock in the invariant that it is bounded (each pending key ↔ one queued item) and
that the new `depth_report` exposes per-tier depth and item age for starvation diagnosis.
"""
from __future__ import annotations

import pytest

from src.scanner.priority.scheduler import PriorityEventQueue


def test_coalescing_keeps_one_item_per_symbol():
    q = PriorityEventQueue()
    q.put(1, ("BTC", "USDT", "binance"))
    q.put(1, ("BTC", "USDT", "okx"))     # same (base, quote) → coalesced
    q.put(2, ("ETH", "USDT", "binance"))
    rep = q.depth_report()
    assert rep["pending"] == 2                  # BTC + ETH, not 3
    assert rep["enqueued_total"] == 2           # the coalesced put did not count
    assert rep["by_priority"] == {1: 1, 2: 1, 3: 0}


async def test_pending_invariant_and_drain():
    q = PriorityEventQueue()
    for sym in ("BTC", "ETH", "SOL"):
        q.put(3, (sym, "USDT", "binance"))
    # Invariant: len(_pending) == sum(qsize) — no orphaned pending keys.
    assert len(q._pending) == q.pending() == 3
    got = [await q.get() for _ in range(3)]
    assert {g[0] for g in got} == {"BTC", "ETH", "SOL"}
    rep = q.depth_report()
    assert rep["pending"] == 0 and len(q._pending) == 0      # fully drained, no leak
    assert rep["dequeued_total"] == 3 and rep["enqueued_total"] == 3


async def test_dequeued_symbol_can_requeue():
    q = PriorityEventQueue()
    q.put(1, ("BTC", "USDT", "binance"))
    await q.get()
    # After processing, a fresh update for the same symbol enqueues again (not stuck).
    q.put(1, ("BTC", "USDT", "binance"))
    assert q.pending() == 1
    assert q.depth_report()["enqueued_total"] == 2


def test_age_fields_present():
    q = PriorityEventQueue()
    q.put(1, ("BTC", "USDT", "binance"))
    rep = q.depth_report()
    assert rep["oldest_age_sec"] >= 0.0 and rep["avg_age_sec"] >= 0.0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
