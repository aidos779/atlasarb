"""Confidence Score (Scanner §11.5 / PRD §3.9) — 0–100 composite.

Six normalized factors; freshness and liquidity weighted highest since stale/illiquid
data is the most common false-positive root cause. Feeds both the §10 min-confidence
gate and the §11.2 ranking composite. Informational otherwise (R-SCHEMA-2).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.config.scanner_config import ScannerConfig


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
