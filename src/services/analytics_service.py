"""Analytics service (PRD §8.2 Analytics, §16.5 admin) — today's summary + engine stats.

MVP Analytics is intentionally limited to today's summary (Future Features defers the
full analytics platform). Draws from the live registry and engine metrics.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.scanner.engine import ScanningEngine
from src.services.signal_registry import SignalRegistry


@dataclass
class TodaySummary:
    total_active: int
    total_today: int
    top_coins: list[tuple[str, int]]
    top_exchanges: list[tuple[str, int]]
    avg_profit_pct: float


class AnalyticsService:
    def __init__(self, registry: SignalRegistry, engine: ScanningEngine) -> None:
        self._registry = registry
        self._engine = engine

    def today(self) -> TodaySummary:
        active = self._registry.all_active()
        coin_counts: dict[str, int] = {}
        exch_counts: dict[str, int] = {}
        total_profit = 0.0
        for s in active:
            coin_counts[s.coin] = coin_counts.get(s.coin, 0) + 1
            for venue in (s.buy_exchange, s.sell_exchange):
                exch_counts[venue] = exch_counts.get(venue, 0) + 1
            total_profit += float(s.net_profit_pct)
        avg = round(total_profit / len(active), 3) if active else 0.0
        metrics = self._engine.metrics.snapshot()
        return TodaySummary(
            total_active=len(active),
            total_today=metrics["signals_created"],
            top_coins=sorted(coin_counts.items(), key=lambda kv: kv[1], reverse=True)[:5],
            top_exchanges=sorted(exch_counts.items(), key=lambda kv: kv[1], reverse=True)[:5],
            avg_profit_pct=avg,
        )

    def engine_metrics(self) -> dict:
        return self._engine.metrics.snapshot()

    def exchange_status(self) -> dict:
        return {v: s.value for v, s in self._engine.status_table().items()}
