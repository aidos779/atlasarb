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
    # ── Phase 1.5 cumulative timing (§17 CPU attribution) ──────────────────────────────
    # Plain float/int accumulators + in-place defaultdict updates: every hot-path record_*
    # below is allocation-free. Dict materialization happens only in timing_snapshot(),
    # called once per engine_stats emission (not in the detection loop).
    # Detection loop time split by call path (event worker vs 1s reconciliation full scan).
    event_detection_ms_total: float = 0.0
    reconciliation_detection_ms_total: float = 0.0
    event_detection_ticks: int = 0
    reconciliation_detection_ticks: int = 0
    # Per-detector cumulative detect() time + call count (attributes detection CPU by type).
    detector_ms_total: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    detector_calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Assembler time split: cheap pre-gate exits vs full-pipeline entries (+ overall total).
    generation_ms_total: float = 0.0
    assemble_pregate_ms_total: float = 0.0
    assemble_pregate_count: int = 0
    assemble_full_ms_total: float = 0.0
    assemble_full_count: int = 0
    # Whole-pass duration of the reconciliation full scan.
    reconciliation_pass_ms_total: float = 0.0
    reconciliation_pass_count: int = 0
    # Reconciliation working-set gauges (Phase 4, last pass) — how much the smart scan
    # skipped vs the old full pair×detector matrix.
    reconciliation_working_set: int = 0
    reconciliation_skipped_pairs: int = 0
    reconciliation_skipped_detector_execs: int = 0
    reconciliation_ran_detector_execs: int = 0
    reconciliation_cadence: dict[str, float] = field(default_factory=dict)
    cache_size: int = 0
    outliers: int = 0
    rejected_prices: int = 0
    missed_updates: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    api_failures: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    started_at: float = field(default_factory=time.time)
    # Per venue-pair funnel (§17): "a↔b" -> checked/candidates/rejected_*/signals.
    venue_pair_stats: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    # Hot-path memos for the venue-pair funnel (see _pair_key / record_pair_checked).
    # venue_pair_stats entries are stable defaultdict objects that are only ever read
    # (venue_pair_snapshot copies, never clears), so caching references into them is safe.
    _pair_key_cache: dict[tuple[str, str], str] = field(default_factory=dict)
    _pair_checked_cache: dict[tuple[str, ...], list[dict[str, int]]] = field(
        default_factory=dict)

    def record_detection(self, duration_ms: float, from_reconciliation: bool = False) -> None:
        self.detection_durations_ms.append(duration_ms)
        if from_reconciliation:
            self.reconciliation_detection_ms_total += duration_ms
            self.reconciliation_detection_ticks += 1
        else:
            self.event_detection_ms_total += duration_ms
            self.event_detection_ticks += 1

    def record_detector_time(self, arb_type: str, duration_ms: float) -> None:
        self.detector_ms_total[arb_type] += duration_ms
        self.detector_calls[arb_type] += 1

    def record_generation(self, duration_ms: float, pregate: bool = False) -> None:
        self.gen_durations_ms.append(duration_ms)
        self.generation_ms_total += duration_ms
        if pregate:
            self.assemble_pregate_ms_total += duration_ms
            self.assemble_pregate_count += 1
        else:
            self.assemble_full_ms_total += duration_ms
            self.assemble_full_count += 1

    def record_reconciliation_pass(self, duration_ms: float) -> None:
        self.reconciliation_pass_ms_total += duration_ms
        self.reconciliation_pass_count += 1

    def record_reconciliation_scan(
        self, working_set: int, skipped_pairs: int, ran_detectors: int,
        skipped_detectors: int, cadence: dict[str, float],
    ) -> None:
        """Last-pass working-set gauges for the smart reconciliation scan (Phase 4)."""
        self.reconciliation_working_set = working_set
        self.reconciliation_skipped_pairs = skipped_pairs
        self.reconciliation_ran_detector_execs = ran_detectors
        self.reconciliation_skipped_detector_execs = skipped_detectors
        self.reconciliation_cadence = cadence

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

    @classmethod
    def reject_bucket(cls, reason: str) -> str:
        """Coarse bucket for a RejectReason. Unknown reasons count as risk."""
        return cls._REJECT_BUCKET.get(reason, "risk")

    def rejections_by_bucket(self) -> dict[str, int]:
        buckets = {"fees": 0, "spread": 0, "liquidity": 0, "freshness": 0, "risk": 0}
        for reason, count in self.rejections.items():
            buckets[self._REJECT_BUCKET.get(reason, "risk")] += count
        return buckets

    # ── per venue-pair funnel ──
    def _pair_key(self, venue_a: str, venue_b: str) -> str:
        """Order-independent "a↔b" key, memoized.

        The venue set is a small fixed roster, so the sorted() + str.join() this used to
        run per call produced the same handful of strings over and over — once per
        venue-combination per symbol, i.e. O(venues²) allocations on every detection
        tick. Look the key up instead and build it only the first time a pair is seen.
        """
        memo = self._pair_key_cache
        key = (venue_a, venue_b)
        cached = memo.get(key)
        if cached is None:
            cached = "↔".join(sorted(key))
            memo[key] = cached
        return cached

    def record_pair_checked(self, venues: list[str]) -> None:
        """One detection pass looked at every 2-combination of these online venues."""
        # The online-venue list is near-identical across symbols, so memoize the whole
        # expanded combination list per roster: the O(venues²) pair walk then runs once
        # per distinct roster rather than once per symbol.
        roster = tuple(venues)
        counters = self._pair_checked_cache.get(roster)
        if counters is None:
            stats = self.venue_pair_stats
            counters = [stats[self._pair_key(venues[i], venues[j])]
                        for i in range(len(venues))
                        for j in range(i + 1, len(venues))]
            self._pair_checked_cache[roster] = counters
        for counter in counters:
            counter["checked"] += 1

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

    def timing_snapshot(self) -> dict:
        """Cumulative CPU-attribution timers (§17). Built once per emission — the hot-path
        record_* methods only touch scalar accumulators, so this materialization never runs
        in the detection loop."""
        return {
            "event_detection_ms_total": round(self.event_detection_ms_total, 1),
            "reconciliation_detection_ms_total": round(
                self.reconciliation_detection_ms_total, 1),
            "event_detection_ticks": self.event_detection_ticks,
            "reconciliation_detection_ticks": self.reconciliation_detection_ticks,
            "detector_ms_total": {k: round(v, 1) for k, v in self.detector_ms_total.items()},
            "detector_calls": dict(self.detector_calls),
            "generation_ms_total": round(self.generation_ms_total, 1),
            "assemble_pregate_ms_total": round(self.assemble_pregate_ms_total, 1),
            "assemble_pregate_count": self.assemble_pregate_count,
            "assemble_full_ms_total": round(self.assemble_full_ms_total, 1),
            "assemble_full_count": self.assemble_full_count,
            "reconciliation_pass_ms_total": round(self.reconciliation_pass_ms_total, 1),
            "reconciliation_pass_count": self.reconciliation_pass_count,
            "reconciliation_working_set": self.reconciliation_working_set,
            "reconciliation_skipped_pairs": self.reconciliation_skipped_pairs,
            "reconciliation_ran_detector_execs": self.reconciliation_ran_detector_execs,
            "reconciliation_skipped_detector_execs": self.reconciliation_skipped_detector_execs,
            "reconciliation_cadence": dict(self.reconciliation_cadence),
        }

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
            "timing": self.timing_snapshot(),
        }
