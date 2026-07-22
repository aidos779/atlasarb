# AtlasArb Scanning Engine — Engineering Report, Phases 2–5

**Status:** implementation complete · **Test suite:** 571 passing, ruff clean, mypy clean on all touched files · **Scope of this report:** the funding-formatter redesign, detector-side economic filtering, reconciliation redesign, and production hardening. Phases 1 (typed event routing) and 1.5 (CPU instrumentation) are the foundation these build on and are referenced where relevant.

> **Measurement convention used throughout.** `MEASURED (prod)` = derived from the real 24h production log. `MEASURED (synthetic)` = a controlled local run through the real engine code (fake adapters — structure is real, absolute cost is not production-representative). `ESTIMATED` = extrapolation, labeled. `THEORETICAL` = directional, unproven. No production CPU numbers exist yet — a staging deploy is required and is the top open item.

---

## PHASE 2 — Funding Formatter Redesign

### Architecture before
Funding signals reused the generic **spot** formatter. Funding legs carry a `Decimal(1)` sentinel price (a delta-neutral carry has no spot execution price), which surfaced to users as **"Buy on okx @ $1.00 / Sell on bitget @ $1.00"** and a spot trade route **"Move NEAR → bitget (network: chain)"**. The instant alert rendered **"Buy okx → Sell bitget."** Additionally, a units bug: the alert scaled the annualized differential from `funding_annualized_spread` (a *fraction*, e.g. `0.438`) through `format_pct` (which expects *percent*), rendering **43.8% as "+0.44%"**.

### Architecture after
A dedicated funding presentation path across every surface (alert, compact card, detailed card, trade route), fed by explicit per-leg data carried Detector → Candidate → Assembler → Signal → formatter. No spot template is ever used for funding; no `$1.00`, no "Buy→Sell", no "move coins."

### Data flow
```
FundingDetector.detect
  ├─ computes low/high.annualized() (already did) → now RETAINED ×100 (percent) on Candidate
  └─ Candidate{ funding_buy_annualized, funding_sell_annualized, funding_annualized_spread, funding_next_time, funding_low/high }
       └─ assembler._assemble_funding (profit math UNCHANGED)
            └─ Signal{ + funding_buy_annualized, funding_sell_annualized, funding_hold_hours }
                 └─ formatter: funding branches read spread_pct (percent) + the per-leg fields
```

### Formatter redesign (`src/bot/formatters/signal.py`)
- `format_alert`: funding branch → `alert.funding_new` + `alert.funding_route` ("Long/Short + annualized"); **units bug fixed** (sources `spread_pct`, percent).
- `format_card`: **new** funding branch → `card.funding_route` (was the Phase-1 gap — the compact list card still used the spot route).
- `format_details` overview: long/short venues, per-leg annualized rates, next-funding with a **past-settlement guard** ("imminent"), holding horizon.
- Profit breakdown: adds "Net over {horizon}" so the net is never read as a per-trade return.
- `_trade_route`: funding hedge guide (open LONG / open SHORT / hold ~N days / close), with a `route.funding_hold_nohorizon` fallback.
- Helpers: `_hold_horizon` (days-if-whole-else-hours, localized), `_funding_next_line`.

### Signal / Candidate / Assembler changes
- **Signal** (`domain/signal.py`): + `funding_buy_annualized`, `funding_sell_annualized` (percent), `funding_hold_hours` — all `| None = None`. Flat-field design (approach A) chosen over a `FundingMetadata` object to match the existing flat funding fields and avoid a new abstraction.
- **Candidate**: + `funding_buy_annualized`, `funding_sell_annualized`.
- **Assembler** (`_assemble_funding`): copies the two per-leg rates + `funding_hold_hours` (from config) onto the Signal. **No change to profit/ROI/fees/sizing.**
- **Detector** (`funding.py`): retains `low/high.annualized()×100`; documents the `Decimal(1)` sentinel.

### i18n changes (`i18n/catalog.py`, EN + RU parity)
New keys: `card.funding_route`, `details.funding_long/short/next/next_now/leg_rate/hold/horizon`, `alert.funding_new/route`, `route.funding_open_long/open_short/hold/hold_nohorizon/close/note`, `unit.days/hours`. Catalog validation stays green.

