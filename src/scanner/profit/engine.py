"""Shared profit engine (Scanner §8) — invoked by all five detectors.

Computes net profit through the full fee pipeline (§8.1) and runs the sizing
optimization (§8.11) plus max-capital-utilization fields (§8.12). Consistency of
this single engine is what makes cross-type ranking (§11) meaningful.
"""
from __future__ import annotations

from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.signal import ProfitBreakdown, SizingProfile
from src.scanner.profit.liquidity_leg import LiquidityLeg
from src.scanner.profit.models import FeeInputs

_SWEEP = (Decimal("0.25"), Decimal("0.50"), Decimal("0.75"), Decimal("1.0"))


class ProfitEngine:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def compute_at_size(
        self, size_usd: Decimal, buy_leg: LiquidityLeg, sell_leg: LiquidityLeg,
        fees: FeeInputs,
    ) -> ProfitBreakdown | None:
        """Full §8.1 pipeline at a fixed size. Returns None if unfillable."""
        buy_best = buy_leg.best_price()
        sell_best = sell_leg.best_price()
        if not buy_best or not sell_best or buy_best <= 0 or size_usd <= 0:
            return None

        base_size = size_usd / buy_best
        buy_fill = buy_leg.fill_price(size_usd)
        sell_fill = sell_leg.fill_price(size_usd)
        if buy_fill is None or sell_fill is None:
            return None  # insufficient depth for this size

        # Gross uses best (top-of-book) prices; slippage captured separately (§8.7).
        gross = base_size * (sell_best - buy_best)

        # Slippage cost per leg (§8.7) — buy fills worse (higher), sell fills worse (lower).
        buy_slip = (buy_fill - buy_best) * base_size
        sell_slip = (sell_best - sell_fill) * base_size
        slippage_cost = max(Decimal(0), buy_slip) + max(Decimal(0), sell_slip)

        buy_fee = fees.buy_fee_rate or Decimal(0)
        sell_fee = fees.sell_fee_rate or Decimal(0)
        trading_fees = base_size * buy_best * buy_fee + base_size * sell_best * sell_fee

        withdrawal = fees.withdrawal_fee_usd or Decimal(0)
        gas = fees.gas_fee_usd or Decimal(0)
        bridge = fees.bridge_fee_usd or Decimal(0)
        conversion = size_usd * fees.conversion_cost_bps / Decimal(10000)

        net = gross - trading_fees - withdrawal - gas - bridge - slippage_cost - conversion
        capital = size_usd  # buy-side notional (+margin handled by funding detector)
        roi = (net / capital * Decimal(100)) if capital else Decimal(0)
        gross_spread_pct = (((sell_best - buy_best) / buy_best * Decimal(100))
                            if buy_best else Decimal(0))
        net_pct = (net / capital * Decimal(100)) if capital else Decimal(0)

        return ProfitBreakdown(
            size_usd=size_usd,
            gross_profit_usd=gross,
            trading_fees_usd=trading_fees,
            withdrawal_fees_usd=withdrawal,
            gas_fees_usd=gas,
            bridge_fees_usd=bridge,
            slippage_cost_usd=slippage_cost,
            conversion_cost_usd=conversion,
            net_profit_usd=net,
            roi_pct=roi,
            gross_spread_pct=gross_spread_pct,
            net_profit_pct=net_pct,
            resolved_categories=fees.resolved_categories(),
        )

    def optimize(
        self, buy_leg: LiquidityLeg, sell_leg: LiquidityLeg, fees: FeeInputs,
        venue_type_buy: str, venue_type_sell: str,
    ) -> tuple[ProfitBreakdown, SizingProfile] | None:
        """§8.11 recommended-size optimization + §8.12 max-utilization fields.

        Returns (breakdown_at_recommended_size, sizing_profile) or None if no
        fillable size exists.
        """
        max_slip_buy = Decimal(str(self._config.max_slippage_pct(venue_type_buy)))
        max_slip_sell = Decimal(str(self._config.max_slippage_pct(venue_type_sell)))
        # Saturation = thinner leg's max within tolerance (§8.12 step 1 / §9.2).
        saturation_usd = min(
            buy_leg.max_size_usd(max_slip_buy),
            sell_leg.max_size_usd(max_slip_sell),
        )
        if saturation_usd <= 0:
            return None

        # Sweep 25/50/75/100% of saturation (§8.11 / §8.12 step 2).
        min_roi = Decimal(str(self._config.min_roi_pct))
        min_profit = Decimal(str(self._config.min_net_profit_usd))
        decay: list[tuple[Decimal, Decimal]] = []
        breakdowns: list[ProfitBreakdown] = []
        for frac in _SWEEP:
            size = saturation_usd * frac
            bd = self.compute_at_size(size, buy_leg, sell_leg, fees)
            if bd is None:
                decay.append((frac * Decimal(100), Decimal(0)))
                continue
            breakdowns.append(bd)
            decay.append((frac * Decimal(100), bd.roi_pct))

        if not breakdowns:
            return None

        # §8.11: profit-maximizing size subject to ROI >= min constraint.
        eligible = [b for b in breakdowns if b.roi_pct >= min_roi]
        pool = eligible or breakdowns
        recommended = max(pool, key=lambda b: b.net_profit_usd)

        # §8.12: Max Recommended Position = largest size still >= min net profit
        # on the decreasing side of the curve.
        profitable = [b for b in breakdowns if b.net_profit_usd >= min_profit]
        max_recommended = (
            max(profitable, key=lambda b: b.size_usd).size_usd if profitable
            else recommended.size_usd
        )

        sizing = SizingProfile(
            recommended_size_usd=recommended.size_usd,
            max_capital_utilization_usd=saturation_usd,
            max_recommended_position_usd=max_recommended,
            liquidity_saturation_usd=saturation_usd,
            profit_decay=decay,
        )
        return recommended, sizing
