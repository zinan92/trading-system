# Brief: PM Command Center (task "front door")

Hand-off brief for building the one-page PM Command Center. Self-contained — an
implementing agent should not need the originating chat. Verify every file/line
reference against the current code before relying on it.

## 1. Objective & product decision

Build **one** new frontend artifact: a single front page that answers, in under
10 seconds, without opening any other artifact:

1. Can the bot run paper now?
2. What blocks it?
3. What changed since last review?
4. Which strategy needs attention?
5. What is the next safe action?

This is **not an 8th dashboard** — it is the single front door. The repo root
already has 7 `dashboard-*.html` files plus `mockups/daily-loop-*.html`; all
existing pages are demoted to deep-link targets. `/` should redirect to the
command center.

Why this artifact (grounded in code, see §2):

- Q1/Q2 already have a backend source (`trade_permission` via
  `/api/system/status`) that no page leads with — 60% of this build is
  composition, not construction.
- Q3 (since last review) and Q5 (next safe action) have **no data source
  anywhere** in the repo. This page creates both.
- Decision traces (the new capability) have **no API and no rollup** — only
  per-day JSON files. The command center's central component (decision funnel)
  is their minimal productization.
- The verdict display is **fail-closed** — a product-level hedge against the
  recurring daily-loss-guardrail fail-open class (NaN / local-vs-UTC run_date /
  stale artifact).

Scope freeze: GOLD/XAUUSDT only, paper/demo/dry-run only. No multi-asset, no
live-money UI, no fee talk.

## 2. What already exists (reuse, don't rebuild)

### 2.1 Trade permission & health — answers Q1/Q2

`pipelines/dashboard_server.py`:

- `_trade_permission` (line ~563): returns `status` in
  `READY_TO_TRADE | BLOCKED_* | PAUSED_POSITION_LIMIT | DEGRADED`, plus
  `headline` / `headline_zh`, `primary_blocker`, `blockers[]`
  (`{status, code, source, message}`), `allows_new_order_if_signal`.
  Priority order: operator HALT → hard-down health rows → demo/reconciliation
  blocker → live-money guardrail → position limit → warn rows (DEGRADED).
- `_system_health_model` (line ~509): vitals rows for `data_feed`,
  `strategy_evaluation`, `runner_liveness`, `execution_blocker`,
  `tp_sl_coverage`, `always_on`; overall `up|warn|down`. **Note: a missing
  vital defaults to `warn`, not `down`** — the command center verdict must be
  stricter (see §3.3).
- Contracts: `build_system_status_contract` (~371, `system-status-v1`),
  `build_trader_overview_contract` (~390, `trader-overview-v1`),
  `build_ops_status_contract` (~438, `ops-status-v1`).
- Routes: `/api/system/status`, `/api/trader/overview`, `/api/ops/status`,
  `/api/replay`, `/api/dashboard`. Served pages are whitelisted at lines
  ~47-54. Local gateway `http://127.0.0.1:8766/…`, public
  `https://goldbot.park-ai-intel.com/…` (lines ~30-31).

### 2.2 Decision traces — feeds the funnel (Q4 + trace drill-down)

`services/decision_trace.py` (`DecisionTrace`), spec in
`docs/decision-trace-spec.md`:

- Persisted at `{output_root}/decision_traces/{run_date}.json` (per-day array)
  plus `current.json` (full copy of last-built day). Global root `outputs/`
  (strategy_id `top_level`, emitted from `pipelines/bot.py::run_bot_cycle`
  ~line 68) and per-strategy roots
  `outputs/strategies/{strategy_id}/decision_traces/` (emitted from
  `services/multi_strategy_runner.py::_run_one` ~line 307). 21 strategy dirs
  exist today.
- Record schema (`_record`, lines ~100-143): `run_date`, `strategy_id`,
  `cycle_id`, `evaluated_at`, `asset`, `signal_id`,
  `signal {regime, strength, confidence, close}`, `gates[]`,
  `first_fail_step`, `outcome`, `outcome_reason`, `source_artifacts{…}`.
- `outcome` ∈ `filtered | pending | rejected | executed`; `pending` means the
  ticket reached the auto gate but there is no executed/rejected decision
  evidence yet.
- Gate node: `{step: 1-5, name, passed: true|false|null, detail}`. Trace names:
  信号闸门 / 数据质量闸门 / 盈亏比 · 质量闸门 / 账户风险闸门 / 自动批准闸门. `passed: null`
  means not evaluated (detail `未评估（前置已失败）` or `等待自动清算`).
