"""Confidence Score (Scanner §11.5 / PRD §3.9) — 0–100 composite.

Six normalized factors; freshness and liquidity weighted highest since stale/illiquid
data is the most common false-positive root cause. Feeds both the §10 min-confidence
gate and the §11.2 ranking composite. Informational otherwise (R-SCHEMA-2).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.scanner import mathx


@dataclass
class ConfidenceInputs:
    price_freshness: Decimal        # 0..100 (1 - staleness/maxAge, min across legs)
    liquidity_score: Decimal        # 0..100 (reused from §9.4)
    spread_stability: Decimal       # 0..100 (persistence across recent ticks)
    exchange_health: Decimal        # 0..100 (rolling error/latency quality)
    historical_reliability: Decimal  # 0..100 (past hit-rate for type/venue-pair)
    data_completeness: Decimal      # 0..100 (fraction of resolved inputs)


class ConfidenceScorer:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def score(self, inp: ConfidenceInputs) -> int:
        cfg = self._config
        raw = (
            Decimal(str(cfg.f_price_freshness)) * inp.price_freshness
            + Decimal(str(cfg.f_liquidity)) * inp.liquidity_score
            + Decimal(str(cfg.f_spread_stability)) * inp.spread_stability
            + Decimal(str(cfg.f_exchange_health)) * inp.exchange_health
            + Decimal(str(cfg.f_historical_reliability)) * inp.historical_reliability
            + Decimal(str(cfg.f_data_completeness)) * inp.data_completeness
        )
        return int(max(Decimal(0), min(Decimal(100), raw)))


@dataclass
class FundingLegQuality:
    """Per-venue funding-data quality inputs (all live, no constants)."""

    age_sec: float                  # seconds since the funding sample was received
    max_age_sec: float              # freshness horizon (config)
    exchange_health: Decimal        # 0..100 rolling venue health
    history: list[Decimal]          # recent funding-rate samples (for volatility)
    has_predicted: bool             # predicted_rate present
    has_next_time: bool             # next_funding_time present/valid


# Funding confidence factor weights (sum = 1.0). Freshness and stability dominate
# because a stale or wildly-varying funding rate is the main false-carry root cause.
_FW_FRESHNESS = Decimal("0.30")
_FW_STABILITY = Decimal("0.25")
_FW_RELIABILITY = Decimal("0.25")
_FW_COMPLETENESS = Decimal("0.20")


def _freshness_score(age_sec: float, max_age_sec: float) -> Decimal:
    if max_age_sec <= 0:
        return Decimal(100)
    # Clamp age to [0, max_age] first: a tiny negative age from clock skew (received_at
    # momentarily ahead of now) must not push the factor above 100.
    age = min(max(age_sec, 0.0), max_age_sec)
    return (Decimal(1) - Decimal(str(age)) / Decimal(str(max_age_sec))) * Decimal(100)


def _stability_score(history: list[Decimal]) -> Decimal:
    """0..100 from the volatility of recent funding rates (robust MAD / |median|).

    Fewer than 3 samples = not enough history to trust → conservative 50 (missing-data
    penalty). A flat rate scores 100; a rate whose MAD is a large fraction of its level
    scores low."""
    if len(history) < 3:
        return Decimal(50)
    med = mathx.robust_median(history)
    dispersion = mathx.mad(history, med)
    scale = abs(med) if med != 0 else max((abs(v) for v in history), default=Decimal(0))
    if scale == 0:
        return Decimal(100)  # all-zero funding: perfectly stable
    rel = dispersion / scale
    return max(Decimal(0), Decimal(1) - min(rel, Decimal(1))) * Decimal(100)


def funding_confidence(low: FundingLegQuality, high: FundingLegQuality) -> tuple[int, dict]:
    """Confidence (0..100) for a funding-carry signal, derived entirely from live data
    quality — freshness, funding-rate stability, exchange reliability and data
    completeness of BOTH legs (the weaker leg drives each factor). Returns the score and
    a per-factor breakdown for logging/debugging."""
    freshness = min(_freshness_score(low.age_sec, low.max_age_sec),
                    _freshness_score(high.age_sec, high.max_age_sec))
    stability = min(_stability_score(low.history), _stability_score(high.history))
    reliability = min(low.exchange_health, high.exchange_health)

    def _completeness(q: FundingLegQuality) -> Decimal:
        present = 2 + int(q.has_predicted) + int(q.has_next_time)
        return Decimal(present) / Decimal(4) * Decimal(100)  # rate+interval always present

    completeness = min(_completeness(low), _completeness(high))

    raw = (_FW_FRESHNESS * freshness + _FW_STABILITY * stability
           + _FW_RELIABILITY * reliability + _FW_COMPLETENESS * completeness)
    score = int(max(Decimal(0), min(Decimal(100), raw)))
    breakdown = {
        "freshness": float(round(freshness, 1)),
        "stability": float(round(stability, 1)),
        "reliability": float(round(reliability, 1)),
        "completeness": float(round(completeness, 1)),
        "score": score,
    }
    return score, breakdown
