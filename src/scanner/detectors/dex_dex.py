"""DEX ↔ DEX detector (Scanner §7.3).

Same-chain case may execute atomically (single tx, flash-swap) → atomic_execution flag
drives favorable execution-risk in ranking. Cross-network DEX pairs are routed to the
Cross-Chain cost model (§7.5) rather than assuming same-chain atomicity.
"""
from __future__ import annotations

from decimal import Decimal

from src.domain.enums import ArbitrageType
from src.domain.signal import Candidate, LegRef
from src.scanner.detectors.base import DetectionContext, Detector


class DexDexDetector(Detector):
    arb_type = ArbitrageType.DEX_DEX.value

    def detect(self, ctx: DetectionContext, base_asset: str, quote_asset: str) -> list[Candidate]:
        if base_asset not in ctx.verified_tokens:
            return []
        pair = f"{base_asset}/{quote_asset}"
        dex = ctx.online_dex_books(pair)
        if len(dex) < 2:
            return []

        candidates: list[Candidate] = []
        for i in range(len(dex)):
            for j in range(i + 1, len(dex)):
                v1, b1 = dex[i]      # type: ignore[misc]
                v2, b2 = dex[j]      # type: ignore[misc]
                p1 = b1.reserve_quote / b1.reserve_base if b1.reserve_base else Decimal(0)
                p2 = b2.reserve_quote / b2.reserve_base if b2.reserve_base else Decimal(0)
                if p1 <= 0 or p2 <= 0:
                    continue
                net1 = ctx.venues.get(v1)
                net2 = ctx.venues.get(v2)
                n1 = net1.network if net1 else None
                n2 = net2.network if net2 else None
                same_chain = n1 == n2
                if not same_chain:
                    # Cross-network pairs are the CrossChainDetector's job — it
                    # attaches a real bridge route. Emitting them here produced
                    # bridge-less CROSS_CHAIN candidates that could only die
                    # NO_BRIDGE_ROUTE in the validator (§7.3/§7.5).
                    continue

                if p1 < p2:
                    buy_v, buy_b, buy_n, sell_v, sell_b, sell_n = v1, b1, n1, v2, b2, n2
                    buy_p, sell_p = p1, p2
                else:
                    buy_v, buy_b, buy_n, sell_v, sell_b, sell_n = v2, b2, n2, v1, b1, n1
                    buy_p, sell_p = p2, p1

                gross = self._gross_spread_pct(buy_p, sell_p)
                if gross <= 0:
                    continue

                # Cross-network DEX-DEX → cross-chain type (§7.3 business rule).
                arb_type = ArbitrageType.DEX_DEX if same_chain else ArbitrageType.CROSS_CHAIN
                candidates.append(Candidate(
                    arb_type=arb_type, base_asset=base_asset, quote_asset=quote_asset,
                    network=buy_n if same_chain else None,
                    buy_leg=LegRef(buy_v, "DEX", buy_p, network=buy_n,
                                   pool_address=buy_b.pool_address),
                    sell_leg=LegRef(sell_v, "DEX", sell_p, network=sell_n,
                                    pool_address=sell_b.pool_address),
                    gross_spread_pct=gross,
                    atomic_execution=same_chain,
                ))
        return candidates
