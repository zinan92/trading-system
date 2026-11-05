# Trading Orchestrator — Roadmap

> Status: paper-trading only (no live execution yet). This document is the
> anchor for getting to a production-grade **strategy evaluation platform**.
> Last updated: 2026-05-29.

## 0. What this product actually is

The goal is to **run many strategies (~50) in parallel on paper, observe which
performs best, and promote winners.** That makes this a **strategy
evaluation / comparison platform** first, and a trading engine second. Almost
every architectural decision below is driven by that outcome — not by a generic
"production quant system" checklist.

## 1. Current-state assessment (maturity is uneven, not a flat 10%)

| Layer | Maturity | Notes |
|---|---|---|
| Data layer | ~70% | `market_data.db` (bars/quotes) + multi-source feeds (binance_usdm / yahoo / broker CSV / OANDA), `data_health` auditor, preflight, lineage, gap doctor, official-feed receipts. Solid. |
| Single-strategy paper pipeline | ~60% | collect → clean → signal → backtest gate → ticket → paper executor (models slippage/spread/commission) → exit monitor → equity → performance → journal. Feature-rich but **not yet trustworthy** (see §2). |
| Risk gates | ~60% | risk_monitor, paper auto-approval gate, portfolio risk, kill switch. |
| Live-readiness scaffold | ~40% | live_submission_safety / activation / readiness / operation_runbook / OANDA. **Built before a strategy framework existed — premature, but kept warm per the live decision below.** |
| **Strategy library / N-parallel (the actual goal)** | **~5%** | **Hardcoded single strategy `gold_5m_v1`.** `SignalEngine` does `config.get("gold_5m_v1")`; `strategy.yaml` has one block; runner runs one engine on one global account. No Strategy interface, no registry, no per-strategy isolated state. |
| Persistence | ~30% | bars/quotes in SQLite; everything else is flat `outputs/<artifact>/<run_date>.json`. Single global $10k paper account, single tradable asset (GOLD). |
| Observability / alerting | ~10% | Almost none. Evidence: defects (chart zigzag, flat NAV, double-counted cost, stale dashboard date, daemon serving stale code) were all caught by a human eyeballing, not by the system. |

**Takeaway:** the foundation (data / risk / paper execution) is genuinely built.
The *product vision* (strategy library at scale) has almost no scaffold. Both
statements are true.

## 2. Core judgments

1. **P&L must be trustworthy before we rank on it.** A 50-strategy leaderboard
   built on wrong P&L is *worse* than none — it promotes the wrong strategy with
   false confidence. "Make single-strategy paper accounting correct + reconciled
   + self-checking" comes **before/with** scale-out, not after. This also makes
   the system speak for itself instead of relying on a human bug-detector.
2. **The JSON→DB rewrite is a trap, not Phase 0.** It would touch all ~79
   services' `load_json`/`write_json`, destabilize the mature layers, and deliver
   no new capability for months. The artifact model is a reasonable append-only
   event log. Build the strategy framework on the existing model under
   `outputs/strategies/<id>/...`; introduce a datastore only when file
   proliferation or cross-strategy queries actually hurt. Design paths now so a
   later migration is clean.
3. **Anchor everything on the evaluation outcome**, not a generic checklist.

## 3. Locked decisions

- **Live timeline: a few months out.** Keep the live scaffold warm; Phase 3
  leaves a broker-execution adapter seam (order / fill / reconcile abstraction).
  Implication: account + risk accounting must reach real-money grade earlier —
  which Phase 1 already drives. Live applies to *promoted winners*, not all 50.
- **Strategy form: GOLD variants now, multi-instrument seam reserved.** The
  `Strategy` interface and data layer are parameterized by symbol (not
  hardcoded), but near-term implementation/testing is GOLD/XAUUSDT only.
  Multi-feed routing: interface yes, implementation later.
- **Isolated paper account per strategy** (the goal forces it — 50 strategies
  cannot share one $10k account and be comparable).