- The card flow still comes from `TradeTicketNotifier._build_view`; the trace
  inserts 数据质量闸门 between the card's signal and quality gates.
- Merge is an upsert keyed `{strategy_id}|{signal_id}|{cycle_id}`
  (~lines 352-370); repeated cycles the same day are safe.
- **There is no API over traces and no rollup/index** — only the raw files
  plus per-strategy `decision_trace_status/count/artifact` fields inside
  `outputs/strategies/summary_{date}.json` / `summary_current.json`.

### 2.3 Infrastructure conventions

- Atomic writes: `services/journal_store.write_json` (tempfile + `os.replace`,
  `ensure_ascii=False`). Use it for every new artifact.
- Frontend pattern: single-file HTML, no build step, no external CDN (pages
  must work over stdlib `http.server` locally AND the goldbot public domain).
  Reference peers: `dashboard-v4.html`, `ops-dashboard.html`,
  `dashboard-replay-v4.html`.

## 3. What to build — backend

### 3.1 New module `services/command_center.py`

Pure composition + rollup. Public entry:
`build_command_center_contract(payload) -> dict` where `payload` is the same
dashboard payload the existing contract builders receive.

Also a pure rollup function over trace files:
read `outputs/decision_traces/{run_date}.json` and each
`outputs/strategies/{id}/decision_traces/{run_date}.json`; produce funnel
counts by `outcome` and gate hotspots by `first_fail_step`. Missing/corrupt
file for one strategy must not fail the whole contract (degrade that row,
see §6).

### 3.2 Contract `command-center-v1` (12 top-level fields)

```
generated_at, run_date,
verdict,                     // RUN | DEGRADED | BLOCKED | UNKNOWN — see 3.3
headline_zh,                 // reuse trade_permission.headline_zh
blockers[ {code, severity, message, source_artifact, action} ],
freshness{ last_cycle_at, data_lag_s, stale },
funnel{ signals, filtered, pending, rejected, executed },
gate_hotspots[ {gate, count} ],          // top blocking gates by first_fail_step
strategies[ {id, tf, funnel_today, pnl_today, trace_status, attention} ],
since_review{ last_review_at, new_traces, new_blockers, resolved_blockers },
next_action{ kind, label, target },      // kind: none | fix | review
links{ ops, replay, traces }
```

Field sources: `verdict/headline_zh/blockers` ← `trade_permission` + health
rows (blockers gain `source_artifact` — the file/endpoint the evidence lives
in — and exactly one `action` deep link). `strategies` ←
`summary_current.json` rows (incl. existing `decision_trace_status/count`).
`funnel/gate_hotspots/strategies.funnel_today` ← new trace rollup.
`since_review` ← new review marker (§3.4). `next_action` ← §3.5.

### 3.3 Verdict derivation — fail-closed (the one hard product rule)

```
UNKNOWN  if contract inputs are stale (generated_at older than STALE_AFTER_S),
         OR trade_permission/health payloads missing/unparseable,
         OR any required field is absent or non-finite (NaN guard)
BLOCKED  if trade_permission.status startswith BLOCKED_ or is PAUSED_POSITION_LIMIT
DEGRADED if status == DEGRADED
RUN      only if status == READY_TO_TRADE AND freshness.stale == false
```

`STALE_AFTER_S`: config constant (module-level, not hardcoded inline);
default 300s but the implementer must sanity-check against the actual runner
cadence (1m strategies) and document the chosen value. UNKNOWN renders as
not-runnable. **The UI must never show RUN on stale or missing data** — this
is the product-level hedge for the daily-loss fail-open class and must have
regression tests (§7).

### 3.4 Review marker

`outputs/review_marker.json`:
`{ last_review_at, snapshot: { trace_count, blocker_codes[] } }`, written
atomically via `journal_store.write_json` on `POST /api/review-marker`.
`since_review` = diff between current state and snapshot: `new_traces`
(count delta), `new_blockers` / `resolved_blockers` (set diff on codes).
No marker file yet → `last_review_at: null`, deltas computed against empty.

### 3.5 `next_action` derivation (owner's decision taxonomy)

There is **no manual-approve middle state**. Mapping:

- Safety-class blocker present → `kind: "none"`, label states the auto-reject
  already happened (e.g. 「安全闸门已自动拦截，无需人工动作」).
- Plumbing-class blocker (health down, stale data, reconciliation) →
  `kind: "fix"`, `target` = the blocker's `source_artifact` / ops deep link.
