"""Pure math helpers for the scanner: robust statistics + market microstructure.

No I/O, no state — fully unit-testable. Implements robust median/MAD (§4.6),
VWAP-against-book slippage (§8.7 CEX), and constant-product AMM output (§5.4 DEX).
"""
from __future__ import annotations

from decimal import Decimal
from statistics import median

from src.domain.market import BookLevel


def robust_median(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    return Decimal(str(median(values)))


def mad(values: list[Decimal], med: Decimal | None = None) -> Decimal:
    """Median Absolute Deviation (§4.6) — robust dispersion measure."""
    if not values:
        return Decimal(0)
    m = med if med is not None else robust_median(values)
    deviations = [abs(v - m) for v in values]
    return robust_median(deviations)


def is_mad_outlier(value: Decimal, window: list[Decimal], k: Decimal) -> bool:
    """True if `value` deviates more than k*MAD from the window median (§4.6)."""
    if len(window) < 3:
        return False  # not enough history to judge — accept (bootstrap)
    med = robust_median(window)
    dispersion = mad(window, med)
    if dispersion == 0:
        # Degenerate: all identical. Flag only if value differs at all.
        return value != med
    return abs(value - med) > k * dispersion


def pct_deviation(value: Decimal, reference: Decimal) -> Decimal:
    if reference == 0:
        return Decimal(0)
    return abs(value - reference) / reference * Decimal(100)


def vwap_fill_price(levels: list[BookLevel], size_base: Decimal) -> Decimal | None:
    """Volume-weighted average fill price walking the book for `size_base` (§8.7).

    Returns None if the book cannot fill the requested size.
    """
    if size_base <= 0 or not levels:
        return None
    remaining = size_base
    notional = Decimal(0)
    for level in levels:
        take = min(remaining, level.qty)
        notional += take * level.price
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        return None  # insufficient depth
    return notional / size_base


def slippage_pct(vwap: Decimal, best_price: Decimal) -> Decimal:
    """Realized slippage relative to best price (§8.7)."""
    if best_price == 0:
        return Decimal(0)
    return abs(vwap - best_price) / best_price * Decimal(100)


def max_fillable_within_slippage(
    levels: list[BookLevel], best_price: Decimal, max_slippage_pct: Decimal
) -> Decimal:
    """Largest base size fillable before cumulative slippage exceeds tolerance (§9.2)."""
    if not levels or best_price == 0:
        return Decimal(0)
    cum_base = Decimal(0)
    cum_notional = Decimal(0)
    for level in levels:
        trial_base = cum_base + level.qty
        trial_notional = cum_notional + level.qty * level.price
        vwap = trial_notional / trial_base if trial_base else Decimal(0)
        if slippage_pct(vwap, best_price) > max_slippage_pct:
            # Binary-search within this level for the boundary size.
            lo, hi = cum_base, trial_base
            for _ in range(40):
                mid = (lo + hi) / 2
                add = mid - cum_base
                v = (cum_notional + add * level.price) / mid if mid else Decimal(0)
                if slippage_pct(v, best_price) > max_slippage_pct:
                    hi = mid
                else:
                    lo = mid
            return lo
        cum_base, cum_notional = trial_base, trial_notional
    return cum_base


def amm_output(reserve_in: Decimal, reserve_out: Decimal, amount_in: Decimal,
               fee_rate: Decimal) -> Decimal:
    """Constant-product (x*y=k) output for a swap of `amount_in` (§5.4, V2-style).

    Δy = y - k/(x + Δx·(1-fee)). Returns amount_out.
    """
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return Decimal(0)
    amount_in_after_fee = amount_in * (Decimal(1) - fee_rate)
    k = reserve_in * reserve_out
    new_reserve_in = reserve_in + amount_in_after_fee
    new_reserve_out = k / new_reserve_in
    return reserve_out - new_reserve_out


def amm_effective_price(reserve_in: Decimal, reserve_out: Decimal, amount_in: Decimal,
                        fee_rate: Decimal) -> Decimal | None:
    """Size-aware execution price (in units of in-asset per out-asset) for a DEX swap."""
    out = amm_output(reserve_in, reserve_out, amount_in, fee_rate)
    if out <= 0:
        return None
    return amount_in / out


def amm_max_size_within_slippage(
    reserve_in: Decimal, reserve_out: Decimal, fee_rate: Decimal,
    max_slippage_pct: Decimal,
) -> Decimal:
    """Largest input size before price impact exceeds tolerance on an AMM pool (§9.2)."""
    if reserve_in <= 0 or reserve_out <= 0:
        return Decimal(0)
    spot = reserve_in / reserve_out  # in per out at size 0+
    lo, hi = Decimal(0), reserve_in  # cannot input more than the pool holds
    for _ in range(60):
        mid = (lo + hi) / 2
        eff = amm_effective_price(reserve_in, reserve_out, mid, fee_rate)
        if eff is None:
            hi = mid
            continue
        impact = slippage_pct(eff, spot)
        if impact > max_slippage_pct:
            hi = mid
        else:
            lo = mid
    return lo


def normalize_scale(value: Decimal, reference: Decimal) -> Decimal:
    """Map a raw metric to 0..100 against a per-asset-tier reference scale (§9.4)."""
    if reference <= 0:
        return Decimal(0)
    ratio = value / reference * Decimal(100)
    return max(Decimal(0), min(Decimal(100), ratio))
