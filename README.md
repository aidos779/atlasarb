# Crypto Arbitrage Scanner Bot

A production-grade, subscription-based **Telegram bot** that continuously scans CEX/DEX
markets across multiple blockchains, computes fee-adjusted arbitrage opportunities
("signals"), and pushes them to subscribers — entirely inside Telegram. It is an
**information & alerting** product: it never requests exchange API keys, never custodies
funds, and never executes trades (MVP scope).

Built strictly to two specifications:
- `01_Product_Requirements_Specification` (PRD/SRS — the bot product)
- `02_Scanning_Engine_Specification` (Part 3 — the scanning engine)

See [`docs/REQUIREMENTS_CHECKLIST.md`](docs/REQUIREMENTS_CHECKLIST.md) for a clause-by-clause
mapping of every requirement to its implementation.

---

## Highlights

- **Event-driven scanning engine** (WebSocket-first) with a ≤1s reconciliation safety net,
  5 arbitrage detectors (CEX↔CEX, CEX↔DEX, DEX↔DEX, Funding, Cross-Chain), a shared profit
  engine, liquidity analysis, validation gates, ranking, confidence scoring, cooldown, and
  a full signal lifecycle.
- **8 modular exchange adapters** (Binance, Bybit, OKX, Bitget, MEXC + Uniswap, PancakeSwap,
  Jupiter) behind one shared interface — adding a venue is one new module, zero engine changes.
- **Complete Telegram bot**: onboarding, Main Menu, Signal List/Details, Search, Filtering,
  Notifications, Settings, Subscription/billing, Favorites, History, Profile, Support, and a
  full Admin Panel — with i18n (English/Russian/Kazakh), inline+reply keyboards, FSM state,
  rate limiting, and tier gating.
- **Clean Architecture**: `domain` (pure) → `application/services` → `infrastructure`
  (adapters, DB, telegram). Dependency Inversion throughout; async end-to-end.
- **Hot-reloadable, layered, validated configuration** for every scanner threshold (§20).

---

## Architecture overview

```
src/
  config/        Settings (env), ScannerConfig (§20 hot-reload), logging
  domain/        Entities, enums, entitlements, ports (interfaces) — no external deps
  scanner/       Scanning engine (Spec Part 3)
    adapters/    Shared adapter contract, rate limiter, backoff, 5 CEX + 3 DEX, gas, fx
    cache/       Market State Cache (invalid-price/outlier/warm-up, event trigger)
    collectors/  Discovery, DEX pool poll, funding poll
    detectors/   5 arbitrage detectors + bridge registry
    profit/      Shared profit engine, slippage, sizing (§8)
    liquidity/   Liquidity score & floors (§9)
    validation/  §10 validation gates
    ranking/     Composite score, tiers, risk, confidence (§11)
    lifecycle/   Cooldown + lifecycle state machine (§12/§13)
    status/      Exchange Health Registry (§14)
    priority/    Priority scheduler & event queue (§1.6)
    reconciliation/  Safety-net scheduler (§1.5)
    monitoring/  Metrics surface (§17)
    engine.py    Orchestrator; assembler.py builds Signals from candidates
  database/      SQLAlchemy models, repositories, Alembic migrations
  services/      user, subscription, notification, favorites, history, search,
                 analytics, admin, support, scheduler, signal registry, engine bridge
  bot/           aiogram handlers, keyboards, middlewares, formatters, i18n, notifier
  app.py         Composition root (DI) — wires everything, runs bot + engine
tests/           unit + integration (pytest)
docker/          Dockerfile; docker-compose.yml at repo root
config/          scanner.toml (tuning layer)
docs/            Architecture + requirements checklist
```

Detailed design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Installation

Requirements: **Python 3.11+** (3.13 works), and for production **PostgreSQL 14+** and
**Redis 7+**. For local dev, SQLite works out of the box.

```bash
git clone <repo> && cd arbitrage-scanner-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # or: pip install -e ".[dev]"
cp .env.example .env                      # then fill BOT_TOKEN
```

