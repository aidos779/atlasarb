# Requirements → Implementation Checklist

Maps every requirement area from both specifications to the code that implements it. ✅ = implemented.

## Part 1 — Product Requirements (PRD/SRS)

### §3 Scope, adapters, data schema
| Req | Where | ✅ |
|---|---|---|
| §3.1 MVP exchanges (5 CEX + 3 DEX) | `scanner/adapters/cex/*`, `scanner/adapters/dex/*`, `registry.py` | ✅ |
| §3.2 ARCH-1 modular adapter interface, zero engine branching | `domain/ports.py:ExchangeAdapter`, `scanner/engine.py` | ✅ |
| §3.3 ARCH-2 official REST/WS/SDK/RPC only, no scraping | CEX WS+REST adapters, DEX RPC/quote adapters | ✅ |
| §3.5 Supported networks + R-NET-1/2 (network as venue identity) | `domain/enums.py:Network`, `CanonicalSymbol.network` | ✅ |
| §3.6 USDT quote only + R-QUOTE-1/2 | adapter `get_markets` filters; discovery `_filter_quote`; cache `track` | ✅ |
| §3.7 auto-discovery BR-ASSET-1/2/4 | `collectors/market_collector.py` | ✅ |
| §3.8 arbitrage types | `domain/enums.py:ArbitrageType` | ✅ |
| §3.9 canonical Signal schema (all fields) | `domain/signal.py:Signal` | ✅ |
| R-SCHEMA-1 Hidden per-user; R-SCHEMA-2 confidence gates alerts | `signal_registry` hide (session); `notification_service` confidence gate | ✅ |

### §5–§8 Roles, navigation, commands, menu
| Req | Where | ✅ |
|---|---|---|
| §5 roles + permission matrix; R-ROLE-1/2/3 | `domain/enums.py:UserRole`, `services/user_service.py`, `entitlements.py` | ✅ |
| §6 navigation (reply+inline, back/home) R-NAV-1/2 | `bot/keyboards/*` (`_nav_row`, reply keyboard) | ✅ |
| §7 all commands (`/start … /admin`) + deep links | `bot/handlers/start.py`, `app.py:_COMMANDS` | ✅ |
| BR-START-1/2/3 onboarding mandatory/resume/deeplink | `handlers/start.py`, `services/user_service.py` | ✅ |
| §8 Main Menu (8 buttons) + R-MENU-1 upsell visible | `keyboards/inline.py:main_menu`, upsell keyboards | ✅ |

### §9–§14 Signals, details, search, filters, notifications, settings
| Req | Where | ✅ |
|---|---|---|
| §9 Signal List: cards, controls, pagination(5), sort, refresh, hide; BR-SIG-1..6 | `handlers/common.py:render_signal_list`, `handlers/signals.py` | ✅ |
| §10 Signal Details: overview/profit/fees/liquidity/risk/history/route/related; BR-DETAILS-1..4 | `bot/formatters/signal.py:format_details` | ✅ |
| §11 Search coins/exchanges, aliasing, rate limit; BR-SEARCH-1/2/3/5 | `services/search_service.py`, `handlers/search.py` | ✅ |
| §12 Filtering: all filters, tier gating, save/reset; BR-FILTER-1..4 | `handlers/filters.py`, `domain/user.py:UserFilter.matches` | ✅ |
| §13 Notifications: types, caps/delay, mute, daily summary; BR-NOTIF-1..4 | `services/notification_service.py`, `services/scheduler.py` | ✅ |
| §14 Settings: language/timezone/currency + BR-SET-1..4 | `handlers/settings.py`, `bot/formatters/money.py` | ✅ |

### §15–§20 Subscription, admin, expiration, cooldown, ranking, exchange status
| Req | Where | ✅ |
|---|---|---|
| §15 tiers, upgrade/renewal/downgrade/cancel; R-SUB-1..4 | `services/subscription_service.py`, `handlers/subscription.py` | ✅ |
| §16 Admin: users, subs, broadcast(2-person), analytics, monitoring, logs, support; R-ADMIN-1..4 | `services/admin_service.py`, `handlers/admin.py` | ✅ |
| §17 Signal expiration + history moves; BR-EXP-1..3 | `scanner/lifecycle/manager.py`, `services/history_service.py` | ✅ |
| §18 Per-user signal cooldown + profit override; BR-COOL-1/2 | `services/notification_service.py` (cooldown + ±0.5pp) | ✅ |
| §19 Ranking levels ⭐/🟢/🟡/⚪; BR-RANK-1/2 | `scanner/ranking/ranker.py` | ✅ |
| §20 Exchange status gating; BR-EXST-1..4 | `scanner/status/health_registry.py`, engine gating | ✅ |

