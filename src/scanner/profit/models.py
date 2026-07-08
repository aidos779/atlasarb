"""Resolved fee inputs for the profit engine (Scanner §8).

A value of None means "not resolved" — the §10 Fees gate rejects any signal built
on an unresolved (would-be assumed-zero) fee category. This makes the never-assume-zero
rule (Business Rule 6) structural, not a convention.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class FeeInputs:
    buy_fee_rate: Decimal | None            # trading fee, buy leg
    sell_fee_rate: Decimal | None           # trading fee, sell leg
    withdrawal_fee_usd: Decimal | None      # §8.4 (None only if genuinely N/A)
    gas_fee_usd: Decimal | None             # §8.5
    bridge_fee_usd: Decimal | None          # §8.6 (None if not cross-chain)
    conversion_cost_bps: Decimal = Decimal(0)  # §8.9
    requires_withdrawal: bool = True
    requires_gas: bool = False
    requires_bridge: bool = False

    def resolved_categories(self) -> frozenset[str]:
        cats: set[str] = set()
        if self.buy_fee_rate is not None and self.sell_fee_rate is not None:
            cats.add("trading")
        if not self.requires_withdrawal or self.withdrawal_fee_usd is not None:
            cats.add("withdrawal")
        if not self.requires_gas or self.gas_fee_usd is not None:
            cats.add("gas")
        if not self.requires_bridge or self.bridge_fee_usd is not None:
            cats.add("bridge")
        cats.add("slippage")  # always computed from live liquidity
        return frozenset(cats)

    def fully_resolved(self) -> bool:
        required = {"trading", "withdrawal", "gas", "bridge", "slippage"}
        return required.issubset(self.resolved_categories())
