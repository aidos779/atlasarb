"""Funding Rate Arbitrage detector (Scanner §7.4).

Long the lower-funding venue, short the higher — collects the annualized differential
on a delta-neutral position. Requires both venues Online, same underlying (alias-verified),
and enough lead time to next settlement to be actionable.
"""
from __future__ import annotations

import time
from decimal import Decimal

from src.domain.enums import ArbitrageType
from src.domain.signal import Candidate, LegRef
from src.scanner.detectors.base import DetectionContext, Detector

_MIN_LEAD_SEC = 120  # §7.4 minimum lead time to settlement
# A delta-neutral carry only clears entry+exit fees once the annualized differential
# is meaningful (breakeven ~10% at default size/hold). Below this the candidate is
# provably unprofitable, so we don't emit it — this is noise suppression, not a
# profit-filter change (nothing that could publish is excluded). It stops BTC/ETH/SOL
# funding from flooding the generator with tens of thousands of doomed candidates.
_MIN_ANNUALIZED_SPREAD = Decimal("0.05")


class FundingDetector(Detector):
    arb_type = ArbitrageType.FUNDING.value

    def detect(self, ctx: DetectionContext, base_asset: str, quote_asset: str) -> list[Candidate]:
        rates = [
            f for f in ctx.cache.all_funding_for(base_asset)
            if ctx.health.is_online(f.venue)
        ]
        if len(rates) < 2:
            return []

        low = min(rates, key=lambda f: f.current_rate)   # go long here
        high = max(rates, key=lambda f: f.current_rate)  # go short here
        if low.venue == high.venue:
            return []

        now = time.time()
        lead = min(low.next_funding_time, high.next_funding_time) - now
        if lead < _MIN_LEAD_SEC:
            return []  # not actionable before settlement

        annualized_spread = high.annualized() - low.annualized()
        if annualized_spread < _MIN_ANNUALIZED_SPREAD:
            return []

        # Reference "price" for legs = current funding rate (informational).
        return [Candidate(
            arb_type=ArbitrageType.FUNDING, base_asset=base_asset, quote_asset=quote_asset,
            buy_leg=LegRef(low.venue, "CEX", Decimal(1)),   # long low-funding
            sell_leg=LegRef(high.venue, "CEX", Decimal(1)),  # short high-funding
            gross_spread_pct=annualized_spread * Decimal(100),
            funding_annualized_spread=annualized_spread,
            funding_next_time=min(low.next_funding_time, high.next_funding_time),
        )]