### §22–§23 Functional & Non-functional
| Req | Where | ✅ |
|---|---|---|
| FR-ONB/SIG/DET/SRCH/FILT/NOTIF/FAV/PROF/SUB/ADM/LOC/EXP/COOL/RANK/EXST | across services + bot + scanner | ✅ |
| NFR-SEC-01..06 (no keys, webhook verify, allow-list, audit, encryption at rest) | `settings.py`, `middlewares/context.py`, `admin_service`, `database` | ✅ |
| NFR-PERF/AVAIL/SCALE (latency budgets, degraded fallback, adapter modularity) | `engine.py`, `reconciliation`, `signals.empty/scanner_down` states | ✅ |
| FR-LOC-01/02 (en/ru, FX ≤5min) | `i18n/*` (strict catalog, validated at startup), `scanner/adapters/fx.py` | ✅ |

## Part 3 — Scanning Engine

| Req | Where | ✅ |
|---|---|---|
| §1.1 event-driven WS-first, stateless detection/stateful cache, fail-open/closed | `cache/market_state_cache.py`, `engine.py` | ✅ |
| §1.5 reconciliation safety net ≤1s | `reconciliation/scheduler.py` | ✅ |
| §1.6 priority system (P1 majors, queue) | `priority/scheduler.py` | ✅ |
| §2 adapter contract, health, reconnect backoff, error classes, retry, rate limits | `adapters/base_cex.py`, `base_dex.py`, `rate_limiter.py`, `backoff.py` | ✅ |
| §3 discovery, delisting, symbol normalization, dedup, verified tokens | `collectors/market_collector.py`, `domain/market.py:CanonicalSymbol`, `pool_registry.py` | ✅ |
| §4 price collection, refresh, invalid-price, outlier (median+MAD), cross-venue | `cache/market_state_cache.py`, `mathx.py` | ✅ |
| §5 order book: bid/ask/spread/depth, DEX AMM curve, book validation | `mathx.py`, `profit/liquidity_leg.py`, cache book checks | ✅ |
| §6 funding collection (annualized, next time) | `collectors/poll_collectors.py`, `domain/market.py:FundingRate` | ✅ |
| §7.1–7.5 five detectors (size-aware DEX, bridge routes) | `scanner/detectors/*` | ✅ |
| §7.6 multi-hop future-compat (documented, not implemented) | `detectors/*` (architecture supports; noted) | ✅ |
| §8 profit engine: spread, all fee categories, gross/net, cross-quote, ROI, sizing §8.11/§8.12 | `profit/engine.py`, `profit/models.py` | ✅ |
| §9 liquidity: floor, max size, depth, liquidity score | `liquidity/analyzer.py` | ✅ |
| §10 validation gates (all 7 + implausible/token/bridge/gas/warmup) | `validation/validator.py` | ✅ |
| §11 ranking composite, tiers, hard overrides, confidence composite | `ranking/ranker.py`, `ranking/confidence.py` | ✅ |
| §12 lifecycle state machine, TTLs per type, updates, archive | `lifecycle/manager.py` | ✅ |
| §13 dedup key, cooldown, significant change | `lifecycle/cooldown.py`, `lifecycle/manager.py` | ✅ |
| §14 exchange status values/transitions/N-consecutive/hard rule | `status/health_registry.py` | ✅ |
| §15 error handling (narrow scope, mark unavailable) | throughout adapters/cache/collectors | ✅ |
| §16 performance budgets (metrics track detection/gen p95) | `monitoring/metrics.py`, config budgets | ✅ |
| §17 monitoring surface | `monitoring/metrics.py`, admin monitoring view | ✅ |
| §18 business rules (21 consolidated) | enforced across engine modules | ✅ |
| §19 acceptance criteria | covered by `tests/` (unit + integration) | ✅ |
| §20 configuration surface (validate, hot-reload, layering, introspection) | `config/scanner_config.py`, `tests/unit/test_scanner_config.py` | ✅ |

## Notes on modeled integration points

- **Payment provider**: the confirm step activates the tier through `SubscriptionService`;
  in production the same activation is driven by the verified payment webhook (NFR-SEC-03).
- **DEX pool set / verified tokens / bridge routes / asset alias table**: these are *engine
  data inputs* per Scanner §3.3/§3.4/§7.5 (explicitly out of code scope). A real, extensible
  starter set ships in `scanner/adapters/dex/pool_registry.py` and `detectors/bridges.py`.
- **Multi-instance scaling** (Redis cache, sharded delivery) is a documented forward
  extension per §1.5 / NFR-SCALE — the MVP runs single-process as specified.
