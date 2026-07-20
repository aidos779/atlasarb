"""CEX ↔ DEX detector (Scanner §7.2).

Evaluates both directions (buy-CEX-sell-DEX and buy-DEX-sell-CEX). DEX-side price is
size-aware via the pool curve (handled downstream by the profit engine); here we use
the pool spot for direction selection and gross-spread candidate emission. Token must
pass verified-token-list check (§3.4).
"""
from __future__ import annotations

from decimal import Decimal

from src.domain.enums import ArbitrageType
from src.domain.signal import Candidate, LegRef
from src.scanner.detectors.base import DetectionContext, Detector


class CexDexDetector(Detector):
    arb_type = ArbitrageType.CEX_DEX.value

    def detect(self, ctx: DetectionContext, base_asset: str, quote_asset: str) -> list[Candidate]:
        self.counters.opportunities_checked += 1
        if base_asset not in ctx.verified_tokens:
            return []
        pair = f"{base_asset}/{quote_asset}"
        cex = ctx.online_cex_prices(pair)
        dex = ctx.online_dex_books(pair)
        if not cex or not dex:
            return []

        # Pre-compute each DEX venue's spot + network once (independent of the CEX leg),
        # instead of recomputing them inside the O(n·m) inner loop.
        dex_priced: list[tuple[str, str | None, Decimal, object]] = []
        for dex_venue, dex_book in dex:  # type: ignore[assignment]
            info = ctx.venues.get(dex_venue)
            network = info.network if info else None
            dex_spot = (dex_book.reserve_quote / dex_book.reserve_base
                        if dex_book.reserve_base else Decimal(0))
            if dex_spot > 0:
                dex_priced.append((dex_venue, network, dex_spot, dex_book))

        candidates: list[Candidate] = []
        for cex_venue, cex_quote in cex:  # type: ignore[assignment]
            for dex_venue, network, dex_spot, dex_book in dex_priced:
                # Direction A: buy CEX (ask), sell DEX (spot)
                gross_a = self._gross_spread_pct(cex_quote.ask, dex_spot)
                # Direction B: buy DEX (spot), sell CEX (bid)
                gross_b = self._gross_spread_pct(dex_spot, cex_quote.bid)

                if gross_a >= gross_b and gross_a > 0:
                    candidates.append(Candidate(
                        arb_type=ArbitrageType.CEX_DEX, base_asset=base_asset,
                        quote_asset=quote_asset, network=network,
                        buy_leg=LegRef(cex_venue, "CEX", cex_quote.ask),
                        sell_leg=LegRef(dex_venue, "DEX", dex_spot, network=network,
                                        pool_address=dex_book.pool_address),
                        gross_spread_pct=gross_a,
                    ))
                elif gross_b <= 0:
                    # Neither direction shows a positive gross spread on this venue pair.
                    self.counters.rejected_by_spread += 1
                else:
                    candidates.append(Candidate(
                        arb_type=ArbitrageType.CEX_DEX, base_asset=base_asset,
                        quote_asset=quote_asset, network=network,
                        buy_leg=LegRef(dex_venue, "DEX", dex_spot, network=network,
                                       pool_address=dex_book.pool_address),
                        sell_leg=LegRef(cex_venue, "CEX", cex_quote.bid),
                        gross_spread_pct=gross_b,
                    ))
        return candidates
