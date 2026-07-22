"""Funding Rate Arbitrage detector (Scanner §7.4).

Long the lower-funding venue, short the higher — collects the annualized differential
on a delta-neutral position. Requires both venues Online, same underlying (alias-verified),
and enough lead time to next settlement to be actionable.
"""
from __future__ import annotations

import time
from decimal import Decimal

from src.domain.enums import ArbitrageType
from src.domain.market import FundingRate
from src.domain.signal import Candidate, FundingSnapshot, LegRef
from src.scanner.detectors.base import DetectionContext, Detector

_MIN_LEAD_SEC = 120  # §7.4 minimum lead time to settlement
# Minimum annualized differential is the config-tunable ``funding_min_annualized_spread``
# (on DetectionContext). A delta-neutral carry only clears entry+exit fees once the
# annualized differential is meaningful; below it the candidate is provably unprofitable,
# so we don't emit it — noise suppression that stops BTC/ETH/SOL funding from flooding the
# generator with doomed candidates, not a profit-filter change.


class FundingDetector(Detector):
    arb_type = ArbitrageType.FUNDING.value

    def detect(self, ctx: DetectionContext, base_asset: str, quote_asset: str) -> list[Candidate]:
        self.counters.opportunities_checked += 1
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

        low_annualized = low.annualized()
        high_annualized = high.annualized()
        annualized_spread = high_annualized - low_annualized
        # Economically-derived floor (§ Phase 3): the carry over the holding horizon must
        # clear the round-trip taker fee AND the minimum net profit — the assembler's exact
        # funding publish condition, solved for the annualized rate. Replaces the fixed
        # noise threshold; drops only carries the assembler would reject anyway. Falls back
        # to funding_min_annualized_spread as an absolute floor.
        breakeven = ctx.funding_breakeven_annualized(low.venue, high.venue)
        if annualized_spread < breakeven and not ctx.is_active_route(
                ArbitrageType.FUNDING.value, base_asset, quote_asset,
                low.venue, high.venue, None):
            self.counters.rejected_economic += 1
            return []

        # Leg prices are a required Decimal on LegRef, but a funding carry has no spot
        # price — Decimal(1) is a sentinel that must never reach a user surface or be read
        # as a price (the formatter renders the funding rates below instead). The per-leg
        # annualized rates are retained ×100 (PERCENT) purely for presentation.
        return [Candidate(
            arb_type=ArbitrageType.FUNDING, base_asset=base_asset, quote_asset=quote_asset,
            buy_leg=LegRef(low.venue, "CEX", Decimal(1)),   # long low-funding (sentinel price)
            sell_leg=LegRef(high.venue, "CEX", Decimal(1)),  # short high-funding (sentinel price)
            gross_spread_pct=annualized_spread * Decimal(100),
            funding_annualized_spread=annualized_spread,
            funding_buy_annualized=low_annualized * Decimal(100),
            funding_sell_annualized=high_annualized * Decimal(100),
            funding_next_time=min(low.next_funding_time, high.next_funding_time),
            funding_low=self._snapshot(ctx, low),
            funding_high=self._snapshot(ctx, high),
        )]

    @staticmethod
    def _snapshot(ctx: DetectionContext, fr: FundingRate) -> FundingSnapshot:
        """Freeze one leg's funding data at detection time for the confidence model."""
        return FundingSnapshot(
            received_at=fr.received_at,
            current_rate=fr.current_rate,
            has_predicted=fr.predicted_rate is not None,
            has_next_time=bool(fr.next_funding_time),
            history=tuple(ctx.cache.funding_window(fr.venue, fr.base_asset)),
        )
