"""Cooldown / Dedup Store (Scanner §13) — engine-level spam prevention.

Distinct from PRD §18 per-user notification cooldown (that lives in the notification
service). This store governs whether the engine emits a NEW signal vs an UPDATE vs
suppresses during post-expiry cooldown.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType


@dataclass
class _CooldownEntry:
    until: float
    arb_type: ArbitrageType


class CooldownStore:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config
        self._cooldowns: dict[tuple, _CooldownEntry] = {}

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def on_expired(self, key: tuple, arb_type: ArbitrageType, now: float | None = None) -> None:
        """Enter cooldown after a signal expires (§13.2)."""
        duration = self._config.cooldown_for(arb_type.value)
        if duration <= 0:  # Funding: interval-governed, no flat cooldown
            return
        self._cooldowns[key] = _CooldownEntry(
            until=(now or time.time()) + duration, arb_type=arb_type
        )

    def in_cooldown(self, key: tuple, now: float | None = None) -> bool:
        entry = self._cooldowns.get(key)
        if entry is None:
            return False
        if (now or time.time()) >= entry.until:
            del self._cooldowns[key]
            return False
        return True

    def significant_change(self, old_net: Decimal, new_net: Decimal) -> bool:
        """§12.3/§13.4 — ±configured% net-profit change (or tier change handled upstream)."""
        if old_net == 0:
            return new_net != 0
        change_pct = abs(new_net - old_net) / abs(old_net) * Decimal(100)
        return change_pct >= Decimal(str(self._config.significant_profit_change_pct))

    def clear(self, key: tuple) -> None:
        self._cooldowns.pop(key, None)
