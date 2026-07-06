# Decision Log

## Dualtrack DT6 Synced Replay

Date: 2026-07-05

### Decisions

- Implement DT6 as a sibling replay surface: `dashboard-dualtrack-replay.html?layout=dualtrack&cycle=<cycle_id>`.
  - Rationale: the existing `dashboard-replay-v4.html` is a single-trade strategy replay. A sibling page keeps that stable while giving dualtrack a purpose-built two-pane review surface.
  - Evidence: `dashboard-dualtrack-replay.html`, `dashboard-dualtrack-v5.html`.

- Treat closed attribution as the only source of machine fill detail.
  - Rationale: machine fills are intentionally blind during the cycle. A replay page that falls back to the machine endpoint would bypass the blind protocol.
  - Evidence: DT6 page fetches `GET /api/dualtrack/attribution/{cycle_id}` and does not call the intraday machine endpoint.

- Add fills to the closed attribution payload.
  - Rationale: DT6 needs both track stats and exact fill timestamps/prices to render synchronized markers. The closed attribution artifact is already the safe reveal boundary.
  - Evidence: `services/dualtrack_scoring.py`.

- Keep one shared cursor for both panes.
  - Rationale: the user is comparing behavior at the same market moment, not separately browsing two histories.
  - Evidence: `cursorRange`, `setCursor`, and `renderPane("human"/"machine")` in `dashboard-dualtrack-replay.html`.

- Make the DT5 "查看完整回放" link point directly to DT6 with `layout=dualtrack&cycle=...`.
  - Rationale: closed-cycle review should be one click from the console's attribution section.
  - Evidence: `dashboard-dualtrack-v5.html`.

### Gotchas

- Do not add `/api/dualtrack/machine` or `/api/dualtrack/human` as a replay fallback. The page must fail closed when attribution is missing.

- Existing attribution files created before DT6 may not contain `fills`. The page tolerates empty fills, but useful dual replay starts with cycles closed after the DT6 payload change.

- The current market-bars endpoint is read-only latest-bars oriented. DT6 filters those bars to the closed cycle window and falls back to a display-only synthetic path when no matching bars are present.

- The replay page is static, so D6-1 is enforced by both sides: frontend only asks for attribution, and backend attribution remains unavailable until close.

- Keep the page no-cache. Reviewing a just-closed cycle with cached JS or HTML can look like a data leak or a missing-fill bug.

- Real-data verification found four bugs after green tests: phantom stops from floor/stop/rung-bottom drift, inverted risk from budget-sized rungs, open-ended range bounds crashing human orders, and live scoreboard feedback making closed-cycle re-runs arm a trend leg that was not armed at cycle start. Keep owner replay checks in the release loop before trusting cash-flow surfaces.

- Open-ended plans mean missing bounds are open, not invalid. Long plans may have only a floor, short plans may have only a ceiling, and out-of-plan checks must enforce only the bounds that exist.

- Machine fills are a per-cycle recomputation artifact. Every intraday or auto run must replace the machine fill file for that cycle; append-only runner logs belong under runner/audit, not fills.

- The trend gate is a cycle-start invariant. Freeze the scoreboard-derived armed flag during pre-cycle and preserve it through intraday, close, scoring, and replay; a cycle's own close must never feed back into that same cycle's machine result.

## DualTrack DT9 Chart Data Integrity P0

Date: 2026-07-06

### Decisions

- Resolve the dashboard market database path relative to the repo root when the configured path is relative.
  - Rationale: the dashboard server may run from a different working directory; relative SQLite paths must still point at the configured repo-local market database instead of silently reading an empty or wrong file.
  - Evidence: `services/dualtrack_market_feed.py` and `tests/test_dualtrack_market_feed.py::test_dualtrack_market_feed_resolves_relative_config_db_from_repo_root`.

- Treat an explicit `symbol=GOLD&timeframe=1m` request as ready real data when matching fresh rows exist.
  - Rationale: a correctly specified request must not silently fall through to a different symbol or synthetic fallback while fresh real rows are present.
  - Evidence: `tests/test_dualtrack_market_feed.py::test_dualtrack_market_feed_requested_gold_one_minute_returns_real_rows`.

- Add a top-level `is_synthetic` and `quality_flags` contract to the market-bars response.
  - Rationale: the frontend should not string-guess hidden bar metadata to know whether a chart is fake; it needs an explicit, stable safety flag.
  - Evidence: `services/dualtrack_market_feed.py`, `tests/test_dualtrack_market_feed.py`, and `tests/test_dashboard_dualtrack_static.py`.

- Render a visible chart-level warning whenever the market payload is synthetic or missing.
  - Rationale: if the backend ever falls back to display-only seed data, the owner must see "模拟数据 · 非真实价格" on the chart itself before making a manual trading decision.
  - Evidence: `dashboard-dualtrack-v5.html` and `tests/test_dashboard_dualtrack_static.py`.

### Gotchas

- Current P0 does not decide the future default venue. It only enforces that real, fresh data beats synthetic fallback. Choosing Tiger/MGC versus Binance/GOLD as the long-term default remains a product decision once Tiger/MGC has proven continuous live coverage.

- Synthetic data can come from either `source_mode=synthetic_fallback`, a synthetic provider string, or `quality_flags` containing `synthetic_seed`. Frontend checks all three plus the explicit `is_synthetic` flag for backward compatibility.

