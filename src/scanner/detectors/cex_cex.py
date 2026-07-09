"""CEX ↔ CEX detector (Scanner §7.1).

Computes best-buy (min ask) and best-sell (max bid) across ALL tracked CEX venues for
the pair each tick — O(n) primary selection, not naive O(n²) pairwise — then emits the
best combination. Both legs must be fresh + 🟢 Online (input gating).
"""
from __future__ import annotations

from decimal import Decimal

from src.config import LogThrottle, get_logger
from src.domain.enums import ArbitrageType
from src.domain.signal import Candidate, LegRef
from src.scanner.detectors.base import DetectionContext, Detector

log = get_logger("scanner.detector")

# A collision pair (same ticker, different token per venue) would emit an implausible
# spread every tick; throttle the diagnostic so it surfaces once per window, not per tick.
_implausible_log_throttle = LogThrottle(interval_sec=300.0)


class CexCexDetector(Detector):
    arb_type = ArbitrageType.CEX_CEX.value

    def detect(self, ctx: DetectionContext, base_asset: str, quote_asset: str) -> list[Candidate]:
        pair = f"{base_asset}/{quote_asset}"
        prices = ctx.online_cex_prices(pair)
        if len(prices) < 2:
            return []

        best_buy_venue: str | None = None
        best_ask = Decimal("Infinity")
        best_sell_venue: str | None = None
        best_bid = Decimal(0)
        # Single O(n) pass over the venues' quotes — no per-venue dict allocation.
        for venue, quote in prices:  # type: ignore[assignment]
            if quote.ask < best_ask:
                best_ask, best_buy_venue = quote.ask, venue
            if quote.bid > best_bid:
                best_bid, best_sell_venue = quote.bid, venue

        if not best_buy_venue or not best_sell_venue or best_buy_venue == best_sell_venue:
            return []

        gross = self._gross_spread_pct(best_ask, best_bid)
        if gross <= 0:
            return []

        # Ticker-collision / bad-tick guard: a double-digit CEX↔CEX spot spread on the
        # same symbol is virtually always two DIFFERENT tokens sharing a ticker across
        # venues (or a stale tick), not a real arb. Drop it before the profit pipeline
        # so a phantom "TOP" signal never reaches users.
        ceiling = ctx.max_plausible_cex_spread_pct
        if ceiling > 0 and gross > ceiling:
            emit, suppressed = _implausible_log_throttle.allow((pair, best_buy_venue,
                                                                best_sell_venue))
            if emit:
                log.warning("cex_cex_spread_implausible", pair=pair,
                            buy=best_buy_venue, sell=best_sell_venue,
                            gross_pct=float(round(gross, 2)),
                            ceiling_pct=float(ceiling), repeats_suppressed=suppressed)
            return []

        buy_leg = LegRef(venue=best_buy_venue, venue_type="CEX", price=best_ask)
        sell_leg = LegRef(venue=best_sell_venue, venue_type="CEX", price=best_bid)
        return [Candidate(
            arb_type=ArbitrageType.CEX_CEX,
            base_asset=base_asset,
            quote_asset=quote_asset,
            buy_leg=buy_leg,
            sell_leg=sell_leg,
            gross_spread_pct=gross,
        )]
