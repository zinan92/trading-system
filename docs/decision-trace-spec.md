# Spec: Per-candle decision trace (task "B")

Hand-off spec for building the decision-trace subsystem. Self-contained — an
implementing agent should not need the originating chat. Verify every file/line
reference against the current code before relying on it.

## 1. Objective

Record, for **every K-line / decision point**, the full ordered path the system
took — which gates passed (✅), which failed (❌), and the final outcome — as a
replayable JSON artifact. Goal (owner's words): *"exactly at each K-line, what
decision did we make and why."*

Only **executed** tickets are pushed to Feishu (already implemented — see
`TradeTicketNotifier._is_executed`). The full trace — including filtered,
pending, and rejected candles — is **system-side only**, for
replay/backtest/debugging.

## 2. What already exists (reuse, don't rebuild)

The raw material is present but scattered across per-cycle artifacts:

- `outputs/signals/{date}.json` — every emitted signal (actionable + `no_signal`
  / `watch`); carries `signal_id`, `regime`, `strength`, `confidence`,
  `generated_at`, `expires_at`, `status`, and the close it was keyed on.
  Signal ids are `sha256(macd:sym:date:direction:CLOSE)` — one per candle as the
  close moves (see `services/macd_signal_engine.py` / `services/signal_engine.py`).
- `services/risk_engine.py::RiskEngine.generate_ticket` — the pre-ticket gate
  chain. On rejection it populates `self.last_rejection` with structured
  sub-gates: `signal_gate` (`direction_passes` / `strength_passes` /
  `confidence_passes`), `market_data_gate`, `trade_quality` (`passes`, `reasons`,
  price-move / equity / reward-to-risk fields), and a top-level `reason`.
  Thresholds come from `services/config_loader.load_risk_rules()['default']`
  (`min_signal_strength=60`, `min_confidence=55`, `risk_reward_min=1.5`,
  `max_loss_pct=0.5`; `trade_quality.effective_leverage`, etc.).
- `outputs/risk_blocks/{date}.json` — persisted pre-ticket rejections (written by
  `services/writers.py`).
- `outputs/trade_tickets/{date}.json` — tickets that passed ALL pre-ticket gates
  (full `trade_quality` with the actual passing values + `generated_at` +
  `latest_price`).
- `outputs/journal_decisions/{date}.json` — terminal decisions
  (`decision_status` ∈ executed_paper / executed / rejected / skipped) + `notes`
  (the auto-resolver writes the reason here).
- `services/cycle_audit.py::CycleAudit` — ALREADY stitches a per-cycle view
  (`no_ticket_reasons` with `reason_code`/`source`/`reason`/`risk_block`,
  `decision_snapshots`, execution status) and has a cycle-id helper
  (`_pending_cycle_id`). Strongly consider extending this rather than duplicating.
- `services/trade_ticket_notifier.py::TradeTicketNotifier._build_view` — already
  computes the **flow node list** for the card (信号闸门 / 盈亏比·质量 / 账户风险 /
  自动批准, with per-gate `detail` strings). The trace adds the upstream
  数据质量闸门 between signal and quality while reusing the card's ticket-stage
  detail formatting so card and trace stay consistent.
- Cycle results (`pipelines/bot.py::run_bot_cycle`,
  `services/multi_strategy_runner._run_one`) now return `auto_resolution`
  (executed/rejected + reasons) and `stale_pending_sweep`.

## 3. Deliverable

A `services/decision_trace.py::DecisionTrace(output_root)` with
`build(run_date, strategy_id="") -> list[dict]` that reads the artifacts above
and writes **one record per evaluated candle/signal** to
`outputs/decision_traces/{run_date}.json` (+ `current.json`). Record schema:

```json
{
  "run_date": "2026-07-04",
  "strategy_id": "gold_1m_macd",
  "cycle_id": "<from cycle_audit or a stable per-candle id>",
  "evaluated_at": "2026-07-04T08:15:00+00:00",
  "asset": "GOLD",
  "signal_id": "sig_macd_gold_20260704_...",
  "signal": { "regime": "macd_golden_cross", "strength": 66, "confidence": 69, "close": 4183.27 },
  "gates": [
    { "step": 1, "name": "信号闸门",    "passed": true,  "detail": "强度 66 ≥ 60 · 置信 69 ≥ 55 · 方向 做多" },
    { "step": 2, "name": "数据质量闸门", "passed": true,  "detail": "305 根 K 线 · 缺失 0% · 合成 0%" },
    { "step": 3, "name": "盈亏比 / 质量闸门", "passed": true,  "detail": "盈亏比 2.0 ≥ 1.5 · 目标涨幅 +4%" },
    { "step": 4, "name": "账户风险闸门", "passed": false, "detail": "账户止损 0.64% > 上限 0.5%" },
    { "step": 5, "name": "自动批准闸门", "passed": null,  "detail": "未评估（前置已失败）" }
  ],
  "first_fail_step": 4,
  "outcome": "filtered",            // filtered | pending | rejected | executed
  "outcome_reason": "estimated account stop risk 0.64% exceeds max loss 0.50%",
  "source_artifacts": { "signals": "...", "risk_blocks": "...", "trade_tickets": "...", "journal_decisions": "..." }
}
```

Rules:
- `passed`: `true` / `false` / `null` (not evaluated because a prior gate failed).
- `first_fail_step`: the ✅…✅❌ boundary (null if all passed).
- `outcome`: `filtered` = never became a ticket (failed a pre-ticket gate);
  `pending` = became a ticket but has no executed/rejected decision evidence;
  `rejected` = became a ticket then auto-rejected (safety/pending[1+]/stale);
  `executed` = auto-executed.
- Reuse ticket-stage gate `detail` formatting from `_build_view`. Trace gate
  names are the five-step QA path: 信号闸门 / 数据质量闸门 / 盈亏比 / 质量闸门 /
  账户风险闸门 / 自动批准闸门.

## 4. Integration

- Instantiate + `build()` each cycle, per namespace, AFTER the pipeline +
  auto-resolver have run, so decisions are final: call it in
  `pipelines/bot.py::run_bot_cycle` (global, base `output_root`) and
  `services/multi_strategy_runner._run_one` (per-strategy, `scoped`) — same two
  integration points the resolver/sweep use.
- Do NOT change execution behavior; this is observability only.
- Decide MVP scope for "every candle": either trace every row in
  `outputs/signals/{date}.json` (includes `no_signal`/`watch`), or only
  actionable signals. Recommend: trace **every actionable signal**
  (direction ∈ long/short) for v1; note `no_signal` candles are out of scope.

## 5. Acceptance criteria (TDD)

1. Candle failing at gate N → trace shows ✅ for 1..N-1, ❌ at N, `null` after,
   `first_fail_step=N`, `outcome` = filtered (pre-ticket) or rejected
   (post-ticket), `outcome_reason` set from `last_rejection`/`journal_decisions`
   notes.
2. Executed candle → all gates ✅, `outcome=executed`, `first_fail_step=null`.
3. Ticket that passed pre-ticket gates but lacks executed/rejected evidence →
   `outcome=pending`, never `rejected`.
4. Gate names/details match the Feishu card's `_build_view` for the same ticket.
5. Written to `outputs/decision_traces/{run_date}.json` (+ `current.json`);
   append/merge-per-cycle (don't clobber earlier cycles the same day).
6. Pure observability: full suite stays green; no execution path changes.

## 6. Non-goals (later, separate)

- Dashboard replay UI over the traces.
- Backfilling historical traces for past dates.
- Tracing all `no_signal` candles (v2). Data-quality-blocked `watch` signals are
  traced because they represent a concrete blocked开单 path.

## 7. Context pointers

Related recent work (branch `feat/autonomy-remaining`, merged/pending on `main`):
- `services/pending_auto_resolver.py` — `resolve_pending_cycle` (execute/auto-reject
  every pending ticket) + `sweep_stale_pending` (close prior-date orphans).
- `services/trade_ticket_card.py` + `TradeTicketNotifier._build_view` — the
  flow-structured card whose gate list this trace should mirror.
- `services/risk_monitor.py` — learning-state warn no longer blocks auto-approve.