- **Persistence DB: deferred** until it hurts (see judgment #2).
- **Backtest: partially un-deferred (2026-06-02).** A real event-driven
  backtester now exists (`services/strategy_backtester.py` +
  `pipelines/backtest_strategies.py`): each engine enumerates its historical
  signals in one pass, trades replay with fixed stop/target/timeout + the paper
  cost model, ranked in a separate backtest leaderboard. Caveat: the chan core's
  per-bar step replay is **superlinear** (~265 bars/s), so faithful chan backtest
  is bounded to a window (full 6mo ≈ 30-60min/strategy); macd/ma are O(N). First
  cut (14d, default exit) shows **no strategy with edge yet** — the engine now
  makes exit/param tuning a minutes-long loop instead of weeks of forward paper.

## 4. Critical path

```
Phase 1  Trustworthy single-strategy paper (correctness + reconciliation + self-check/alerts)
   │
Phase 2  Strategy abstraction (interface + registry; symbol-parameterized)
   │
Phase 3  Multi-strategy runner + isolated paper account per strategy + per-strategy artifacts
   │      (+ broker-execution adapter seam, kept dormant)
   │
Phase 4  Dashboard leaderboard + drill-down (the NAV-vs-gold view becomes per-strategy)
   │
Phase 5  Ops hardening (parallel/later): daemon restart-safety, secrets, env separation,
          queryable run history, move off Mac launchd
```

## 5. Phases (deliverables + definition of done)

### Phase 1 — Trustworthy paper accounting (IN PROGRESS)
- [x] Unify accounting semantics: canonical `equity = starting + realized + unrealized`
  (costs already netted in — subtracting again was the NAV-curve bug); drawdown is
  peak-based everywhere (the −3.58 vs −3.22 was per-bar vs daily granularity).
- [x] Per-cycle hard reconciliation: `accounting_invariant` check in
  `paper_reconciliation` fails on equity/cost drift. Validated live (drift 0).
- [x] Self-check + alerting backend: `AlertNotifier` consumes the per-cycle
  `HealthCheck` rollup, pages only on hard states (fail/error/block), transition-
  based dedup, pluggable delivery. Wired into the runner. **To enable real push,
  set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (or `TRADING_ORCHESTRATOR_*`
  variants) in env; otherwise alerts are recorded to `outputs/alerts/` + stdout.**
- [ ] Surface reconciliation / alert status on the dashboard (red/green badge),
  not just JSON.
- [ ] Daemon-running-stale-code detection → needs a version/heartbeat mechanism
  (deferred to Phase 5 ops hardening).
- **DoD:** across N consecutive cycles, accounting self-reconciles green, and any
  data/feed/accounting anomaly is surfaced by the system *before* a human notices.

### Phase 2 — Strategy abstraction (CORE DONE)
- [x] `Strategy` + `StrategyRegistry` (`services/strategy_registry.py`): each
  strategy = id + symbol + params + enabled, and `.signal_engine()` builds a
  `SignalEngine` from its own params. Symbol-parameterized (defaults GOLD).
- [x] `SignalEngine` takes a `strategy_id` (default preserves legacy behavior);
  `strategy.yaml` is now a library entry (symbol/enabled + signal block).
- [x] `daily.py` routes through `StrategyRegistry().default()` — identical output
  with one strategy; this is the seam Phase 3 widens to run all enabled strategies.
- [ ] Per-strategy config *versioning* (lighter follow-up; library structure is in place).
- **DoD:** ✅ `gold_5m_v1` runs through the registry with identical output
  (test + live, suite green); a second variant registers via config only (test).

### Strategy type: Chan (缠论) 二买/类二买 — ENGINE DONE, integration pending
A second engine *type* (not a param variant), selected via `engine: chan`.
- [x] Vendored chan core (`services/chan_vendor/`, 52 files; runs on py>=3.11 + pandas).
- [x] `ChanSignalEngine` (`services/chan_signal_engine.py`): drives the core in step
  mode with `macd_algo:area` + the proven loosened config, reads segment buy/sell
  points of type `2` (二买/二卖) and `2s` (类二买/类二卖) via `get_seg_bsp()`, emits
  long/short. TDD-proven on a committed real 1m-gold fixture.
- [x] Engine-type plug in `StrategyRegistry.signal_engine()` (lazy import; the 3.9
  MA jobs never load pandas).
- [x] Cracked 3 fork quirks: csvAPI CWD/missing-file, class-level caches across
  constructions, and `CChanConfig` mutating its input dict.
- **Key finding:** segment 二买 are sparse on gold; needs **1m** data + loosened
  `max_bs2_rate:1.0`, `divergence_rate:1.1` (→ ~42 二买/类二买 over 14d 1m gold).
- [x] **Integration DONE — live on 1m gold.**
  - 1m XAUUSDT feed (`binance_usdm_1m_feed`) + resilient chunked backfill
    (`pipelines.backfill_gold_1m`, retry/pause/resume) → ~248k (GOLD,1m) bars
    (2025-12-11 → now). The strategies CLI refreshes the 1m tail each cycle.
  - Per-strategy `timeframe` on the registry; `run_daily_pipeline` fetches +
    signals on the strategy's timeframe (chan → 1m, gold_5m_v1 unchanged at 5m);
    `KlineClient` serves GOLD non-5m from the local store.
  - `gold_1m_chan` block (`engine: chan`, `timeframe: 1m`, `enabled: true`,
    `signal.fetch_bars: 7200`).
  - `strategies` launchd job switched to Python 3.13 (env
    `TRADING_ORCHESTRATOR_STRATEGIES_PYTHON`); runner/daily-review/dashboard stay 3.9.
  - **Real per-strategy paper fills:** `data_source_preflight` + `PaperExecutor`
    clean-bar reads generalized to the strategy's timeframe (1m), so a chan 二买
    → 开多 ticket actually fills (costs modelled), marks-to-market on 1m, and shows
    real evolving P&L on the leaderboard. GOLD/5m behaviour byte-identical.

### Phase 3 — Multi-strategy parallel + isolated state (INFRA ENABLED)
- [x] `MultiStrategyRunner` (`services/multi_strategy_runner.py`) + CLI
  (`pipelines/strategies.py`): runs every enabled strategy into its OWN
  `outputs/strategies/<id>/` namespace (signals → tickets → trades → positions →
  equity → performance → reconciliation), sharing only the market DB. Built as a
  NEW path — the legacy global cycle, dashboard, and health/alerting are untouched.
- [x] Isolation gate verified: no per-strategy service writes account state to a
  global path that bypasses `output_root`.
- [x] Per-strategy `starting_equity` on the registry (defaults equal).
- [ ] Broker-execution adapter seam (order/fill/reconcile interface), dormant —
  deferred until the live decision (a few months out).
- **DoD:** ✅ isolation proven with two DIVERGENT strategies (long vs watch):
  separate namespaces, no cross-write, no global leak, and each account
  independently passes `accounting_invariant` (tests + live single-strategy run).
- **Note:** "point dashboard/health at per-strategy namespaces" = Phase 4
  (the leaderboard IS the per-strategy dashboard).

### Phase 4 — Leaderboard + drill-down (DONE)
- [x] `StrategyLeaderboard` (`services/strategy_leaderboard.py`): scans every
  `outputs/strategies/<id>/` namespace, computes return / max drawdown / win rate
  / profit factor / net PnL / trades / vs-gold-benchmark / Sharpe, ranks by return.
  Built by `MultiStrategyRunner`, exposed in the dashboard API.
- [x] Dashboard **策略对比** tab: ranked leaderboard table (color-coded return /
  vs-gold).
- [x] Drill-down: clicking a strategy row fetches `/api/dashboard?strategy=<id>`
  (a sanitized param that scopes `DashboardState` to that namespace — reuses the
  whole battle-tested per-bar MTM nav-curve) and renders its NAV-vs-gold chart
  inline. Path-traversal guarded.
- **Freshness note:** leaderboard reflects the last `pipelines.strategies` run; to
  keep it live it needs a cadence (small launchd job / additive cycle step → Phase 5).
- **DoD:** ✅ ranked comparison of all running strategies, each drillable
  (live-verified single strategy; ranking + isolation proven by tests).

### Phase 5 — Ops hardening (DONE — except two decision-gated items)
- [x] **Leaderboard freshness**: `com.wendy.trading-orchestrator.strategies`
  launchd job (`schedule_manager`) runs `pipelines.strategies --paper-auto-approve`
  every 300s → per-strategy namespaces + leaderboard stay live. Installed + verified.
- [x] **Daemon restart-safety**: `CodeReloadGuard` (`services/code_reload.py`) +
  a watcher thread in `dashboard_server` — on any `services/`/`pipelines/` change
  the daemon exits and launchd respawns it with fresh code (≤30s). Verified live
  (PID changed on a file touch). Kills the stale-code bug that bit us twice.
- [x] **Secrets audit**: `services/secrets_audit.py` — redacted posture report
  (configured/placeholder per key, never values), live.env 0600 + gitignore
  checks. Wired into `HealthCheck` (`secrets_posture`) → badge + alerting cover it.
- [x] **Queryable run history**: `services/run_history.py` — append-only JSONL,
  one record per cycle (`pipelines.run_history --recent/--since/--state`). Wired
  into the runner. (Deliberately NOT the JSON→DB rewrite — that stays deferred.)
- [ ] **Paper/live env separation** — DECISION-GATED. Should be built *with* the
  live broker integration (Phase 3's dormant seam), not before; building it now
  risks the same premature-abstraction trap as the existing live scaffold. Defer
  until the live decision lands.
- [ ] **Market-view source dependency hardening** — DECISION-GATED for live. The
  daily pipeline syncs `market_views/*` from an Obsidian vault before strategy
  gating; live deployment must make `TRADING_ORCHESTRATOR_OBSIDIAN_ROOT`, note
  freshness, and missing/stale-vault behavior explicit infra dependencies so a
  stale local vault cannot silently change gate outcomes.
- [ ] **Containerize / off Mac launchd** — DECISION-GATED. Requires a deployment
  target (Docker? Linux server? cloud VM? stay on Mac?). Can't be completed
  without that choice; it's a substantial separate effort (Dockerfile, process
  manager, secrets injection, persistent volumes). Awaiting your deployment call.

## 6. Honest tradeoff (chosen, not a surprise)

With backtest deferred, **forward paper-testing is the only evaluation channel.**
50 GOLD-correlated strategies on forward data need **weeks of real time** before
the leaderboard has statistically meaningful signal. This long feedback loop is
an accepted constraint of the "no backtest now" decision.
