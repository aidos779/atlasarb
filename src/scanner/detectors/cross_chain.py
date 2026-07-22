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
        # Each unordered cross-network pool pair is examined once (j > i). Buy on the
        # cheaper spot, sell on the dearer — the single profitable direction. The previous
        # full directed loop (both i→j and j→i) evaluated every pair twice: the mirror leg
        # was always gross<=0 and counted a phantom rejected_by_spread, which is why the
        # prod telemetry showed cross_chain rejected_spread == 2x candidates. The emitted
        # candidate set is unchanged; only the duplicate work and the miscount are removed.
        for i in range(len(priced)):
            for j in range(i + 1, len(priced)):
                v1, n1, p1, b1 = priced[i]
                v2, n2, p2, b2 = priced[j]
                if n1 == n2:
                    continue  # same chain handled by DEX-DEX detector
                if p1 <= p2:
                    buy_v, buy_n, buy_p, buy_b = v1, n1, p1, b1
                    sell_v, sell_n, sell_p, sell_b = v2, n2, p2, b2
                else:
                    buy_v, buy_n, buy_p, buy_b = v2, n2, p2, b2
                    sell_v, sell_n, sell_p, sell_b = v1, n1, p1, b1
                gross = self._gross_spread_pct(buy_p, sell_p)
                if gross <= 0:
                    self.counters.rejected_by_spread += 1
                    continue
                route = self._bridges.route(buy_n, sell_n, base_asset)
                if route is None:
                    # No bridge = unexecutable (§7.5).
                    self.counters.rejected_bridge += 1
                    continue
                # Detector-side economic floor (§ Phase 3): both legs are DEX (no flat
                # taker); the fixed bridge flat fee + a gas estimate are amortized into a
                # lower-bound %-term on top of min ROI. Rejects cross-chain spreads that
                # cannot clear the bridge, before the Candidate is built.
                if (not ctx.clears_economic_floor(
                        gross, buy_v, "DEX", sell_v, "DEX", quote_asset,
                        route.flat_fee_usd + ctx.gas_estimate_usd)
                        and not ctx.is_active_route(
                            ArbitrageType.CROSS_CHAIN.value, base_asset, quote_asset,
                            buy_v, sell_v, None)):
                    self.counters.rejected_bridge += 1
                    continue
                candidates.append(Candidate(
                    arb_type=ArbitrageType.CROSS_CHAIN, base_asset=base_asset,
                    quote_asset=quote_asset,
                    buy_leg=LegRef(buy_v, "DEX", buy_p, network=buy_n,
                                   pool_address=buy_b.pool_address),
                    sell_leg=LegRef(sell_v, "DEX", sell_p, network=sell_n,
                                    pool_address=sell_b.pool_address),
                    gross_spread_pct=gross,
                    bridge_name=route.name,
                    bridge_fee_usd=route.flat_fee_usd,
                    bridge_time_sec=route.time_sec,
                ))
        return candidates
