"""Liquidity Analysis (Scanner §9): floor gate, max trade size, 0–100 liquidity score.

The score (§9.4) combines depth (dominant, normalized against the per-tier
liquidity_reference_usd scale), book balance, and recent price stability (robust
dispersion supplied by the caller; weight redistributed when no history exists).
Below the configurable floor it is an automatic §10 rejection regardless of profit.
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
        reference_usd: Decimal, stability_dispersion_pct: Decimal | None = None,
    ) -> Decimal:
        """§9.4 liquidity score 0–100.

        ``reference_usd`` is the depth-saturation scale (ScannerConfig
        .liquidity_reference_usd — NOT the §11.2 profit reference).
        ``stability_dispersion_pct`` is the recent relative price dispersion (robust
        MAD/median, percent) of the worse leg; ``None`` means "no history yet", in
        which case the stability weight is redistributed over depth+balance instead of
        being awarded as a fictitious perfect 100.
        """
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

        w_depth = Decimal(str(cfg.lw_depth))
        w_balance = Decimal(str(cfg.lw_balance))
        w_stability = Decimal(str(cfg.lw_stability))
        if stability_dispersion_pct is None:
            # No stability evidence — renormalize the remaining weights so the score
            # stays on the 0..100 scale without inventing a stability value.
            known = w_depth + w_balance
            if known <= 0:
                return Decimal(0)
            return (w_depth * depth_score + w_balance * balance) / known

        # Stability: lower recent dispersion -> higher score.
        stability = max(Decimal(0), Decimal(100) - stability_dispersion_pct * Decimal(2))
        return w_depth * depth_score + w_balance * balance + w_stability * stability

    def meets_score_floor(self, score: Decimal) -> bool:
        return score >= Decimal(str(self._config.liquidity_score_floor))
