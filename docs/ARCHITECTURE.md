# Architecture Overview

## Layering (Clean Architecture)

```
┌─────────────────────────────────────────────────────────────┐
│  bot/ (aiogram handlers, keyboards, formatters, i18n)        │  Interface
│  app.py (composition root / DI)                              │
├─────────────────────────────────────────────────────────────┤
│  services/ (use cases: user, subscription, notification, …)  │  Application
│  scanner/ (engine, detectors, profit, ranking, lifecycle)    │
├─────────────────────────────────────────────────────────────┤
│  domain/ (entities, enums, entitlements, ports)              │  Domain (pure)
├─────────────────────────────────────────────────────────────┤
│  scanner/adapters, database/ (SQLAlchemy), FX/gas providers  │  Infrastructure
└─────────────────────────────────────────────────────────────┘
```

Dependencies point inward. `domain/` imports nothing external. The engine and services
depend only on **ports** (`domain/ports.py`): `ExchangeAdapter`, `NotificationQueue`,
`SignalHistoryStore`, `GasPriceProvider`, `FxRateProvider`. Concrete implementations are
injected at the composition root (`app.py`). This is what makes ARCH-1 true: the core has
no exchange-specific branching.

## Scanning engine data flow (Spec Part 3)

```
Adapters (WS/REST/RPC)
   └─ normalize → Market State Cache  (invalid-price §4.5, outlier §4.6, warm-up §3.1)
        └─ cache-write event → Priority Queue (§1.6)
             └─ Generator worker → Detectors (§7, x5)
                  └─ Candidate → Signal Assembler
                       ├─ Profit Engine (§8) — fees, slippage, sizing (§8.11/§8.12)
                       ├─ Liquidity Analyzer (§9)
                       ├─ Risk (§10.5) + Confidence (§11.5)
                       ├─ Ranking (§11) — composite score + hard overrides
                       └─ Validator (§10) — all gates
                            └─ Lifecycle Manager (§12) — new/update/expire + cooldown (§13)
                                 └─ NotificationQueue (EngineBridge) → SignalRegistry + NotificationService

Reconciliation Scheduler (§1.5) ── every ≤1s ──▶ full-matrix re-scan + age sweep + health pass
```

The **event-driven path** is primary (recalc scoped to the symbol an update touched). The
**reconciliation pass** is a safety net that re-runs all detectors across the full matrix,
sweeps age-based expiries, and runs passive staleness health checks.

## Bot ↔ Engine boundary

`EngineBridge` implements `NotificationQueue` + `SignalHistoryStore`. The engine hands
finished `Signal`s to `publish()`; the bridge updates the in-memory `SignalRegistry` (which
the Signal List / Details / Search read) and enqueues them for the `NotificationService`,
which evaluates per-user eligibility (filters, tier delay/caps, cooldown, mutes, favorite
alerts) and dispatches via the `TelegramNotifier`. On expiry, `archive()` persists the
signal to history and removes it from the registry.

## Configuration (Spec §20)

`ConfigManager` owns a `ScannerConfig` composed from **base → environment → runtime** layers,
validated at load (ranges + cross-parameter invariants, e.g. cooldown ≤ TTL, DEX slippage ≥
CEX). Hot-reload applies atomically (all-or-nothing) and notifies subscribers (the engine
propagates the new config to every component). Every parameter's effective value and
originating layer is introspectable.

## Subscription & tiers

All tier behavior resolves through `domain/entitlements.py` (single source of truth): signal
delay/caps, favorite caps, allowed arbitrage types, filter availability, history/advanced
details access. `SubscriptionService` handles immediate upgrades with pro-rata credit,
end-of-period downgrades that **freeze** (never delete) excess data, self-service cancel, and
the 2-retries-over-72h renewal state machine.

## Persistence

Async SQLAlchemy 2.0. Repositories map ORM rows to domain aggregates. The audit log is
append-only by construction (no update/delete methods). Per-user history lists cap at 50.
Alembic manages migrations; the baseline revision builds the schema from metadata.

## Concurrency & fault tolerance

Each adapter runs its own supervised WS task with §2.3 exponential-backoff reconnect; a
crashing venue never blocks others (fail-open per source). A signal built on stale/missing
data is never emitted (fail-closed per signal). Health transitions to offline force-expire
that venue's signals (BR-EXST-1/4). The reconciliation loop and all collectors catch and log
exceptions without dying.
