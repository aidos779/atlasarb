"""Exchange Health Registry (Scanner §14 / PRD §20) — single source of venue status.

Consulted by Signal Generator (pre-candidate), Validator (§10 gate), and Ranking
(venue risk tier). Status is never re-derived elsewhere. Recovery to Online requires
N consecutive good checks (§14.2) to prevent flapping. Also tracks rolling latency/error
for the confidence Exchange-Health factor (§11.5) and fires transition callbacks (BR-EXST-4).
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from src.config import get_logger
from src.config.scanner_config import ScannerConfig
from src.domain.enums import ExchangeStatus

log = get_logger("scanner.health")

TransitionCallback = Callable[[str, ExchangeStatus, ExchangeStatus], None]


@dataclass
class VenueHealth:
    status: ExchangeStatus = ExchangeStatus.UNKNOWN
    consecutive_success: int = 0
    consecutive_failure: int = 0
    last_success_at: float = 0.0
    last_ws_data_at: float = 0.0   # last real market-data tick (WS/pool), not REST ping
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=50))
    errors: deque[int] = field(default_factory=lambda: deque(maxlen=50))  # 1=err,0=ok

    def error_rate(self) -> float:
        return (sum(self.errors) / len(self.errors)) if self.errors else 0.0

    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        idx = min(len(ordered) - 1, int(0.95 * len(ordered)))
        return ordered[idx]


class HealthRegistry:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config
        self._venues: dict[str, VenueHealth] = defaultdict(VenueHealth)
        self._callbacks: list[TransitionCallback] = []

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def register(self, venue: str) -> None:
        _ = self._venues[venue]

    def on_transition(self, callback: TransitionCallback) -> None:
        self._callbacks.append(callback)

    def status(self, venue: str) -> ExchangeStatus:
        return self._venues[venue].status

    def health(self, venue: str) -> VenueHealth:
        return self._venues[venue]

    def is_online(self, venue: str) -> bool:
        return self._venues[venue].status.signal_allowed

    def all_statuses(self) -> dict[str, ExchangeStatus]:
        return {v: h.status for v, h in self._venues.items()}

    def record_success(self, venue: str, latency_ms: float = 0.0,
                       stream: bool = False) -> None:
        h = self._venues[venue]
        h.consecutive_success += 1
        h.consecutive_failure = 0
        h.last_success_at = time.time()
        if stream:
            h.last_ws_data_at = h.last_success_at
        h.latencies_ms.append(latency_ms)
        h.errors.append(0)
        if h.status != ExchangeStatus.ONLINE:
            # Cold start (§14.2): the first healthy check from UNKNOWN brings the
            # venue Online immediately. The N-consecutive gate only guards *recovery*
            # from a degraded state (Maintenance / API Offline) — that is where
            # anti-flap matters. Without this an alternating check leaves a venue
            # stuck in UNKNOWN forever, silently excluded from signal generation.
            if (h.status == ExchangeStatus.UNKNOWN
                    or h.consecutive_success >= self._config.health_recovery_consecutive):
                self._transition(venue, ExchangeStatus.ONLINE)

    def record_failure(self, venue: str, hard: bool = False) -> None:
        h = self._venues[venue]
        h.consecutive_failure += 1
        h.consecutive_success = 0
        h.errors.append(1)
        if hard:
            self._transition(venue, ExchangeStatus.API_OFFLINE)
            return
        # WebSocket-first (§1.1): a soft failure (typically a REST health-check
        # timeout) must NOT offline a venue whose WS is actively delivering data.
        # The live stream is the primary liveness signal; the REST probe is
        # secondary. This was the OKX Online<->API Offline flapping root cause.
        if h.last_ws_data_at and (time.time() - h.last_ws_data_at
                                  <= self._config.ws_idle_timeout_sec):
            return
        if h.consecutive_failure >= self._config.ws_fast_reconnect_max:
            self._transition(venue, ExchangeStatus.API_OFFLINE)

    def mark_offline(self, venue: str) -> None:
        self._transition(venue, ExchangeStatus.API_OFFLINE)

    def mark_maintenance(self, venue: str) -> None:
        self._transition(venue, ExchangeStatus.MAINTENANCE)

    def check_staleness(self, venue: str, threshold_sec: float, now: float | None = None) -> None:
        """Passive health (§2.2): degrade to Maintenance if a venue that *was*
        streaming market data has gone quiet past the freshness threshold.

        Keyed off ``last_ws_data_at`` (real WS/pool ticks) — NOT ``last_success_at``,
        which also advances on the periodic REST health-check. Keying it off the
        health-check clock produced false Maintenance flaps: with the health-check
        loop probing venues serially, a venue could simply be waiting its turn and
        get marked Maintenance even though it was perfectly healthy. A venue that
        has never streamed (``last_ws_data_at == 0``) is REST-health-managed only
        and is left to record_success/record_failure.
        """
        h = self._venues[venue]
        if h.last_ws_data_at == 0:
            return
        if (now or time.time()) - h.last_ws_data_at > threshold_sec:
            if h.status == ExchangeStatus.ONLINE:
                self._transition(venue, ExchangeStatus.MAINTENANCE)

    def _transition(self, venue: str, new: ExchangeStatus) -> None:
        h = self._venues[venue]
        old = h.status
        if old == new:
            return
        h.status = new
        if new != ExchangeStatus.ONLINE:
            h.consecutive_success = 0
        log.info("venue_status_change", venue=venue, old=old.value, new=new.value)
        for cb in self._callbacks:
            cb(venue, old, new)

    def health_factor(self, venue: str) -> Decimal:
        """0..100 quality for confidence factor (§11.5) — beyond binary Online/Offline."""
        h = self._venues[venue]
        if h.status != ExchangeStatus.ONLINE:
            return Decimal(0)
        err_penalty = Decimal(str(h.error_rate())) * Decimal(100)
        lat_penalty = min(Decimal(30), Decimal(str(h.p95_latency_ms())) / Decimal(50))
        return max(Decimal(0), Decimal(100) - err_penalty - lat_penalty)
