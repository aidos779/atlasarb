"""Monitoring surface (Scanner §17). In-memory metric aggregation exposed to the
Admin Signal Monitoring view (PRD §16.6) and health snapshots.

This is the *what to expose*; alerting thresholds/paging are an ops concern (§17.6).
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class Metrics:
    signals_created: int = 0
    signals_updated: int = 0
    signals_expired: int = 0
    candidates_generated: int = 0
    snapshots_received: int = 0        # market-data cache writes
    opportunities_checked: int = 0     # (base,quote) symbols run through detectors
    notifications_sent: int = 0        # telegram alerts actually delivered
    rejections: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    expiry_reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    signals_by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    detection_durations_ms: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    gen_durations_ms: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    cache_size: int = 0
    outliers: int = 0
    rejected_prices: int = 0
    missed_updates: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    api_failures: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    started_at: float = field(default_factory=time.time)
    # Per venue-pair funnel (§17): "a↔b" -> checked/candidates/rejected_*/signals.
    venue_pair_stats: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int)))

    def record_detection(self, duration_ms: float) -> None:
        self.detection_durations_ms.append(duration_ms)

    def record_generation(self, duration_ms: float) -> None:
        self.gen_durations_ms.append(duration_ms)

    def record_candidate(self) -> None:
        self.candidates_generated += 1

    def record_snapshot(self) -> None:
        self.snapshots_received += 1

    def record_opportunity_checked(self) -> None:
        self.opportunities_checked += 1

    def record_notification_sent(self) -> None:
        self.notifications_sent += 1

    def record_reject(self, reason: str) -> None:
        self.rejections[reason] += 1

    # RejectReason -> coarse diagnostic bucket for the ENGINE STATS line.
    _REJECT_BUCKET = {
        "BELOW_MIN_PROFIT": "spread", "IMPLAUSIBLE_SPREAD": "spread",
        "UNPROFITABLE_AFTER_FEES": "fees", "MISSING_FEE_DATA": "fees",
        "GAS_EXCEEDS_LIMIT": "fees",
        "INSUFFICIENT_LIQUIDITY": "liquidity", "NO_VIABLE_SIZE": "liquidity",
        "STALE_DATA": "freshness",
        "RISK_TOO_HIGH": "risk", "UNVERIFIED_TOKEN": "risk", "NOT_WARMED_UP": "risk",
        "EXCHANGE_NOT_ONLINE": "risk", "LOW_CONFIDENCE": "risk",
        "NO_BRIDGE_ROUTE": "risk", "BRIDGE_TIME_EXCEEDED": "risk",
    }

    def rejections_by_bucket(self) -> dict[str, int]:
        buckets = {"fees": 0, "spread": 0, "liquidity": 0, "freshness": 0, "risk": 0}
        for reason, count in self.rejections.items():
            buckets[self._REJECT_BUCKET.get(reason, "risk")] += count
        return buckets

    # ── per venue-pair funnel ──
    @staticmethod
    def _pair_key(venue_a: str, venue_b: str) -> str:
        return "↔".join(sorted((venue_a, venue_b)))

    def record_pair_checked(self, venues: list[str]) -> None:
        """One detection pass looked at every 2-combination of these online venues."""
        for i in range(len(venues)):
            for j in range(i + 1, len(venues)):
                self.venue_pair_stats[self._pair_key(venues[i], venues[j])]["checked"] += 1

    def record_pair_candidate(self, buy_venue: str, sell_venue: str) -> None:
        self.venue_pair_stats[self._pair_key(buy_venue, sell_venue)]["candidates"] += 1

    def record_pair_reject(self, buy_venue: str, sell_venue: str, reason: str) -> None:
        bucket = self._REJECT_BUCKET.get(reason, "risk")
        self.venue_pair_stats[self._pair_key(buy_venue, sell_venue)][f"rejected_{bucket}"] += 1

    def record_pair_signal(self, buy_venue: str, sell_venue: str) -> None:
        self.venue_pair_stats[self._pair_key(buy_venue, sell_venue)]["signals"] += 1

    def venue_pair_snapshot(self) -> dict[str, dict[str, int]]:
        return {pair: dict(counters)
                for pair, counters in sorted(self.venue_pair_stats.items())}

    def record_signal(self, event: str, arb_type: str) -> None:
        if event == "new":
            self.signals_created += 1
            self.signals_by_type[arb_type] += 1
        elif event == "update":
            self.signals_updated += 1

    def record_expiry(self, reason: str) -> None:
        self.signals_expired += 1
        self.expiry_reasons[reason] += 1

    def record_api_failure(self, venue: str) -> None:
        self.api_failures[venue] += 1

    def record_missed_update(self, symbol: str) -> None:
        self.missed_updates[symbol] += 1

    @staticmethod
    def _p95(values: deque[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]

    def snapshot(self) -> dict:
        return {
            "uptime_sec": int(time.time() - self.started_at),
            "signals_created": self.signals_created,
            "signals_updated": self.signals_updated,
            "signals_expired": self.signals_expired,
            "candidates_generated": self.candidates_generated,
            "signals_by_type": dict(self.signals_by_type),
            "rejections": dict(self.rejections),
            "expiry_reasons": dict(self.expiry_reasons),
            "detection_p95_ms": round(self._p95(self.detection_durations_ms), 2),
            "generation_p95_ms": round(self._p95(self.gen_durations_ms), 2),
            "cache_size": self.cache_size,
            "outliers": self.outliers,
            "rejected_prices": self.rejected_prices,
            "api_failures": dict(self.api_failures),
            "venue_pairs": self.venue_pair_snapshot(),
        }