### Telegram UX — complete before/after
**Before (instant alert):**
```
🚨 New Signal: NEAR/USDT  +0.25%
Buy okx → Sell bitget
Liquidity $2,000 · Risk ●○○○○
```
**After (instant alert):**
```
💰 Funding Arb: NEAR/USDT  +0.25%
Long okx / Short bitget · +43.80% annualized
Liquidity $2,000.00 · Risk ●○○○○
```
**Before (details overview):** `Buy on okx @ $1.00` / `Sell on bitget @ $1.00`

**After (details, real render):**
```
Long (low funding) on okx
Short (high funding) on bitget
Funding (annualized): long +10.95% · short +54.75%
Next funding: 2026-07-21 22:00 UTC
Holding horizon: 7 days
...
Net Profit: +0.25% (~$5.01)
Net over 7 days
...
Trade Route
1. Open LONG NEAR perpetual on okx (lower funding)
2. Open SHORT NEAR perpetual on bitget (higher funding)
3. Hold the delta-neutral hedge ~7 days — collect the differential each interval
4. Close both legs once the spread compresses
No coin transfer between exchanges — this is a hedged position, not a spot move.
```
RU verified fully localized.

### New tests / backward compatibility / limitations
- **Tests:** `test_funding_formatter.py` (6) + `test_phase2_funding.py` (11) — detector/assembler propagation, units regression, alert/card/detailed rendering, past-settlement guard, hold-horizon, localization, and non-funding unchanged.
- **Backward compat:** all new fields default `None`, guarded individually; non-funding rendering byte-identical.
- **Limitations:** `Decimal(1)` sentinel still persisted as `buy_price=1.0` into `SignalHistory` (cosmetic analytics leak, out of scope); hedge instructions inherit the *existing* tier gating (currently ungated dev-mode — not newly introduced).

---

## PHASE 3 — Detector-side Economic Filtering

### New pipeline
```
BEFORE:  Detector → Candidate → Assembler(pre-gate reject) → …
AFTER:   Detector → economic viability check → Candidate → Assembler(pre-gate = final net) → …
```
The **safety invariant**: each detector floor is a *lower bound* on the assembler's publish condition, so it drops only candidates the assembler would drop — **never a publishable one**. The assembler pre-gate is retained as the final net.