- Nothing blocked → `kind: "review"` pointing at the most attention-worthy
  strategy, or `none` with 「等待下一张合格信号」.

### 3.6 New routes in `pipelines/dashboard_server.py`

- `GET /api/command-center` → `build_command_center_contract`.
- `GET /api/decision-traces?strategy=&date=&limit=` → reads the matching
  `decision_traces/{date}.json` (global root when `strategy` empty or
  `top_level`), newest-first, `limit` default 20.
- `POST /api/review-marker` → writes marker (§3.4), returns the new
  `since_review` object.
- Add `command-center.html` to the served-pages whitelist; make `/` redirect
  to it.

## 4. What to build — frontend `command-center.html`

Single file, no build, no CDN. 1440-wide dark layout, one screen, top-down:

1. **VerdictBand** (full width; the only large type on the page): four states
   RUN 绿 / DEGRADED 琥珀 / BLOCKED 红 / UNKNOWN 灰; shows `headline_zh`,
   `run_date`, freshness stamp, and the 「标记本次复盘」 button (right).
   UNKNOWN copy: 「状态不新鲜，按不可跑处理」.
2. **BlockerStack** (left column, 1/3): severity-ordered cards — one
   sentence + class tag (安全/管道) + exactly one action button.
3. **DecisionFunnel + GateHotspots** (right top): horizontal 5-segment bar
   信号 → filtered → pending → rejected → executed, plus chips for today's top
   blocking gates.
4. **StrategyTable** (right bottom): 21 dense rows — 策略 / 周期 /
   今日信→执 / 当日PnL / trace状态 / 注意力 — sorted by attention.
5. **SinceReviewBar + NextActionCard** (bottom strip): 「新 trace N 条 ·
   新增阻塞 X · 已解除 Y」 + the derived next safe action.

Components: `VerdictBand / BlockerStack / DecisionFunnel / GateHotspots /
StrategyTable / TraceDrawer / GateWaterfall / SinceReviewBar /
NextActionCard`.

Visual language: cool gray-blue base, one accent color, **color expresses
state only** (never decoration); all numerals `font-variant-numeric:
tabular-nums` monospace; 12–13px density; no hero sections, no marketing
whitespace. Premium, calm, dense, operational.

## 5. Interaction model

- **Click a blocker** → right-side evidence drawer: `message` + excerpt of
  `source_artifact` + class tag + single action button deep-linking to the
  matching `ops-dashboard.html` section. The drawer locates; it does not fix.
- **Click a strategy row** → inline expansion: today's traces for that
  strategy (via `/api/decision-traces`), each with `outcome_reason`; a 「回放」
  button deep-links `dashboard-replay-v4.html?strategy={id}&date={run_date}`.
- **Click a trace** → **GateWaterfall**: steps 1-4 with tri-state passed
  (过/拦/未评估), reusing the existing Chinese `detail` strings verbatim.
  Handle the five trace gates by step number.
- **Click 「标记本次复盘」** → `POST /api/review-marker`; `since_review`
  resets in place.

## 6. Empty / error states

- Contract fetch fails or required fields missing → full-page gray
  fail-closed banner (treated as UNKNOWN); never a partially-green page.
- Zero traces today → funnel empty state showing 0s + a "is the data feed
  running?" check link (data_feed vital), not a blank area.
- One strategy's trace file unreadable → inline error badge on that row
  only; page still renders.
- Review marker absent → SinceReviewBar shows 「尚未标记过复盘」 and offers
  the button.

## 7. Acceptance criteria

1. Opening the one page answers all 5 questions in <10s with no other
   artifact opened.
2. **Fail-closed pytest suite**: simulated stale `generated_at`, missing
   fields, and NaN inputs each force verdict ≠ RUN. This is the
   non-negotiable test.
3. Every blocker row has a `source_artifact` path and exactly one action.
4. GateWaterfall renders 4-gate and 3-gate records correctly (step-number
   fill for missing steps).
5. After marking review, `since_review` zeroes and the marker write is
   atomic (temp + replace).
6. Works with no build step over both stdlib `http.server` (port 8766) and
   the goldbot public domain; `/` lands on the command center; existing
   dashboards untouched except demotion to deep-link targets.

## 8. Non-goals

- No new data collection, no changes to gates/risk logic, no writes other
  than the review marker.
- No framework/build tooling; do not refactor `dashboard-v4.html`.
- No live-money surfaces; `paper_only` style badges are display-only today —
  do not present the command center as an execution control panel.
