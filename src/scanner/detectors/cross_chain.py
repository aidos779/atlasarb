"""Cross-Chain Arbitrage detector (Scanner §7.5).

Same canonical asset priced on different networks, requiring a bridge to move between
legs. Only emits when a viable bridge route exists (never emit an unexecutable signal).
Bridge fee/time/capacity feed the profit engine and the cross-chain ranking penalty.
"""
from __future__ import annotations

from decimal import Decimal

from src.domain.enums import ArbitrageType
from src.domain.signal import Candidate, LegRef
from src.scanner.detectors.base import DetectionContext, Detector
from src.scanner.detectors.bridges import BridgeRegistry


class CrossChainDetector(Detector):
    arb_type = ArbitrageType.CROSS_CHAIN.value

    def __init__(self, bridges: BridgeRegistry) -> None:
        super().__init__()
        self._bridges = bridges

    def detect(self, ctx: DetectionContext, base_asset: str, quote_asset: str) -> list[Candidate]:
        self.counters.opportunities_checked += 1
        if base_asset not in ctx.verified_tokens:
            return []
        pair = f"{base_asset}/{quote_asset}"
        dex = ctx.online_dex_books(pair)
        if len(dex) < 2:
            return []

        # Group by network; cross-chain requires differing networks.
        priced: list[tuple[str, str, Decimal, object]] = []
        for venue, book in dex:  # type: ignore[assignment]
            info = ctx.venues.get(venue)
            network = info.network if info else None
            if not network or not book.reserve_base:
                continue
            spot = book.reserve_quote / book.reserve_base
            if spot > 0:
                priced.append((venue, network, spot, book))

        candidates: list[Candidate] = []
        for i in range(len(priced)):
            for j in range(len(priced)):
                if i == j:
                    continue
                bv, bn, bp, bb = priced[i]
                sv, sn, sp, sb = priced[j]
                if bn == sn:
                    continue  # same chain handled by DEX-DEX detector
                gross = self._gross_spread_pct(bp, sp)
                if gross <= 0:
                    self.counters.rejected_by_spread += 1
                    continue
                route = self._bridges.route(bn, sn, base_asset)
                if route is None:
                    # No bridge = unexecutable, same class as a validator drop (§7.5).
                    self.counters.rejected_by_validation += 1
                    continue
                candidates.append(Candidate(
                    arb_type=ArbitrageType.CROSS_CHAIN, base_asset=base_asset,
                    quote_asset=quote_asset,
                    buy_leg=LegRef(bv, "DEX", bp, network=bn, pool_address=bb.pool_address),
                    sell_leg=LegRef(sv, "DEX", sp, network=sn, pool_address=sb.pool_address),
                    gross_spread_pct=gross,
                    bridge_name=route.name,
                    bridge_fee_usd=route.flat_fee_usd,
                    bridge_time_sec=route.time_sec,
                ))
        return candidates
