"""Scanning Engine orchestrator (Scanner §1.4/§1.5).

Wires the event-driven primary path (cache write → priority queue → generator worker →
detectors → assembler → lifecycle → notification queue) and the reconciliation safety
net. Owns collector/health/discovery tasks. The one place scheduling lives; detection
math lives in the assembler/detectors (SRP).
"""
from __future__ import annotations

import asyncio
import time

from src.config import describe_exc, get_logger
from src.config.scanner_config import ScannerConfig
from src.domain.enums import ExchangeStatus, ExpiryReason
from src.domain.ports import (
    ExchangeAdapter,
    GasPriceProvider,
    NotificationQueue,
    SignalHistoryStore,
)
from src.domain.signal import Candidate
from src.scanner.assembler import SignalAssembler
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.collectors.market_collector import MarketCollector
from src.scanner.collectors.poll_collectors import DexPoolCollector, FundingCollector
from src.scanner.detectors.base import DetectionContext, Detector, VenueInfo
from src.scanner.detectors.bridges import BridgeRegistry
from src.scanner.detectors.cex_cex import CexCexDetector
from src.scanner.detectors.cex_dex import CexDexDetector
from src.scanner.detectors.cross_chain import CrossChainDetector
from src.scanner.detectors.dex_dex import DexDexDetector
from src.scanner.detectors.funding import FundingDetector
from src.scanner.lifecycle.cooldown import CooldownStore
from src.scanner.lifecycle.manager import LifecycleManager
from src.scanner.monitoring.metrics import Metrics
from src.scanner.priority.scheduler import PriorityClassifier, PriorityEventQueue
from src.scanner.reconciliation.scheduler import ReconciliationScheduler
from src.scanner.status.health_registry import HealthRegistry

log = get_logger("scanner.engine")


