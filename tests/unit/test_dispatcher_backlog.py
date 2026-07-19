"""Bridge backlog bounds — hard cap + staleness TTL (issue #11).

An arbitrage alert is only actionable while the spread is live, so the outbound queue is
not a work buffer to be drained eventually: it is bounded memory holding perishable
items. These lock in that (a) the queue never grows past MAX_PENDING, (b) the entry
evicted at the cap is the OLDEST, and (c) an entry that waited past MAX_QUEUE_AGE_SEC is
discarded on dequeue instead of delivered.
"""
from __future__ import annotations

import time

import pytest

from src.domain.signal import Signal
from src.services import engine_bridge
from src.services.engine_bridge import MAX_PENDING, MAX_QUEUE_AGE_SEC, EngineBridge
from src.services.signal_registry import SignalRegistry


def _bridge() -> EngineBridge:
    return EngineBridge(SignalRegistry(), database=None, history_repo_factory=None)


def _sig(coin: str) -> Signal:
    return Signal(coin=coin, trading_pair=f"{coin}/USDT",
                  buy_exchange="binance", sell_exchange="okx")


async def test_queue_is_capped_and_evicts_oldest():
    bridge = _bridge()
    for i in range(MAX_PENDING + 50):
        await bridge.publish(_sig(f"C{i}"), "new")

    assert bridge.pending() == MAX_PENDING          # bounded, no unbounded growth
    assert bridge.drop_stats()["dropped_full"] == 50

    # Drop-oldest: the first 50 are gone, the newest survived.
    event, signal = await bridge.next_event()
    assert signal.coin == "C50"


async def test_stale_entries_are_dropped_on_dequeue(monkeypatch):
    bridge = _bridge()
    await bridge.publish(_sig("STALE"), "new")

    # Advance the clock past the TTL without sleeping.
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        engine_bridge.time, "monotonic",
        lambda: real_monotonic() + MAX_QUEUE_AGE_SEC + 1)

    await bridge.publish(_sig("FRESH"), "new")      # enqueued at the shifted "now"
    event, signal = await bridge.next_event()

    assert signal.coin == "FRESH"                   # STALE never dispatched
    assert bridge.drop_stats()["dropped_stale"] == 1


async def test_fresh_entry_survives():
    bridge = _bridge()
    await bridge.publish(_sig("BTC"), "new")
    event, signal = await bridge.next_event()
    assert (event, signal.coin) == ("new", "BTC")
    assert bridge.drop_stats() == {"dropped_full": 0, "dropped_stale": 0}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
