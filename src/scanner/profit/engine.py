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

        buy_fill = buy_leg.fill_price(size_usd)
        if buy_fill is None or buy_fill <= 0:
            return None  # insufficient depth for this size
        # Physical base quantity: spending size_usd at the *fill* price buys
        # size_usd / buy_fill — not size_usd / buy_best (which overstated the held
        # quantity by the buy leg's slippage, and with it gross and net). The sell leg
        # is then sized to that exact quantity: passing base_size × sell_best makes
        # both leg implementations (CexBookLeg divides by best; DexPoolLeg by spot)
        # recover base_size precisely, so the same coins bought are the coins sold.
        base_size = size_usd / buy_fill
        sell_notional = base_size * sell_best
        sell_fill = sell_leg.fill_price(sell_notional)
        if sell_fill is None:
            return None  # insufficient depth for this size

        # Gross uses best (top-of-book) prices on the physical quantity; slippage is
        # captured separately (§8.7). Identity (exact, no approximation):
        #   proceeds - cost = base·sell_fill - size_usd = gross - buy_slip - sell_slip.
        gross = base_size * (sell_best - buy_best)

        # Execution cost per leg (§8.7) — buy fills worse (higher), sell worse (lower).
        buy_slip = max(Decimal(0), (buy_fill - buy_best) * base_size)
        sell_slip = max(Decimal(0), (sell_best - sell_fill) * base_size)
        execution_cost = buy_slip + sell_slip

        # Fees charged on top of the fill price: a CEX taker fee settles outside the
        # book, so none of it is in the fill. A DEX leg's rate is 0 here (assembler) —
        # its pool fee is inside the fill and is recovered below. Charged on the actual
        # notionals: the buy fee on what was spent, the sell fee on what was received.
        buy_fee = fees.buy_fee_rate or Decimal(0)
        sell_fee = fees.sell_fee_rate or Decimal(0)
        rate_fees = size_usd * buy_fee + base_size * sell_fill * sell_fee

        withdrawal = fees.withdrawal_fee_usd or Decimal(0)
        gas = fees.gas_fee_usd or Decimal(0)
        bridge = fees.bridge_fee_usd or Decimal(0)
        conversion = size_usd * fees.conversion_cost_bps / Decimal(10000)

        # Net is deliberately computed from the two *aggregate* costs, in the exact
        # association this engine has always used. The reclassification below only
        # redistributes `execution_cost` between the two reported fields, and doing it
        # after this line means it cannot perturb net/ROI even in the last Decimal ulp
        # (Decimal carries 28 significant digits; re-associating the sum moves the final
        # digit, which is how a pure relabel could otherwise leak into the totals).
        net = gross - rate_fees - withdrawal - gas - bridge - execution_cost - conversion

        # ── reporting split (no effect on any total) ──
        # For an AMM leg, execution_cost is two different things added together: the
        # pool's swap fee, skimmed off the input before the curve, and genuine price
        # impact from our size. Reporting them as one number showed paying users
        # "Trading fees: $0.00" beside a slippage figure inflated by what is a fee.
        # Each leg's attribution is capped at the cost that leg actually incurred, so
        # the split can never manufacture or lose value: the two fields still sum to
        # rate_fees + execution_cost.
        pool_fees = (min(buy_leg.pool_fee_cost_usd(size_usd), buy_slip)
                     + min(sell_leg.pool_fee_cost_usd(sell_notional), sell_slip))
        trading_fees = rate_fees + pool_fees
        slippage_cost = execution_cost - pool_fees
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