class ScanningEngine:
    def __init__(
        self, config: ScannerConfig, adapters: dict[str, ExchangeAdapter],
        queue: NotificationQueue, history: SignalHistoryStore, gas: GasPriceProvider,
        verified_tokens: set[str], cache: MarketStateCache | None = None,
        health: HealthRegistry | None = None, perp_assets: set[str] | None = None,
    ) -> None:
        self._config = config
        self._adapters = adapters
        self._metrics = Metrics()
        self._cache = cache or MarketStateCache(config)
        self._health = health or HealthRegistry(config)
        self._priority = PriorityClassifier(config)
        self._event_queue = PriorityEventQueue()
        self._cooldown = CooldownStore(config)
        self._lifecycle = LifecycleManager(config, self._cooldown, queue, history)
        self._bridges = BridgeRegistry()
        self._assembler = SignalAssembler(
            config, self._cache, self._health, adapters, gas, self._priority
        )
        self._verified_tokens = {t.upper() for t in verified_tokens}
        # Funding scans need breadth: majors almost never clear the carry breakeven
        # (~10% annualized); alt perps regularly do. Assets without a perp on a venue
        # simply return None from that adapter's funding fetch.
        self._perp_assets = perp_assets or {
            "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "LTC", "DOT",
            "AVAX", "BNB", "SUI", "APT", "ARB", "OP", "PEPE", "SHIB", "WIF",
            "JUP", "TON", "NEAR", "TRX", "UNI", "AAVE", "FIL",
        }

        self._venue_info: dict[str, VenueInfo] = {}
        for vid, adapter in adapters.items():
            self._venue_info[vid] = VenueInfo(
                id=vid, venue_type=adapter.venue_type,
                network=getattr(adapter, "network", None),
            )
            self._health.register(vid)

        self._detectors: list[Detector] = [
            CexCexDetector(), CexDexDetector(), DexDexDetector(),
            CrossChainDetector(self._bridges), FundingDetector(),
        ]

        self._market_collector = MarketCollector(
            config, self._cache, adapters, self._priority,
            self._lifecycle.force_expire_delisted,
        )
        self._dex_collector = DexPoolCollector(config, self._cache, adapters)
        self._funding_collector = FundingCollector(
            config, self._cache, adapters, self._perp_assets
        )
        self._reconciliation = ReconciliationScheduler(
            config, self._run_full_scan, self._sweep_and_count, self._health_pass
        )

        self._worker_task: asyncio.Task | None = None
        self._healthcheck_task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()

        self._cache.subscribe(self._on_cache_write)
        self._health.on_transition(self._on_status_transition)

    # ── public accessors (Admin Signal Monitoring §16.6) ──
    @property
    def metrics(self) -> Metrics:
        return self._metrics

    @property
    def cache(self) -> MarketStateCache:
        return self._cache

    @property
    def health(self) -> HealthRegistry:
        return self._health

    def active_signals(self):
        return self._lifecycle.active_signals()

    def status_table(self) -> dict[str, ExchangeStatus]:
        return self._health.all_statuses()

    def kill_switch(self, venue: str, disabled: bool) -> None:
        """Admin per-connector kill-switch (PRD §16.6 / FR-ADM-05)."""
        if disabled:
            self._health.mark_offline(venue)
        # Re-enable happens naturally via health checks recovering.

    def update_config(self, config: ScannerConfig) -> None:
        """Hot-reload propagation (§20.4)."""
        self._config = config
        for comp in (self._cache, self._cooldown, self._lifecycle, self._assembler,
                     self._priority, self._market_collector, self._dex_collector,
                     self._reconciliation, self._health):
            comp.update_config(config)

    # ── event-driven primary path (§1.4) ──
    def _on_cache_write(self, base: str, quote: str, venue: str) -> None:
        self._metrics.record_snapshot()
        priority = self._priority.priority(base)
        self._event_queue.put(priority, (base, quote, venue))

    async def _generator_worker(self) -> None:
        while not self._stop.is_set():
            try:
                base, quote, _venue = await asyncio.wait_for(
                    self._event_queue.get(), timeout=1.0
                )
            except TimeoutError:
                continue
            await self._process_symbol(base, quote)

    async def _process_symbol(self, base: str, quote: str) -> None:
        started = time.perf_counter()
        self._metrics.record_opportunity_checked()
        ctx = self._detection_context()
        online_venues = [v for v in self._cache.venues_for_pair(f"{base}/{quote}")
                         if self._health.is_online(v)]
        if len(online_venues) >= 2:
            self._metrics.record_pair_checked(online_venues)
        candidates: list[Candidate] = []
        for detector in self._detectors:
            try:
                candidates.extend(detector.detect(ctx, base, quote))
            except Exception as exc:  # noqa: BLE001
                log.warning("detector_error", detector=detector.arb_type, error=describe_exc(exc))
        self._metrics.record_detection((time.perf_counter() - started) * 1000)
        # Collapse duplicate opportunities within this tick: several detectors (and
        # repeated cache-write events for the same pair) can emit the same venue-pair
        # opportunity, and assembling each one runs the full profit pipeline. Keep only
        # the highest-gross candidate per dedup key so no work is done twice.
        if len(candidates) > 1:
            best: dict[tuple, Candidate] = {}
            for cand in candidates:
                key = cand.dedup_key()
                cur = best.get(key)
                if cur is None or cand.gross_spread_pct > cur.gross_spread_pct:
                    best[key] = cand
            candidates = list(best.values())
        for cand in candidates:
            self._metrics.record_candidate()
            log.debug("candidate_created", arb_type=cand.arb_type.value,
                      coin=base, buy=cand.buy_leg.venue, sell=cand.sell_leg.venue,
                      gross_pct=float(round(cand.gross_spread_pct, 4)))
            await self._handle_candidate(cand)

    async def _handle_candidate(self, cand: Candidate) -> None:
        gen_started = time.perf_counter()
        result = await self._assembler.assemble(cand)
        self._metrics.record_generation((time.perf_counter() - gen_started) * 1000)
        self._metrics.record_pair_candidate(cand.buy_leg.venue, cand.sell_leg.venue)
        if result.reject_reason is not None:
            self._metrics.record_reject(result.reject_reason.value)
            self._metrics.record_pair_reject(cand.buy_leg.venue, cand.sell_leg.venue,
                                             result.reject_reason.value)
            # Spread closed on an active signal -> immediate expiry (§12.4).
            if result.reject_reason.value in ("BELOW_MIN_PROFIT", "UNPROFITABLE_AFTER_FEES"):
                await self._lifecycle.close_if_spread_gone(cand.dedup_key(), 0)
            return
        signal = result.signal
        published, event = await self._lifecycle.admit(signal)
        if published:
            self._metrics.record_signal(event, signal.arb_type.value)
            self._metrics.record_pair_signal(cand.buy_leg.venue, cand.sell_leg.venue)

    def _detection_context(self) -> DetectionContext:
        # DetectionContext holds only stable references (cache, health, venue map,
        # verified-token set) — none of which change per tick. Build it once and reuse
        # it to avoid allocating a fresh context (and re-copying the token set) on every
        # opportunity check in the hot loop.
        ctx = getattr(self, "_ctx", None)
        if ctx is None:
            ctx = DetectionContext(
                cache=self._cache, health=self._health, venues=self._venue_info,
                verified_tokens=self._verified_tokens,
            )
            self._ctx = ctx
        return ctx

    # ── reconciliation safety net (§1.5) ──
    async def _run_full_scan(self) -> None:
        for pair in self._cache.tracked_pairs():
            base, _, quote = pair.partition("/")
            if base and quote:
                await self._process_symbol(base, quote)
        self._metrics.cache_size = self._cache.size()
        self._metrics.outliers = self._cache.outliers
        self._metrics.rejected_prices = self._cache.rejected_prices

    async def _sweep_and_count(self) -> int:
        count = await self._lifecycle.sweep_expired()
        for _ in range(count):
            self._metrics.record_expiry(ExpiryReason.TTL_EXCEEDED.value)
        return count

    async def _health_pass(self) -> None:
        now = time.time()
        for venue, adapter in self._adapters.items():
            threshold = (self._config.stale_cex_ws_sec
                         if adapter.venue_type.value == "CEX"
                         else self._config.stale_dex_rpc_sec)
            self._health.check_staleness(venue, threshold, now)

    async def _probe_one(self, venue: str, adapter: ExchangeAdapter) -> None:
        start = time.perf_counter()
        try:
            ok = await asyncio.wait_for(
                adapter.health_check(), timeout=self._config.healthcheck_timeout_sec
            )
            latency = (time.perf_counter() - start) * 1000
            if ok:
                self._health.record_success(venue, latency)
            else:
                self._health.record_failure(venue)
                self._metrics.record_api_failure(venue)
        except Exception:  # noqa: BLE001
            self._health.record_failure(venue)
            self._metrics.record_api_failure(venue)

    async def _stats_loop(self) -> None:
        """Emit an ENGINE STATS diagnostic line every 60s (§17) so pipeline health
        after discovery is observable at a glance."""
        tick = 0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60.0)
            except TimeoutError:
                pass
            if self._stop.is_set():
                break
            tick += 1
            m = self._metrics
            buckets = m.rejections_by_bucket()
            online = sum(1 for s in self._health.all_statuses().values()
                         if s.signal_allowed)
            total_books, multi_venue_pairs = self._cache.book_coverage()
            log.info(
                "engine_stats",
                venues_online=online, venues_total=len(self._adapters),
                cache_size=self._cache.size(),
                tracked_pairs=len(self._cache.tracked_pairs()),
                fresh_books=total_books,
                pairs_with_2plus_books=multi_venue_pairs,
                queue_pending=self._event_queue.pending(),
                snapshots_received=m.snapshots_received,
                opportunities_checked=m.opportunities_checked,
                candidates_created=m.candidates_generated,
                rejected_by_fees=buckets["fees"],
                rejected_by_spread=buckets["spread"],
                rejected_by_liquidity=buckets["liquidity"],
                rejected_by_freshness=buckets["freshness"],
                rejected_by_risk=buckets["risk"],
                signals_published=m.signals_created,
                telegram_sent=m.notifications_sent,
                reject_reasons=dict(m.rejections),
            )
            # Per venue-pair funnel every 5 min (§17) — the engine exposes the
            # numbers directly; no DEBUG log parsing required.
            if tick % 5 == 0:
                for pair, counters in m.venue_pair_snapshot().items():
                    log.info("venue_pair_stats", pair=pair, **counters)

    async def _healthcheck_loop(self) -> None:
        while not self._stop.is_set():
            # Probe every venue concurrently. Serial probing meant one slow venue
            # (up to healthcheck_timeout_sec each) pushed the *effective* per-venue
            # interval past the staleness threshold, causing false Maintenance flaps.
            await asyncio.gather(
                *(self._probe_one(v, a) for v, a in self._adapters.items()),
                return_exceptions=True,
            )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._config.active_healthcheck_interval_sec
                )
            except TimeoutError:
                pass

    def _on_status_transition(self, venue: str, old: ExchangeStatus,
                              new: ExchangeStatus) -> None:
        """BR-EXST-4 — offline transition force-expires that venue's signals."""
        if new != ExchangeStatus.ONLINE:
            # Keep a strong reference so the task is not GC'd mid-flight and any
            # exception is retrieved (avoids the "task exception never retrieved"
            # warning and the orphan-task leak during status churn).
            task = asyncio.create_task(self._lifecycle.force_expire_venue(venue))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    # ── lifecycle ──
    async def start(self) -> None:
        self._stop.clear()
        for venue, adapter in self._adapters.items():
            try:
                # connect() only opens the transport session — it is NOT a health
                # signal. Recording a success here flipped even unreachable venues to
                # Online for one cycle (a spurious Online->API_OFFLINE transition).
                # Real status is established by the health-check loop / first data tick.
                await adapter.connect()
            except Exception as exc:  # noqa: BLE001
                log.warning("adapter_connect_failed", venue=venue, error=describe_exc(exc))
                self._health.record_failure(venue, hard=True)
        self._market_collector.start()
        self._dex_collector.start()
        self._funding_collector.start()
        self._reconciliation.start()
        self._worker_task = asyncio.create_task(self._generator_worker(), name="generator")
        self._healthcheck_task = asyncio.create_task(self._healthcheck_loop(), name="health")
        self._stats_task = asyncio.create_task(self._stats_loop(), name="engine-stats")
        log.info("scanning_engine_started", adapters=len(self._adapters))

    async def stop(self) -> None:
        self._stop.set()
        await self._reconciliation.stop()
        await self._market_collector.stop()
        await self._dex_collector.stop()
        await self._funding_collector.stop()
        for task in (self._worker_task, self._healthcheck_task, self._stats_task):
            if task:
                await asyncio.gather(task, return_exceptions=True)
        for adapter in self._adapters.values():
            try:
                await adapter.disconnect()
            except Exception:  # noqa: BLE001
                pass
        log.info("scanning_engine_stopped")
