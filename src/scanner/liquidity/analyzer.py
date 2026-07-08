"""Liquidity Analysis (Scanner §9): floor gate, max trade size, 0–100 liquidity score.

The score (§9.4) combines depth (dominant), book balance, and recent stability, each
normalized against a per-asset-tier reference scale. Below the configurable floor it is
an automatic §10 rejection regardless of profit.
"""
from __future__ import annotations

from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.scanner import mathx
from src.scanner.profit.liquidity_leg import LiquidityLeg


class LiquidityAnalyzer:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def liquidity_usd(self, buy_leg: LiquidityLeg, sell_leg: LiquidityLeg,
                      buy_type: str, sell_type: str) -> Decimal:
        """Executable liquidity = thinner leg within its tolerance (§9.1/§9.2)."""
        buy_liq = buy_leg.liquidity_usd(Decimal(str(self._config.max_slippage_pct(buy_type))))
        sell_liq = sell_leg.liquidity_usd(Decimal(str(self._config.max_slippage_pct(sell_type))))
        return min(buy_liq, sell_liq)

    def meets_floor(self, liquidity: Decimal, venue_type: str) -> bool:
        return liquidity >= Decimal(str(self._config.min_liquidity_usd(venue_type)))

    def score(
        self, buy_leg: LiquidityLeg, sell_leg: LiquidityLeg, buy_type: str, sell_type: str,
        reference_usd: Decimal, stability_stddev_pct: Decimal = Decimal(0),
    ) -> Decimal:
        """§9.4 liquidity score 0–100."""
        cfg = self._config
        buy_liq = buy_leg.liquidity_usd(Decimal(str(cfg.max_slippage_pct(buy_type))))
        sell_liq = sell_leg.liquidity_usd(Decimal(str(cfg.max_slippage_pct(sell_type))))
        depth = min(buy_liq, sell_liq)

        depth_score = mathx.normalize_scale(depth, reference_usd)
        # Balance: 100 when perfectly symmetric, decays with asymmetry.
        total = buy_liq + sell_liq
        if total > 0:
            balance = (Decimal(1) - abs(buy_liq - sell_liq) / total) * Decimal(100)
        else:
            balance = Decimal(0)
        # Stability: lower recent stddev -> higher score.
        stability = max(Decimal(0), Decimal(100) - stability_stddev_pct * Decimal(2))

        return (
            Decimal(str(cfg.lw_depth)) * depth_score
            + Decimal(str(cfg.lw_balance)) * balance
            + Decimal(str(cfg.lw_stability)) * stability
        )

    def meets_score_floor(self, score: Decimal) -> bool:
        return score >= Decimal(str(self._config.liquidity_score_floor))