### Detector vs assembler responsibilities
- **Detectors** now reject economically-impossible opportunities *before Candidate creation*, via a single shared `DetectionContext.clears_economic_floor()` (mirrors the assembler's `_fee_floor_pct` size-independent math) + `funding_breakeven_annualized()`.
- **Assembler**: unchanged final validation; still catches anything that slips through (proven by test).

### Per-detector (previous → new → impact)
| Detector | Previous | New floor | Impact |
|---|---|---|---|
| **CEX↔CEX** | emit any `gross>0` (below ceiling) | `2×taker + min_ROI` ≈ **0.35%** | prunes sub-fee spreads pre-candidate |
| **CEX↔DEX** | emit any positive direction | `CEX taker + amortized gas + min_ROI` ≈ **0.25%** | fewer candidates |
| **DEX↔DEX** | emit any `gross>0` | `amortized gas + min_ROI` ≈ **0.15%** | **dramatic** — kills the sub-0.15% same-chain flood |
| **Cross-chain** | emit any `gross>0` w/ route (directed 2× loop) | `bridge flat fee + gas + min_ROI` (directed loop already fixed Phase 1) | **dramatic** — bridge-aware pruning; no-route → `rejected_bridge` |
| **Funding** | fixed `annualized < 0.05` | economically-derived breakeven `(min_net/size + round-trip taker)/(hold/8760)` ≈ **36.5%** | prunes carries that cannot clear fees + min net |

### §12.4 preservation (critical)
A prior engineer had *reverted* detector pruning because it suppressed spread-collapse expiry (the assembler's `BELOW_MIN_PROFIT` reject is what promptly expires a stale active signal). Solution: **active-route exemption** — a route with a live signal keeps emitting its candidate (so the assembler still fires `close_if_spread_gone`), via a live `active_routes` keys-view threaded from the lifecycle, short-circuited to zero cost when no signals are active.

### Metrics / detector-stats / config / tests
- **Metrics:** detector-side `rejected_economic`, `rejected_bridge` (separate from assembler-side `asm_rejected_*`). (`rejected_gas` was added then removed in Phase 5 as dead.)
- **Config:** `detector_economic_floor_enabled` (True), `detector_cost_amortization_usd` (1,000,000 — large so fixed-cost terms are a strict lower bound), `detector_gas_estimate_usd` (1.0). Backward compatible.
- **Tests** (`test_phase3_economic_floor.py`, 12): per-detector floors, exact boundaries, floor-disabled compat, funding breakeven, active-route exemption, assembler-still-catches, never-prune-publishable.
- **Measured (synthetic):** candidates **−90%** (300→30), assembler invocations **−90%**, published signals **unchanged (0→0)**, detector time **−12%**.

---

## PHASE 4 — Reconciliation Redesign

### Before → After (diagram)
```
BEFORE (every ≤1s):
   for pair in ALL tracked_pairs (~2,465):
       run ALL 5 detectors
   → ~1000s of detector execs/pass regardless of eligibility, cadence, or yield
   → MEASURED (prod): reconciliation ≈ 75% of all _process_symbol calls (the dominant CPU consumer)

AFTER (every ≤1s: sweep + health UNCHANGED; detection scan is smart):
   due = cadence.due_detectors(now)                # per-detector interval + yield backoff
   for pair in tracked_pairs:
       run_set = due ∩ eligibility(pair)           # working set + detector-aware
       if run_set: process_symbol(pair, only_detectors=run_set)
       else: skip
```

### Working set & detector scheduling
- **Working set:** `_reconcile_eligibility(base,quote)` — a **safe superset** of "can form a candidate" from online-venue coverage (≥2 CEX → cex_cex; CEX+DEX → cex_dex; ≥2 DEX → dex_dex; ≥2 DEX networks → cross_chain; ≥2 online funding venues → funding). Over-inclusion only wastes an early-returning `detect()`; it can never hide an opportunity.
- **Adaptive cadence** (`reconciliation/cadence.py`): funding 1s, CEX 2s, DEX↔DEX 5s, cross-chain 10s (all configurable).
- **Yield-aware:** a detector with no publish for `idle_after_sec` (300s) has cadence ×`idle_backoff` (4); recovers to base cadence instantly on its next publish. **Never disabled — only slowed.**

### Event interaction & guarantees
The event path stays **authoritative**; reconciliation is verification. Coverage is preserved with **bounded eventual-consistency latency** = base × backoff per detector (cross-chain worst case 40s); sweep/health run every ≤1s regardless. Correctness proven by tests: safe-superset eligibility, loss-free skipping (`only_detectors` subset == full run when others ineligible), live-opportunity detection by reconciliation, and cold-detector recovery.

### Metrics / latency / tests
- **Metrics:** `reconciliation_working_set`, `reconciliation_skipped_pairs`, `reconciliation_ran/skipped_detector_execs`, per-detector `reconciliation_cadence`, integrated into the Phase 1.5 `timing` block.
- **Latency:** normal (event) detection **unchanged** (sub-second); only missed-event catch latency is now bounded per detector.
- **Tests** (`test_phase4_reconciliation.py`, 14): cadence ordering, yield backoff/recovery, eligibility matrix, working-set skipping + metrics, detector-aware execution, loss-free skipping, coverage, recovery.
- **Measured (synthetic):** detector executions/pass **1000 → 100 (−90%)** on a full-due pass, **→ 0 on cadence-gated passes**; working set 200→100 pairs; pass time 0.8→0.5 ms.

---

## PHASE 5 — Production Hardening

- **Code/metrics cleanup:** removed the dead `rejected_gas` counter (declared/reset but never incremented). No TODO/FIXME/debug/`print`/`pdb` anywhere in `src/`.
- **Config cleanup:** added validation ranges for all 9 Phase 3/4 config fields (were unvalidated); bad values now fail at load. No config removed (layered env-override system makes removal a compat risk).
- **Logging:** audited — already production-grade (structured, consistent event names/severity, hot-path DEBUG gated behind `debug_enabled()`, high-frequency events throttled). No functional change needed; confirmed no new spam introduced by any phase.
- **Error handling:** confirmed full isolation — per-detector try/except, collectors/reconciliation never die, formatter/notifier failures isolated per-user with WARNING+trace. **No detector/adapter/assembler/formatter failure can stop the engine.**
- **Docs:** `ARCHITECTURE.md` data-flow + reconciliation description updated to match typed routing / economic floor / smart scan; stale `scheduler.py` "full pair×venue matrix" docstring corrected.
- **Technical debt removed:** dead metric, stale docs/docstrings, unvalidated config. **Remaining (documented):** `Decimal(1)` funding sentinel, write-only legacy `Signal.funding_annualized_spread` (kept as canonical §3.9 field), `_reconcile_eligibility` per-pass recompute, process-local cache.

---

## Overall Architecture — final pipeline

```
Market Data (adapters: WS/REST/RPC)
  │  normalize
  ▼
Cache (MarketStateCache) ── invalid-price §4.5, outlier §4.6, warm-up §3.1; TYPED emit (price|book|funding)
  ▼
Routing (Priority Queue, coalesced) ── event carries which input changed
  ▼
Detectors (×5) ── TYPED ROUTING: only detectors whose input changed run
  │             ── ECONOMIC FLOOR: reject taker/gas/bridge+ROI-impossible ops BEFORE Candidate
  │                (active routes exempt → §12.4 preserved)
  ▼
Candidate  ── deduped per tick (highest gross per route)
  ▼
Assembler ── fee-floor pre-gate (FINAL net) → Profit Engine §8 → Liquidity §9 → Risk/Confidence → Ranking §11 → Validator §10
  ▼
Lifecycle (§12) ── new/update/expire, cooldown §13; publishes drive yield-cadence recovery
  ▼
Publisher (EngineBridge → SignalRegistry + NotificationService) ── per-user eligibility (filters, tiers, caps, mutes)
  ▼
Telegram (TelegramNotifier) ── dedicated funding formatter; spot template never reused for funding

Reconciliation Scheduler (≤1s) ── age sweep + health pass + SMART re-scan (working set × due∩eligible detectors)
```
**Stage responsibilities:** *Cache* = canonical validated market state + typed change events. *Routing* = coalesced priority delivery of changed symbols. *Detectors* = identify opportunities and reject the economically impossible early. *Candidate* = deduped opportunity. *Assembler* = full economics + final validation. *Lifecycle* = signal state machine + expiry. *Publisher* = per-user delivery. *Telegram* = localized, type-correct rendering. *Reconciliation* = bounded-latency safety net verifying the event path.

---

## File-by-file summary

| File | Purpose | Changes | Business logic? | Presentation? | Compat |
|---|---|---|---|---|---|
| `domain/signal.py` | Signal/Candidate model | + funding presentation fields | No (additive carriers) | Enables funding UX | Full (defaults None) |
| `scanner/detectors/funding.py` | Funding detector | retain per-leg annualized; economic breakeven threshold | Threshold shift-left (safe = assembler cond.) | Feeds UX | Full |
| `scanner/detectors/cex_cex/cex_dex/dex_dex.py` | Spot/DEX detectors | economic floor + active-route exemption | Shift-left prune (lower bound) | No | Full (floor toggle) |
| `scanner/detectors/cross_chain.py` | Cross-chain detector | directed-loop fix (P1) + bridge floor; `rejected_bridge` | Shift-left prune; identical candidates | No | Full |
| `scanner/detectors/base.py` | DetectionContext | economic-floor inputs + `clears_economic_floor`/`funding_breakeven_annualized`/`is_active_route` | Shared floor logic | No | Full |
| `scanner/assembler.py` | Signal assembler | funding presentation propagation; `pregate_hit` flag (P1.5) | No (profit math intact) | Funding fields | Full |
| `scanner/engine.py` | Orchestrator | typed routing (P1), instrumentation (P1.5), context floor inputs (P3), smart `_run_full_scan`/eligibility/cadence (P4) | Scheduling only; detection unchanged | No | Full |
| `scanner/reconciliation/cadence.py` | **New** — cadence planner | per-detector + yield-aware scheduling | Scheduling only | No | New |
| `scanner/reconciliation/scheduler.py` | Reconciliation timer | docstring update | No | No | Full |
| `scanner/lifecycle/manager.py` | Lifecycle | read-only `active_route_keys()` | No | No | Full |
| `scanner/cache/market_state_cache.py` | Market cache | funding emit channel (P1) | No | No | Full |
| `scanner/monitoring/metrics.py` | Metrics | P1.5 timers + P4 reconciliation gauges | No | No | Full |
| `scanner/monitoring/detector_stats.py` | Detector funnel | split detector/assembler counters (P1); economic counters (P3); removed dead `rejected_gas` (P5) | No | No | Report shape grew |
| `bot/formatters/signal.py` | Telegram rendering | dedicated funding formatter | No | **Yes** | Full (non-funding identical) |
| `bot/formatters/stats.py` | /stats screen | detector-dropped vs assembler-rejected split | No | Yes (admin) | Full |
| `i18n/catalog.py` | Localization | funding keys (EN+RU) | No | Yes | Full (additive) |
| `config/scanner_config.py` | Config | economic + cadence fields + validation ranges | No | No | Full |
| `docs/ARCHITECTURE.md` | Docs | pipeline/reconciliation updated | — | — | — |

No file changed core profit, ROI, fee, sizing, ranking, confidence, or lifecycle *math*.

---

## Metrics — before vs after

| Aspect | Before | After | Source |
|---|---|---|---|
| Detector executions/event | all 5 per cache write | 1–3 (typed routing) | MEASURED (synthetic) confirms routing |
| Detector executions/reconciliation pass | 5 × all pairs | (due ∩ eligible) — **−90%** full-due, **0** cadence-gated | MEASURED (synthetic) |
| Candidate generation | 24.46M/24h; ~78% structurally dead | **−90%** doomed pruned pre-candidate | MEASURED (prod baseline) + MEASURED (synthetic) reduction |
| Assembler executions | ≈ candidates | **−90%** | MEASURED (synthetic) |
| Reconciliation share of CPU | ~75% of `_process_symbol` calls (dominant) | working-set + cadence gated | MEASURED (prod) baseline; reduction ESTIMATED for prod |
| Telemetry | mixed counters; `rejected_spread` = 2× (cross-chain artifact); no timing; no reconciliation gauges | split detector/assembler counters; per-path + per-detector timing; reconciliation gauges | MEASURED (code) |
| Publish path | funding-only; funding never triggered by funding data | funding routed on funding events; funding UX correct | MEASURED (code); publish-rate gain THEORETICAL |

---

## Performance — measured / estimated / theoretical

- **MEASURED (synthetic):** candidates −90%, assembler invocations −90%, reconciliation detector execs/pass −90% (→0 when cadence-gated), detector time −12%; published signals unchanged.
- **MEASURED (prod, baseline only):** reconciliation ≈ 75% of `_process_symbol` calls; 24.46M candidates → 1 published/24h; funding the sole publisher.
- **ESTIMATED (prod):** aggregate scanning-CPU reduction is large because both dominant consumers (reconciliation scan, doomed-candidate assembly) were cut ~90% synthetically; exact figures require deploy + `reconciliation_detection_ms_total` before/after.
- **THEORETICAL:** publish-rate improvement from funding-event routing and (future) CEX↔CEX starvation fix — directionally positive, unproven.
- **Publish latency:** event-path detection unchanged (sub-second); missed-event catch latency now bounded per detector (≤1–40s).
- **Memory:** neutral — a few scalar counters, per-CEX taker-rate dict, and a live keys-view (no copies); no new per-tick allocations on the hot path.

---

## Testing

- **Total: 571 passing** (baseline 502 at Phase 1 start → +69). Ruff clean; mypy clean on all touched files (residual errors are pre-existing `object`-scan/`settings.py` typing).
- **New test files (~66 tests):** routing (12), instrumentation (11), funding formatter (6+11), economic floor (12), reconciliation (14).
- **Coverage by area:** formatter (alert/card/detail, units regression, localization, non-funding unchanged); detectors (per-detector floors, exact boundaries, active-route exemption, assembler-still-catches, never-prune-publishable); reconciliation (cadence, yield recovery, eligibility, working-set, coverage, loss-free skipping); integration (`test_engine_pipeline` still publishes end-to-end); regression (§12.4 preserved, detector-stats consistency).

---

## Risks / limitations / deferred

- **No production CPU measurement yet** — synthetic only; deploy required (top item).
- **CEX↔CEX candidate starvation** (book freshness / WS subscription caps) — a publish-rate limit, not CPU; needs the earlier-proposed instrumentation to resolve.
- **`_reconcile_eligibility` per-pass recompute** — cacheable via venue-transition invalidation.
- **Reconciliation ignores priority tiers** — majors not scanned first under load.
- **Fixed-cost detector floors are conservative** (large amortization) — meaningful tightening needs production data.
- **Process-local cache** — the scaling ceiling; Redis-backed sink is the sharding extension point.
- **`Decimal(1)` funding sentinel** persisted as `buy_price=1.0` — cosmetic analytics leak.

---

## Production readiness — scores

| Dimension | Score | Justification |
|---|---|---|
| **Architecture** | **9/10** | Clean ports/adapters, event-first + bounded safety net, economics shifted left with a final assembler net. −1: funding sentinel/legacy-field footguns. |
| **Maintainability** | **9/10** | Single-source floor logic, consistent detector interface, spec-cited docstrings, docs match code, zero dead code/TODOs. −1: dense multi-phase comments in hot files. |
| **Performance** | **8/10** | ~90% synthetic cuts to the two dominant consumers; instrumentation makes CPU attributable. −2: prod figures unmeasured; eligibility recompute remains. |
| **Reliability** | **9/10** | Full error isolation, §12.4 preserved through every optimization, bounded eventual consistency, config validated at load. −1: single-process state. |
| **Scalability** | **7/10** | Coalescing + working set + adaptive cadence scale detection; process-local cache and unsharded reconciliation cap horizontal scale. |
| **Observability** | **9/10** | Per-path + per-detector timing, honest split funnel, reconciliation gauges, structured throttled logs. −1: per-detector reconciliation time not split by path. |
| **Testability** | **9/10** | Pure, injectable components (cadence planner, DetectionContext, formatters); 571 fast deterministic tests. −1: some reliance on synthetic engine harnesses. |
| **Production Readiness** | **9/10** | Green suite, clean lint/types, validated config, rich telemetry, no shortcuts. −1: needs staging deploy to confirm synthetic numbers and close CEX↔CEX. |

---

## Final verdict

**What was accomplished (Phases 2–5):** the engine was transformed from a system that published ~1 signal/24h with reconciliation-dominated CPU and a broken funding UX into a production-grade scanner with (2) a correct, type-specific funding formatter; (3) detector-side economic filtering that removes ~90% of structurally-dead candidates before the assembler while provably never dropping a publishable opportunity and preserving §12.4 spread-collapse expiry; (4) a reconciliation safety net redesigned to scan only the eligible working set on per-detector adaptive cadence (~90% fewer detector executions per pass) with bounded eventual-consistency guarantees; and (5) a hardening pass removing dead metrics, validating config, correcting docs, and confirming full error isolation.

**Biggest architectural improvements:** shifting economics left into the detectors (with the assembler as final net), and converting reconciliation from an unconditional full-matrix scan into an eligibility-and-cadence-gated verifier — both while keeping the event path authoritative.

**Biggest performance improvements:** ~90% synthetic reductions in candidate generation, assembler invocations, and reconciliation detector executions — the three items that dominated the measured production baseline.

**Future work:** a staging deploy to replace synthetic numbers with real CPU/latency, the CEX↔CEX starvation fix (publish-rate), eligibility caching, priority-tier-aware reconciliation, and eventual cache sharding for horizontal scale.

**Production ready?** **Yes** — the scanning engine is production-grade: all changes are test-covered and proven not to reduce real profitable opportunities, failures are isolated, config is validated, and observability is comprehensive. The one gating recommendation before declaring victory on the performance claims is a **staging deployment to capture real `engine_stats.timing` data**, which the Phase 1.5 instrumentation was purpose-built to provide.