Get a bot token from [@BotFather](https://t.me/BotFather) and put it in `.env` as `BOT_TOKEN`.

### Run (local, SQLite)

```bash
python -m src.app
```

The app auto-creates the schema on first run when using SQLite. For Postgres, run migrations.

### Run (Docker — Postgres + Redis + migrations + bot)

```bash
cp .env.example .env      # set BOT_TOKEN, ADMIN_USER_IDS, RPC URLs, etc.
docker compose up --build
```

`docker compose` starts Postgres, Redis, runs `alembic upgrade head`, then launches the bot.

---

## Configuration guide

Two configuration surfaces:

1. **App settings** — environment variables (see [`.env.example`](.env.example) and the
   table below). Loaded once via `pydantic-settings`; secrets are never hardcoded.
2. **Scanner tuning** (`ScannerConfig`, Spec §20) — every threshold/interval/limit. Layered
   as **base defaults → environment layer → runtime overrides**, validated at load
   (range + cross-parameter invariants), and **hot-reloadable** without restart. Non-prod
   environments automatically run with wider tolerances (`config/scanner.toml`, §20.6).

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `ENVIRONMENT` | `development` / `staging` / `production` | `development` |
| `LOG_LEVEL` / `LOG_JSON` | Logging verbosity / JSON output | `INFO` / `false` |
| `BOT_TOKEN` | Telegram bot token (BotFather) | — (required) |
| `BOT_USERNAME` | Bot username for deep links | `arb_bot` |
| `TELEGRAM_WEBHOOK_SECRET` | Webhook secret token verification (NFR-SEC-02) | — |
| `ADMIN_USER_IDS` | CSV of Telegram user_ids granted Admin (R-ROLE-1) | empty |
| `SUPPORT_USER_IDS` | CSV of Telegram user_ids granted Support | empty |
| `DATABASE_URL` | Async SQLAlchemy URL (`postgresql+asyncpg://…` or `sqlite+aiosqlite://…`) | sqlite |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | Connection pool sizing | 20 / 10 |
| `REDIS_URL` | Redis (cooldown/rate-limit/future shared cache) | `redis://localhost:6379/0` |
| `TELEGRAM_PAYMENTS_PROVIDER_TOKEN` | Payments provider token | empty |
| `PAYMENT_WEBHOOK_SECRET` | Payment webhook verification (NFR-SEC-03) | — |
| `PRICE_BASIC_USD` / `PRICE_PRO_USD` | Tier pricing | 19 / 79 |
| `*_REST_URL` / `*_WS_URL` | Per-CEX official REST/WS endpoints (ARCH-2) | public endpoints |
| `*_RPC_URLS` | Per-network CSV of ≥2 RPC providers (§2.4 failover) | empty |
| `JUPITER_API_URL` | Jupiter quote API | public |
| `SCANNER_CONFIG_FILE` | Path to `scanner.toml` tuning layer | `config/scanner.toml` |

**No exchange API keys anywhere** — the bot only uses public market-data endpoints
(NFR-SEC-01). There is structurally no field to enter a key.

---

## Deployment guide

1. Provision Postgres + Redis (managed services recommended).
2. Set production env vars (`ENVIRONMENT=production`, real `DATABASE_URL`, `BOT_TOKEN`,
   `ADMIN_USER_IDS`, funded RPC provider URLs with ≥2 per network).
3. Run migrations: `alembic upgrade head`.
4. Deploy the container (`docker/Dockerfile`) — runs as non-root, includes a healthcheck.
5. Scale: the scanning engine is single-process for MVP (§1.5). Notification delivery and
   the market cache are designed to shard (Redis-backed cache, sharded delivery) for the
   100k-user target (NFR-SCALE-01) — documented as a forward extension.

Long-polling is used by default. For webhook mode, terminate TLS at your ingress and verify
`X-Telegram-Bot-Api-Secret-Token` against `TELEGRAM_WEBHOOK_SECRET` (NFR-SEC-02).

---

## Testing

```bash
pip install -e ".[dev]"     # or: pip install pytest pytest-asyncio aiosqlite
pytest                      # 50+ tests: math, profit, validation, ranking, config,
                            # cache, filters, entitlements, subscriptions, repos, engine E2E
```

---

## Security

- No exchange API keys / private keys requested or stored (NFR-SEC-01) — structurally absent.
- Telegram webhook secret-token verification (NFR-SEC-02); payment webhook verification hook
  (NFR-SEC-03).
- Admin allow-list checked on every request, not just at session start (NFR-SEC-04); role is
  derived, never self-declared.
- Append-only audit log for all admin mutations (NFR-SEC-05); repositories expose no
  update/delete path for it.
- Input validation (pydantic, typed filters, Decimal money math), safe parameterized DB
  access (SQLAlchemy), sanitized user-facing error messages.

---

## License

Proprietary — built to specification for the AutomateX-style operator described in the PRD.