- A missing market payload still renders the warning because the page otherwise uses frontend seed candles. Loading failures should be visibly unsafe, not silently chart-like.


## DualTrack DT9 Decision Page P1 Cleanup

Date: 2026-07-06

### Decisions

- Remove Tiger venue and connector onboarding panels from `dashboard-dualtrack-v5.html`.
  - Rationale: the dual-track decision page should focus on chart, plan, machine track, human track, risk, and replay/ledger context; operator onboarding state belongs on ops surfaces.
  - Evidence: `dashboard-dualtrack-v5.html` no longer contains `venueCard`, `connectorCard`, `/api/dualtrack/venue/tiger`, `/api/connectors`, `老虎`, `TIGER`, or `CONNECTOR`.

- Keep backing API endpoints and services untouched for P1.
  - Rationale: this phase is a page-scope cleanup, not an ops backend deletion; other tooling can continue to consume those read models.
  - Evidence: P1 commit `c4b1539` changed only `dashboard-dualtrack-v5.html`.

- Replace positive UI assertions for OPS/connector copy with a negative static contract.
  - Rationale: future changes should fail tests if this decision page reintroduces venue onboarding/admin content.
  - Evidence: `tests/test_dashboard_dualtrack_static.py::test_dualtrack_v5_removes_ops_connector_panels_from_decision_page`.

### Gotchas

- P1 deliberately leaves connector and Tiger APIs in `pipelines/dashboard_server.py` and service modules. Removing or relocating those endpoints is outside this phase.

- The forbidden words still appear in static tests as negative assertions; the contract is that `dashboard-dualtrack-v5.html` does not contain them.

- Do not start P2 polish until this P1 surface cleanup is accepted or explicitly continued.

## DualTrack DT9 Decision Page P2 Polish

Date: 2026-07-06

### Decisions

- Increase the 15m/1h context chart viewport and rendered height from 96/100px to 150px.
  - Rationale: the operator needs to read floor/high/low labels and the trend line without zooming or guessing.
  - Evidence: `dashboard-dualtrack-v5.html` uses `viewBox="0 0 460 150"` and `.context-body{height:150px;display:block}`.

- Enlarge mini-chart annotations and trend styling without changing the market-data path.
  - Rationale: P2 is a visual-readability pass; timeframe aggregation and backend payload selection were already handled before this phase.
  - Evidence: `drawMini()` keeps the same candle inputs while using larger labels, a wider plot area, and a thicker trend polyline.

- Rebuild plan cards as grouped label/value blocks.
  - Rationale: direction/range, key levels, invalidation, and confidence/source are different operator decisions and should not be compressed into one raw inline row.
  - Evidence: `planBody()` now renders `.plan-grid` and `.plan-metric` groups for `方向 / RANGE`, `关键位`, `失效条件`, and `信心 / 来源`.

### Gotchas

- P2 must not alter source selection, timeframe derivation, default venue, or any trading/action endpoint. This pass is presentation only.

- The AI card can still be hidden during the blind window; the grouped layout applies when the AI plan is revealed or rendered in the top plan strip.

- The main-chart blank-space check should be done in a browser because the original symptom was visual layout, not a backend invariant.

## Tiger Connector Current Authorization Gate

Date: 2026-07-06

### Decisions

- Treat the current Tiger price-feed lane as ready for an explicit attended config switch, not as already switched.
  - Rationale: the owner value is a clean same-venue MGC price source, but the system must still preserve an auditable human gate before runtime config changes.
  - Evidence: `outputs/connector_config_apply/status_current.json` reports `operator_stage.stage=ready_for_attended_config_switch`, `can_switch_config_with_operator_authorization=true`, `runtime_switched_to_tiger_mgc=false`, `can_trade_machine_track=false`, and `can_submit_tiger_orders=false`.

- Keep the price-feed refresh runbook dormant once fresh evidence exists.
  - Rationale: after acceptance is fresh, the dashboard should show that no price-feed refresh is required instead of encouraging another QuoteClient acceptance run.
  - Evidence: `outputs/connector_config_apply/price_feed_refresh_runbook_current.json status=not_required`, `command_sequence=[]`, and `refresh_window_gate.status=not_required`.

- Keep dashboard/API runbooks redacted even while local Markdown/JSON artifacts can contain terminal commands.
  - Rationale: browser-facing surfaces should inform the operator without becoming execution controls for config writes or broker actions.
  - Evidence: `build_connector_price_feed_refresh_runbook_response()` and `build_tiger_paper_order_refresh_runbook_response()` return no `python3 -m` command text, `endpoint_safety.submits_orders=false`, and `endpoint_safety.writes_runtime_config=false`.

### Gotchas

- `99.99%` progress means the price-feed switch package is ready for explicit human authorization. It does not mean the runtime has been switched to Tiger/MGC.

- `status=dry_run_ready` at the top level is expected while `operator_stage.stage=ready_for_attended_config_switch`; the latest apply receipt is still dry-run because no attended config write has been authorized.

- The price-feed gate can become stale again with time. If that happens, the system should return to `price_feed_evidence_stale_refresh_acceptance` without implying a code regression.

- Tiger paper-order readiness remains blocked and separate. Passing price-feed acceptance must not be used to submit, cancel, or close Tiger orders.
