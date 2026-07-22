# Decision Log

## Paper Grid Range Replacement

Date: 2026-07-21

### Decisions

- Treat chart dragging as a draft only. The final mutation is a separate `replace_grid` control action whose button is exactly `停止+平仓+撤单+交易新网格`.
  - Rationale: repeated mouse tuning must not create repeated stop/start side effects.
  - Evidence: `dashboard-gridmind.html`, `tests/test_dashboard_gridmind_range_drag.mjs`.

- Reuse the existing execution adapter, canonical risk port, deterministic grid command identity, and staged-plan activation boundary.
  - Rationale: the replacement needs stronger orchestration, not another execution engine or order schema.
  - Evidence: `services/strategy_control_plane.py`, `services/strategy_plan_execution.py`.

- Persist the candidate StrategyPlan and replacement request fingerprint before stopping the old grid.
  - Rationale: a crash after stop must leave a durable statement of what was requested and which plan/version/execution set authorized it.
  - Evidence: `StrategyControlPlane._replace_grid`, `test_replace_grid_persists_request_before_stop_attempt`.

- Bind final execution to exact active-plan ID/version, preview ID, accepted-order IDs, open-position IDs, current trusted market, account and canonical risk.
  - Rationale: any fill, cancel, plan change, price move or risk change between preview and click must reject before old-grid mutation.
  - Evidence: `StrategyControlPlane._assert_expected_execution`, `test_replace_grid_rejects_execution_drift_before_staging_or_stop`, `test_replace_grid_rechecks_risk_and_range_before_any_execution_change`.

- Activate the staged plan only after all replacement orders are visible as accepted/filled and paper reconciliation passes.
  - Rationale: runtime and active-plan identity must never claim that an incomplete replacement grid is running.
  - Evidence: `StrategyControlPlane._launch_staged_replacement`, `StrategyControlPlane._activate_staged_range_plan`.

- Make retries idempotent through one request fingerprint and deterministic command IDs. If only the final runtime write fails, retry repairs runtime without resubmitting orders.
  - Rationale: an uncertain HTTP response must not create a second grid.
  - Evidence: `test_replace_grid_stages_before_stop_then_activates_once`, `test_replace_grid_retry_repairs_final_runtime_write_without_duplicate_orders`, `test_replace_grid_same_fingerprint_recovers_after_process_crash`.

- Reject empty or duplicate identities in both the browser-provided execution set and the current server snapshot.
  - Rationale: set equality alone can hide two current records that share one order or position ID.
  - Evidence: `test_replace_grid_rejects_duplicate_ids_in_current_execution_snapshot`, `tests/test_dashboard_gridmind_range_drag.mjs`.

- On replacement launch failure, cancel/flatten only state attributed to the staged plan; foreign plan state remains untouched and visible.
  - Rationale: cleanup is not permission to mutate another strategy's paper state.
  - Evidence: `StrategyControlPlane._cleanup_staged_replacement`, `test_replace_grid_failure_cleans_only_staged_plan_state`. Cleanup deliberately does not call global Nautilus flush or inject a market event.

### Gotchas

- This action intentionally differs from the existing running edge adjustment. `extend_range` preserves live positions and spacing while adding/removing edge orders; `replace_grid` stops, flattens, cancels and rebuilds every grid level with the dragged geometry.

- A replacement preview can become invalid before the final click. The server recomputes it and rejects on preview, market, execution or risk drift; the frontend preview is never execution authority.

- `stop_failed` leaves the staged plan as a durable non-active record and preserves the old active plan identity. It is not silently promoted or treated as success.

- After the old grid has stopped, a failed new-grid launch leaves runtime visibly stopped/error. Cleanup is staged-plan-scoped; any foreign accepted order remains visible for operator intervention.

- A hard process exit can bypass Python exception cleanup. Same-fingerprint retries therefore inspect durable staging/runtime/execution state before the normal running-old-plan gate and either converge the exact staged grid or fail closed; they never create another StrategyPlan.

- The optional gstack browser binary is not built locally, but the repository's existing Playwright/Chrome acceptance harness is available. Visual evidence must therefore come from that checked-in harness, not be described as gstack evidence.

## Tiger OpenAPI Integration

Date: 2026-07-05

### Decisions

- Keep Tiger integration price-feed-first. The current Tiger work may use read-only quote/data APIs and may write imported bars plus validation artifacts, but it must not call order submission, cancel, close, or position-mutating APIs until an explicit attended trading milestone is approved.
  - Rationale: data correctness can be proven without opening broker execution risk.
  - Evidence: `services/tiger_futures_feed.py`, `pipelines/tiger_futures_feed.py`, `services/tiger_realtime_validation.py`, `pipelines/tiger_realtime_validation.py`.

- Keep the architecture split as Ports and Adapters. Market data ingestion, dashboard reads, broker execution, and activation planning stay as separate surfaces.
  - Rationale: Tiger can become one data provider and one future broker adapter without coupling the strategy engine to Tiger-specific SDK behavior.
  - Evidence: `services/broker_adapter.py`, `services/dualtrack_market_feed.py`, `services/tiger_venue_status.py`.

- Store Tiger futures data under explicit Tiger/COMEX provider identity instead of overwriting Binance or generic gold symbols.
  - Rationale: research and execution must remain venue-specific; Binance spot-like gold and COMEX futures should not be mixed silently.
  - Evidence: Tiger MGC bars are stored as `symbol=MGCmain`, `provider=tiger_openapi`, `venue=COMEX`.

- Use backend-owned market bars for the dualtrack dashboard. The frontend should call the backend market-bars API instead of connecting directly to a browser Binance websocket.
  - Rationale: the backend can enforce provider priority, database reads, and fallback behavior consistently.
  - Evidence: `GET /api/dualtrack/market/bars`, `services/dualtrack_market_feed.py`, `dashboard-dualtrack-v5.html`.

- Set dashboard source priority to Tiger/COMEX first, then cached Binance data, then an explicit synthetic display seed only when no real bars exist.
  - Rationale: the dashboard should show the target execution venue when Tiger data exists, while preserving a visible degraded fallback during development.
  - Evidence: M24/M25 dashboard reads showed `source=tiger_openapi` for `MGCmain`.

- Keep the connector UI and activation planning preview-only. Selecting Tiger in a plan may produce reviewable configuration text, but it must not silently write runtime routing config.
  - Rationale: venue changes are trading-control decisions, not ordinary UI preferences.
  - Evidence: no Tiger connector milestone writes `configs/pipeline.yaml` as an activation side effect.

- Keep Tiger credential handling outside repo artifacts. Runtime config may point to a local owner-only properties file, but generated docs, validation JSON, screenshots, and logs must not store private key material or the exact local secret path.
  - Rationale: connector work should remain auditable without leaking credentials.
  - Evidence: validation artifacts expose status and permission summaries only.

- Treat realtime validation as an observable venue-health signal, not as a market-data import. It should poll/read and write a compact validation artifact, but it should not write market bars.
  - Rationale: validating quote freshness and permission state is different from importing historical bars.
  - Evidence: `outputs/tiger_realtime_validation/current.json`.

- Surface Tiger realtime validation in the venue read model and dashboard. `pending_market_open` is informational and should not degrade a ready paper venue; `warn` or `fail` should degrade the venue status and headline.
  - Rationale: off-session checks should not create false operational failures, but stale or denied realtime quotes should block trust.
  - Evidence: `services/tiger_venue_status.py`, `dashboard-dualtrack-v5.html`, `tests/test_tiger_venue_status.py`.

- Add a separate market-hours validation gate instead of changing the default realtime validator behavior. `--require-market-hours-pass` exits 0 only for `status=pass`, exits 75 for `pending_market_open`, and exits 2 for market-hours `warn`/`fail`.
  - Rationale: manual inspection and automation need different behavior; automation needs a retry/non-pass signal, while default runs should still produce artifacts without surprising operators.
  - Evidence: `services/tiger_realtime_validation.py`, `pipelines/tiger_realtime_validation.py`, `tests/test_tiger_realtime_validation.py`.

- Surface market-hours gate state in the Tiger venue read model and dashboard without changing order controls or venue degradation rules.
  - Rationale: operators need to see the difference between "not open yet, retry later" and "market data failed" directly in the dual-track console.
  - Evidence: `services/tiger_venue_status.py`, `dashboard-dualtrack-v5.html`, `tests/test_tiger_venue_status.py`, `tests/test_dashboard_dualtrack_static.py`.

- Add a separate Tiger price-feed readiness gate that reads local artifacts and requires both historical import and market-hours realtime validation.
  - Rationale: "Tiger price feed works" should be a falsifiable gate, not an inference from one successful historical import.
  - Evidence: `services/tiger_price_feed_readiness.py`, `pipelines/tiger_price_feed_readiness.py`, `tests/test_tiger_price_feed_readiness.py`, `outputs/tiger_price_feed_readiness/current.json`.

- Surface Tiger price-feed readiness in the venue read model and dashboard without changing broker/order readiness.
  - Rationale: the operator should see that Tiger broker paper venue can be ready while Tiger price-feed promotion is still waiting for market-hours evidence.
  - Evidence: `services/tiger_venue_status.py`, `dashboard-dualtrack-v5.html`, `tests/test_tiger_venue_status.py`, `tests/test_dashboard_dualtrack_static.py`.

- Make Tiger connector catalog price-feed readiness depend on `tiger_price_feed_readiness`, not just credential presence.
  - Rationale: entering a valid API key/path proves access setup, but it does not prove the executable price feed is ready for research/dashboard promotion.
  - Evidence: `services/connector_catalog.py`, `services/connector_onboarding.py`, `services/connector_activation_plan.py`, `tests/test_connector_catalog.py`, `tests/test_connector_onboarding.py`, `tests/test_connector_activation_plan.py`.

- Add a one-command Tiger price-feed acceptance receipt above realtime validation, price-feed readiness, and connector catalog.
  - Rationale: operator acceptance should be a single artifact-backed answer: accepted, pending market open, or blocked.
  - Evidence: `services/tiger_price_feed_acceptance.py`, `pipelines/tiger_price_feed_acceptance.py`, `tests/test_tiger_price_feed_acceptance.py`.

- Surface the Tiger price-feed acceptance receipt in the existing Tiger venue read model and dashboard.
  - Rationale: operators should not have to inspect JSON files to know whether Tiger is accepted, pending market open, or blocked as a price source.
  - Evidence: `services/tiger_venue_status.py`, `dashboard-dualtrack-v5.html`, `tests/test_tiger_venue_status.py`, `tests/test_dashboard_dualtrack_static.py`.

- Expose Tiger price-feed acceptance metadata in the connector catalog without making catalog status circularly depend on the acceptance artifact.
  - Rationale: future connector onboarding UI needs accepted/pending/blocked context, but acceptance itself refreshes the catalog, so catalog `price_feed` status must continue to use the underlying readiness gate.
  - Evidence: `services/connector_catalog.py`, `tests/test_connector_catalog.py`.

- Propagate Tiger price-feed acceptance context into connector onboarding and activation preview results.
  - Rationale: operator setup should say "waiting for market-hours acceptance" with next-window evidence instead of a generic "price_feed is not ready" failure.
  - Evidence: `services/connector_onboarding.py`, `services/connector_activation_plan.py`, `tests/test_connector_onboarding.py`, `tests/test_connector_activation_plan.py`.

- Render connector onboarding blockers and activation warnings in the dual-track connector panel.
  - Rationale: the frontend should show why Tiger price-feed setup is blocked, including the market-hours acceptance next window, without requiring operators to inspect JSON artifacts.
  - Evidence: `dashboard-dualtrack-v5.html`, `tests/test_dashboard_dualtrack_static.py`.

- Add Tiger price-feed readiness as the Tiger broker feedback loop in live readiness.
  - Rationale: `tiger_openapi` was already a recognized broker provider, but live readiness must not pass broker feedback until the Tiger price-feed readiness gate has passed.
  - Evidence: `services/live_readiness.py`, `tests/test_live_readiness.py`.

- Make Tiger execution-venue data preflight depend on Tiger price-feed readiness, not just local bars.
  - Rationale: `MGCmain`/`1m` bars in SQLite are useful for research and paper visibility, but they should not become execution-grade/live-ready until the Tiger market-hours readiness/acceptance gate has passed.
  - Evidence: `services/data_source_preflight.py`, `configs/pipeline.yaml`, `tests/test_data_source_preflight.py`.

- Namespace non-default data-source preflight artifacts.
  - Rationale: checking `MGCmain`/`1m` should not overwrite the global `GOLD`/`5m` preflight current file that existing dashboards, runner gates, and risk monitors read.
  - Evidence: `services/data_source_preflight.py`, `pipelines/data_source_preflight.py`, `tests/test_data_source_preflight.py`.

- Surface the Tiger `MGCmain`/`1m` data-source preflight in the Tiger venue read model and dashboard.
  - Rationale: operators should see whether the target Tiger price series is stale, pending market-hours acceptance, or live-ready without opening JSON artifacts, while keeping broker-order readiness separate.
  - Evidence: `services/tiger_venue_status.py`, `dashboard-dualtrack-v5.html`, `tests/test_tiger_venue_status.py`, `tests/test_dashboard_dualtrack_static.py`.

- Refresh the Tiger `MGCmain`/`1m` data-source preflight as part of one-command price-feed acceptance.
  - Rationale: after an operator runs acceptance, the dashboard's `数据源预检` row should reflect the same acceptance attempt instead of a stale previous preflight artifact.
  - Evidence: `services/tiger_price_feed_acceptance.py`, `tests/test_tiger_price_feed_acceptance.py`.

- Surface Tiger price-feed acceptance operator timing as an explicit dashboard state.
  - Rationale: `pending_market_open` is not actionable enough by itself; operators need to know whether to wait, rerun acceptance now, or refresh an expired validation window.
  - Evidence: `services/tiger_venue_status.py`, `dashboard-dualtrack-v5.html`, `tests/test_tiger_venue_status.py`, `tests/test_dashboard_dualtrack_static.py`.

- Make live readiness select the market-data preflight by broker provider.
  - Rationale: once broker routing switches to Tiger, live readiness must evaluate `MGCmain`/`1m` Tiger data instead of continuing to gate on the legacy `GOLD`/`5m` Binance path.
  - Evidence: `services/live_readiness.py`, `services/data_gap_doctor.py`, `services/official_market_data_gate.py`, `tests/test_live_readiness.py`, `tests/test_data_gap_doctor.py`.

- Make live activation and live switch planning select market-data evidence by broker provider.
  - Rationale: after Tiger live readiness evaluates `MGCmain`/`1m`, the final activation/switch plan must not fall back to the legacy `GOLD`/`5m` preflight and produce a false block or false pass.
  - Evidence: `services/live_activation.py`, `services/live_switch_plan.py`, `tests/test_live_activation.py`, `tests/test_live_switch_plan.py`.

- Load local live env before connector catalog/onboarding/activation/acceptance readiness checks.
  - Rationale: operator-facing connector readiness should reflect the same local runtime credentials used by broker preflight, while still keeping credential values and file paths out of artifacts.
  - Evidence: `services/connector_catalog.py`, `services/connector_onboarding.py`, `services/connector_activation_plan.py`, `services/tiger_price_feed_acceptance.py`, `tests/test_connector_catalog.py`.

- Make Tiger/COMEX data-source freshness session-aware while market-hours acceptance is pending.
  - Rationale: before the next COMEX window, the last Friday MGC bar should not be treated as a broken/stale feed for research and paper visibility, but it still must not be live-ready until market-hours validation passes.
  - Evidence: `services/data_source_preflight.py`, `tests/test_data_source_preflight.py`, `outputs/data_source_preflight/MGCmain_1m/current.json`.

- Store operator next action directly in the Tiger price-feed acceptance receipt.
  - Rationale: CLI, dashboard, and future onboarding UI should not each reinterpret pending market-open timestamps differently; the acceptance artifact should be the single receipt that says wait, rerun now, expired, accepted, or blocked.
  - Evidence: `services/tiger_price_feed_acceptance.py`, `tests/test_tiger_price_feed_acceptance.py`, `outputs/tiger_price_feed_acceptance/current.json`.

- Make the Tiger venue read model prefer acceptance receipt `operator_next_action`.
  - Rationale: the dashboard should display the operator guidance emitted by the acceptance receipt instead of recomputing timing locally, while still supporting legacy receipts without that field.
  - Evidence: `services/tiger_venue_status.py`, `tests/test_tiger_venue_status.py`, `outputs/tiger_venue_status/current.json`.

- Propagate Tiger acceptance `operator_next_action` through connector surfaces.
  - Rationale: future platform/API-key onboarding should show the same Tiger price-feed next action in catalog, dry-run onboarding, activation preview, and browser connector blockers instead of each surface presenting a different interpretation of the same receipt.
  - Evidence: `services/connector_catalog.py`, `services/connector_onboarding.py`, `services/connector_activation_plan.py`, `dashboard-dualtrack-v5.html`, `tests/test_connector_catalog.py`, `tests/test_connector_onboarding.py`, `tests/test_connector_activation_plan.py`, `tests/test_dashboard_dualtrack_static.py`.

- Add an explicit connector activation gate before any future config-write milestone.
  - Rationale: activation preview should not only show a patch; it should tell the operator and future frontend whether the requested connector switch is blocked, preview-ready with warnings, or ready for a separate manual config-write milestone.
  - Evidence: `services/connector_activation_plan.py`, `dashboard-dualtrack-v5.html`, `tests/test_connector_activation_plan.py`, `tests/test_dashboard_dualtrack_static.py`.

- Add an artifact-only connector activation runbook.
  - Rationale: future platform switching needs an operator-readable path from blocker resolution to separate config write to post-apply verification, without letting the preview endpoint become a config writer or broker control.
  - Evidence: `services/connector_activation_plan.py`, `dashboard-dualtrack-v5.html`, `tests/test_connector_activation_plan.py`, `tests/test_dashboard_dualtrack_static.py`.

- Add an artifact-only connector config-apply approval package.
  - Rationale: future platform switching needs an auditable package that ties patch preview, operator acknowledgement, rollback requirement, and post-apply validation together before any separate config-write milestone.
  - Evidence: `services/connector_activation_plan.py`, `dashboard-dualtrack-v5.html`, `tests/test_connector_activation_plan.py`, `tests/test_dashboard_dualtrack_static.py`.

- Add a connector switch audit receipt.
  - Rationale: future frontend platform switching needs one top-level answer for whether a connector can move forward, while still separating config writes and broker-order authorization from the preview endpoint.
  - Evidence: `services/connector_activation_plan.py`, `dashboard-dualtrack-v5.html`, `tests/test_connector_activation_plan.py`, `tests/test_dashboard_dualtrack_static.py`.

- Use validation reference time for Tiger acceptance operator guidance.
  - Rationale: `operator_next_action` must be reproducible for historical `as_of` runs and artifact-only aggregation; otherwise the same pending-market-open receipt can flip from waiting to rerun-now as wall-clock time passes.
  - Evidence: `services/tiger_price_feed_acceptance.py`, `tests/test_tiger_price_feed_acceptance.py`.

- Keep future Tiger order execution behind the existing broker-adapter boundary and fail-closed runbooks.
  - Rationale: changing broker SDKs reopens known execution-path risks, especially naked-position windows, protective-order attachment, reconciliation, and daily-loss guardrails.
  - Evidence: no new Tiger network order submission path has been enabled in M23-M55.

### Gotchas

- The current Tiger permission check reports stock L1 permission only and does not yet prove futures realtime quote entitlement. Historical or recent MGC bars can import, but market-hours realtime validation still needs a successful check after COMEX opens.

- The latest realtime validation happened before the next COMEX trading window, so `pending_market_open` is expected. The next detected window starts at `2026-07-05T22:00:00+00:00`.

- Exit code 75 from realtime validation gate means "not open yet, retry later", not a Tiger SDK failure. The real pre-open M28 run at `2026-07-05T15:21:57+00:00` returned this expected 75 state and kept the Tiger venue read model `ready`.

- Dashboard `行情门禁` is an observability row only. It must not become a button, shortcut, or hidden order-control surface.

- Price-feed readiness blocked by `realtime_market_hours_gate` is expected before COMEX opens. It means the gate is waiting for market-hours evidence, not that the historical feed import failed.

- Dashboard `价格源门` is also observability only. A blocked price-feed gate must not be interpreted as permission to bypass market-hours validation or switch broker routing.

- Connector catalog can now show Tiger `broker_order=ready` while Tiger `price_feed=blocked`; that split is intentional. Broker credential readiness and price-feed promotion readiness are different gates.

- Tiger price-feed acceptance is a convenience orchestration layer, not a new source of truth. If underlying realtime/readiness/catalog artifacts disagree, acceptance must stay blocked.

- Tiger price-feed acceptance may open a read-only QuoteClient when `--skip-realtime-run` is not used. It must still never open TradeClient, write market bars, submit orders, or create an order-control endpoint.

- Tiger price-feed acceptance `operator_next_action` must be based on the validation reference time: explicit `as_of` first, loaded realtime artifact `checked_at` for artifact-only aggregation, otherwise current acceptance time. It should not unconditionally use wall-clock time.

- `tiger_price_feed_acceptance.status=accepted` means only that Tiger price feed promotion is ready. It does not authorize Tiger broker order routing, paper TradeClient submission, or real-money execution.

- Dashboard `验收收据` is display-only. It must stay a read-model summary over `outputs/tiger_price_feed_acceptance/current.json` and must not become a run button or hidden realtime/order control.

- The 2026-07-06 market-hours acceptance run passed for `MGCmain`: realtime validation advanced during the polling window, `tiger_price_feed_acceptance.status=accepted`, and scoped `MGCmain_1m` data-source preflight reported `status=pass` after refreshing local Tiger bars to `2026-07-06T05:31:00+00:00`.

- The Tiger feed preflight still reports quote permission names as `aStockQuoteLv1` and `has_futures_realtime=false`; the operational source of truth for promotion is the market-hours acceptance receipt plus fresh local MGC bars, not the permission-name label alone.

- After price-feed acceptance, Tiger connector activation for `price_feed` + `broker_order` is now previewable with no blockers.
  - Rationale: the previous market-hours price-feed blocker has been cleared; remaining operator work is config-write authorization and explicit warning review.
  - Evidence: `outputs/connector_activation_plan/current.json status=preview_ready`, `outputs/connector_config_apply/current.json status=dry_run_ready`.

- Keep Tiger/MGC config apply separate from preview even when dry-run is ready.
  - Rationale: applying the profile would rewrite local runtime config and change broker/dualtrack routing. That remains an attended config-write milestone, not an automatic consequence of price-feed acceptance.
  - Evidence: config apply dry-run reports 31 planned changes and `writes_runtime_config=false`.

- Connector catalog `price_feed.acceptance` is context, not the status source of truth. `price_feed.status` should remain based on credential readiness plus `tiger_price_feed_readiness` to avoid a circular dependency during `tiger_price_feed_acceptance` runs.

- Connector onboarding and activation may surface `tiger_price_feed_acceptance_not_ready`, but they still must not run the acceptance command, open QuoteClient, open TradeClient, write runtime config, or store credentials. They explain the gate; they do not operate it.

- Dashboard connector rows `验证阻塞` and `预览警告` are display-only summaries over the API response. They must not become submit/refresh buttons, hidden realtime checks, or any broker/order control.

- Live readiness `broker_feedback` for Tiger must read local price-feed readiness/acceptance artifacts only. It must not run Tiger acceptance, open Tiger SDK clients, or treat price-feed readiness as broker-order authorization.

- `DataSourcePreflight(symbol="MGCmain", timeframe="1m")` now has a Tiger-specific execution-venue gate. If Tiger bars exist but `tiger_price_feed_readiness.ready_for_price_feed` is not true, the result can support paper/research but must stay `ready_for_live=false`.

- Shared-output non-default preflight runs write under `outputs/data_source_preflight/<symbol>_<timeframe>/`. The flat `outputs/data_source_preflight/current.json` remains the legacy global `GOLD`/`5m` surface unless a caller explicitly passes a scoped output root.

- Tiger feed imports write SQLite bars and `tiger_futures_feed` receipts, not `clean_bars` artifacts. Data preflight must fall back to the market DB latest bar when clean bars are absent.

- Dashboard `数据源预检` is display-only. It summarizes `outputs/data_source_preflight/MGCmain_1m/current.json`; it must not run preflight, refresh Tiger QuoteClient, open TradeClient, write market bars, or authorize broker-order routing.

- `tiger_price_feed_acceptance` may refresh `outputs/data_source_preflight/MGCmain_1m/current.json`, but that refresh reads local DB/artifacts only. It must not import market bars or convert data-source live readiness into broker-order authorization.

- Dashboard `验收下一步` is also display-only. It computes wait/rerun/expired guidance from the local acceptance artifact and current time; it must not auto-run acceptance or schedule broker/feed SDK work.

- Tiger `live_readiness` now writes non-default data checks under namespaced `MGCmain_1m` artifacts. A Tiger live-readiness run must not overwrite legacy `GOLD`/`5m` `data_source_preflight/current.json` or `data_gaps/current.json`.

- Tiger `live_activation` and `live_switch_plan` must read `outputs/data_source_preflight/MGCmain_1m/current.json` when the active broker provider is `tiger_openapi`. The legacy flat `data_source_preflight/current.json` can remain `GOLD`/`5m` without deciding the Tiger gate.

- Connector catalog/onboarding/activation/acceptance may load local `configs/live.env` into process env before checking credentials. They must still return only key names, present/file-exists/owner-only booleans, and mode; they must not return credential values or the Tiger properties file path.

- Tiger `data_source_preflight` can suspend current-session freshness only when the local Tiger readiness gate is explicitly `pending_market_open` and the recorded next COMEX window has not started. Once the window starts, stale bars must block again until realtime validation/import refreshes the evidence.

- `tiger_price_feed_acceptance.operator_next_action` is guidance, not a control. It may include the next safe command string, but it must not auto-run acceptance, open QuoteClient, open TradeClient, write market bars, or authorize broker routing.

- Tiger venue/dashboard operator guidance should read `operator_next_action` from the acceptance artifact when present. Local time-based fallback exists only for backward compatibility with old receipts.

- Connector catalog/onboarding/activation/frontend blocker surfaces should pass through `operator_next_action` from the acceptance artifact as guidance. They must not turn the safe `next_command` into an auto-run button, hidden QuoteClient call, config write, or broker authorization.

- Connector activation `activation_gate` is a decision receipt, not a config writer. Even when it says `preview_ready`, it must keep `can_apply_config_from_this_endpoint=false`, `writes_runtime_config=false`, `opens_network_clients=false`, and `submits_orders=false`.

- Connector activation `activation_runbook` is also a receipt, not an executor. It may list safe follow-up commands, but it must not run them, write configs, open SDK clients, or authorize broker orders from the preview endpoint.

- Connector activation `config_apply_package` is a pre-approval artifact, not a config write. It can require an acknowledgement and backup/rollback boundary, but it must keep `can_apply_from_this_endpoint=false` and must not store credentials, expose credential values, open network clients, or submit orders.

- Connector activation `switch_audit` is a top-level read model for operator/frontend clarity. It must keep `can_switch_connector_from_this_endpoint=false`, `can_apply_config_now=false`, and `can_enable_broker_orders=false`; passing this audit can only justify opening a separate explicit config-write milestone, not performing the switch.

- `DataGapDoctor` can read SQLite market bars when clean-bars artifacts are absent, but this is still an evidence check. It must not import bars or open Tiger SDK clients.

- Tiger trading-window APIs may return duplicate or overlapping candidate windows across nearby trading dates. The validator deduplicates the next window and preserves the later trading date when needed.

- COMEX session assumptions are not 24/7. Dualtrack clocks, research windows, and freshness checks need a 23x5 session model rather than Binance-style continuous trading.

- `MGCmain` is acceptable for dashboard and research feed work, but live order placement must resolve to a concrete tradable contract such as a dated MGC contract.

- MGC contract granularity remains a strategy constraint. One MGC contract is 10 oz, so data integration does not by itself solve grid sizing for a small account.

- Any mounted or downloaded Tiger properties file may have weaker file permissions than the local runtime copy. Use an owner-only local copy for actual runs, and keep paths out of artifacts.

- SQLite store initialization can create tables. Dashboard read paths that claim to be read-only should use read-only SQLite connections and check database existence first.

- Test fixtures for dualtrack plan invalidation must keep the range boundary aligned with direction: long plans use `range.low` below invalidation; short plans use `range.high` above invalidation.

- Do not use a Tiger price source to execute Binance strategy decisions, or vice versa, without explicitly labeling it as cross-venue research. Same venue, same instrument, same fee model is required for a clean human-vs-machine comparison.

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

## Tiger/MGC Dualtrack Operational Cost Boundary

Date: 2026-07-06

### Decisions

- Add an optional dualtrack venue cost model instead of replacing the legacy bp/notional path.
  - Rationale: current GOLD/Binance-style tests and dashboards must remain stable while Tiger/MGC is introduced as a separate execution profile.
  - Evidence: `services/dualtrack_costs.py`, `services/dualtrack_grid_core.py`, `services/dualtrack_machine.py`, `services/dualtrack_human.py`.

- Represent Tiger/MGC machine rungs as whole contracts, not continuous dollars.
  - Rationale: one MGC contract is 10 oz; a `$10k` abstract rung cannot be traded directly on MGC.
  - Evidence: Tiger/MGC test fills record `contracts=1`, `quantity=1`, and `notional=price * 10`.

- Use the shared venue fee model from `configs/risk_rules.yaml` for Tiger/MGC dualtrack accounting.
  - Rationale: lab and operational ledgers must charge the same fixed-per-contract side cost; MGC is not a bp-fee instrument in this venue.
  - Evidence: `tiger_mgc` fixed-per-contract model and dualtrack tests expecting `$2.70` per side for one contract.

- Require explicit `contracts`/`quantity` for manual Tiger/MGC fills.
  - Rationale: human attribution must reflect real broker fills. Guessing contract count from an arbitrary notional can silently corrupt the human-vs-machine comparison.
  - Evidence: `DualTrackHumanEngine` rejects Tiger/MGC notional-only fills.

- Do not switch the checked-in dualtrack default to Tiger/MGC in this milestone.
  - Rationale: accounting is now realistic, but COMEX session masking and automatic Tiger fill import into the human ledger are still separate prerequisites.

### Gotchas

- Tiger/MGC accounting support is not strategy approval. The R5 MGC contract-grid lab result still showed no positive non-oracle arm; a direction edge is still required.

- The `$2.70` side fee is the current configured estimate and remains marked `requires_bill_confirmation=true`; the user's real Tiger bill should still be used to calibrate the final all-in fee.

- Venue mode fill `notional` is side notional at that fill price. Entry and exit notionals can differ slightly because futures price changes; realized PnL uses oz exposure (`contracts * contract_multiplier`).

- `grid.max_rungs` is the practical inventory cap for machine-grid contract count in this layer. Do not enable Tiger/MGC with the old implicit 10-rung assumption unless the account size and risk gate are explicitly reviewed.

- Manual front-end payloads for Tiger/MGC must send contract count, not only dollar notional. Broker sync should map Tiger fill quantity directly into `contracts`/`quantity`.

- This milestone does not solve COMEX trading hours. Dualtrack cycle timing still needs a session mask before a clean Tiger/MGC operational run.

## Tiger/MGC Dualtrack COMEX Session Mask

Date: 2026-07-06

### Decisions

- Add COMEX futures session masking as an optional dualtrack profile capability.
  - Rationale: the legacy GOLD/Binance path and many historical dualtrack tests assume fixed 12h/24x7 behavior; Tiger/MGC needs COMEX 23x5 behavior without destabilizing that path.
  - Evidence: `services/dualtrack_clock.py`, `pipelines/dualtrack_cycle_runner.py`.

- Keep the existing `YYYY-MM-DD_DAY` / `YYYY-MM-DD_NIGHT` cycle IDs and apply a session mask inside those cycles.
  - Rationale: the user-facing dualtrack comparison cadence stays stable, while closed-market minutes are excluded from execution/research inputs.
  - Evidence: `filter_bars_for_market_session` filters bars after the normal cycle window is selected.

- Use `America/New_York` timezone conversion instead of hard-coded UTC COMEX hours.
  - Rationale: COMEX daily break and weekly open/close are defined in New York time, and UTC boundaries shift with DST.
  - Evidence: `tests/test_dualtrack_clock.py` covers July DST and January standard-time opens.

- Skip runner work during closed COMEX sessions when the mask is enabled.
  - Rationale: the machine should not recompute or advance operational state while COMEX is in the daily break or weekend close.
  - Evidence: `DualTrackCycleRunner.auto()` and `intraday_tick()` return `reason=market_closed`.

- Filter closed-session bars from `previous_cycle_range()` and `_cycle_bars()`.
  - Rationale: a bad imported or synthetic bar during the 17:00-18:00 New York break must not widen the grid or trigger fills.
  - Evidence: `test_comex_session_mask_filters_closed_break_bars_from_cycle_and_previous_range`.

### Gotchas

- The session mask is not enabled in the checked-in dualtrack default. A Tiger/MGC profile must explicitly set `market_session.enabled=true` and `market_session.venue=comex_futures`.

- This mask models regular COMEX Globex hours only. Exchange holidays, early closes, and broker-specific maintenance still require Tiger/CME session artifacts for exact handling.

- Sunday evening can fall inside an existing `NIGHT` cycle after the cycle's nominal UTC start. If the runner was skipped before open, the first open-session auto run will prepare the current cycle then.

- Closed-session skipping is operational, not a broker kill switch. It does not cancel or close positions; it only prevents dualtrack paper-cycle advancement during known closed periods.

- Session filtering is applied after market bars are loaded. The data feed should still avoid importing impossible closed-session bars, but the runner now has a second guard against those bars contaminating dualtrack calculations.

## Tiger Fill Import Into Human Ledger

Date: 2026-07-06

### Decisions

- Import Tiger filled orders through a separate artifact consumer instead of extending the broker adapter.
  - Rationale: human attribution should be able to read broker evidence without opening an order-control path.
  - Evidence: `services/dualtrack_tiger_human_sync.py`, `pipelines/dualtrack_tiger_human_sync.py`.

- Use `outputs/tiger_order_sync/current.json` as the only input for this importer.
  - Rationale: `TigerOpenApiOrderSync` is already the read-only TradeClient boundary; the dualtrack importer should not open Tiger SDK clients itself.
  - Evidence: importer safety receipt has `opens_tiger_sdk_client=false`.

- Store a stable `source_fill_id` on imported human fills and skip duplicates on repeated runs.
  - Rationale: order sync can be rerun many times during a cycle; human ledger attribution must be idempotent.
  - Evidence: `tests/test_dualtrack_tiger_human_sync.py::test_tiger_human_sync_is_idempotent_for_same_source_fill`.

- Map Tiger futures `filled_quantity` directly to dualtrack `contracts`/`quantity`.
  - Rationale: MGC is a whole-contract instrument; attribution should preserve actual broker fill size rather than estimating from dollar notional.
  - Evidence: imported fills record `contracts=2`, `quantity=2`, and `notional=price * contracts * 10`.

- Prefer broker-reported `commission` when present, otherwise use the configured Tiger/MGC estimate.
  - Rationale: the user's bill is the best fee source; until it exists, the fixed-per-contract fee model remains the fallback.
  - Evidence: broker-reported cost tests retain `cost_model.estimated_cost`.

- Preserve `out_of_plan` on imported Tiger fills.
  - Rationale: human attribution should still distinguish disciplined in-plan execution from manual deviations, even when the fill came from broker sync.

### Gotchas

- This importer does not prove Tiger realtime price-feed readiness, does not submit paper orders, and does not enable broker routing.

- It currently imports MGC filled orders only. Other Tiger futures roots should be added only after their contract multiplier and fee model are represented in `configs/risk_rules.yaml`.

- The importer maps a fill timestamp to the existing dualtrack 12h cycle ID. COMEX session masking prevents closed-session cycle advancement, but fill-cycle attribution still follows the dualtrack cycle clock.

- Broker-reported commission may not equal the final all-in statement cost if Tiger reports fees later or separately. The imported fill keeps the value observed in order sync; bill reconciliation remains a later calibration step.

- A missing or unsynced `tiger_order_sync/current.json` blocks import without writing human fills. The intended sequence is: order sync first, then human-ledger import.

## Runner Human-Fill Sync Before Close

Date: 2026-07-06

### Decisions

- Wire human-fill sync into `DualTrackCycleRunner.close_cycle()` behind an explicit config gate.
  - Rationale: Tiger/MGC close attribution should import broker-observed human fills before scoring, while the existing GOLD/Binance path must stay unchanged.
  - Evidence: `pipelines/dualtrack_cycle_runner.py`, `tests/test_dualtrack_dt8_cycle_runner.py`.

- Run human-fill sync before machine recomputation and scoring.
  - Rationale: if human broker evidence is unavailable, the cycle should not be closed with incomplete human attribution. Closing first would make later imported fills invisible because closed attribution is single-shot.
  - Evidence: `test_tiger_human_fill_sync_blocks_close_when_artifact_missing` asserts no attribution or machine fill is written on missing sync evidence.

- Keep order-sync refresh disabled by default.
  - Rationale: importing an existing artifact is local and safe; refreshing order sync may open a read-only Tiger TradeClient network path and therefore needs explicit profile activation.
  - Evidence: `human_fill_sync.refresh_order_sync_before_import=false` in defaults.

- Allow explicit read-only order-sync refresh before import.
  - Rationale: a Tiger/MGC operational profile can use the sequence "refresh Tiger order sync -> import human fills -> close/scoring" without a separate manual command.
  - Evidence: `test_tiger_human_fill_sync_can_refresh_order_sync_before_import`.

- Treat missing/blocked human-fill sync as a close blocker when `require_success_before_close=true`.
  - Rationale: incomplete broker evidence should not silently become a zero-human-fill attribution.

### Gotchas

- This close-time sync is not an order-control path. It imports fills and optionally refreshes read-only order-sync evidence; it does not submit, cancel, or close broker orders.

- The checked-in default remains disabled. A Tiger/MGC profile must explicitly enable `human_fill_sync.enabled=true`.

- If `refresh_order_sync_before_import=true`, the runner can open a read-only Tiger TradeClient through `TigerOpenApiOrderSync`. This should be used only after credentials and runtime path are intentionally configured.

- A skipped close due to `human_fill_sync_blocked` is intentional. The operator should refresh or repair `tiger_order_sync/current.json`, then rerun close.

- Existing closed attribution stays single-shot. If a cycle was already closed before human-fill sync was enabled, this runner will not rewrite that attribution.

## Tiger/MGC Dualtrack Profile Preview

Date: 2026-07-06

### Decisions

- Add `dualtrack_profile_preview` to connector activation planning instead of immediately writing `configs/dualtrack.yaml`.
  - Rationale: future frontend connector switching needs to show the operational effect of choosing Tiger/MGC, but config mutation still needs a separate reversible approval milestone.
  - Evidence: `services/connector_activation_plan.py`, `tests/test_connector_activation_plan.py`.

- Bundle Tiger/MGC dualtrack requirements into one profile preview.
  - Rationale: a clean Tiger/MGC run requires the MGC price series, COMEX session mask, integer-contract cost model, contract-count cap, and human-fill sync to move together. Switching only the broker/feed would leave the operation in a misleading hybrid state.
  - Evidence: previewed changes cover `market_data`, `market_session`, `execution_cost_model`, `grid.max_rungs`, and `human_fill_sync`.

- Make `DualTrackCycleRunner` read default `market_data.symbol/timeframe` from dualtrack config.
  - Rationale: after a future profile write, the runner should naturally use `MGCmain/1m` without relying on a manual `--symbol` override. The checked-in default remains `GOLD/1m`.
  - Evidence: `test_cycle_runner_uses_configured_market_data_symbol_by_default`.

- Keep the Tiger/MGC profile explicit about strategy and execution gates.
  - Rationale: a correct operational profile is not proof of strategy edge and cannot authorize Tiger order submission.
  - Evidence: profile gates include `requires_strategy_edge_approval=true` and `can_enable_broker_orders_from_this_profile=false`.

### Gotchas

- `dualtrack_profile_preview.status=preview_ready` or `operator_review_required` does not write `configs/dualtrack.yaml`. The activation endpoint remains preview-only.

- The profile intentionally plans `grid.max_rungs=2` for the current $10k-per-track scale. This is an operational safety cap, not a claim that the two-rung MGC strategy is profitable.

- When `broker_order` is requested, the profile plans `human_fill_sync.refresh_order_sync_before_import=true`. If later applied, close can open read-only Tiger TradeClient through the existing order-sync boundary; it still must not submit, cancel, or close orders.

- `market_data.provider=tiger_openapi:COMEX` is audit metadata for the profile. The runner currently selects bars by `symbol/timeframe`; price-feed acceptance and market DB content still prove whether those bars exist and are fresh.

- The config-apply package now lists both broker/feed config paths and `configs/dualtrack.yaml` paths. A future write milestone must back up or stage both surfaces before mutation.

## Dashboard Tiger/MGC Profile Preview Surface

Date: 2026-07-06

### Decisions

- Render `dualtrack_profile_preview` inside the connector panel after activation preview.
  - Rationale: the operator should see the dualtrack runtime effect of a Tiger connector switch in the same place as broker/feed activation status.
  - Evidence: `dashboard-dualtrack-v5.html`, `tests/test_dashboard_dualtrack_static.py`.

- Show only compact operational facts: profile status, MGC market layer, fee/contract quantum, human-fill sync, and strategy/order gates.
  - Rationale: the frontend should make the switching impact scannable without exposing raw config internals or credential details.
  - Evidence: dashboard rows `双轨档案`, `MGC运行层`, `MGC费用/粒度`, `人工成交同步`, `策略/下单门`.

- Keep profile preview rendering as a receipt display, not a control surface.
  - Rationale: connector preview can guide a future config-write milestone, but it must not run validation commands, write config, or authorize broker actions from the browser.
  - Evidence: existing `/api/connectors/activation/plan` remains outside `_DUALTRACK_POST_ENDPOINTS` and dashboard static tests still assert no `submit_command`.

### Gotchas

- `双轨档案` rows appear only after the operator runs activation preview. Loading the dashboard or catalog alone does not run connector activation planning.

- The dashboard intentionally does not render `post_apply_validation_commands` or `next_command` as executable buttons. Commands remain artifact/runbook text for manual operator review.

- The `人工成交同步` row can say `on · read-only refresh` in preview. That describes a future applied profile; the current dashboard view itself does not open Tiger TradeClient.

- `策略/下单门=edge gate · orders off` is the expected state. It means the Tiger/MGC operational profile can be reviewed while strategy approval and broker-order authorization remain separate.

## Connector Config Apply And Rollback Boundary

Date: 2026-07-06

### Decisions

- Add a separate `ConnectorConfigApply` service instead of letting `ConnectorActivationPlan` write config.
  - Rationale: preview and mutation need different failure modes. Preview can be broad and display-oriented; config writes must be attended, acknowledged, backed up, and reversible.
  - Evidence: `services/connector_config_apply.py`, `pipelines/connector_config_apply.py`.

- Make config apply dry-run by default.
  - Rationale: operators and future frontend flows should be able to inspect the apply receipt without mutating runtime files.
  - Evidence: `test_connector_config_apply_dry_run_does_not_write_configs`.

- Require exact acknowledgement and explicit warning acceptance before writing config.
  - Rationale: Tiger/MGC switching can alter broker provider, feed enablement, dualtrack symbol, session mask, and human-fill sync. This should not happen from an accidental click or partial payload.
  - Evidence: `test_connector_config_apply_requires_ack_for_write`.

- Back up every targeted config file before write and make rollback a first-class receipt.
  - Rationale: connector switching should be reversible without relying on git checkout or memory of prior values.
  - Evidence: `test_connector_config_apply_writes_pipeline_and_dualtrack_with_backup`, `test_connector_config_apply_rolls_back_from_backup`.

- Expose config apply and rollback through separate connector API routes, not through dualtrack order routes.
  - Rationale: future frontend can call an explicit config boundary, while the dualtrack order surface remains restricted to paper plan/order/verdict controls.
  - Evidence: `/api/connectors/config/apply`, `/api/connectors/config/rollback`, and `_DUALTRACK_POST_ENDPOINTS` tests.

### Gotchas

- Dry-run receipts do not create backup files. Backups are created only for `write=true` applies after ack/warning gates pass.

- Apply writes local config only. It does not run Tiger price-feed acceptance, open QuoteClient or TradeClient, submit/preview/cancel/close orders, or enable broker-order authorization.

- Rollback overwrites current config files from the recorded backup. It has its own explicit acknowledgement: `I_UNDERSTAND_CONNECTOR_CONFIG_ROLLBACK_WILL_OVERWRITE_CURRENT_CONFIG`.

- The config writer trusts the activation plan payload shape but still rejects blocked activation plans. A fresh activation preview should be generated immediately before an apply.

- Applying the Tiger/MGC profile can enable read-only human-fill refresh in dualtrack config, but that only affects future runner close cycles. It still does not arm Tiger order submission.

## Dashboard Connector Config Receipt Surface

Date: 2026-07-06

### Decisions

- Add a read-only connector config status endpoint for dashboard receipts.
  - Rationale: the operator should see the latest apply/rollback receipt and backup state without opening artifact files or running a write command.
  - Evidence: `/api/connectors/config/status`, `ConnectorConfigApply.status()`, `test_connector_config_status_get_route_is_read_only_receipt_surface`.

- Add a dashboard `写入预检` button that submits config apply without `write=true`.
  - Rationale: the browser can produce and display an apply dry-run receipt, while attended writes remain CLI/API-only with exact acknowledgement.
  - Evidence: `submitConnectorConfigDryRun()` posts only `{activation_plan: state.connectorActivationPlan}`.

- Render config apply, backup, and rollback receipt summaries in the connector panel.
  - Rationale: future connector switching needs an operator-facing receipt trail: dry-run/apply status, backup location, and rollback result.
  - Evidence: dashboard rows `写入预检`, `配置写入收据`, `备份位置`, `回滚收据`.

### Gotchas

- The dashboard button is intentionally a dry-run producer only. It does not send `write=true`, `accept_warnings`, or an acknowledgement.

- The dashboard displays rollback receipts but does not expose a rollback button. Rollback still requires the separate explicit acknowledgement flow.

- A dry-run receipt can say `dry_run_ready` while no backup exists. Backup files are expected only after an attended write succeeds.

- The status endpoint reads local receipt artifacts only. It does not open Tiger SDK clients, fetch prices, write configs, submit orders, cancel orders, or close positions.

## Dualtrack Live Tick And Obsidian Human Plan Sync

Date: 2026-07-06

### Decisions

- Import the operator's Obsidian market view into the human track plan store.
  - Rationale: the morning Feishu/Codex/Obsidian workflow represents the operator's own plan, so the dashboard must show it as the human plan instead of leaving the form empty.
  - Evidence: `DualTrackPlanStore.ensure_human_plan_from_market_view`, `test_human_plan_import_from_market_view_is_draft_after_lock_deadline`.

- Treat Obsidian human imports after the cycle lock deadline as draft/late, not blind-gradeable answers.
  - Rationale: filling the dashboard after the open is useful operationally, but it must not pollute blind human-vs-machine hit-rate evidence.
  - Evidence: `test_late_draft_human_plan_is_not_graded_as_blind_answer`.

- Sync both the current cycle and the next cycle when polling Obsidian.
  - Rationale: a morning note can be too late for the active DAY cycle but still valid for the upcoming NIGHT cycle before its lock deadline.
  - Evidence: `DualTrackCycleRunner.sync_obsidian_human_plans(include_next=True)`, `test_obsidian_plan_sync_imports_current_draft_and_next_locked`.

- Add a five-minute live tick separate from the 09:00/21:00 boundary job.
  - Rationale: boundary jobs handle cycle close/open transitions, but live sampling needs a recurring heartbeat that syncs Obsidian and advances the machine track during the cycle.
  - Evidence: `DualTrackCycleRunner.live_tick`, `test_live_tick_syncs_obsidian_plan_and_runs_intraday`, generated LaunchAgent `com.wendy.trading-orchestrator.dualtrack-live-tick`.

- Make the live tick LaunchAgent a first-class generated schedule job.
  - Rationale: live tick should be reproducible from repo artifacts, not depend on a manually installed plist in `~/Library/LaunchAgents`.
  - Evidence: `ScheduleManager._dualtrack_live_tick_job`, `ScheduleStatus` required jobs, `CompletionAudit._schedule_artifacts`.

### Gotchas

- A visible dashboard is not proof that dualtrack is collecting a live sample. Verify the runner heartbeat under `outputs/dualtrack/runner/<cycle>.json`, launchd `runs/last exit code`, and `/api/dualtrack/machine/<cycle>`.

- The original boundary-only LaunchAgent can leave the machine track stale for hours inside an open cycle. That is acceptable for close/open orchestration, but not for "is the bot running now?" verification.

- Generated schedule artifacts are not proof of an installed/loaded LaunchAgent. `ScheduleStatus` must show the generated `dualtrack-live-tick` plist is installed, matches generated, and is loaded.

- If the operator note appears after the active cycle's lock deadline, the current cycle human plan should show as draft/late. The next cycle can still be locked cleanly if it is before that cycle's deadline.

- The AI fallback is still sourced from the same market-view artifact until a separate independent AI-plan source is introduced. When comparing human vs AI, inspect the `source` field and plan creation path.

## Schedule Current-Version Audit

Date: 2026-07-06

### Decisions

- Split schedule installed/loaded counts from current-version matching counts.
  - Rationale: a launchd job can exist and be loaded while still running an older plist than the one generated from the current repo.
  - Evidence: `ScheduleStatus` now returns `installed_count`, `loaded_count`, `matching_generated_count`, and `active_current_count`.

- Add `stale_installed` as an explicit schedule status.
  - Rationale: stale installed jobs are more actionable than generic `generated_only`; the fix is reinstalling generated plists, not merely generating them again.
  - Evidence: `test_schedule_status_detects_stale_loaded_launch_agents`.

- Surface current-version schedule status in the OPS dashboard.
  - Rationale: schedule mismatch is operational state, not trader-facing signal. OPS should show whether the installed jobs match generated artifacts and list stale installs.
  - Evidence: OPS rows `current / active` and `stale installs`.

### Gotchas

- `loaded=true` from launchd is not proof that a job matches the current repo-generated plist.

- `installed_count=required_count` can coexist with `matching_generated_count < required_count`; this means the jobs exist but some installed plists are stale.

- `stale_installed` should block claims that local automation is current-version active, even when the old jobs are loaded and running.

## Schedule Install Dry-Run Plan

Date: 2026-07-06

### Decisions

- Add a dry-run install plan before any launchd install/restart step.
  - Rationale: stale installed jobs should be reviewable as an explicit replacement plan before the operator modifies `~/Library/LaunchAgents` or restarts launchd jobs.
  - Evidence: `ScheduleInstaller.plan`, `pipelines.schedule_install --dry-run`, `tests/test_schedule_manager.py::test_schedule_installer_plan_reports_stale_loaded_job_without_modifying_launchd`.

- Emit per-job planned actions and planned commands as a receipt artifact.
  - Rationale: the operator needs to see whether each job would be left alone, copied only, replaced, bootstrapped, or restarted.
  - Evidence: `outputs/schedules/install_plan_current.json`, `outputs/schedules/install_plan_<date>.json`.

- Surface the install plan in the OPS dashboard, not the trader console.
  - Rationale: schedule replacement is operational state; it should not be mixed with trader-facing signal quality or strategy PnL.
  - Evidence: OPS row `install plan`.

### Gotchas

- A dry-run install plan is not an install. `planned_commands` are text receipts only; they are not executed.

- `--no-restart-loaded` can stage a current plist on disk while launchd keeps running the old loaded definition until the job is restarted.

- The dry-run plan still calls `launchctl print` through `ScheduleStatus` and writes receipt artifacts under `outputs/schedules`; it does not copy plists, bootstrap, bootout, kickstart, open broker clients, or submit orders.

## Schedule Attended Install Gate

Date: 2026-07-06

### Decisions

- Require an explicit acknowledgement before non-dry-run schedule install can modify LaunchAgents.
  - Rationale: replacing stale launchd jobs is the moment local automation changes from old-version to current-version runtime, so accidental command execution must fail closed.
  - Evidence: `SCHEDULE_INSTALL_ACKNOWLEDGEMENT`, `test_schedule_installer_blocks_apply_without_acknowledgement`.

- Run the install plan before applying and block apply when the plan is blocked.
  - Rationale: the real install path should reuse the same reviewable evidence the operator saw in dry-run, rather than blindly copying plists.
  - Evidence: `ScheduleInstaller.install` calls `ScheduleInstaller.plan` before any write.

- Back up existing LaunchAgent plists before replacing them.
  - Rationale: a local launchd install should have a reversible file-level breadcrumb even though launchd runtime restart itself is not reversible.
  - Evidence: `outputs/schedules/launch_agent_backups/<install_id>/`, `test_schedule_installer_backs_up_existing_plists_before_replacing`.

- Surface the latest install apply receipt in OPS.
  - Rationale: a blocked apply is operational state that should be visible next to schedule status and install plan.
  - Evidence: `schedule_install`, OPS row `install apply`.

### Gotchas

- The acknowledgement authorizes only local LaunchAgent replacement/restart. It does not authorize Tiger/Binance/OANDA broker routing or real-money order submission.

- A blocked install still writes `install_current.json` as an audit receipt and can call `launchctl print` through the plan/status path; it does not create LaunchAgents, copy plists, bootout, bootstrap, or kickstart.

- Backups cover plist files on disk. They do not restore launchd runtime state automatically; rollback/restart still needs an explicit operator step.

## Schedule Rollback Gate

Date: 2026-07-06

### Decisions

- Add a rollback dry-run plan based on the install receipt's backup records.
  - Rationale: an install backup is not operationally useful unless the operator can verify which jobs are restorable before touching LaunchAgents again.
  - Evidence: `ScheduleInstaller.rollback_plan`, `tests/test_schedule_rollback_plan_blocks_when_install_receipt_has_no_backups`.

- Require a separate rollback acknowledgement before restoring backup plists.
  - Rationale: rollback is another local launchd mutation and should not reuse the install acknowledgement.
  - Evidence: `SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT`, `tests/test_schedule_rollback_blocks_apply_without_acknowledgement`.

- Back up the current target plists before applying rollback.
  - Rationale: restoring an older LaunchAgent definition should still leave a breadcrumb for the state being replaced.
  - Evidence: `outputs/schedules/rollback_target_backups/<rollback_id>/`, `tests/test_schedule_rollback_restores_backup_with_acknowledgement`.

- Surface rollback plan/apply receipts in OPS.
  - Rationale: rollback readiness is operational state next to install plan/apply, not a trader-facing signal.
  - Evidence: dashboard payload fields `schedule_rollback_plan` and `schedule_rollback`, OPS rows `rollback plan` and `rollback apply`.

### Gotchas

- Rollback requires a successful install receipt with per-job backup paths. A blocked install receipt has no backup records, so rollback plan is expected to be blocked.

- Rollback acknowledgement authorizes only local LaunchAgent plist restoration/restart. It does not authorize broker config changes or order activity.

- Rollback restores plist files. It does not prove market data freshness, strategy readiness, or Tiger order-path readiness.

## Schedule Post-Install Verification Gate

Date: 2026-07-06

### Decisions

- Add a dedicated post-install verifier for schedule takeover.
  - Rationale: after an attended install, the operator needs one receipt proving current-version launchd jobs are active, not a collection of separate commands.
  - Evidence: `SchedulePostInstallVerifier`, `pipelines.schedule_post_install_verify`.

- Verify four independent claims: current/active schedule, successful install receipt, rollback readiness, and runner heartbeat freshness.
  - Rationale: a loaded launchd job alone does not prove current-version takeover, and a fresh runner heartbeat can come from stale jobs.
  - Evidence: checks `schedule_current_active`, `install_receipt`, `rollback_ready`, and `runner_heartbeat`.

- Surface post-install verification in OPS only.
  - Rationale: schedule takeover is operational evidence, not trader-facing market or strategy signal.
  - Evidence: dashboard payload field `schedule_post_install_verify`, OPS row `post-install verify`, and ops-status runner contract.

### Gotchas

- A fresh runner heartbeat is not enough. The verifier still blocks if `ScheduleStatus` reports stale installed plists.

- Rollback readiness is part of post-install proof. If an install receipt has no backup records, the verifier blocks even if launchd jobs are loaded.

- The verifier calls `launchctl print` and writes receipt artifacts; it does not copy plists, bootout, bootstrap, kickstart, open broker clients, or submit orders.

## Schedule Takeover Package

Date: 2026-07-06

### Decisions

- Add a no-write takeover package before attended install.
  - Rationale: the operator should approve from a durable artifact containing current evidence and exact commands, not from chat text or memory.
  - Evidence: `ScheduleTakeoverPackage`, `pipelines.schedule_takeover_package`, `outputs/schedules/takeover_package_current.json`.

- Include exact install, post-install verification, rollback dry-run, and attended rollback commands.
  - Rationale: installing current launchd jobs is safe only when the verification and recovery commands are part of the same handoff package.
  - Evidence: package `commands.review_install_plan`, `commands.attended_install`, `commands.post_install_verify`, `commands.review_rollback_plan`, and `commands.attended_rollback`.

- Surface takeover package state in OPS.
  - Rationale: authorization readiness is operational state, not trader-facing alpha or PnL state.
  - Evidence: dashboard payload field `schedule_takeover_package`, OPS row `takeover package`, and ops-status runner contract.

### Gotchas

- `ready_for_attended_install` means the dry-run plan is reviewable and unblocked. It does not mean the install has happened.

- The attended install command in the package contains the local schedule acknowledgement. It authorizes only local launchd replacement/restart, not broker routing or real-money order submission.

- The package writes receipt artifacts and calls `launchctl print` through its component checks; it does not copy plists, bootout, bootstrap, kickstart, open broker clients, or submit orders.

## Schedule Takeover Package Binding

Date: 2026-07-06

### Decisions

- Bind attended schedule install to the current takeover package id.
  - Rationale: an acknowledgement string alone can be copied from an old handoff; the install command must prove it belongs to the package that was just reviewed.
  - Evidence: `ScheduleTakeoverPackage.package_id`, `ScheduleInstaller._package_gate`, and `pipelines.schedule_install --package-id`.

- Make takeover packages time-limited.
  - Rationale: schedule status can drift quickly when generated plists or loaded jobs change, so a stale package should not remain an install authorization surface.
  - Evidence: package fields `valid_for_minutes` and `expires_at`, plus installer blocker `expired_package`.

- Surface the package id in OPS.
  - Rationale: operators need a human-checkable short id next to the takeover package status before running an attended install command.
  - Evidence: OPS row `takeover package` includes `scheduleTakeoverPackage.package_id`.

- Surface takeover package expiry in OPS.
  - Rationale: `ready_for_attended_install` is not enough once the package's `expires_at` has passed; the operator needs to see that a fresh package must be regenerated.
  - Evidence: OPS row `takeover package` reads `scheduleTakeoverPackage.expires_at` and renders `expired` when stale.

- Add a no-write current package check before attended install.
  - Rationale: operators need a standalone answer for whether the latest package can authorize install, without invoking the install command just to discover expiry or mismatch.
  - Evidence: `ScheduleTakeoverPackage.check_current`, `pipelines.schedule_takeover_package --check-current`, and `outputs/schedules/takeover_package_check_current.json`.

- Render package-gate-blocked install receipts as authorization gate state, not as failed launchd mutation.
  - Rationale: a deliberately blocked package-id mismatch proves the install guard worked; operators should not confuse it with a failed copy/bootstrap/kickstart attempt.
  - Evidence: OPS row `install apply` reads `scheduleInstallReceipt.package_gate` and shows `gate <blocker>`.

### Gotchas

- The install acknowledgement is necessary but no longer sufficient. A non-dry-run install that needs replacement must also pass `--package-id` from `takeover_package_current.json`.

- If the package id is missing, mismatched, for a different run date, not ready, or expired, install blocks before copying plists, bootout, bootstrap, or kickstart.

- A new takeover package may produce a different package id when the schedule evidence changes. Regenerate the package instead of reusing an old command.

- A package file can exist and still be invalid. Check `status`, `run_date`, `package_id`, and `expires_at`; the installer enforces all four before any local launchd mutation.

- `install_current.json status=blocked` with `package_gate.ok=false` is an authorization-gate receipt. It should be read as "wrong or stale command was rejected" unless `safety.writes_launch_agents=true` or launchd mutation commands were actually attempted.

- A takeover package can have `status=ready_for_attended_install` and still be expired. Regenerate it before using its attended install command.

- Run `python3 -m pipelines.schedule_takeover_package --check-current --date YYYY-MM-DD --json` immediately before attended install. It is read/check-only for launchd and broker surfaces and should say `authorize_attended_install` before the install command is used.

## Dualtrack Dashboard Market Source Freshness

Date: 2026-07-06

### Decisions

- Treat dashboard market bars as usable only when the latest bar is fresh for its timeframe.
  - Rationale: an old preferred venue feed is more dangerous than a clearly labeled fallback because it can make the live console look current while showing stale prices.
  - Evidence: `DualTrackMarketFeed._freshness`, `test_dualtrack_market_feed_skips_stale_tiger_and_uses_fresh_binance`.

- Keep Tiger/COMEX as first preference only when its MGC bars are fresh.
  - Rationale: Tiger/MGC is the intended execution-venue view, but stale MGC bars from a prior acceptance run must not override fresh Binance `GOLD/1m` bars during today's sample.
  - Evidence: `/api/dualtrack/market/bars` now returns `binance_usdm_fallback` when MGC latest is stale.

- Expose freshness metadata in the market-bars payload.
  - Rationale: the operator and frontend need to see whether the chart is current, not just which provider won selection.
  - Evidence: payload fields `fresh`, `age_minutes`, `max_age_minutes`, and `access_issues`.

### Gotchas

- A chart label can look official while still being stale. Always check `latest_timestamp` and `fresh`, not just `provider`.

- `synthetic_seed:dualtrack_dashboard` is display-only and must never be interpreted as live market evidence or trading-signal input.

- `MGCmain/tiger_openapi:COMEX` currently has historical rows in the local DB. Unless the Tiger feed is actively refreshing, default dualtrack charting should fall back to fresh `GOLD/1m binance_usdm`.

## Dualtrack Context Chart Usability

Date: 2026-07-06

### Decisions

- Replace frontend-sampled context sparklines with real timeframe payloads.
  - Rationale: a 15m/1h context panel must show higher-timeframe structure, not every-Nth 1m candle from the main chart.
  - Evidence: dashboard now requests `/api/dualtrack/market/bars?symbol=GOLD&timeframe=15m&limit=64` and `timeframe=1h`; static test rejects `state.candles.filter((_, i) => i %`.

- Derive 15m/1h context bars from fresh local 1m data when native 15m/1h rows are absent.
  - Rationale: the live DB has fresh `GOLD/1m binance_usdm` rows but not native 15m/1h rows, so falling back to synthetic data made the context unusable.
  - Evidence: `DualTrackMarketFeed._derived_bars`, `test_dualtrack_market_feed_derives_context_timeframes_from_one_minute_bars`.

- Add context metadata and readable mini-chart annotations.
  - Rationale: the operator needs to see source mode, freshness, change %, high/low, latest price, and floor proximity at a glance.
  - Evidence: `contextMeta`, `drawMini(..., market, label)`.

### Gotchas

- A context chart without its own timeframe data is not context. Sampling a 1m chart down visually does not create 15m/1h structure.

- If native 15m/1h bars are missing, derived bars are acceptable only when the source 1m/5m bars are fresh and the payload says `derived_from_1m` or `derived_from_5m`.

## Tiger/MGC Config Write Package Binding

Date: 2026-07-06

### Decisions

- Bind attended connector config writes to the reviewed activation plan with a stable `config_apply_package.package_id`.
  - Rationale: the static acknowledgement phrase proves intent to write config, but it does not prove the operator is approving this exact Tiger/MGC plan after the preview evidence changes.
  - Evidence: `outputs/connector_activation_plan/current.json` now includes `config_apply_package.package_id=631b8ed6cb3310de`.

- Require `--package-id` for any non-dry-run `connector_config_apply apply --write`.
  - Rationale: config writes change active broker and dualtrack routing, so the write must fail closed if the command is copied from a stale approval package.
  - Evidence: `ConnectorConfigApply._validate_plan` blocks missing or mismatched package ids before writing runtime config.

- Bind package ids to full change content, not only changed paths.
  - Rationale: approving `broker.provider -> tiger_openapi` is different from approving the same path with a different planned value; path-only hashes are too weak for attended config writes.
  - Evidence: `config_apply_package.patch_digest` includes `current`, `planned`, and `reason` for each config change.

- Recompute the package id inside the config writer before any non-dry-run write.
  - Rationale: the writer must not blindly trust a package id embedded in a modified activation plan.
  - Evidence: tampering `config_patch_preview[0].planned` while reusing the same id returns `config_apply_package_id_stale` and `safety.writes_runtime_config=false`.

- Keep package-id enforcement out of dry-run.
  - Rationale: operators and the frontend still need cheap no-write previews while reviewing connector changes.
  - Evidence: `outputs/connector_config_apply/current.json status=dry_run_ready`, `mode=dry_run`, `package_id=631b8ed6cb3310de`, `safety.writes_runtime_config=false`.

### Gotchas

- The acknowledgement string is necessary but no longer sufficient for config write. The attended command must also carry the package id shown in the reviewed activation package.

- If the activation plan contents change, the package id must change. A reused id with changed contents is treated as stale and blocked before writing config.

- A `dry_run_ready` receipt with a package id is still not a config write. It does not change `configs/pipeline.yaml`, `configs/dualtrack.yaml`, broker routing, launchd jobs, or order submission state.

- The package id binds the config-write plan only. It does not authorize Tiger TradeClient paper orders or real-money execution.

## Tiger/MGC Config Package UI Visibility

Date: 2026-07-06

### Decisions

- Show the reviewed config package id in the dualtrack connector card.
  - Rationale: operators need to compare the visible package id with any later attended command before approving a config write.
  - Evidence: `dashboard-dualtrack-v5.html` renders `审批包ID` from `configApplyPackage.package_id`.

- Show the config write boundary as display-only UI.
  - Rationale: the frontend should make it clear that this is a separately authorized write, not an inline activation button.
  - Evidence: `dashboard-dualtrack-v5.html` renders `写入边界` from `configApplyPackage.config_write_boundary.requires_package_id`.

- Show the latest config apply dry-run package id beside the dry-run receipt.
  - Rationale: the operator should see whether the latest dry-run receipt and activation package refer to the same reviewed plan.
  - Evidence: `connectorConfigRows` renders `预检包ID` from `latest_apply.package_id`.

### Gotchas

- Package id visibility is not authorization. The dashboard still must not contain write-mode payloads, warning acceptance flags, acknowledgement strings, rollback controls, or order controls.

- A displayed package id is only useful if it matches the current activation package and the later attended command. If activation evidence changes, regenerate the preview and dry-run.

## Tiger/MGC Config Package Check Current

Date: 2026-07-06

### Decisions

- Add a read-only `connector_config_apply check` step before attended config write.
  - Rationale: before the operator authorizes config write, the system should independently confirm that the activation package, latest dry-run receipt, and recomputed package id still match.
  - Evidence: `outputs/connector_config_apply/check_current.json status=ready_for_attended_config_write`, `package_id=631b8ed6cb3310de`.

- Treat warning acceptance as part of the next operator action, not as automatic approval.
  - Rationale: `paper_network_order_not_armed` is intentional, but it still must be explicitly accepted before a config write that changes broker/dualtrack routing.
  - Evidence: `operator_next_action.action=authorize_attended_config_write_with_warning_acceptance`, `requires_accept_warnings=true`.

- Surface package check status in the dualtrack connector card.
  - Rationale: the frontend should show whether the reviewed package is currently usable without providing an inline write button.
  - Evidence: `dashboard-dualtrack-v5.html` renders `包检查` and `授权下一步` from `latest_check`.

### Gotchas

- `ready_for_attended_config_write` means the package is internally consistent and current. It still does not write config, switch broker, arm Tiger orders, or authorize real-money execution.

- A package check depends on the latest dry-run receipt. Regenerate dry-run and check if activation evidence changes.

## Tiger/MGC Post-Apply Validation Boundary

Date: 2026-07-06

### Decisions

- Include post-apply validation commands in the read-only config package check.
  - Rationale: a config write is not the end state; after changing routing, the operator must immediately re-validate connector catalog, Tiger price feed, live readiness, and live switch plan.
  - Evidence: `outputs/connector_config_apply/check_current.json post_apply_validation_commands` contains 4 commands.

- Include rollback boundary evidence in the read-only config package check.
  - Rationale: an attended config write must be reversible through its backup receipt; the operator should see that rollback is part of the write boundary before approving.
  - Evidence: `outputs/connector_config_apply/check_current.json rollback_boundary.required=true`.

- Surface post-apply and rollback requirements in the dualtrack connector card without exposing write controls.
  - Rationale: the frontend should show what remains after authorization while still preventing accidental config writes from the page.
  - Evidence: `dashboard-dualtrack-v5.html` renders `写后验收` and `回滚边界`.

### Gotchas

- Post-apply validation commands in the check receipt are a runbook, not commands that have been run.

- Rollback is available only after an attended write creates backups. Before write, `rollback_boundary.required=true` means the future write must create rollback evidence.

## Tiger/MGC Operator Handoff Artifact

Date: 2026-07-06

### Decisions

- Generate a read-only operator handoff artifact for the current Tiger/MGC config package.
  - Rationale: the operator should be able to review one human-readable artifact instead of correlating activation, dry-run, check, and dashboard state manually.
  - Evidence: `outputs/connector_config_apply/handoff_current.md` and `outputs/connector_config_apply/handoff_current.json`.

- Include the current runtime state in the handoff.
  - Rationale: the handoff must make clear that the system has not switched yet.
  - Evidence: handoff `current_runtime.broker_provider=binance_usdm`, `current_runtime.dualtrack_symbol=GOLD`.

- Include the attended apply command, post-apply validation commands, rollback boundary, and explicit "not authorized" list in the same artifact.
  - Rationale: the handoff should define exactly what a future manual authorization would do and what it still would not authorize.
  - Evidence: handoff `attended_apply_command`, 4 `post_apply_validation_commands`, `rollback_boundary.required=true`, and `not_authorized`.

### Gotchas

- The handoff is an approval artifact, not an execution artifact. Generating it does not write config, switch broker routing, open Tiger clients, submit orders, or arm paper/real-money execution.

- The attended command in the Markdown is for manual review. It must not be wired into the dashboard as a button or run automatically.

## Tiger/MGC Handoff Evidence Chain

Date: 2026-07-06

### Decisions

- Add an evidence chain to the operator handoff.
  - Rationale: the handoff must prove which activation plan, dry-run, check receipt, and current config files it was generated from so stale or mixed evidence is easier to catch.
  - Evidence: `outputs/connector_config_apply/handoff_current.json evidence_chain`.

- Fingerprint current runtime config files in the handoff.
  - Rationale: the operator needs proof that the current runtime is still `binance_usdm`/`GOLD` at handoff time, without exposing config contents or secrets.
  - Evidence: handoff `evidence_chain.current_config` includes SHA256 entries for `configs/pipeline.yaml` and `configs/dualtrack.yaml`.

- Surface the latest handoff summary in the connector status and dashboard.
  - Rationale: the frontend should show whether an operator handoff exists, how much evidence it binds, and what runtime it saw, while keeping write authorization out of the browser.
  - Evidence: `/api/connectors/config/status latest_handoff`; `dashboard-dualtrack-v5.html` renders `交接单` and `运行现状`.

### Gotchas

- A matching evidence chain still is not authorization. It does not write config, switch broker routing, open Tiger clients, submit orders, accept warnings, or create rollback backups.

- Evidence fingerprints help catch stale artifacts, but any new activation preview, dry-run, package check, or config edit requires regenerating the handoff before manual approval.

## Tiger/MGC Handoff-Bound Config Write

Date: 2026-07-06

### Decisions

- Require a current operator handoff before any non-dry-run config write.
  - Rationale: exact acknowledgement and package id are necessary but not sufficient; the write should also prove the operator reviewed the latest handoff evidence.
  - Evidence: `ConnectorConfigApply._validate_handoff_for_write` blocks with `config_handoff_missing` when no current handoff exists.

- Block config write when handoff fingerprints are stale.
  - Rationale: a config file, dry-run, check receipt, or activation plan may change after handoff generation; the write must not proceed from stale evidence.
  - Evidence: `tests/test_connector_config_apply.py` covers stale `pipeline_config` fingerprint blocking before write.

- Keep the handoff-bound write guard out of the browser.
  - Rationale: the dashboard should show status, not become an execution surface for switching broker routing.
  - Evidence: static dashboard tests still reject `write:true`, `accept_warnings`, acknowledgement strings, rollback controls, and order controls.

### Gotchas

- A `ready_for_attended_config_write` check is no longer enough for write-mode apply; the current handoff must also match the plan, dry-run/check artifacts, and current config fingerprints.

- If any config file or connector artifact changes, regenerate check and handoff before a future attended write.

## Tiger/MGC Check Write Guard Visibility

Date: 2026-07-06

### Decisions

- Split read-only check into package readiness and write preflight readiness.
  - Rationale: dry-run/package consistency can be ready while the actual config write is still blocked by missing or stale handoff evidence.
  - Evidence: `connector_config_apply check` now returns `write_preflight.status` and `usable_for_attended_config_write`.

- Surface the write preflight guard in the dualtrack connector card.
  - Rationale: the operator should see whether a handoff-bound write is actually ready without attempting the write.
  - Evidence: `dashboard-dualtrack-v5.html` renders `写入守卫` from `latest_check.write_preflight_status`.

- Keep `persisted_config_check` as handoff evidence, but not as a hard self-invalidating write gate.
  - Rationale: running a read-only check updates `check_current.json`; if that SHA were a hard gate, checking readiness would invalidate the handoff it is meant to inspect.
  - Evidence: `tests/test_connector_config_apply.py` covers check-after-handoff followed by successful guarded write in temp configs.

### Gotchas

- The operator sequence is: dry-run, check, handoff, check/status, then attended write if explicitly authorized. The second check should not force handoff regeneration by itself.

- `write_preflight.status=ready` still does not write config or authorize trading; it only proves the reviewed artifacts are current enough for a future attended write.

## Tiger/MGC Sandbox Config Rehearsal

Date: 2026-07-06

### Decisions

- Add a sandbox rehearsal for the attended config write and rollback path.
  - Rationale: before a real operator-authorized switch, we need evidence that the same write/rollback logic works end-to-end without mutating runtime config.
  - Evidence: `python3 -m pipelines.connector_config_apply rehearse --plan outputs/connector_activation_plan/current.json --json`.

- Rehearsal copies current config into an isolated sandbox and runs the real sequence there.
  - Rationale: tests prove code behavior, but an operator receipt should prove the current activation package can apply and roll back against current config contents.
  - Evidence: rehearsal steps include `dry_run`, `pre_handoff_check`, `handoff`, `write_check`, `sandbox_apply`, and `sandbox_rollback`.

- Surface rehearsal status through connector config status and the dashboard.
  - Rationale: the operator should see whether a full sandbox switch/rollback has passed without opening the artifact tree.
  - Evidence: `latest_rehearsal` in connector config status; `dashboard-dualtrack-v5.html` renders `切换演练`.

### Gotchas

- A passed rehearsal is not a runtime config write. It only writes sandbox copies under `outputs/connector_config_apply/rehearsals/`.

- Rehearsal proves the package can apply and roll back against the current config shape; it does not prove Tiger live market data, broker network order submission, launchd takeover, strategy edge, or real-money readiness.

## Tiger/MGC Rehearsal-Bound Config Write

Date: 2026-07-06

### Decisions

- Require a passed current rehearsal before any non-dry-run config write.
  - Rationale: handoff proves evidence provenance, but it does not prove the write and rollback path still works against the current config shape.
  - Evidence: `ConnectorConfigApply._validate_rehearsal_for_write` blocks write-mode apply with `config_rehearsal_missing` or `config_rehearsal_*` when rehearsal evidence is absent or stale.

- Make read-only check report the extra rehearsal step.
  - Rationale: after handoff, the operator should see `ready_for_rehearsal`, not a misleading write-ready state.
  - Evidence: `test_connector_config_check_reports_ready_for_rehearsal_after_matching_handoff`.

- Keep rehearsal from self-depending on rehearsal.
  - Rationale: the rehearsal itself must be able to prove sandbox write/rollback without first requiring an already-passed rehearsal.
  - Evidence: sandbox apply inside `ConnectorConfigApply.rehearse` bypasses only the rehearsal requirement while preserving package, warning, acknowledgement, and handoff checks inside sandbox.

### Gotchas

- The operator sequence is now: dry-run, check, handoff, rehearsal, check/status, then attended write if explicitly authorized.

- If runtime config changes after rehearsal, regenerate handoff and rehearsal. A fresh handoff alone is no longer enough.

## Gold Strategy Research: VWAP Z-Score Cost Floor

Date: 2026-07-06

### Decisions

- Treat today's strategy-book problem as sample/execution leakage plus cost discipline, not lack of strategy count.
  - Rationale: `strategy_frequency/current.json` showed `portfolio_executed_count=0`, `effective_strategy_count=0`, and `below_min_strategy_count=23`.
  - Evidence: `outputs/strategy_research/2026-07-06-vwap-zscore-cost-floor.md`.

- Do not promote or expand yesterday's `gold_1m_vwap_zscore_reversion` without a cost floor.
  - Rationale: a read-only single-strategy R1 replay showed it reduced raw Bollinger noise and drawdown but still had negative gross and negative maker/taker expectancy.
  - Evidence: R1 replay in the research artifact: 115 OOS trades, gross bp/trip -0.5684, maker 2bp expectancy -0.4569/trade.

- Propose exactly one improvement class: `gold_1m_vwap_zscore_cost_floor`.
  - Rationale: the current variant needs minimum VWAP distance, ATR-scaled target room, event jump cooling, and maker-first cost assumptions before it can become a serious shadow candidate.
  - Evidence: research artifact under `outputs/strategy_research/`.

- Do not edit `configs/strategy.yaml` or active/demo/live routing in this run.
  - Rationale: the repo is already dirty and the candidate has not passed cost discipline; writing a config would expand risk without evidence.
  - Evidence: no strategy code/config changes were made.

### Gotchas

- Lower drawdown is not edge if the expected trade remains negative after costs.

- A strategy with `no_lab_evidence` in the leaderboard should not be treated as paper-eligible, even if its forward paper PnL looks calm.

- R1 scans must not be run casually in full mode during research because they can rewrite lab reports; use targeted read-only evaluation unless the run is explicitly a lab refresh.

- Cost-floor research is still shadow/lab work. It does not authorize active strategy changes, demo broker routing, leverage increases, or live-money execution.

## Dualtrack Operator Readiness Red Lights

Date: 2026-07-06

### Decisions

- Treat live runner heartbeat as a first-class dashboard signal.
  - Rationale: a page can render while the machine track is no longer collecting a valid intraday sample.
  - Evidence: `/api/dualtrack/runtime/status` reports `runner.latest_ts`, `runner.age_seconds`, `sample.valid_now`, and blocks when the heartbeat exceeds the allowed age.

- Context charts must use real timeframe bars, or explicitly marked derived bars.
  - Rationale: sampled 1m bars made the 15m/1h context visually misleading and did not answer the operator's context question.
  - Evidence: `dashboard-dualtrack-v5.html` now fetches `timeframe=15m` and `timeframe=1h`; `services/dualtrack_market_feed.py` marks derived bars as `derived_from_1m` with freshness metadata.

- Show source independence directly on plan cards.
  - Rationale: Obsidian-derived fallback is useful for continuity, but it is not an independent AI opinion and should not be confused with one.
  - Evidence: AI plan cards mark `Obsidian fallback`; human late/draft plans show that they are recorded but not blind-score eligible.

- Keep machine fill details hidden until close while still showing sample health.
  - Rationale: the operator needs to know whether the machine is running, but open-cycle fill details would break the blind comparison.
  - Evidence: runtime status exposes `machine_fills_hidden=true` and omits `machine_fill_count` for open cycles.

- Surface the most recent closed cycle separately from the current cycle.
  - Rationale: after a cycle boundary the main console moves to the next live cycle, but the operator still needs an immediate link to the just-closed attribution and replay.
  - Evidence: runtime status now includes `previous_closeout` with closed-cycle attribution, ledger, fill counts, and replay URL while preserving current-cycle blind protection.

### Gotchas

- Tests can be green while launchd is not loaded. Runtime readiness needs a live heartbeat check, not just static API and unit-test coverage.

- A manually successful `live-tick` run is not enough. The LaunchAgent must also be loaded, have `StartInterval=300`, and report a recent zero-exit run.

- `warn` is acceptable for an open cycle when all checks are OK; `blocked` means the current sample is not trustworthy.

- Obsidian fallback keeps the machine track alive, but it is not an independent AI source. Treat those samples differently when reviewing human-vs-AI quality.

- Derived 15m/1h context is acceptable only when the source 1m bars are fresh and the response visibly carries `derived_from_1m`.

- A "current cycle" API will naturally roll forward at the boundary. Closed-cycle review needs an explicit previous-cycle handoff, otherwise the just-finished sample can disappear from the main operator view.

## Tiger/MGC Operator Authorization Package

Date: 2026-07-06

### Decisions

- Add a final read-only authorization package before any attended config write.
  - Rationale: handoff and rehearsal prove separate pieces; the operator needs one current package that ties together dry-run, check, handoff, rehearsal, current runtime, validation commands, and rollback boundary.
  - Evidence: `python3 -m pipelines.connector_config_apply authorization --plan outputs/connector_activation_plan/current.json --json`.

- Authorization is a local evidence artifact, not an execution control.
  - Rationale: the dashboard may show readiness, but it must not expose `write:true`, acknowledgement text, warning acceptance, rollback controls, Tiger clients, or order controls.
  - Evidence: status now includes `latest_authorization`; the dashboard renders only status and blocker count.

- The package is only ready while runtime config still matches reviewed pre-switch evidence.
  - Rationale: if `configs/pipeline.yaml` or `configs/dualtrack.yaml` changes after rehearsal, the package must be regenerated before an attended write.
  - Evidence: authorization reuses the current write preflight, handoff, and rehearsal validators.

### Gotchas

- `ready_for_operator_authorization` still does not mean "switched". It means the next action is a human-attended config write, if explicitly authorized.

- The authorization package records the attended apply command in local artifacts, but the browser dashboard deliberately does not display that command.

- This package does not prove realtime futures subscription, strategy edge, paper order submission, launchd takeover, or real-money readiness.

## Tiger/MGC Final Readiness Audit

Date: 2026-07-06

### Decisions

- Record the current state as `pre-switch authorization-ready`, not as active Tiger/MGC runtime.
  - Rationale: the authorization package has no blockers, but the checked runtime config is still `binance_usdm` and `GOLD`.
  - Evidence: `outputs/connector_config_apply/final_readiness_audit_current.md`.

- Treat market-hours price-feed acceptance as cleared from current artifacts.
  - Rationale: `tiger_price_feed_acceptance/current.json` reports `accepted`, realtime validation reports `pass`, and the shared market DB has fresh `MGCmain/1m/tiger_openapi:COMEX` rows.
  - Evidence: `outputs/tiger_price_feed_acceptance/current.json`, `outputs/tiger_realtime_validation/current.json`, `data/market_data.db`.

- Keep strategy edge and broker-order authorization separate from config-switch authorization.
  - Rationale: switching the local feed/broker profile does not prove a machine-track strategy edge and does not authorize Tiger TradeClient submission.
  - Evidence: activation profile still has `requires_strategy_edge_approval=true` and `can_enable_broker_orders_from_this_profile=false`; paper-order approval artifacts have `submit_requested=false`.

### Gotchas

- Do not call the current state "switched". It is ready for a manual switch review, while active config remains Binance/GOLD.

- Market-open wait is no longer the current readiness blocker according to artifacts from 2026-07-06. If those artifacts become stale, rerun the price-feed acceptance gate before applying config.

- `ready_for_operator_authorization` is not permission to submit paper or live orders. Broker-order submission remains a separate authorization path.

## Tiger/MGC Repeatable Go/No-Go Readiness Audit

Date: 2026-07-06

### Decisions

- Convert the final readiness audit from a static Markdown snapshot into a repeatable read-only command.
  - Rationale: the operator should not have to manually compare acceptance, authorization, runtime config, market DB coverage, and broker-order gates across several files.
  - Evidence: `python3 -m pipelines.connector_config_apply readiness-audit --plan outputs/connector_activation_plan/current.json --json`.

- Define `go_for_attended_config_switch` narrowly.
  - Rationale: the current system can be ready for a human-approved config switch while still not being trade-ready.
  - Evidence: generated audit reports `can_switch_config_with_operator_authorization=true`, `can_trade_machine_track=false`, and `can_submit_tiger_orders=false`.

- Require local MGC market coverage in the Go/No-Go audit.
  - Rationale: price-feed acceptance alone is too indirect; the switch should also see actual `MGCmain/1m/tiger_openapi:COMEX` bars in the shared market DB.
  - Evidence: audit reads `data/market_data.db` and reports the Tiger/COMEX bar count and latest timestamp.

- Surface the audit in connector status and dashboard as read-only state.
  - Rationale: the dashboard should answer "can I consider the attended switch now?" without exposing write payloads or operator acknowledgement strings.
  - Evidence: connector config status includes `latest_readiness_audit`; dashboard renders `最终审计`.

### Gotchas

- `go_for_attended_config_switch` is not `go_for_trading`.

- A zero-blocker audit still does not write config. It only says the attended config write command in the local authorization package is ready for human review.

- If price-feed artifacts, market DB coverage, or config authorization artifacts drift, rerun `readiness-audit` before relying on the dashboard state.

## Tiger/MGC Post-Switch Validation

Date: 2026-07-06

### Decisions

- Add a separate post-switch validation command.
  - Rationale: after an attended config write, the operator needs a direct answer to "did the runtime actually switch to Tiger/MGC, and are broker orders still closed?"
  - Evidence: `python3 -m pipelines.connector_config_apply post-switch-validate --package-id <package-id> --json`.

- Keep post-switch validation read-only.
  - Rationale: validation should inspect active config, apply receipt, rollback backup, price-feed artifacts, and local market DB coverage without opening Tiger SDK clients or mutating config.
  - Evidence: validation safety reports `writes_runtime_config=false`, `opens_network_clients=false`, and `submits_orders=false`.

- Require the switched runtime to match the full Tiger/MGC operating profile.
  - Rationale: partial switches are dangerous; broker provider alone is not enough.
  - Evidence: validation checks `broker.provider=tiger_openapi`, `market_data.symbol=MGCmain`, `market_data.provider=tiger_openapi:COMEX`, COMEX session enabled, Tiger/MGC cost model, integer contracts, and human-fill sync enabled.

- Treat broker order enablement as a failure in this post-switch validator.
  - Rationale: the current milestone is price-feed/config switch readiness, not paper TradeClient submission.
  - Evidence: validation blocks if runtime profile would enable broker orders.

### Gotchas

- Before the attended config write, `post-switch-validate` should be blocked. That is expected and confirms the real config has not switched.

- `validated_post_switch` still does not mean the machine strategy is approved or Tiger orders can be submitted.

- A successful post-switch validation requires a rollback backup from an applied config write. A dry-run receipt is not enough.

## Tiger/MGC Rehearsal Includes Post-Switch Validation

Date: 2026-07-06

### Decisions

- Extend sandbox rehearsal to include authorization, final readiness audit, sandbox apply, post-switch validation, and rollback.
  - Rationale: a passed rehearsal should prove the same package can switch in sandbox, validate as Tiger/MGC, and roll back, not merely write and restore files.
  - Evidence: `python3 -m pipelines.connector_config_apply rehearse --plan outputs/connector_activation_plan/current.json --json`.

- Seed rehearsal with read-only local acceptance artifacts and a SQLite backup of the market DB.
  - Rationale: post-switch validation requires price-feed acceptance and local MGC coverage; direct file copy of SQLite can miss WAL rows, so rehearsal uses SQLite backup semantics.
  - Evidence: rehearsal `steps.post_switch_validation.status=validated_post_switch` and `runtime_config.unchanged=true`.

- Keep rehearsal bypasses private to the rehearsal path.
  - Rationale: sandbox authorization must avoid circularly requiring an already-completed rehearsal, but the public `authorization` CLI must still require the current rehearsal.
  - Evidence: `ConnectorConfigApply._authorization(... require_current_rehearsal=False)` is used internally by `rehearse`; `authorization` keeps the strict default.

### Gotchas

- Rehearsal still writes only sandbox config copies under `outputs/connector_config_apply/rehearsals/`.

- A passed rehearsal now includes `post_switch_validation=validated_post_switch`, but it still does not switch the real runtime config.

- SQLite market DB snapshots must use SQLite backup, not plain `copy2`, otherwise WAL-backed rows can disappear in the sandbox.

## Tiger/MGC Dualtrack Same-Venue Scoring Guard

Date: 2026-07-06

### Decisions

- Treat the dualtrack operations ledger as invalid unless machine and human tracks use the same Tiger/MGC cost and sizing model after the Tiger/MGC profile is applied.
  - Rationale: the user value is a clean human-vs-machine comparison on the same venue, same contract, same fees, and same session calendar. A machine track scored with legacy bp costs while human fills use Tiger fixed contract costs would make attribution misleading.
  - Evidence: `tests/test_dualtrack_dt8_cycle_runner.py::test_mgc_dualtrack_close_scores_machine_and_human_with_same_tiger_contract_cost_model`.

- Keep this as a post-profile behavioral guard, not a real runtime config write.
  - Rationale: active config must remain Binance/GOLD until the operator explicitly authorizes the attended config switch, but the code path for the future Tiger/MGC profile must be covered now.
  - Evidence: the test injects a Tiger/MGC dualtrack config and local Tiger order-sync artifact, then closes one cycle through `DualTrackCycleRunner`.

- Verify the full close-cycle path instead of only the individual cost helper.
  - Rationale: the failure mode lives at integration boundaries: market symbol selection, COMEX masking, machine grid fills, Tiger human-fill import, scorer attribution, and daily ledger writing.
  - Evidence: the guard asserts both machine and human fills use `cost_model.venue=tiger_mgc`, `quantity_mode=integer_contracts`, one MGC contract per fill, notional equal to `price * 10`, and $2.70 per side.

### Gotchas

- The checked-in active runtime can still show `GOLD`, `binance_usdm`, and `cost_per_side_bp=0.5`; that is expected before attended config switch.

- This guard proves the Tiger/MGC profile path can score both tracks consistently. It does not prove the strategy has positive edge or that Tiger realtime price-feed entitlement is live.

- COMEX timestamps matter in tests: a sample cycle placed in the weekend closure will correctly skip, so same-venue scoring tests must use an open COMEX window.

## Tiger/MGC Operator Stage Status

Date: 2026-07-06

### Decisions

- Expose a read-only `connector_config_apply status` command with an `operator_stage` summary.
  - Rationale: operators need one answer to "where are we now?" without manually stitching together activation plan, rehearsal, authorization, readiness audit, post-switch validation, price-feed receipts, and active config.
  - Evidence: `python3 -m pipelines.connector_config_apply status --json`.

- Make `ready_for_attended_config_switch` a narrow stage.
  - Rationale: the system can be ready for an explicit human-approved config switch while still not being ready for machine trading or Tiger order submission.
  - Evidence: current status reports `operator_stage.stage=ready_for_attended_config_switch`, `price_feed_ready=true`, `can_switch_config_with_operator_authorization=true`, `can_trade_machine_track=false`, and `can_submit_tiger_orders=false`.

- Split price-feed status from price-feed freshness.
  - Rationale: a previously accepted Tiger price-feed receipt should not remain a live switch signal hours later. The operator stage must force a refresh when acceptance/readiness/realtime artifacts are too old.
  - Evidence: `operator_stage.price_feed_status_ready=true` can coexist with `operator_stage.price_feed_evidence_fresh=false`, producing `operator_stage.stage=price_feed_evidence_stale_refresh_acceptance` and `operator_stage.can_switch_config_with_operator_authorization=false`.

- Add a 15-minute freshness window for price-feed evidence used by the config-switch stage.
  - Rationale: realtime price-feed validation is market-sensitive; an attended config switch should be based on a fresh acceptance run, not an old market-hours receipt.
  - Evidence: `services.connector_config_apply.PRICE_FEED_EVIDENCE_MAX_AGE_SECONDS=900` and `tests/test_connector_config_apply.py::test_connector_config_status_downgrades_ready_stage_when_price_feed_evidence_is_stale`.

- Include explicit refresh commands in `operator_stage` when price-feed evidence is stale.
  - Rationale: the operator should not have to reconstruct the acceptance/audit/status sequence from old notes. The status receipt should say exactly what to run next and which command opens a read-only QuoteClient.
  - Evidence: `operator_stage.refresh_commands` includes `refresh_tiger_price_feed_acceptance`, `refresh_final_readiness_audit`, and `show_connector_switch_status`.

- Add a `--plan-only` mode for Tiger price-feed acceptance and put it first in stale refresh guidance.
  - Rationale: before opening a Tiger QuoteClient, the operator should be able to inspect the exact refresh sequence and safety boundary from a local plan artifact.
  - Evidence: `pipelines.tiger_price_feed_acceptance --plan-only --json`, `outputs/tiger_price_feed_acceptance_plan/current.json`, and `tests/test_tiger_price_feed_acceptance.py::test_tiger_price_feed_acceptance_plan_only_does_not_open_quote_client_or_refresh_artifacts`.

- Surface `operator_stage` in the connector dashboard card as read-only guidance.
  - Rationale: the browser should answer "what stage are we in and what is the next safe refresh boundary?" without requiring operators to inspect `connector_config_apply status` JSON.
  - Evidence: `dashboard-dualtrack-v5.html` renders `接入阶段`, `阶段下一步`, `刷新预览`, `真实验收`, and `交易权限`; `tests/test_dashboard_dualtrack_static.py` locks the strings and safety markers.

- Add a read-only Tiger/MGC price-feed refresh runbook artifact.
  - Rationale: at market open, the operator should have one durable package with the exact refresh sequence and safety boundary instead of copying commands from status JSON or chat.
  - Evidence: `python3 -m pipelines.connector_config_apply price-feed-refresh-runbook --json`, `outputs/connector_config_apply/price_feed_refresh_runbook_current.md`, `outputs/connector_config_apply/status_current.json`, and `tests/test_connector_config_apply.py::test_connector_config_price_feed_refresh_runbook_writes_safe_operator_package`.

- Surface the latest price-feed refresh runbook in connector status and the dashboard.
  - Rationale: operators should see whether a refresh package exists and whether its status snapshot exists without opening JSON artifacts.
  - Evidence: `latest_price_feed_refresh_runbook` in `connector_config_apply status`, `dashboard-dualtrack-v5.html` rows `刷新操作包` and `操作包证据`, and `tests/test_dashboard_dualtrack_static.py`.

- Include active runtime config in the status response.
  - Rationale: the most common confusion is assuming readiness artifacts mean the runtime has already switched. The status must show whether active config is still `binance_usdm`/`GOLD` or already Tiger/MGC.
  - Evidence: current status reports `runtime_switched_to_tiger_mgc=false`, `current_runtime.broker_provider=binance_usdm`, and `current_runtime.dualtrack_symbol=GOLD`.

- Expose the existing price-feed refresh runbook through a read-only dashboard API.
  - Rationale: the frontend and external operator tools need to fetch the market-open refresh package without generating a new runbook, opening Tiger clients, or mutating config.
  - Evidence: `GET /api/connectors/config/price-feed-refresh-runbook`, `dashboard-dualtrack-v5.html`, `tests/test_dashboard_server.py`, and `tests/test_dashboard_dualtrack_static.py`.

- Render the refresh runbook steps in the dashboard as display-only guidance.
  - Rationale: at market open the operator should see the safe sequence in the console without opening JSON/Markdown files, while still keeping execution in the terminal under explicit operator control.
  - Evidence: `dashboard-dualtrack-v5.html` renders `刷新步骤 1..4`, and `tests/test_dashboard_dualtrack_static.py` locks the labels and safety markers.

- Force the refresh-runbook API `endpoint_safety` from server code rather than trusting the artifact.
  - Rationale: `endpoint_safety` describes the behavior of the HTTP read path, so it must stay correct even if an old or malformed runbook artifact contains stale safety metadata.
  - Evidence: `pipelines/dashboard_server.py` overwrites `endpoint_safety`, and `tests/test_dashboard_server.py` verifies unsafe artifact metadata is replaced.

- Add a local COMEX refresh-window gate to the price-feed refresh runbook.
  - Rationale: at market open the operator should know whether the read-only QuoteClient acceptance step is currently appropriate, without opening Tiger or reading a separate calendar file.
  - Evidence: `refresh_window_gate` in `price_feed_refresh_runbook_current.json`, `--as-of` support in `pipelines.connector_config_apply price-feed-refresh-runbook`, dashboard row `验收窗口`, and `tests/test_connector_config_apply.py`.

- Refresh Tiger/MGC price-feed evidence during an open COMEX window before any config switch.
  - Rationale: the switch gate must be based on current market-hours evidence, not old acceptance receipts.
  - Evidence: `outputs/tiger_realtime_validation/current.json status=pass`, `bar_advanced=true`, `fresh=true`, `opens_quote_client=true`, `opens_trade_client=false`, `submits_orders=false`; `outputs/tiger_price_feed_acceptance/current.json status=accepted`; `outputs/connector_config_apply/final_readiness_audit_current.json status=go_for_attended_config_switch`.

- Refresh local MGC market bars after real-time acceptance.
  - Rationale: Tiger realtime validation proves QuoteClient can observe advancing bars, but the dashboard and scoped data-source preflight also need current bars in the local SQLite market DB.
  - Evidence: `python3 -m pipelines.tiger_futures_feed --date 2026-07-06 --contract MGCmain --limit 500` imported 500 bars and moved local coverage to `2026-07-06T09:48:00+00:00`; `outputs/data_source_preflight/MGCmain_1m/current.json status=pass`, `ready_for_live=true`.

- Move operator stage to attended config-switch readiness while keeping runtime and orders closed.
  - Rationale: after price-feed, rehearsal, authorization, and final audit are fresh, the next user-value step is explicit human review of the config switch, not automatic trading.
  - Evidence: `operator_stage.stage=ready_for_attended_config_switch`, `can_switch_config_with_operator_authorization=true`, `runtime_switched_to_tiger_mgc=false`, `can_trade_machine_track=false`, and `can_submit_tiger_orders=false`.

- Add a read-only attended switch review summary to `operator_stage` and the dashboard.
  - Rationale: the operator needs to see "ready for human config-switch review" as a first-class state without opening Markdown files or exposing the raw acknowledgement string in the browser.
  - Evidence: `operator_stage.attended_switch_review`, dashboard rows `人工切换`, `切换包ID`, `切后验收`, and `切后交易`, plus `tests/test_connector_config_apply.py` and `tests/test_dashboard_dualtrack_static.py`.

- Add a dedicated read-only attended-switch review API.
  - Rationale: future frontend/tools need a stable, redacted endpoint for final human review without parsing the full connector status or exposing the raw write acknowledgement.
  - Evidence: `GET /api/connectors/config/attended-switch-review`, `build_connector_attended_switch_review_response`, dashboard fetch `api("/api/connectors/config/attended-switch-review")`, and `tests/test_dashboard_server.py`.

- Promote current runtime and order-gate facts to top-level fields in the attended-switch review API.
  - Rationale: the operator must be able to tell, without reading nested status objects, that "ready for explicit config switch" is still not "runtime switched" and not "Tiger orders enabled."
  - Evidence: `runtime_switched_to_tiger_mgc`, `can_trade_machine_track`, and `can_submit_tiger_orders` in `GET /api/connectors/config/attended-switch-review`; `dashboard-dualtrack-v5.html`; `tests/test_dashboard_server.py`; `tests/test_dashboard_dualtrack_static.py`.

- Require same-run-date evidence before Tiger attended paper-order readiness can pass.
  - Rationale: a venue read model may display the latest known flat/account/order state, but a future API order canary must not rely on stale prior-day reconciliation, account, order-sync, kill-switch, contract, or drill evidence.
  - Evidence: `services/tiger_openapi_paper_order_readiness.py` and `tests/test_tiger_openapi_paper_order_readiness.py`.

- Separate reconciliation flatness from the attended paper-order entry gate in the Tiger venue read model.
  - Rationale: `can_open_new_orders` describes the latest reconciliation artifact only; operators need `can_enter_attended_paper_order` to know whether the stricter paper-order readiness package is current and passable.
  - Evidence: `services/tiger_venue_status.py`, `dashboard-dualtrack-v5.html`, `tests/test_tiger_venue_status.py`, and `tests/test_dashboard_dualtrack_static.py`.

- Surface Tiger paper-order readiness blockers with a read-only refresh path in the venue read model and dashboard.
  - Rationale: a blocked paper-order gate should tell the operator whether evidence is stale and which read-only refresh steps are needed, without exposing clickable execution controls or weakening the TradeClient/order boundary.
  - Evidence: `paper_order_readiness.operator_next_action`, dashboard rows `下单证据` and `刷新路径`, `tests/test_tiger_venue_status.py`, and `tests/test_dashboard_dualtrack_static.py`.

- Add an artifact-only Tiger paper-order evidence refresh runbook.
  - Rationale: future paper canary work needs a durable operator package for refreshing stale contract/reconciliation/account/order/kill-switch/drill evidence before authorization, but generating that package must not run the refresh commands.
  - Evidence: `python3 -m pipelines.tiger_openapi_paper_order_readiness --refresh-runbook --json`, `outputs/tiger_paper_order_readiness/refresh_runbook_current.json`, dashboard row `证据刷新包`, and `tests/test_tiger_openapi_paper_order_readiness.py`.

- Mark paper-order refresh runbooks stale when they no longer match the current readiness receipt.
  - Rationale: evidence can change independently of the runbook; the dashboard must not present an old refresh package as current after readiness is rerun or blockers change.
  - Evidence: `paper_order_refresh_runbook.matches_current_readiness`, dashboard `current/stale package` copy, and `tests/test_tiger_venue_status.py`.

- Expose Tiger paper-order refresh runbooks through a redacted read-only dashboard API.
  - Rationale: frontend/operator tools need the current runbook status, step labels, and safety flags, but they should not receive raw executable command text from the artifact.
  - Evidence: `GET /api/dualtrack/venue/tiger/paper-order-refresh-runbook`, `build_tiger_paper_order_refresh_runbook_response`, dashboard fetch `api("/api/dualtrack/venue/tiger/paper-order-refresh-runbook")`, and `tests/test_dashboard_server.py`.

- Redact raw command text from dashboard runbook APIs.
  - Rationale: local Markdown/JSON runbooks can carry terminal commands for deliberate operator use; browser/API consumers should receive step labels and safety flags, not directly executable command strings.
  - Evidence: `GET /api/connectors/config/price-feed-refresh-runbook`, `GET /api/dualtrack/venue/tiger/paper-order-refresh-runbook`, `_redacted_runbook_steps`, and `tests/test_dashboard_server.py`.

### Gotchas

- Top-level config-apply `status=dry_run_ready` can coexist with `operator_stage.stage=ready_for_attended_config_switch`; the former describes the last apply receipt, while the latter synthesizes all current gate artifacts.

- `price_feed_ready=true` is not permission to trade. It means the price-feed evidence cleared; strategy edge and broker-order authorization remain separate gates.

- `price_feed_status_ready=true` is weaker than `price_feed_ready=true`. The former means the last receipts passed; the latter also requires freshness.

- Current real status can move from `ready_for_attended_config_switch` to `price_feed_evidence_stale_refresh_acceptance` without any config change. That means the evidence aged out, not that the Tiger integration code broke.

- After rerunning the real read-only readiness audit with stale evidence, `final_readiness_audit_current.json` is expected to be `blocked` with blocker `tiger_price_feed_evidence_stale`.

- The first stale-refresh command is now `preview_tiger_price_feed_acceptance_refresh`; it writes only a local plan artifact and must report `opens_quote_client=false`, `opens_trade_client=false`, `submits_orders=false`, and `writes_runtime_config=false`.

- The second stale-refresh command is the real price-feed acceptance command and opens a Tiger QuoteClient if the operator runs it. That is expected for acceptance, but it still must report `opens_trade_client=false`, `submits_orders=false`, and `writes_runtime_config=false`.

- Dashboard `接入阶段` rows are display-only. They must not become a run button for plan-only, price-feed acceptance, config writes, rollback, schedule takeover, or Tiger orders.

- `price-feed-refresh-runbook` is an operator package, not an acceptance run. It writes local JSON/Markdown only; it must not open QuoteClient, TradeClient, write market DB bars, write runtime config, or submit/cancel/close orders.

- A refresh runbook must persist the `status_current.json` snapshot it was built from. Otherwise the operator package can point at evidence that does not exist.

- `latest_price_feed_refresh_runbook` in status is a summary over an existing artifact. The status endpoint must not generate a new runbook or run Tiger acceptance as a side effect.

- The status command is intentionally read-only. It must not run Tiger realtime validation, refresh market DB bars, write runtime config, open Tiger clients, or submit/cancel/close orders.

- The dashboard refresh-runbook API must only read `price_feed_refresh_runbook_current.json`. It must not call `price_feed_refresh_runbook()`, generate artifacts, run Tiger acceptance, open QuoteClient/TradeClient, write runtime config, or submit/cancel/close orders.

- Dashboard refresh steps are labels and safety markers only. They must not become clickable run buttons, hidden fetches to run plan-only/acceptance/audit, terminal automation, config writes, or Tiger order controls.

- `endpoint_safety` returned by the dashboard API is an endpoint contract, not artifact provenance. Do not pass through artifact-provided endpoint safety fields.

- `refresh_window_gate` is a local COMEX session-calendar check only. It must not open QuoteClient, open TradeClient, run Tiger acceptance, import bars, write runtime config, or submit/cancel/close orders.

- `refresh_window_gate.status=ready_to_run_acceptance_now` means the operator may run the explicit read-only acceptance command. It does not mean price-feed acceptance has passed, config can switch, or Tiger orders can be submitted.

- Real-time acceptance and local DB refresh are separate operations. Acceptance may pass while scoped data-source preflight still reports stale bars until `tiger_futures_feed` imports recent bars.

- The current `tiger_price_feed_acceptance/current.json` may show `opens_quote_client=false` if it was refreshed with `--skip-realtime-run` after the real QuoteClient validation. Use `tiger_realtime_validation/current.json` for the actual market-hours QuoteClient evidence.

- `ready_for_attended_config_switch` authorizes only the next human review step. It is not runtime switched, not machine strategy approved, and not Tiger order submission.

- `attended_switch_review` is a read model. It must not expose the raw acknowledgement string, run config apply, open SDK clients, submit/cancel/close orders, or bypass post-apply validation.

- The attended-switch review API must not expose `attended_apply_command` or the raw acknowledgement string. The exact write command remains in the local authorization Markdown for explicit terminal-only operator use.

- `can_switch_config_with_operator_authorization=true` is not the same as `runtime_switched_to_tiger_mgc=true`. Until an attended write receipt and post-switch validation exist, runtime can still be Binance/GOLD and machine/Tiger order gates must stay closed.

- `tiger_venue_status.status=ready` is a local artifact read model, not paper-order permission. `tiger_openapi_paper_order_readiness` must re-check evidence freshness for the requested run date before an attended paper canary can be considered.

- `can_open_new_orders=true` can coexist with `can_enter_attended_paper_order=false` when reconciliation is flat but paper-order readiness is stale or blocked. The dashboard `新单门` must use the stricter paper-order entry gate.

- Dashboard paper-order refresh path rows are labels only. They may say a refresh step uses read-only TradeClient evidence, but they must not run reconciliation/account/order sync, open SDK clients, submit/cancel/close orders, or write runtime config.

- The Tiger paper-order refresh runbook can include command text for terminal use, but runbook generation itself must report `opens_trade_client=false`, `submits_orders=false`, and `writes_runtime_config=false`. The dashboard may show runbook status and step count only, not executable controls.

- A `ready_for_operator_refresh` runbook is only current if `matches_current_readiness=true`. Otherwise the operator must regenerate the runbook before using its command sequence.

- The paper-order refresh-runbook API must redact raw `command` text even though the local Markdown/JSON artifact contains commands for terminal use. The dashboard may display labels, step counts, and safety flags only.

- The price-feed refresh-runbook API follows the same redaction rule as the paper-order refresh-runbook API. If an operator needs exact command text, they should read the local Markdown artifact intentionally, not receive it through dashboard JSON.

- Redaction must include nested status snapshots, not only top-level command sequences. Dashboard APIs may keep status/schema/stage summaries, but any nested `refresh_commands` carrying executable command text must be stripped.

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
  - Evidence: only `dashboard-dualtrack-v5.html`, `tests/test_dashboard_dualtrack_static.py`, and this log were changed after the P0 commit.

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

## Standard Kline Package

Date: 2026-07-06

### Decisions

- Add `data/standard-kline.js` as the reusable vanilla browser package instead of introducing React or a frontend build pipeline.
  - Rationale: the current dualtrack pages are static HTML, and the repo already vendors `lightweight-charts.standalone.production.js`; a vanilla module gives reuse without expanding the build/runtime surface.
  - Evidence: `dashboard-dualtrack-v5.html` and `dashboard-dualtrack-replay.html` both load `data/standard-kline.js`.

- Keep the adapter boundary on the backend `Bar`/`dualtrack-market-bars-v1` contract.
  - Rationale: Tiger, Binance, derived, and future providers should share OHLCV formatting while retaining separate market identity.
  - Evidence: `StandardKline.adaptBarPayload()` converts ISO UTC timestamps to epoch seconds and preserves `provider`, `source_mode`, `is_synthetic`, and array `quality_flags` in chart metadata.

- Replace the DualTrack v5 main SVG chart and DualTrack replay pane SVG charts with the standard kline package.
  - Rationale: the main operator chart and replay panes need the same candlestick, volume, marker, zoom, drag/pan, crosshair, resize, and synthetic-watermark behavior.
  - Evidence: the pages now instantiate `StandardKline.StandardKlineChart`; the old `drawChart()` main SVG path and replay pane SVG hosts are removed.

- Leave 15m/1h context charts on the existing compact SVG path for this phase.
  - Rationale: context charts are allowed to remain simplified, and changing them would increase the diff without improving the standard main/replay K line contract.
  - Evidence: `drawMini()` remains the only SVG chart path in `dashboard-dualtrack-v5.html`.

### Gotchas

- This does not choose Tiger or Binance as the default venue. The adapter standardizes format only; source priority and venue semantics stay in the existing backend/feed layer.

- `quality_flags` must stay as arrays at the frontend adapter boundary. Do not collapse them into display strings before synthetic/trust checks run.

- Synthetic display must trigger on `payload.is_synthetic === true` or any of `synthetic_seed`, `display_only`, or `not_for_trading_signal`, even if the provider string does not include `synthetic`.

- Replay still obeys closed-only attribution. The chart package renders bars and markers after attribution is available; it must not fetch `/api/dualtrack/machine` or `/api/dualtrack/human` as a fallback.

## DualTrack 12h Paper PNL Closure

Date: 2026-07-06

### Decisions

- Treat human manual fills as position events, not isolated cost rows.
  - Rationale: the owner value is a 12h human-vs-AI PNL comparison; a manual entry followed by a manual exit must realize price-difference PNL, not only per-side fees.
  - Evidence: `services/dualtrack_human.py` infers `entry`/`exit`, pairs exits against open entries, writes `dualtrack/trades/<cycle_id>_human.json`, and updates account realized PNL from paired fills.

- Add an explicit AI bracket path without replacing the legacy machine grid path.
  - Rationale: AI can now express a simple `entry` / `take_profit` / `stop_loss` paper order while existing grid/effective-plan behavior remains intact for plans without `bracket`.
  - Evidence: `services/dualtrack_store.py` preserves normalized `bracket` fields; `services/dualtrack_machine.py` routes bracket plans through deterministic entry/TP/SL/flatten simulation.

- Let an AI plan with an explicit bracket run as the machine track even when a timely human plan exists.
  - Rationale: the comparison being closed here is human manual execution versus AI bracket execution in the same window; without this override, `effective_plan` can select the human plan and erase the independent AI side.
  - Evidence: `DualTrackMachineRunner.run_effective_plan()` prefers the AI plan only when that AI plan has a `bracket`.

- Keep the default venue unchanged.
  - Rationale: choosing Tiger/MGC versus Binance/GOLD is a product/runtime decision; this milestone only makes the PNL accounting venue-agnostic.
  - Evidence: no change to `configs/dualtrack.yaml` or default `market_data` routing is required by this closure.

- Add cycle-level trade and delta fields while preserving existing fill/ledger fields.
  - Rationale: existing dashboard/API consumers still read `fills`, `tracks`, and `realized_pnl`; the closure needs additional `trades` and `delta_machine_minus_human` without breaking old contracts.
  - Evidence: `services/dualtrack_scoring.py` writes attribution `trades.machine/human`, track trade counts, and ledger delta fields.

### Gotchas

- Human exits with no explicit `event` are inferred only when there is an opposite open human position under the same `position_id`/symbol scope. A first sell in a long plan is still treated as a new short/manual entry and can be flagged out of plan.

- For bp-cost mode, an exit may omit `notional`; the engine derives exit notional from the open entry units and exit price. For venue contract mode, contracts can be inferred from the open contract lot.

- AI bracket same-bar ambiguity is conservative by default: when TP and SL are both touched in the same candle, `same_bar_priority=stop` is used unless the bracket explicitly says otherwise.

- Open human positions are not silently fabricated into broker exits. A clean realized human PNL requires a paired manual/broker exit; open-position visibility remains separate from realized fill PNL.

- The new AI bracket path is a paper simulator. It does not submit, cancel, modify, or attach broker orders.

## Standard Kline Programmatic Range Controls

Date: 2026-07-07

### Decisions

- Keep zoom, pan, and fit on the standard kline package boundary instead of adding page-specific fixes in DualTrack v5 or replay.
  - Rationale: the user value is one reusable K line package whose controls behave the same for live and replay charts.
  - Evidence: `StandardKlineChart` now wraps its lightweight-charts time scale and routes toolbar, public chart methods, and direct `timeScale().setVisibleLogicalRange()` calls through the same protected range path.

- Clamp programmatic logical ranges before passing them to lightweight-charts.
  - Rationale: very wide or far-out logical ranges can leave the chart in a bad viewport state in some browser/runtime paths; the package should keep external calls within a bounded data-plus-buffer window.
  - Evidence: `clampLogicalRange()` limits the viewport to the available candle count plus a small buffer and enforces a minimum visible bar width.

- Keep `getVisibleLogicalRange()` truthful and drive toolbar controls through native visual state changes.
  - Rationale: a wrapper that returns the target range before the chart has actually moved makes range-based verification meaningless and can hide a broken viewport.
  - Evidence: the package no longer patches `getVisibleLogicalRange()`; zoom/fit/direct `setVisibleLogicalRange()` translate bounded logical ranges into `timeScale().applyOptions({barSpacing})` plus `scrollToPosition()`, and pan uses the same scroll path.

### Gotchas

- Do not validate toolbar zoom/pan only by screenshot diff. A focused browser check must print `getVisibleLogicalRange()` before and after the first programmatic action on a freshly reloaded page.

- Programmatic chart controls must be validated with the native lightweight-charts range and pixel evidence. Do not patch getters to report intended state unless the rendered chart has already reached that state.

- Browser verification must print `document.visibilityState` before clicking controls. If the tab is hidden, rAF-backed chart commits can be suspended and a visual validation may be measuring the automation environment instead of the chart code.

- The range clamp protects chart view state only. It does not change provider/source-mode semantics, synthetic-watermark logic, trading execution, PNL, or strategy behavior.

## Standard Kline Package Extraction

Date: 2026-07-07

### Decisions

- Move the chart wrapper out of `data/standard-kline.js` into `packages/standard-kline/` (`standard-kline.js` + `package.json` + `README.md` + `LICENSE` + `standard-kline.test.js`), so it can be published as an independent, provider-agnostic module.
  - Rationale: the user wants to publish this as a standalone GitHub/npm package; the in-place file had DualTrack-specific vocabulary baked in that a generic consumer shouldn't need to know about.
  - Evidence: `dashboard-dualtrack-v5.html` and `dashboard-dualtrack-replay.html` now load `packages/standard-kline/standard-kline.js` instead of `data/standard-kline.js`.

- Remove `buildPlanPriceLines`, `buildFillPriceLines`, and `buildFillMarkers` from the package. The package only accepts generic `overlays.priceLines` / `overlays.markers` arrays via `setAdaptedData`/`setPayload`.
  - Rationale: "trade plan", "fill", and "human/machine track" are DualTrack concepts, not generic OHLCV charting concepts. A caller with a different domain model (no plans, no fills) shouldn't need to satisfy this shape.
  - Evidence: `dashboard-dualtrack-v5.html` now defines local `buildHumanPlanPriceLines`/`buildTrackFillPriceLines`/`buildTrackFillMarkers`; `dashboard-dualtrack-replay.html` defines its own local `buildTrackFillPriceLines`/`buildTrackFillMarkers`. Both build overlay arrays themselves and pass them to the package.

- Parameterize synthetic-data detection instead of hardcoding DualTrack's flag names in the package.
  - Rationale: `SYNTHETIC_FLAGS = ["synthetic_seed", "display_only", "not_for_trading_signal"]` was DualTrack's own quality-flag convention; a generic package should only ship the provider/source_mode-substring and explicit `is_synthetic` heuristics by default, and let the caller opt in extra flags.
  - Evidence: `adaptBarPayload(payload, options)` and the `StandardKlineChart` constructor now accept `options.syntheticFlags`; both dashboards pass `DUALTRACK_SYNTHETIC_FLAGS` / `REPLAY_SYNTHETIC_FLAGS` explicitly. Without an explicit opt-in, `quality_flags` values are not treated as synthetic (only `is_synthetic === true` or a provider/source_mode string containing "synthetic" trip the default detector).

- Delete `data/standard-kline.js` outright rather than keeping a duplicate/shim.
  - Rationale: a single source of truth avoids the two copies silently drifting apart.
  - Evidence: `git status` shows the file removed; both dashboards' script tags updated in the same change.

### Gotchas

- The package's own test suite (`packages/standard-kline/standard-kline.test.js`, run via `node --test`) only covers the pure data functions (`adaptBarPayload`, `clampLogicalRange`, `nearestTime`, synthetic detection). `StandardKlineChart` needs a real DOM + `lightweight-charts`, so it's still only exercised through the consuming dashboards' browser checks.
- `tests/test_standard_kline_adapter.py` still shells out to `node -e` against `packages/standard-kline/standard-kline.js` from the repo root — if the package is ever actually split into its own repo, that pytest file needs to either vendor a copy or be retired in favor of the package's own `npm test`.
- The MIT `LICENSE` in `packages/standard-kline/` uses a placeholder copyright holder ("standard-kline contributors") — replace it with the real name/organization before publishing externally.

## Human Bias Ledger DT-INV-2

Date: 2026-07-08

### Decisions

- Make `BiasLedger` the single service boundary for human market-view adjudication.
  - Rationale: `MarketViewIntake.record()` and the settle CLI must share the same fail-closed settlement path; duplicating logic in the CLI would make the safety gate drift.
  - Evidence: `services/bias_ledger.py`, `pipelines/bias_ledger_settle.py`, `services/market_view_intake.py`.

- Keep initial missing reference price entries as `status=open` with `pending_reason=missing_reference_price`, then advance them to `pending_data` only when they expire and settlement is attempted.
  - Rationale: the spec says the entry is still created at intake time, but settlement keeps it pending when price is missing; blocking should only happen after the view is overdue.
  - Evidence: `BiasLedger.append_open_view()` and `BiasLedger.settle_expired()`.

- Treat `pending_data` as a blocking terminal state until a future explicit repair flow exists.
  - Rationale: the spec requires fail-closed and does not define retry semantics for pending rows; silently retrying or mutating pending rows later would create a hidden product decision.
  - Evidence: `BiasLedger.settle_expired()` only advances current `open` entries; `guard_before_record()` blocks overdue `open` and `pending_data`.

- Keep completion audit read-only for the bias ledger.
  - Rationale: audit should report missing/stale/overdue evidence, not fix the ledger while measuring it.
  - Evidence: `CompletionAudit._human_bias_ledger()` reads ledger events, summary freshness, and overdue blockers without calling settlement.

### Gotchas

- Settlement uses the nearest 1m bar at or before `expires_at` and rejects bars older than `SETTLE_BAR_TOLERANCE_MIN`; a valid 5m bar alone will not adjudicate this v1 ledger.

- `summary.json` is derived from all current ledger entries, not only one run date. It includes `ledger_event_count` so audit can detect a stale summary.

- Views with scores between 40 and 60 become `no_claim` on a non-neutral move: they count toward Brier score but not hit rate.

- `undecidable` rows exclude both hit rate and Brier. This is intentional for moves inside the neutral threshold.

- A future repair flow for `pending_data` is not implemented in this task. If the product wants late-arriving market data to unblock old views, that needs an explicit rule for appending a new repair/adjudication event without mutating prior rows.

## DualTrack Schedule Drift Heartbeat

Date: 2026-07-08

### Decisions

- Add a dedicated `DualTrackCycleHeartbeat` service for close-cycle liveness.
  - Rationale: `schedule_status` can prove launchd shape, but it cannot prove the 12h DualTrack close artifact actually exists. The heartbeat closes that gap without touching orders, broker, or risk paths.
  - Evidence: `services/dualtrack_cycle_heartbeat.py`, `tests/test_dualtrack_cycle_heartbeat.py`.

- Derive expected close boundaries from `configs/dualtrack.yaml` `cycle_hours_utc` with a fixed `CLOSE_GRACE_MINUTES=120`.
  - Rationale: the check should follow the configured DAY/NIGHT windows instead of hardcoding dates or labels.
  - Evidence: latest required boundary was `2026-07-07T13:00:00+00:00`; heartbeat reported `fresh` after backfill.

- Require both attribution and daily ledger coverage for a boundary to count as closed.
  - Rationale: a single artifact can be partial; completion should require both the per-cycle attribution and the daily rollup.
  - Evidence: backfilled `2026-07-06_DAY` and `2026-07-06_NIGHT` attribution files plus `outputs/dualtrack/ledger/daily/2026-07-06.json`.

- Wire `_dualtrack_cycle_liveness` into `CompletionAudit` immediately after `_schedule_artifacts`.
  - Rationale: schedule drift should fail completion even if static schedule files exist.
  - Evidence: related pytest suite passes with the new audit check.

- Follow the live launchd safety rule strictly: install only after dry-run/package ack, then roll back when an originally loaded job becomes unloaded.
  - Rationale: Phase 2 touches live launchd; a half-installed scheduler is worse than a known stale scheduler.
  - Evidence: install made `dashboard` unload, rollback restored the prior shape, and `strategies` was manually bootstrapped back to loaded.

### Gotchas

- `launchctl kickstart` timeout is not by itself the source of truth. `pipelines.schedule_status --json` is the authoritative post-check for installed, matching, and loaded counts.

- Rollback can also leave a different label unloaded. After rollback, verify all 9 labels and recover only the failed label if needed.

- Current live scheduler state is still `stale_installed`: `installed_count=9`, `loaded_count=9`, `matching_generated_count=0`, `mismatched_jobs=9`. This is safe-loaded, but it is not the Phase 2 healthy DOD.

- Backfill must only run when source intraday data is complete. If data is incomplete, write a void reason such as `schedule_drift_gap`; do not fabricate a close score.

- The heartbeat can be `fresh` while launchd is still `stale_installed`. Artifact liveness and scheduler install health are separate gates.

## Frontend Shell Single State Source

Date: 2026-07-08

### Decisions

- Add `/api/system-state` as the shell's single read-only state source.
  - Rationale: every current page needs the same fail-closed global status instead of reconstructing system reality from different artifacts.
  - Evidence: `services/system_state.py`, `pipelines/dashboard_server.py`, `tests/test_system_state_api.py`.

- Keep the aggregation priority `BLOCKED > UNKNOWN > DEGRADED > RUN`.
  - Rationale: an unreadable input is more dangerous than a known degraded condition, while a known hard block must dominate every other check.
  - Evidence: system-state tests cover BLOCKED+UNKNOWN and UNKNOWN+DEGRADED priority combinations.

- Inject one vanilla JS/CSS shell into the five current pages without changing their internal layouts.
  - Rationale: the task is a shared navigation/status shell, not a redesign of page bodies.
  - Evidence: `assets/shell.js`, `assets/shell.css`, and static shell tests across `ops-dashboard.html`, `dashboard-v4.html`, `dashboard-replay-v4.html`, `dashboard-dualtrack-v5.html`, and `dashboard-dualtrack-replay.html`.

- Retire old dashboard routes as redirect stubs.
  - Rationale: `dashboard-v3.html`, `dashboard.html`, and `dashboard-replay.html` should not keep attracting new links or tests after v4/v4-replay became the current surfaces.
  - Evidence: redirect stub tests in `tests/test_dashboard_v3_static.py`, `tests/test_dashboard_replay_static.py`, and `tests/test_frontend_shell_static.py`.

- Remove OPS polling of the stale `/api/dashboard?view=ops` and `/api/public-access-health` paths.
  - Rationale: OPS should no longer create a repeated console 404 storm for retired data sources; the shell now owns the cross-page status light via `/api/system-state`.
  - Evidence: `ops-dashboard.html` no longer contains those paths or `dashboard-v3.html`.

### Gotchas

- The current live `/api/system-state` is `DEGRADED`, not `BLOCKED`: latest strategy reconciliation artifacts report no blocked naked position, while schedule status is still `stale_installed` and the latest daily review is `fail`.

- `dualtrack_heartbeat` can be `RUN` while `schedule` is `DEGRADED`; close-cycle artifacts and launchd installation health are intentionally separate checks.

- The shell shows `UNKNOWN` on fetch failure and never hides the status lamp. A missing or malformed artifact must not become green UI.

- Browser favicon requests can create console 404 errors even when page JS is healthy. Current active pages explicitly set `rel="icon" href="data:,"` so console-error checks stay meaningful.

## Binance Protective/Emergency-Close Error Visibility

Date: 2026-07-08

### Decisions

- Capture the actual Binance error body (`{"code", "msg"}`) on protective-order posting, protective-order recovery, protective-coverage checks, and the protective-failure emergency close, instead of the bare `str(exc)` from `urllib.error.HTTPError`.
  - Rationale: `urllib` raises `HTTPError` before its response body is read; every one of these four sites collapsed every rejection reason into the generic "HTTP Error 400: Bad Request", which is exactly why root-causing a live recurring naked-position incident (two occurrences, 2026-07-05 and 2026-07-07/08, both with identical `protective_status=failed` + `emergency_close=failed` signatures) took manual archaeology through raw order-request artifacts instead of a one-line log read.
  - Evidence: new `_binance_exception_error()` helper (`services/broker_adapter.py`) reuses the existing, already-correct `_http_error_payload`/`_binance_error_message` pair (previously only applied to the entry-submit path via `_classify_binance_entry_submit_error`) and applies it at the three gap sites that build `protective_errors`/`emergency_close` dicts, plus a fourth read-only site (`_existing_resting_protective_coverage`) where the original string-shaped `error` field is preserved but its content is now enriched.
  - Tests: `tests/test_binance_demo_broker_adapter.py::test_binance_demo_adapter_protective_and_emergency_close_http_errors_capture_binance_body` (asserts the real Binance `code`/`msg` survives into `protective_errors` and `emergency_close`, TDD red confirmed against the un-fixed code first) and `::test_binance_demo_adapter_protective_non_http_error_keeps_generic_message` (regression guard: non-HTTP exceptions, e.g. `TimeoutError`, still produce `str(exc)` with no `http_status`/`binance_error` keys — this fix is additive-only for `HTTPError`, not a behavior change for other exception types).

- Root cause of the underlying rejection is still **not confirmed**. The failure signature (every `reduceOnly=true` order — stop-loss, take-profit, and the plain-MARKET emergency close — rejected with HTTP 400; the non-`reduceOnly` LIMIT entry always succeeds) is consistent with a Binance Hedge Mode / `positionSide` mismatch, but a direct read-only check (`GET /fapi/v1/positionSide/dual`) returned `401 {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action"}` for this demo API key, so the hypothesis could not be verified in this session.
  - Rationale for not guessing a fix: acting on an unconfirmed diagnosis on the live-money-adjacent order path violates this repo's own verification discipline; this fix intentionally does nothing except make the next occurrence self-diagnosing.
  - Next step (not done here): the next time `protective_errors`/`emergency_close` fires, the captured `binance_error.code`/`msg` will state the real reason directly, closing this loop with evidence instead of speculation.

### Gotchas

- `_binance_exception_error()` is intentionally the *only* new abstraction — the entry-submit path (`_classify_binance_entry_submit_error`) already had correct error-body capture before this change and was left untouched to keep the diff minimal on a file with a history of mainnet-blocking bugs.
- Downstream consumers (`dashboard_state.py`, `health_check.py`, `trade_ticket_notifier.py`, `paper_executor.py`) all read these dicts via `.get(...)` with no exact-shape assertions, so adding `http_status`/`binance_error` keys is safe; `trade_ticket_notifier._protective_error_summary()` in particular will now surface the real Binance rejection reason in operator-facing alert text instead of a useless generic HTTP status line.
- Two live naked-position incidents on `gold_1m_macd` (Binance USDM demo/testnet) during this investigation window were manually flattened via `pipelines.binance_demo_close_position --confirm-close-demo-position` after dry-run verification; both confirmed `system_state=READY`/`exchange_positions=[]` afterward. Neither incident involved real money.

## 2026-07-08 DualTrack Focus Mode Schedule

### Decisions

- Park the strategy fleet for the 12-hour human-vs-AI DualTrack loop.
  - Rationale: owner scope is now only the human manual range/trade lane plus the DualTrack machine grid lane; the wider MACD/chan/bollinger/vwap/breakout fleet is negative expected value for the current close-the-loop objective and was tied to two demo naked-position incidents.
  - Evidence: `configs/strategy.yaml` keeps all 25 strategy entries but has 0 enabled strategies; `configs/pipeline.yaml` sets `demo_trading.enabled=false`.

- Make local launchd generation profile-driven.
  - Rationale: focus mode should be reversible without deleting builders or strategy definitions.
  - Evidence: `schedule.profile=dualtrack_focus` generates only `dualtrack-cycle`, `dualtrack-live-tick`, `dashboard`, and `deadman-ping`; `schedule.profile=full` restores the 9-job schedule.

- Generated schedule plists must not depend on the shell environment at generation time.
  - Rationale: a regenerated plist must not silently strip deadman routing because the operator shell lacks runtime env vars.
  - Evidence: schedule generation and `deadman_ping` call `apply_live_env()` and load keys from `configs/live.env`; tests cover key presence/absence without committing secret values.

- Treat daily review and runner heartbeat as parked in focus mode, not as passing trading evidence.
  - Rationale: parked checks should not degrade the focus loop, but they also should not pretend the full workflow is operating.
  - Evidence: `completion_audit.daily_review_run` returns `warn` with `parked_by_focus_mode`; system-state removes `daily_review` under focus and keeps `dualtrack_heartbeat`.

- Keep bias-ledger intake alive outside the parked `trading-plan` launchd job.
  - Rationale: human direction scoring is required for the 12-hour comparison loop.
  - Evidence: `dashboard_server` still exposes the market-view intake POST path and CLI/Obsidian intake paths still call `MarketViewIntake.record()`.

### Recovery Path

- To return to full mode: set `configs/pipeline.yaml` `schedule.profile` to `full`, re-enable the selected strategy entries in `configs/strategy.yaml`, decide explicitly whether `demo_trading.enabled` should be restored, regenerate schedules, then run schedule install dry-run before applying.

### Validation

- TDD red-to-green targeted tests covered focus config, profile job sets, byte-identical live-env generation, orphan removal/rollback, focus audit/system-state semantics, and all-disabled runner no-op.
- Live install on 2026-07-08 ended `active` with `required_count=4`, `loaded_count=4`, `matching_generated_count=4`, `orphan_count=0`; 5 old full-mode LaunchAgents were backed up and removed.
- Full test suite: `1203 passed`.

### Gotchas

- In focus mode a healthy launchd surface is 4 jobs, not 9. Seeing no `runner`, `strategies`, `trading-plan`, `daily-review`, or `evening-review` launchd label is expected.
- `completion_audit` can still be overall `fail` because business evidence such as the human bias ledger is missing; that is not the same as schedule failure.
- Do not print deadman URL values in logs, tests, decision notes, or PR text; only report key presence and delivery status.


## 2026-07-08 DualTrack Focus Mode Feed Gap

### Decisions

- Add a dedicated focus-mode GOLD 1m feed heartbeat instead of re-enabling the full `strategies` launchd job.
  - Rationale: GOLD 1m bars are shared DualTrack infrastructure, but the existing automatic refresh lived inside the fleet strategy job that focus mode intentionally parked.
  - Evidence: `pipelines/gold_1m_feed_heartbeat.py` only calls `run_binance_usdm_1m_feed_import`; focus launchd now includes `com.wendy.trading-orchestrator.gold-1m-feed`.

- Keep `full` schedule at 9 jobs.
  - Rationale: full mode already refreshes GOLD 1m through `pipelines.strategies`; adding a second heartbeat there would duplicate writes.
  - Evidence: `services/schedule_profiles.py` adds `gold-1m-feed` only to `FOCUS_SCHEDULE_LABELS`.

- Let system-state freshness use the local market DB when the preflight artifact is stale.
  - Rationale: the heartbeat writes bars to SQLite, not `data_source_preflight/current.json`; reading only the artifact would keep reporting stale data after the feed is actually healthy.
  - Evidence: `/api/system-state` now reports `data_freshness=RUN` with `source=local_market_db`.

- Add a one-hour freshness gate to naked-position reconciliation.
  - Rationale: a stale `BLOCKED_*` reconciliation snapshot should not remain a permanent live warning after focus mode stops refreshing demo strategy reconciliation.
  - Implementation choice: fresh `BLOCKED_*` stays `BLOCKED`; stale active demo reconciliation becomes `UNKNOWN`; stale inactive/demo-disabled reconciliation is `RUN` with `skipped=true` because no demo strategy is currently scheduled to refresh it.

### Validation

- Live dry-run showed exactly one new job: `com.wendy.trading-orchestrator.gold-1m-feed`; the existing 4 focus jobs were already current and no orphans were planned.
- Live install ended `active` with `required_count=5`, `loaded_count=5`, `matching_generated_count=5`, and `orphan_count=0`.
- GOLD 1m DB latest bar advanced from `2026-07-08T04:06:00+00:00` to `2026-07-08T05:08:00+00:00` after the new heartbeat ran.
- Full test suite: `1210 passed`.

### Gotchas

- `pipelines/backfill_gold_1m.py --refresh-only` also calls the same import function, but it is a manual/backfill tool, not a launchd heartbeat.
- `data_source_preflight/current.json` may remain stale even when the market DB is fresh; system-state must look at the DB for this focus-mode feed heartbeat.
- Rollback of a newly created launchd job has no old plist backup; rollback must remove the created plist rather than block on a missing backup.


## 2026-07-08 Goldbot Dashboard Freshness Triage

### Decisions

- Do not change `dashboard-v4.html` for the park-ai-intel site maintenance pass.
  - Rationale: the local dashboard HTML at `/Users/wendy/trading-orchestrator/dashboard-v4.html` is byte-identical to `https://goldbot.park-ai-intel.com/dashboard-v4.html`; the dashboard shell itself is not stale.
  - Evidence: byte comparison returned equal on 2026-07-08.

- Treat the local dashboard API as fresh in this check.
  - Rationale: `http://127.0.0.1:8765/api/dashboard?view=trader` returned `run_date=2026-07-08`, latest 5m bar `2026-07-08T03:00:00+00:00`, latest quote `2026-07-08T03:04:00Z`, and performance generated at `2026-07-08T03:04:31+00:00`.
  - Evidence: dashboard API response from the local service.

### Gotchas

- The public `goldbot.park-ai-intel.com/api/dashboard?view=trader` endpoint can return `403 Forbidden`; use the local `127.0.0.1:8765` endpoint for operator freshness checks unless public access is intentionally enabled.
- The portal repo only owns the link to Goldbot. Dashboard data, trading API, strategy state, and freshness are owned by `/Users/wendy/trading-orchestrator`.

## 2026-07-08 Command Center for DualTrack Focus Mode

### Decisions

- Build `command-center.html` as the default landing page and route `/` to it.
  - Rationale: the owner now needs one 10-second command surface for system health, current DualTrack cycle, human-vs-machine PnL, bias calibration, and the next safe action.
  - Evidence: `command-center.html`, `pipelines/dashboard_server.py`, `tests/test_command_center_static.py`.

- Add `GET /api/command-center-state` as a read-only composition endpoint over existing system-state, cycle, ledger, bias-ledger summary, and DualTrack cycle heartbeat surfaces.
  - Rationale: Command Center should not create a new judgment engine; it should display the current state from already-owned sources and fail closed to `UNKNOWN` when a source is unreadable.
  - Evidence: `services/command_center.py`, `tests/test_command_center_api.py`.

- Drop the old `docs/command-center-brief.md` fleet concepts for this phase: no decision funnel, no GateWaterfall, no 21-strategy table, and no new review-marker API.
  - Rationale: those concepts were written for the parked strategy fleet. Focus mode needs DualTrack health and next-action clarity, not another fleet operations dashboard.
  - Evidence: `codex-task-05-command-center.md`, `services/command_center.py`, `command-center.html`.

- Keep `dashboard-v4.html` reachable by URL but remove the old "驾驶舱" link from the primary shell navigation.
  - Rationale: `dashboard-v4.html` still has legacy value and replay links, but it is no longer the main room in DualTrack focus mode. The primary nav should start with "指挥台", then "作战台", then "运维".
  - Evidence: `assets/shell.js`, `tests/test_frontend_shell_static.py`.

### Gotchas

- `cycle_window()` always chooses the active 12-hour window for a timestamp, so Command Center treats `revealed` as a phase derived from the cycle payload and heartbeat evidence, not by inventing another cycle selector.

- Bias-ledger `summary.json` may not exist yet. That is a normal empty calibration state and must render as "尚未有人轨方向分裁决记录", not as an error.

- The page must never default to green when the command-center API fetch fails. The fallback state is `UNKNOWN` with "数据不可读".

- The shell still retains the `cockpit -> dashboard-v4.html` URL mapping for deep links and replay returns even though cockpit is no longer in the primary nav.

## 2026-07-08 DualTrack Console Functional Fixes

### Decisions

- Treat `runtime_status.status == "warn"` during an open cycle as a display clarity issue, not a backend state change.
  - Rationale: backend `warn` currently means "not closed yet" when no sub-check is blocked; the frontend now renders that as "盘中 · 正常" and reserves alarm copy for actual blocked checks.
  - Evidence: `dashboard-dualtrack-v5.html`, `tests/test_dashboard_dualtrack_static.py`.

- Route the main K-line timeframe selector through the backend market-bars API.
  - Rationale: the existing 1m/5m buttons had no handler; switching now updates `state.mainTf`, requests `timeframe=${state.mainTf}`, and rerenders the same human-only overlays against the new candles.
  - Evidence: `bindTimeframeSegment`, `loadMainMarket`, and the static contract test.

- Honor the requested timeframe in the read-only default market feed fallback.
  - Rationale: browser validation showed `/api/dualtrack/market/bars?timeframe=5m` could still return the default 1m candidate when no symbol was specified; the reader now checks native 5m rows first, then derives the requested timeframe from fresh fallback 1m bars.
  - Boundary: default no-symbol requests may skip stale candidates to find a fresh fallback, while explicit symbol requests keep stale real derived bars instead of silently falling to synthetic display data.

- Replace the 15m/1h handwritten SVG context charts with `StandardKlineChart` instances without overlays.
  - Rationale: context charts are for higher-timeframe direction only; using the standard K-line package makes them readable while avoiding any fill markers or plan price lines.
  - Blind-protocol guard: context rendering passes no `markers` and no `priceLines`; the main chart still reads only `state.human?.fills`.

- Keep machine fill density controls machine-scoped.
  - Rationale: duplicate TP/SL/fill prices should be grouped when machine overlays are rendered after close, but the intraday human chart must keep its own markers unchanged.
  - Strategy: `buildMachineOverlay()` dedupes near-identical price lines within 0.05% and buckets multiple markers that land on the same candle time.

- Disable replay buttons when the replay URL is missing.
  - Rationale: an empty `href` refreshes the current page and looks like a broken action; the UI now shows "暂无可回放周期" / "暂无完整回放" instead.

### Gotchas

- Do not "fix" WATCH by changing `pipelines/dashboard_server.py` status semantics; other code may still depend on `warn` meaning open-cycle watch state.

- `dashboard-dualtrack-v5.html` currently has no separate machine K-line card; the machine overlay helpers are intentionally not attached to the intraday main chart.

- Context charts must remain fill-free. Adding markers there would create a new blind-protocol leak surface even if the data came from the human track.

## 2026-07-08 Phase D Design Tokens Rollout

### Decisions

- Make `assets/tokens.css` the only visual token source for the active command, DualTrack, OPS, and replay pages.
  - Rationale: page-local `:root` color/type definitions drifted across rooms; all target pages now link `assets/tokens.css` before inline styles and consume token variables.
  - Evidence: `assets/tokens.css`, `tests/test_design_tokens_static.py`.

- Keep the approved mockup immutable.
  - Rationale: `mockups/design-tokens-proposal.html` is the owner-approved visual spec; implementation changes must adapt to it, not rewrite it.
  - Evidence: `git diff -- mockups/design-tokens-proposal.html` is empty.

- Apply the three owner Q decisions as implementation rules.
  - Q1: `ops-dashboard.html` moved from the old light paper palette to the shared dark token palette.
  - Q2: K-line up/down defaults and replay candlestick config now use run/block literals `#35d07f` / `#ef5f5f`; track markers keep human/machine identity colors.
  - Q3: external IBM/Hanken font links were removed from replay-v4; pages use the system `--sans` / `--mono` stacks.

- Lock color drift with an explicit style-literal whitelist.
  - Rationale: future style edits should not introduce ad hoc colors. The new static test extracts HTML `<style>` blocks and allows only token literal values from `assets/tokens.css`; `assets/shell.css` must use `var(--*)` and no color literals.
  - Boundary: JS chart configuration can still hold required K-line literal values, because Q2 explicitly requires those defaults.

- Use this old-variable to token mapping:
  - `--surface`, page-local `--panel` -> `--panel`; `--surface2`, `--surface3`, page-local `--panel2` -> `--panel2`.
  - `--text` -> `--ink`; `--line` -> `--rule`; `--line2` -> `--rule-strong`.
  - brand amber/gold -> `--gold`; status amber -> `--warn`.
  - `--teal` / `--up` -> `--run`; `--red` / `--down` -> `--block`.
  - `--blue` / `--cyan` -> `--human`; `--violet` -> `--machine`.
  - `--gray` -> `--unknown`; `--dim` -> `--faint`; old dark/light OPS palette -> shared dark tokens.

- Replace visual emphasis shadows with borders or token backgrounds.
  - Rationale: Phase D forbids box-shadow and standardizes shape through `--r`; track identity accents now use border-left instead of inset shadow.

- Update only the obsolete color assertions in `tests/test_dashboard_dualtrack_static.py`.
  - Rationale: the old test asserted inline `--bg:#08090b` and `--gold:#d8aa3f`, which conflicts with the new token-source rule. Blind-answer assertions were left intact and stayed green.

### Gotchas

- Do not reintroduce page-local `:root` color variables in the five target HTML files; add new shared visual values to `assets/tokens.css` and extend the whitelist intentionally.

- `color-mix(in srgb, var(--token) ...)` is the preferred way to express subtle tinted backgrounds in page styles without adding new color literals.

- `dashboard-dualtrack-replay.html` needs a closed cycle query parameter for full visual validation. `?layout=dualtrack` without `cycle` correctly renders the closed-only refusal state.

- `packages/standard-kline/standard-kline.js` must remain standalone, so its default K-line color literals are allowed; page overlays should pull identity/state colors from CSS tokens where practical.

- OPS dark conversion can make old low-alpha light-paper layers invisible if `rgba(255,250,241,...)` or `rgba(38,31,18,...)` returns. The static test blocks the old `#f6efe4` / `#fffaf1` / `#17130c` anchors, but visual review still matters for contrast.

## 2026-07-08 Task 08 Chart Indicators and Composition

### Decisions

- Promote `packages/standard-kline` to v0.2 with provider-agnostic EMA/MACD support.
  - Rationale: indicator math belongs in the reusable chart package, while business overlays stay in the calling dashboard.
  - Evidence: `computeEma`, `computeMacd`, `setAdaptedData(..., {indicators})`, package version `0.2.0`, and the Node fixture tests.

- Use the vendored Lightweight Charts 5.2 pane API for MACD.
  - Rationale: the bundled library exposes `addPane`, `removePane`, `paneIndex`, and pane-local `addSeries`, so MACD can render in a real lower pane instead of a fake overlay.
  - Evidence: browser validation created a MACD pane and series without page errors.

- Merge same-band main-chart price lines before rendering.
  - Rationale: range, level, fill, target, and invalidation lines can land within 0.05%; grouping keeps the chart readable while preserving all titles.
  - Priority: invalid > range > fill/target > level.

- Keep context charts bars-only with optional EMA50.
  - Rationale: context panels are for higher-timeframe direction, not intraday execution evidence; no markers or price lines are passed into context charts.
  - Blind-protocol guard: `renderContextKline` still passes no `markers` and no `priceLines`, and `renderMainKline` reads only `state.human?.fills`.

- Persist user-facing chart controls in localStorage.
  - Rationale: EMA defaults on, MACD defaults off, and the two context slots default to 15m/1h but can be independently changed to 15m/30m/1h/4h.
  - Evidence: browser validation toggled MACD on and changed `ctx15` to 30m with persisted storage and updated title.

### Gotchas

- `packages/standard-kline` must stay free of DualTrack and human/machine business vocabulary; the package test encodes forbidden terms so the test file itself does not pollute direct text searches.

- MACD depends on the vendored Lightweight Charts 5.2 pane API. If the vendor bundle is replaced, re-check `addPane`, `removePane`, `paneIndex`, and pane-local `addSeries` before changing chart code.

- Context charts must remain fill-free. Adding markers or price lines there would create a blind-answer leak surface even if the source data is otherwise safe.

- `mockups/design-tokens-proposal.html` remains immutable; Task 08 only consumes the approved tokens and keeps the token color whitelist green.

### Evidence

- Red phase: new package tests initially failed because `computeEma` / `computeMacd` were undefined; new dashboard static tests initially failed on missing merge, selectors, composition, and indicators.
- Green phase: `node --test packages/standard-kline/standard-kline.test.js`, `python3 -m pytest -q tests/test_dashboard_dualtrack_static.py tests/test_design_tokens_static.py tests/test_standard_kline_adapter.py`, and full `python3 -m pytest -q` passed.
- Browser phase: `http://127.0.0.1:8875/dashboard-dualtrack-v5.html` loaded without JS errors; main/context canvases were nonblank; MACD pane/toggle and context timeframe persistence worked.

## 2026-07-08 Browser Comment Fixes for Task 08

### Decisions

- Display all DualTrack console times as Beijing time.
  - Rationale: raw UTC slices made `locked_at=2026-07-08T01:00:04+00:00` appear as `01:00`, while the user mental model is Beijing `09:00`.
  - Evidence: the blind title now renders `你的作战单 · 已锁定 北京 09:00`; cycle start renders `开始 北京 21:00`; top clock includes `北京时间`.

- Make a locked blind plan visibly read-only.
  - Rationale: after lock, the form is not a draft surface. It now syncs to the locked plan snapshot, disables all form controls, and applies a grey locked style.
  - Evidence: browser validation confirmed `#planForm.locked` and all plan inputs/buttons disabled.

- Keep the chart toolbar short and push full market-source detail into `title`.
  - Rationale: the standard-kline toolbar was overflowing into the right rail. The visible label now uses a short source formatter such as `GOLD 1m · 本地缓存 · 240根`, while the full `binance_usdm_fallback` technical string stays in the title.
  - Boundary: `fallback` is not hidden; chart-foot/source title explains it as `fallback：主源不可用或不新鲜`.

- Load more bars for interactive zoom-out.
  - Rationale: 96 bars made the left side feel artificially capped. Main and context market requests now use `MARKET_BAR_LIMIT = 240`.

- Add standard-kline time affordances.
  - Rationale: TradingView-style use needs visible time context. The package now formats time axis labels through configurable `timeZone`/`locale` and shows crosshair time in the toolbar on hover.

- Make EMA visible and editable.
  - Rationale: a bare `EMA` toggle did not explain which periods were active. The dashboard now shows editable `EMA 20` / `EMA 50` controls with color dots, persisted through localStorage.

- State context source truthfully.
  - Rationale: the 15m/30m/1h/4h panels are not guaranteed native exchange timeframe bars. The UI now says `由1m聚合` when that is the backend source mode and `原生K线` only for native/requested series.

### Gotchas

- Do not label UTC-derived fields by slicing the ISO string. Always format through the Beijing helper when the page is user-facing.

- Do not remove `binance_usdm_fallback` from the full source title; the short label can be readable, but provenance must remain inspectable.

- `services.command_center._parse_time(None)` must return `None`, not `parse_utc(None)`, because the latter resolves to real current time and makes fixed phase tests date-sensitive after the fixture cycle end.

### Evidence

- Static/browser-comment regression: `python3 -m pytest -q tests/test_command_center_api.py tests/test_dashboard_dualtrack_static.py tests/test_design_tokens_static.py tests/test_standard_kline_adapter.py` passed.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed.
- Full regression: `python3 -m pytest -q` passed with `1244 passed`.
- Browser phase: `http://127.0.0.1:8765/dashboard-dualtrack-v5.html` loaded with no JS errors; screenshot saved at `outputs/codex_browser_comments_fix.png`.

## 2026-07-09 Task 09a Split Canvas Rebuild

### Decisions

- Ship Task 09 in two steps and complete only 09a in this pass.
  - Rationale: the owner value is the new mental model first: two independent canvases with the same clear widget set. The order-level trades/config/unrealized PnL work changes backend data contracts and belongs in 09b.
  - Boundary: `dashboard-dualtrack-v5.html` remains untouched and `dashboard-dualtrack-split.html` is a parallel page, not a cutover.

- Add the split canvas as a new shell entry named `双画布`.
  - Rationale: the existing `作战台` link remains production-safe on v5, while the new page is reachable for review and incremental hardening.

- Keep 09a on existing read endpoints only.
  - Reused: cycle/current, plan, machine, human, ledger, runtime/status, market/bars, attribution only after `cycleClosed`.
  - Deferred to 09b: `/api/dualtrack/trades/<cycle_id>?track=...`, `/api/dualtrack/config`, `apply_unrealized()`, and full risk/PnL data wiring.

- Render real K lines through `standard-kline`, not mockup drawings.
  - Rationale: the split page must inherit the time axis, hover/crosshair, source metadata, and Lightweight Charts behavior from the standard package.
  - Evidence: the browser DOM contains six chart hosts with real canvas children and `window.dualtrackStandardKline` is present.

- Encode the 14-widget registry as static page structure before deeper data wiring.
  - Rationale: future changes can target a stable widget slug (`direction`, `levels`, `signal`, `kline`, `context`, `order`, `risk`, `position`, `fills`, `pnl`, `data`, `status`, `review`, plus header `phase`) without re-litigating layout names.

### Gotchas

- Do not fetch machine order-level trades in mid phase. 09a intentionally has no `/api/dualtrack/trades/` call path, and the machine canvas keeps the blind veil visible in `mid`.

- Do not rename `pnl` back to `balance`, and do not display `Context图`; the owner-approved wording is `收益`/`pnl` and `大级别图`/`context`.

- Risk widget copy must keep the simplified-liquidation warning visible. The 09a skeleton includes the directional labels (`最大可亏(距SL)` versus `距失效价`) but real leverage/config math is still 09b.

- The split page is now part of the design-token whitelist. Any new inline color literal in its style block should fail `tests/test_design_tokens_static.py`.

- Browser visual proof matters here: static tests can lock naming and endpoints, but only browser validation catches right-edge overflow, missing chart canvases, and phase display mistakes.

### Evidence

- Red phase: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_design_tokens_static.py` failed because `dashboard-dualtrack-split.html` and the shell/cache registrations did not exist.
- Green phase: the same command passed with `14 passed`.
- Focused regression: `python3 -m pytest -q tests/test_command_center_api.py tests/test_dashboard_dualtrack_static.py tests/test_dashboard_dualtrack_split_static.py tests/test_design_tokens_static.py tests/test_standard_kline_adapter.py` passed with `50 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `16` tests.
- Full regression: `python3 -m pytest -q` passed with `1251 passed`.
- Browser phase: `http://127.0.0.1:8765/dashboard-dualtrack-split.html` loaded with 0 console errors, no horizontal overflow at `1451x1324`, six K-line canvas hosts, and machine `mid`/`post` phase toggling correctly hid/restored the blind veil.

## 2026-07-09 Task 09b Split Canvas Data Wiring

### Decisions

- Add `apply_unrealized(trades, mark_price, *, mark_fresh=True)` as a pure function.
  - Rationale: open trade unrealized PnL needs one shared calculation for close-cycle attribution and live trades endpoints.
  - Fail-closed behavior: missing, non-finite, or stale marks set `unrealized_pnl` to `null`; closed trades are not given an unrealized value.

- Add two read-only API surfaces: `/api/dualtrack/trades/<cycle_id>?track=human|machine` and `/api/dualtrack/config`.
  - Rationale: split widgets need order rows and leverage/config without coupling to broker/write paths.
  - Safety: config only exposes display-safe fields such as `max_leverage`; no credential or secret fields are included.

- Reject machine order rows during mid-cycle with HTTP 403.
  - Rationale: returning a visually masked machine order table would still leak through DOM/network. A 403 is a cleaner blind-protocol boundary than sending hidden data.
  - Browser evidence: the page itself does not call machine trades in mid; a manual probe returned 403.

- Wire split widgets to real 09b data while keeping v5 untouched.
  - `fills`: human mid uses order rows; machine mid uses blind aggregate copy; machine post can render rows.
  - `pnl`: uses trades summary plus ledger realized base.
  - `risk`: reads `state.config?.max_leverage`; liquidation remains labeled `简化估算`.
  - `review`: keeps direction-only grading and leaves key-level/signal rows as `未建立评分口径`.

### Gotchas

- `null` must render as `--`, not `$0.00`. The split page money/number helpers now treat `null` and empty string as missing before coercing to `Number`.

- Do not let request query params force a machine reveal. Handler-level trades requests ignore attacker-provided `as_of`; deterministic tests call the builder directly with `as_of`.

- A 403 network probe will appear as a browser console resource error if manually fetched from DevTools/Playwright. Page-load console still needs to be checked separately; the page itself avoids that request in mid.

- `apply_unrealized()` must not mutate input trade rows. Several existing stores cache trade JSON, so mutating rows in place would make stale mark data look fresh later.

- Risk values are display-only. The new risk widget math must not feed back into order submission, risk monitor, broker state, or the machine runner.

### Evidence

- Red phase: `tests/test_dualtrack_09b_unrealized.py` failed on missing `apply_unrealized`; API/static tests then failed until trades/config/page wiring existed.
- Green phase: `python3 -m pytest -q tests/test_dualtrack_09b_unrealized.py tests/test_dualtrack_09b_api_contracts.py tests/test_dashboard_dualtrack_split_static.py` passed with `15 passed`.
- Focused regression: `python3 -m pytest -q tests/test_dualtrack_09b_unrealized.py tests/test_dualtrack_09b_api_contracts.py tests/test_dualtrack_api_contracts.py tests/test_dualtrack_dt4_scoring_ledger.py tests/test_dashboard_dualtrack_static.py tests/test_dashboard_dualtrack_split_static.py tests/test_design_tokens_static.py tests/test_standard_kline_adapter.py` passed with `72 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `16` tests.
- Full regression: `python3 -m pytest -q` passed with `1259 passed`.
- Browser phase: `http://127.0.0.1:8876/dashboard-dualtrack-split.html` loaded with 0 page-load console errors; config returned `max_leverage=10` with no secret text; human trades returned 200; machine trades manual mid-cycle probe returned 403; blind veil remained visible.

## 2026-07-09 Task 10a Split Canvas Frontend Polish

### Decisions

- Keep Task 10a strictly frontend and defer the real trading chain to Task 10b.
  - Rationale: 10a needed to establish trustworthy layout, phase gating, and copy semantics before wiring real POST payloads and TP/SL interaction.
  - Boundary: the three POST handlers remain stubs except for removing the internal task suffix from their `source` string; no backend API behavior changed.

- Replace the old 7/3 chart row with a vertical composition: main K-line full width, two context charts below in a two-column row.
  - Rationale: two side-by-side canvases make the old 70/30 same-row ratio mathematically unusable; browser measurement now shows the main chart at roughly 96% of its canvas width at 1280/1440/1600.

- Move human buy/sell buttons below the standard-kline toolbar instead of overlaying the toolbar row.
  - Rationale: the real chart toolbar exists and must remain clickable; browser DOM validation measured zero intersection area between `.inbtns` and every toolbar button.

- Drive default tabs and action disabled states from the real cycle phase.
  - Rationale: viewing non-current phases is useful, but actions there must fail closed. Non-current phase buttons now get disabled with a `非当前阶段` title.

- Shrink the machine mid-cycle K-line into a 120px blind PnL strip.
  - Rationale: mid-cycle machine detail must remain blind, and a 500px empty chart frame was wasting attention. The strip shows only allowed aggregate PnL.

- Make status, empty position, risk, and review copy operator-facing.
  - Rationale: raw runtime keys and empty `--` matrices looked like debug output. Runtime `warn/run/block/unknown` now maps to Chinese states, empty position/risk renders `无持仓`, and unclosed review renders one pending sentence.

- Add provider-agnostic `standard-kline` support for compact toolbar controls and duplicate-id sanitization.
  - Rationale: small context charts only need fit plus timeframe controls, and multi-instance vendor DOM ids must not repeat. Package version moved to `0.2.1`.

### Gotchas

- Do not bring back `09a`/`09b` labels in `dashboard-dualtrack-split.html`; the static test now treats internal milestone labels as UI leakage.

- `machine_fills_hidden` should not be rendered or even used as page copy. Use the derived cycle phase for the user-facing machine detail state.

- The split page breakpoint must keep two canvases at 1280px. The single-column breakpoint is now below that, so future layout edits need browser proof at 1280.

- `standard-kline` id sanitization must stay idempotent; it runs after chart setup and resize, so repeated resize must not append suffixes repeatedly.

- Task 10b still owns true order payloads, fresh-price fail-closed behavior, TP/SL drag lines, and visible POST errors. Do not claim this 10a pass makes the page trade-ready.

### Evidence

- Static/token regression: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_design_tokens_static.py` passed with `19 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `17` tests.
- Browser phase: `http://127.0.0.1:8877/dashboard-dualtrack-split.html` loaded with 0 console errors at 1280/1440/1600; toolbar/button intersection area was 0; canvases stayed two-column; main chart width ratio was ~0.96; machine blind stage was 120px; duplicate ids were 0; screenshots saved to `outputs/codex-task10a-split-1280.png`, `outputs/codex-task10a-split-1440.png`, and `outputs/codex-task10a-split-1600.png`.
- Full regression: `python3 -m pytest -q` passed with `1263 passed`.
- Protected files: zero diff for `dashboard-dualtrack-v5.html`, `tests/test_dashboard_dualtrack_static.py`, `pipelines/lab_run.py`, `services/lab_registry.py`, and `services/lab_r4_promotion.py`.

## 2026-07-09 Task 10b Split Canvas Trading Chain

### Decisions

- Replace the split page's placeholder POSTs with real plan, order, and verdict payloads.
  - Plan lock now submits `cycle_id`, `direction`, `range`, `key_levels`, `invalidation`, `confidence`, and `source`.
  - Order submit now sends `cycle_id`, `ts`, `side`, `order_type`, `price`, `notional`, `sl`, `tp`, and `source`.
  - Verdict submit now sends `cycle_id` and the operator note.

- Fail closed when the human market is not fresh.
  - Rationale: stale prices must not remain clickable on a trading surface. When `fresh:false` or no last close exists, buy/sell and confirm are disabled, button prices show `--`, and the status reads `行情不新鲜 · 禁止下单`.

- Keep TP/SL explicitly as recorded metadata, not an execution promise.
  - Rationale: the backend records `sl`/`tp` on fills, but there is no automatic executor in this task. The UI therefore labels `TP/SL 仅记录 · 不自动执行` next to confirmation and on chart price lines.

- Implement TP/SL suggestion and manual drag as chart-coordinate behavior in `standard-kline`.
  - The split page suggests TP/SL from recent highs/lows, lets the user drag the displayed lines, and switches to manual mode after a drag.
  - `standard-kline` exposes provider-agnostic `priceToY()` and `yToPrice()` methods so the app does not reach into chart internals.

- Show backend failures instead of swallowing them.
  - Rationale: a rejected order is an operator-facing event. The split page no longer uses `.catch(() => null)` for these POST paths, and rejected POST messages are written into visible status lines.

### Gotchas

- A drag ending over an order-side button can generate a follow-up click at the pointer release location. The split page suppresses that synthetic click for 250ms so moving a TP/SL line cannot flip `buy` to `sell`.

- The successful order message is now shown after `loadAll()` completes. Showing success before the trade table refresh made browser validation observe a successful status with stale rows.

- Browser validation uses mocked API routes against the real page and real Chrome. This proves DOM behavior and POST payloads without creating persistent paper-account writes.

- The order-rejection browser case intentionally returns HTTP 400. Chrome emits a resource error for that expected request, so the validation report separates `expectedHttpErrors` from page console errors.

- Task 10b does not implement an automatic TP/SL executor. Any future work that makes TP/SL executable must remove or rewrite the `仅记录 · 不自动执行` copy and add backend execution/reconciliation tests.

### Evidence

- Static/token regression: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_design_tokens_static.py` passed with `21 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `18` tests.
- Static bans: no `.catch(() => null)`, `split_canvas_09a`, `09a`, `09b`, `machine_fills_hidden`, inline `style=`, or inline `<svg` remains in `dashboard-dualtrack-split.html`.
- Browser phase: `http://127.0.0.1:8878/dashboard-dualtrack-split.html` passed positive order, stale-market, rejected-order, plan-lock, and verdict-submit cases with real Chrome. Report saved to `outputs/codex-task10b-browser-validation.json`; screenshots saved to `outputs/codex-task10b-order-positive.png`, `outputs/codex-task10b-stale-market.png`, and `outputs/codex-task10b-plan-lock.png`.
- Browser payload proof: positive order posted `side:"buy"`, `notional:5000`, and numeric `price`/`sl`/`tp`; stale market posted `0` orders; plan lock posted range/key-level/invalidation/confidence fields; verdict posted the note.
- Full regression: `python3 -m pytest -q` passed with `1265 passed`.
- Protected files: zero diff for `dashboard-dualtrack-v5.html`, `tests/test_dashboard_dualtrack_static.py`, `pipelines/lab_run.py`, `services/lab_registry.py`, and `services/lab_r4_promotion.py`.

## 2026-07-09 Task 10c Split Canvas Order Affordance

### Decisions

- Split the order confirm bar into parameter and action rows.
  - Rationale: the one-row layout was overloaded by preview text, three 88px inputs, the TP/SL note, and the confirm button. The confirm button now has `flex-shrink:0` and the action row owns the disclaimer plus cancel/confirm buttons.

- Keep the existing mobile breakpoint, but explicitly reconcile it with the new two-row structure.
  - Desktop keeps cancel/confirm side by side.
  - At `max-width:760px`, confirm bar rows stack vertically and the button group switches to column so full-width buttons do not overflow horizontally.

- Bind plan direction buttons to the same locked state as the rest of the plan form.
  - Rationale: the click guard already prevented mutation, but the buttons looked active. `[data-plan-direction]` now receives `disabled` from `humanPlanLocked()` / `state.planPending`, and `.seg button[disabled]` gives the same visible gray state.

- Bind chart buy/sell buttons directly to `orderBlockReason()` during trading-control rendering.
  - Rationale: `updateActionGates()` already gated actions, but render-only forced states such as stale market must also update the visible `.inbtn` disabled state without waiting for another gate pass.

### Gotchas

- DOM width checks alone can miss mobile layout overflow. The first 375px pass measured the confirm button as wide enough, but the screenshot showed cancel/confirm still arranged horizontally with two 100%-width buttons. The final CSS sets `.confirmbar-buttons` to column under `760px`, and the browser report records `overflow: []`.

- The non-current phase branch can disable the chart buttons even when the lower order status line still says the neutral "选择做多/做空后确认参数" copy, because changing phase calls `updateActionGates()` but not a full trading-control render. The operator-facing affordance is the disabled button state and title `非当前阶段`.

- The cancel button is frontend-only state reset. It clears the selected side and chart price lines; it does not call any API.

### Evidence

- Static/token regression: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_design_tokens_static.py` passed with `22 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `18` tests.
- Browser phase: real Chrome against `http://127.0.0.1:8879/dashboard-dualtrack-split.html`; report saved to `outputs/codex-task10c-browser-validation.json`.
- C1 browser proof: at 1280/1440/1600, `.confirmbar .go` measured `48px` wide with `scrollWidth == clientWidth == 46`; at 375px, confirm width was `265px`, `.confirmbar-buttons` direction was `column`, and overflow list was empty.
- C2 browser proof: locked plan direction buttons all had `disabled:true`, `opacity:"0.45"`, and clicking short kept the selected direction on `做多`.
- C3 browser proof: stale market, wrong phase, and non-current phase all set buy/sell buttons to `disabled:true` with `opacity:"0.45"` and branch-specific titles.
- Full regression: `python3 -m pytest -q` passed with `1266 passed`.
- Protected files: zero diff for `dashboard-dualtrack-v5.html`, `tests/test_dashboard_dualtrack_static.py`, `pipelines/lab_run.py`, `services/lab_registry.py`, and `services/lab_r4_promotion.py`.

## 2026-07-09 Split Canvas Trade Visibility Hotfix

### Decisions

- Stop hiding machine-track order rows during the active cycle.
  - Rationale: owner explicitly changed the product rule: machine and human tracks should show the same class of information, with no blind protocol in this split page.
  - Backend `build_dualtrack_trades_response()` now returns machine trades mid-cycle with `blind:false` and `machine_mid_order_rows_hidden:false`.

- Derive machine trade rows from fills even when machine fills omit `trade_id` and `pnl_units`.
  - Rationale: machine fills often carry `layer`, `rung`, `notional`, and `price` rather than human-style matched entry metadata.
  - The trade builder now infers units from `notional / price` and matches exits by layer/rung/side when explicit `matched_entries` are absent.

- Show trade lifecycle time columns in both human and machine fills tables.
  - Added `成交时间`, `持仓时长`, and `平仓时间` columns.
  - Open rows show live holding duration and no close time/close price. Closed rows show close time and close price.

- Make open-position detection more tolerant than `status === "open"`.
  - Rationale: the page should not miss a real position if backend status strings drift but `remaining_units` is still positive.

### Gotchas

- The owner's real human trade did exist in `outputs/dualtrack/fills/2026-07-09_DAY_human.json`, but by the time of inspection it had a later sell/exit fill, so the correct current position was `无持仓`. The missing product affordance was that the fills table did not show the lifecycle clearly enough.

- `rung:0` must not be treated as an empty value. The first machine matching attempt failed because a truthy check collapsed rung zero; the final matcher compares zero explicitly.

- Open partial positions can have previous exit fills. The UI intentionally shows `平仓时间` only for fully closed rows; still-open rows keep `平仓时间` and `平仓` as `--` while showing realized/unrealized PnL separately.

### Evidence

- Real data check: `outputs/dualtrack/fills/2026-07-09_DAY_human.json` contained entry fill `2026-07-09_DAY_human_0001` and exit fill `2026-07-09_DAY_human_0002`; the browser report shows human position as `无持仓` and the fills row as `已平仓` with entry/exit times.
- Browser phase: real Chrome against `http://127.0.0.1:8880/dashboard-dualtrack-split.html`; report saved to `outputs/codex-split-trades-reveal-validation.json`, screenshots saved to `outputs/codex-split-trades-reveal-real.png` and `outputs/codex-split-trades-reveal-mocked-open.png`.
- Mocked order proof: after a mocked successful order POST, status was `paper fill recorded`, human position rendered `多 0.4900`, and human fills rendered `持仓中` with `成交时间` and `持仓时长`.
- Machine reveal proof: browser report had `blindTextCount:0`, machine K-line visible, machine position populated, and machine fills headers included `成交时间`, `持仓时长`, and `平仓时间`.
- Focused regression: `python3 -m pytest -q tests/test_dualtrack_09b_api_contracts.py tests/test_dashboard_dualtrack_split_static.py tests/test_design_tokens_static.py tests/test_dualtrack_09b_unrealized.py` passed with `29 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `18` tests.
- Full regression: `python3 -m pytest -q` passed with `1266 passed`.
- Protected files: zero diff for `dashboard-dualtrack-v5.html`, `tests/test_dashboard_dualtrack_static.py`, `pipelines/lab_run.py`, `services/lab_registry.py`, and `services/lab_r4_promotion.py`.

## 2026-07-09 Split Canvas Browser Comment Fixes

### Decisions

- Replace operator-facing `未知` copy with explicit status language.
  - The global shell now starts at `读取状态` and falls back to `未连接`; split runtime status also maps unknown runtime to `未连接`.

- Keep machine-track logic truthful instead of inventing nonexistent technical signals.
  - The machine signal widget now says the actual rule: no MACD divergence, top/bottom fractal, or small-candle trigger exists in the machine runner today.
  - It separately describes grid status and trend-leg eligibility from layer keys such as `grid:traded` and `trend:armed`.

- Restore main-chart operator controls in split canvas.
  - Main charts now support shared `1m` / `5m` switching.
  - EMA/MACD controls and editable EMA periods are wired into the existing `standard-kline` indicator surface.

- Treat chart buy/sell buttons as entry buttons.
  - Split-canvas buy/sell payloads now include `event:"entry"` so `做空` does not get guessed by the backend as a close-long order.
  - Close/flatten needs an explicit future UI, not hidden side-effect semantics.

- Recompute TP/SL every time the selected side changes while in suggested mode.
  - `做多`: TP = recent high, SL = recent low.
  - `做空`: TP = recent low, SL = recent high.

- Make the order confirm bar translucent rather than opaque over volume.
  - The confirm bar uses a token-derived background with alpha `0.72` and a small backdrop blur, keeping controls legible while chart volume remains visible behind it.

### Gotchas

- Playwright's CSS computed value for `color-mix(... transparent)` came back as `color(srgb ... / 0.72)`, not `rgba(...)`. The first transparency check falsely failed until the validation parsed the alpha from the CSS color string.

- Hidden phased DOM nodes return `0x0` rectangles. The first overflow pass counted hidden option/read nodes as outside the signal card. The final browser check filters to visible nonzero rectangles and reports no pre-phase signal overflow.

- The first browser attempt with Python Playwright hung during `greenlet` import in system Python. The bundled Node runtime plus `NODE_PATH` could load Playwright, but launching system Chrome headless was killed by the OS. The final validation used Playwright's bundled Chromium.

- A forced HTTP 400 remains in the browser validation to prove English backend errors are translated. Chromium reports that expected 400 as a console resource error, so the report separates it under `expectedHttpErrors`.

### Evidence

- Static/token regression: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_frontend_shell_static.py tests/test_design_tokens_static.py` passed with `30 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `18` tests.
- API/trade focused regression: `python3 -m pytest -q tests/test_dualtrack_09b_api_contracts.py tests/test_dualtrack_09b_unrealized.py` passed with `7 passed`.
- Browser validation: real Playwright Chromium against `http://127.0.0.1:8881/dashboard-dualtrack-split.html`; report saved to `outputs/codex-browser-comments-fix-validation.json`.
- Browser screenshots: `outputs/codex-browser-comments-fix-1561.png` and `outputs/codex-browser-comments-fix-prephase.png`.
- Browser proof points: no `未知`, machine signal explains actual rule, `1m` to `5m` switch updates the chart label, MACD turns on a second pane, EMA period changes to `EMA 10`, short order has `SL 4111.5` and `TP 4099.1`, order payload includes `event:"entry"`, confirm bar alpha is `0.72`, pre-phase signal overflow list is empty, and page horizontal overflow is `1561 / 1561`.

## 2026-07-09 Split Canvas Risk/TP Display Fixes

### Decisions

- Position cards now prefer the open trade's own TP/SL over the plan fallback.
  - Rationale: the operator needs to see the protection recorded on the actual fill, not the broader plan invalidation level.
  - The API trade rows now carry entry-fill `sl` and `tp`; the browser also falls back to the matching entry fill when older trade rows are missing those fields.

- Risk cards calculate max loss in dollars, not raw price points.
  - Rationale: a 0.4868-unit position with a 3.1-point stop is about `$1.51` of risk, not `3.10` or `46.20`.
  - The display uses remaining open units, so partial exits reduce the displayed max loss.

- Replace `强平距离` with `估算强平价`.
  - Rationale: the previous `4,519.7` number was the estimated liquidation price for a short position at 10x, not the distance.
  - The row now shows the estimated liquidation price plus directional distance from the current mark, for example `4,519.7 · 上方 407.9`.

### Gotchas

- The user's visible `4,155.0` was the plan invalidation fallback, not the SL recorded on the latest human fill. The latest open short fill has `tp:4104.1` and `sl:4111.9`.

- Human trade files can lag field shape because older `_build_trades()` rows did not include TP/SL. The split page should not rely only on persisted trade rows; it now uses the entry fill as a fallback source.

- `最大可亏` must be a money value: `(SL - entry) * remaining_units` for shorts and `(entry - SL) * remaining_units` for longs. Showing only the price distance is misleading for small notional positions.

### Evidence

- API check: `build_dualtrack_trades_response("2026-07-09_DAY", track="human")` returned the open short with `entry_price:4108.8`, `remaining_units:0.4867601246`, `sl:4111.9`, and `tp:4104.1`.
- Browser validation: real Playwright Chromium against `http://127.0.0.1:8882/dashboard-dualtrack-split.html`; report saved to `outputs/codex-risk-tpsl-fix-validation.json`.
- Browser proof points: human position rendered `TP / SL 4,104.1 / 4,111.9`; risk rendered `最大可亏(按SL) $-1.51`; liquidation rendered `估算强平价 4,519.7 · 上方 407.9`; page horizontal overflow was `1395 / 1395`.
- Focused regression: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_dualtrack_09b_api_contracts.py tests/test_dualtrack_09b_unrealized.py` passed with `25 passed`.
- Shell/token regression: `python3 -m pytest -q tests/test_frontend_shell_static.py tests/test_design_tokens_static.py` passed with `13 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `18` tests.
- Full regression: `python3 -m pytest -q` passed with `1270 passed`.

## 2026-07-09 Split Canvas Remove Liquidation Price

### Decisions

- Remove liquidation-price display from the split-canvas risk card.
  - Rationale: the operator already controls risk through SL; showing estimated liquidation price adds a distracting broker/margin concept to a panel whose job is "how much do I lose if SL is hit?"
  - The risk card now shows only `杠杆`, `保证金占用`, `最大可亏(按SL)`, and `SL 状态`.

- Keep `最大可亏(按SL)` as the primary risk number.
  - Rationale: this is the operator-facing decision number for the current position and respects remaining open units.

### Gotchas

- The earlier `4,519.7` value was mathematically explainable as a rough short-position liquidation price, but the fact that it needed explanation proved it did not belong in this UI.

- Do not reintroduce liquidation math into `dashboard-dualtrack-split.html` unless the page gets a separate broker-margin diagnostics area.

### Evidence

- Static regression: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_frontend_shell_static.py tests/test_design_tokens_static.py tests/test_dualtrack_09b_api_contracts.py tests/test_dualtrack_09b_unrealized.py` passed with `38 passed`.
- Browser validation: real Playwright Chromium against `http://127.0.0.1:8883/dashboard-dualtrack-split.html`; report saved to `outputs/codex-risk-remove-liquidation-validation.json`.
- Browser proof points: human position rendered `空 0.4868` and `TP / SL 4,104.1 / 4,111.9`; human risk rendered `最大可亏(按SL) $-1.51`; `hasLiquidationCopy:false`; risk row count is `4`.

## 2026-07-09 Split Canvas Live Kline Strictness

### Decisions

- Make the split-canvas chart feed fail closed instead of using local cached or generated bars.
  - Rationale: the owner compared the 5m chart against Binance and found extra lower wicks; investigation showed local 5m bars were stale while Binance public Futures REST had fresh `XAUUSDT` bars.
  - Boundary: this applies to `dashboard-dualtrack-split.html`. The protected v5 page is left untouched.

- Use browser-direct Binance Futures REST for initial bars and Binance WebSocket klines for realtime updates.
  - Rationale: browser fetch to `https://fapi.binance.com/fapi/v1/klines?symbol=XAUUSDT&interval=5m` succeeded in Chromium, so the page can match the execution venue feed without a local fallback layer.
  - The data card now exposes provider, exchange symbol, feed status, stream status, latest bar time, and access issues.

- Add explicit manual close for human open positions.
  - Rationale: a visible `持仓中` row needs a direct operator affordance to close it, not an implicit opposite-side interpretation.
  - The close button sends `event:"exit"` with `order_type:"market"` and no notional; backend lot matching remains responsible for the actual open size.

- Suppress Lightweight Charts attribution text in the generic K-line wrapper and keep the time axis explicitly visible.
  - Rationale: the owner selected leaked `tv-attr-logo` CSS text in the chart area, and the visible chart still lacked obvious time ticks.

### Gotchas

- The local backend market feed can still return stale real bars and has historical fallback behavior for older pages; do not treat that backend contract as the split page's chart truth source.

- Browser WebSocket failure must be visible, but it should not erase already fetched real REST bars. The page disables trading on stale/blocked market status while keeping provenance visible.

- Manual close is a write action. Browser validation should verify the button and mocked payload shape, not click a real close against the owner's live paper fills unless explicitly asked.

### Evidence

- Static/API regression: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_frontend_shell_static.py tests/test_design_tokens_static.py tests/test_dualtrack_09b_api_contracts.py tests/test_dualtrack_09b_unrealized.py tests/test_standard_kline_adapter.py` passed with `43 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `19` tests.
- Browser validation: real Playwright Chromium against `http://127.0.0.1:8884/dashboard-dualtrack-split.html`; report saved to `outputs/codex-live-kline-strict-validation.json`, screenshot saved to `outputs/codex-live-kline-strict-validation.png`.
- Browser proof points: 5m main chart reported `source_mode=binance_usdm_live`, `provider=binance_usdm`, `exchange_symbol=XAUUSDT`, `bar_count=240`, `fresh=true`, and WebSocket `readyState=1`; body text had no fallback/synthetic copy and no `tv-attr-logo` leak; the human open position row showed a `平仓` button; console/page errors were empty.

## 2026-07-09 Split Canvas Order Risk Sizing and TP/SL Sync

### Decisions

- Treat order amount buttons as nominal notional, not margin.
  - Rationale: `$1k/$2k` was ambiguous and too small if interpreted as nominal. The split page now offers `$10k/$20k/$50k/$100k` and labels the row as `按钮=名义本金`.
  - The order preview now shows nominal, required margin, estimated units, added actual leverage, post-order nominal exposure, and post-order actual leverage.

- Separate max trading leverage from actual portfolio leverage.
  - Rationale: `10x` is the margin multiplier allowed by config; actual risk is `nominal exposure / track AUM`. A `$2k` nominal position on `$10k` AUM is `0.20x`, not `10x`.
  - The risk widget now shows `交易杠杆上限`, `本轨 AUM`, `名义持仓`, `实际杠杆率`, `保证金占用`, `最大可亏(按SL)`, and `SL 状态`.

- Make `standard-kline` emit a generic `standard-kline:viewchange` event, and make the split page listen to it for TP/SL overlay sync.
  - Rationale: TP/SL editable DOM lines are outside the chart engine, so they must recalculate against `priceToY` whenever the chart view changes.
  - Mouse, wheel, touch, toolbar clicks, and standard-kline view-change events all feed the same sync scheduler.

### Gotchas

- `10x` should never be displayed as the user's actual portfolio leverage. It is only the conversion from nominal notional to required margin.

- DOM TP/SL overlays and native Lightweight Charts price lines are two different layers. Native price lines move with the chart automatically; editable DOM overlays need explicit view-change synchronization.

- Long chart drags can outlive a short animation loop. New sync requests now extend the remaining sync frames instead of being ignored while a previous loop is running.

### Evidence

- Static/API regression: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_frontend_shell_static.py tests/test_design_tokens_static.py tests/test_dualtrack_09b_api_contracts.py tests/test_dualtrack_09b_unrealized.py tests/test_standard_kline_adapter.py` passed with `43 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `20` tests.
- Syntax/diff checks: split page inline scripts compiled with `new Function`, and `git diff --check` passed for touched files.
- Browser validation: real in-app browser against `http://127.0.0.1:8765/dashboard-dualtrack-split.html`; report saved to `outputs/codex-order-risk-tpsl-sync-validation.json`, screenshot saved to `outputs/codex-order-risk-tpsl-sync-validation.png`.
- Browser proof points: `$20k` preview rendered `保证金 $2,000.00`, `预估仓位 4.8715`, `新增实际杠杆 2.00x`, and `下单后本轨杠杆 2.20x`; human risk rendered `名义持仓 $1,999.60`, `实际杠杆率 0.20x`, and `保证金占用 $199.96`; SL overlay moved by `69.140625px` after zoom and then recalculated again after pan.

## 2026-07-09 Split Canvas R and TradingView Parity Controls

### Decisions

- Rename order preview leverage labels to reduce ambiguity.
  - `新增实际杠杆` becomes `本单实际杠杆`, calculated as `order notional / track AUM`.
  - `下单后名义持仓` becomes `成交后名义敞口`, calculated as `current open nominal exposure + order notional`.

- Add a large order preview `R` badge in the split page, not inside `standard-kline`.
  - Rationale: R depends on order-side, entry, TP, and SL, so it is a DualTrack trading overlay rather than generic chart behavior.
  - Formula: buy uses `(TP - entry) / (entry - SL)`; sell uses `(entry - TP) / (SL - entry)`.

- Add TradingView-like chart affordances to `standard-kline`.
  - The toolbar now shows OHLC plus movement from the first candle of that chart day.
  - The chart now exposes bottom-right `A` for auto-fit and `L` for log-scale toggle.

### Gotchas

- The top OHLC movement is chart-day movement from the visible payload, not an exchange-provided official daily previous close. This keeps `standard-kline` provider-neutral until a caller passes an explicit session reference.

- A bad R value is a real signal. In browser validation the automatic TP/SL suggestion produced `R 0.05`, meaning the suggested reward distance was tiny relative to the stop distance.

- `standard-kline` must remain domain-neutral: R, order lines, and fill semantics stay in the calling page.

### Evidence

- Static/API regression: `python3 -m pytest -q tests/test_dashboard_dualtrack_split_static.py tests/test_frontend_shell_static.py tests/test_design_tokens_static.py tests/test_dualtrack_09b_api_contracts.py tests/test_dualtrack_09b_unrealized.py tests/test_standard_kline_adapter.py` passed with `43 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `21` tests.
- Syntax/diff checks: split page inline scripts compiled with `new Function`, and `git diff --check` passed for touched files.
- Browser validation: real in-app browser against `http://127.0.0.1:8765/dashboard-dualtrack-split.html`; report saved to `outputs/codex-rr-ohlc-scale-validation.json`, screenshot saved to `outputs/codex-rr-ohlc-scale-validation.png`.
- Browser proof points: `$50k` preview rendered `保证金 $5,000.00`, `本单实际杠杆 5.00x`, `成交后名义敞口 $100,093.81`, and `成交后本轨杠杆 10.01x`; K-line rendered `O/H/L/C` plus `+0.76 (+0.02%)`; `A` and `L` buttons were visible; `L` toggled active and was reset off after validation; order preview rendered `R 0.05`.

## 2026-07-09 Split Canvas Protective Exit and Live Tick Refresh

### Decisions

- Move SL/TP trigger semantics into the human paper ledger backend.
  - Rationale: once the UI says an SL has triggered, the owner expects it to be executed in the local paper ledger. The backend now appends a stop/target exit fill when a fresh mark reaches the stored SL/TP.
  - Trigger fills use the configured SL/TP price as the paper execution price, not the already-overrun mark, because this ledger models "到价成交" and does not model slippage yet.

- Keep protective exits local to paper accounting.
  - Rationale: the owner asked to change backend behavior, not to submit broker orders. The sweep writes DualTrack human fills/accounts/trades only and does not open broker clients.

- Let the split page pass its fresh Binance WebSocket mark into the trades API refresh.
  - Rationale: the page currently sees newer Binance ticks than the backend market database. Passing `mark_price` lets the backend make the SL/TP decision immediately while keeping the decision and PnL calculation server-side.

- Make `standard-kline` follow live ticks only when the user is already near the right edge.
  - Rationale: TradingView-style live behavior should keep the latest candle visible, but should not yank the chart away when the user intentionally pans back in history.

### Gotchas

- `position_id=manual` is not unique enough for protective exits. Exit matching now honors `trade_id` when present so one triggered trade cannot accidentally close another manual trade sharing the same position id.

- A stale or missing mark must not trigger protective exits. The sweep only runs when the mark is finite and fresh; otherwise the API returns a skipped sweep.

- Frontend text must not say `TP/SL 仅记录 · 不自动执行` after this change. That wording is now materially false for the split canvas paper ledger.

### Evidence

- Backend/API/static regression: `python3 -m pytest -q tests/test_dualtrack_dt3_human_track.py tests/test_dualtrack_09b_api_contracts.py tests/test_dashboard_dualtrack_split_static.py tests/test_dualtrack_09b_unrealized.py tests/test_standard_kline_adapter.py` passed with `40 passed`.
- Package regression: `node --test packages/standard-kline/standard-kline.test.js` passed with `21` tests.
- Syntax/diff checks: split page inline script compiled with `node --check`, and `git diff --check` passed for touched files.
- Live ledger validation: `outputs/dualtrack/fills/2026-07-09_DAY_human.json` now contains stop fill `2026-07-09_DAY_human_0009` for `trade_0005`, exit side `buy`, execution price `4108.6`, trigger mark `4109.05`, gross PnL `-118.32442851`, exit-fill realized PnL `-120.83034473`, and the trade is closed with `remaining_units=0.0`.
- API validation: `http://127.0.0.1:8765/api/dualtrack/trades/2026-07-09_DAY?track=human&mark_price=4109.88&mark_source=codex_validation` returned `mark_fresh=true` and no duplicate protective trigger after the trade was already closed.
- Browser validation note: in-app browser refresh was blocked by the browser URL policy, so no browser screenshot was produced in this turn.

## 2026-07-09 Engine Adapter Direction and QuantDinger Review

### Decisions

- Do not start with a large engine migration.
  - Rationale: the immediate product value is stable paper accounting behind a fixed `orders/fills/positions/pnl` schema, not replacing the whole trading stack at once.
  - The next architecture step should be an `ExecutionEngineAdapter` boundary where adapter 1 wraps the current local paper ledger and adapter 2 spikes an external engine.

- Treat NautilusTrader as the stronger candidate for an execution-engine spike.
  - Rationale: its native shape is order lifecycle, execution, risk, position state, portfolio/accounting, and reconciliation, which maps directly to the split canvas failures seen today.

- Treat vectorbt as a research/backtest companion, not the live/paper execution adapter.
  - Rationale: vectorized portfolio simulation is excellent for parameter sweeps and strategy research, but it is not the natural owner for real-time order state, partial fills, manual close reconciliation, and broker-like position lifecycle.

- Treat QuantDinger as an implementation reference and possible integration surface, not as the first embedded adapter.
  - Rationale: QuantDinger is a full self-hosted trading OS with Flask, PostgreSQL, Redis, workers, UI, exchange adapters, agent gateway, and billing/ops surfaces. Useful ideas include order intent contracts, fill persistence, close-size retry, and ledger-vs-exchange reconciliation, but embedding it whole would be a second platform inside this repo.

### Gotchas

- QuantDinger's valuable parts are mostly product/runtime patterns, not a small reusable math library. Importing it wholesale would add deployment, DB, worker, auth, and UI coupling before proving accounting equivalence.

- The adapter contract must be ours. External engines should conform to the split canvas `orders/fills/positions/pnl` schema; the frontend widgets must not leak Nautilus-, QuantDinger-, or vectorbt-specific fields.

- Do not confuse strategy-signal standards with execution-accounting standards. QuantDinger's four-way signal contract is useful, but the current pain is fill matching, manual close, SL/TP trigger execution, and position/PnL reconciliation.

### Evidence

- QuantDinger root README describes a self-hosted trading OS spanning AI research, strategy code, backtest, paper/live execution, and monitoring.
- QuantDinger backend docs explicitly separate routes, services, live-trading adapters, grid reconciliation, and data-source boundaries.
- QuantDinger source includes `OrderIntent`, `FillSnapshot`, `PositionSnapshot`, and `ExchangeOrderAdapter` contracts, plus fill persistence and phantom-ledger reconciliation helpers.
- NautilusTrader docs describe `ExecutionEngine`, `RiskEngine`, positions/accounting/portfolio/reports, backtest/sandbox/live contexts, and ports-and-adapters architecture.
- vectorbt docs position it as pandas/NumPy/Numba/Rust vectorized analysis and high-scale parameter/backtest tooling.

## 2026-07-09 Split Canvas Fills Persistence and Machine Fill Sanity

### Decisions

- Add `display_trades` / `display_summary` to the DualTrack trades API.
  - Rationale: when the page rolls from `DAY` to `NIGHT`, the just-completed human fills still exist in the DAY cycle but the current NIGHT cycle may be empty. The UI should show the latest same-day cycle with trades instead of making recent fills and realized PnL appear to disappear after refresh.

- Keep raw cycle trades separate from display trades.
  - Rationale: widgets need stable current-cycle data for actions, but the reader-facing fills/PnL panels need a non-surprising display fallback. The API now exposes `display_cycle_id` and `display_reason` so the frontend can state when it is showing the previous same-day cycle.

- Filter invalid machine fills before building display trades and summaries.
  - Rationale: machine fills are simulated and can be recomputed. A long entry whose SL is not below entry, or a short entry whose SL is not above entry, is invalid execution geometry and must not become a real-looking order row or PnL contribution.

- Count realized PnL from partially open trades in trade summaries.
  - Rationale: a position can be partly closed and still have real realized PnL. Showing realized only for fully closed trades hid machine partial exits.

- Rename fills table headers from `开仓` / `平仓` to `开仓价` / `平仓价`.
  - Rationale: the owner explicitly read the fills table as missing entry price; the column label needs to say price, not imply an action.

### Gotchas

- Current-cycle emptiness is not the same as no recent fills. A refresh right after 21:00 Beijing can legitimately switch the page to `NIGHT` while the user's just-recorded fills are still in `DAY`.

- Do not trust generated machine fills blindly. If the simulated entry and protective levels are geometrically impossible, fail closed in the display/API layer even if a stale artifact already exists on disk.

- Raw ledger files can remain polluted by historical bad machine fills. Reader-facing widgets must use sanitized API summaries until a deliberate ledger repair/migration is approved.

- Realized PnL and closed-trade PnL are different concepts. Partial exits should contribute to realized PnL even when the trade row remains `open`.

### Evidence

- Regression tests: `python3 -m pytest -q tests/test_dualtrack_09b_api_contracts.py tests/test_dashboard_dualtrack_split_static.py tests/test_dualtrack_dt3_human_track.py tests/test_dualtrack_09b_unrealized.py` passed with `40 passed`.
- Syntax/diff checks: split page inline script passed `node --check`, and `git diff --check` passed for the touched files.
- API validation: `http://127.0.0.1:8765/api/dualtrack/trades/2026-07-09_NIGHT?track=human` returns `display_cycle_id=2026-07-09_DAY`, `display_reason=latest_same_day`, `display_summary.realized_pnl=112.8825787`, and 6 display trades.
- API validation: `http://127.0.0.1:8765/api/dualtrack/trades/2026-07-09_NIGHT?track=machine` returns `display_cycle_id=2026-07-09_DAY`, filters 4 invalid machine fills from display safety, and reports `display_summary.realized_pnl=72.79008591`.
- Browser DOM validation: real Playwright tab against `http://127.0.0.1:8765/dashboard-dualtrack-split.html` showed `显示 2026-07-09 日盘 最近成交`, human PnL `+$112.88`, machine PnL `+$72.79`, `开仓价` / `平仓价` headers, and no machine fills containing `4,155.0` or `4,121.3`.

## 2026-07-10 DualTrack P0 Trust Boundary and Execution Adapter

### Decisions

- Supersede the 2026-07-09 decision that let the split page pass its WebSocket
  mark into a GET endpoint.
  - All DualTrack GET endpoints are now pure reads.
  - Client `mark_price` and `mark_source` values are ignored and no longer sent
    by the split page.
  - Human protective exits run from the scheduled `dualtrack-live-tick` path.

- Fail closed at the network order boundary.
  - DualTrack writes require the same local HTTP origin.
  - The server replaces client timestamps with its receive time.
  - Market exits use the canonical server mark.
  - Stale, synthetic, fallback, and non-canonical server market snapshots block
    order submission.

- Enforce order geometry in the backend, not only in the UI.
  - Long: `SL < entry < TP`.
  - Short: `TP < entry < SL`.
  - Order timestamps must belong to the requested cycle.
  - The split confirm button is disabled when R cannot be computed or geometry
    is invalid.

- Correct machine exit sizing by quantity.
  - Grid target, stop, and flatten fills now carry `matched_entries`.
  - Exit notional is `entry units * exit price`, so price changes do not leave
    residual positions.
  - A grid rung at exactly the stop price is invalid and is excluded from the
    frozen golden contract.

- Remove generated and automatic substitute market data.
  - `DualTrackMarketFeed` returns `blocked/unavailable` when its requested source
    is missing.
  - It no longer switches to a second provider or generates synthetic seed bars.
  - The v5 browser no longer generates local synthetic candles.
  - Old synthetic/mock/fallback rows are rejected by pre-cycle, intraday,
    close-cycle, plan-import, and previous-range inputs.

- Make the canonical market identity and clock explicit.
  - The default DualTrack market identity is now
    `GOLD / 1m / binance_usdm`.
  - Network orders reject provider mismatch, invalid/future timestamps, and
    market events from outside the current cycle.
  - One-minute data becomes stale after three minutes, not fifteen.

- Keep failure visible in both dashboard entry points.
  - The v5 chart clears old candles when data is missing, stale, fallback, or
    synthetic instead of leaving the previous chart on screen.
  - Closed fill rows display original trade units; position and risk widgets
    continue to use remaining units.
  - At narrow chart widths, duplicate source text is hidden so all OHLC values
    remain visible; full provenance remains in the chart title and data widget.
  - Static shell and standard-kline assets are `no-store`, so a normal refresh
    loads the current code.
  - Global `UNKNOWN` now names the failed check, such as `调度待确认`, instead
    of incorrectly saying the whole server is disconnected.
  - A healthy open cycle now reports runtime `ok` instead of an unconditional
    `warn`; machine fill count remains visible during the cycle and
    `machine_fills_hidden=false`.

- Introduce `ExecutionEngineAdapter` as the engine boundary.
  - `legacy_paper` wraps the existing human ledger and is now used by dashboard
    order submission and live-tick protective events.
  - `nautilus` remains fail-closed until the isolated parity spike passes.
  - The full contract and cutover gates live in
    `docs/dualtrack-execution-engine-adapter-spec.md`.

### Gotchas

- `legacy_paper` still has no native order lifecycle; its canonical snapshot
  therefore returns `orders=[]` and declares that capability explicitly.

- Protective execution currently receives the latest canonical server mark at
  the five-minute live-tick cadence. A brief wick that crosses SL and recovers
  before the sampled mark can still be missed. The Nautilus spike must define a
  bar high/low or trade-tick matching rule before cutover.

- A fresh Binance WebSocket in the browser no longer overrides a stale local
  server feed. The order will be blocked until the canonical server feed is
  repaired; this is intentional fail-closed behavior.

- Existing historical machine fill files are not rewritten. New grid runs close
  equal units; historical derived positions need a separate, auditable rebuild.

- Removing fallback changes the v5 main-chart behavior when Tiger is absent.
  The caller now requests `symbol=GOLD` explicitly; missing GOLD data produces an
  empty blocked chart rather than silently showing another source.

- The current `/api/system-state` result is `UNKNOWN` because
  `outputs/schedules/status_current.json` is stale, while DualTrack heartbeat
  and GOLD data freshness are both RUN. The shell now says `调度待确认`; this
  operational artifact still needs its scheduler owner to refresh it.

- The standard-kline source label is hidden below a 720px chart-container
  width to preserve complete OHLC text. Full source lineage remains available
  in the title attribute and the dashboard data widget.

### Evidence

- Tests were written red-first for GET purity, client-mark rejection, order
  geometry, cycle timestamps, scheduled protective exits, synthetic rejection,
  same-local-origin writes, canonical server marks, machine unit conservation,
  and adapter routing.
- `tests/test_dualtrack_execution_engine_adapter.py` covers the compatibility
  adapter snapshot, protective market event, reconciliation, and fail-closed
  Nautilus factory behavior.
- The machine golden fixture was intentionally updated after exit quantity and
  stop-rung semantics changed.
- Focused backend/frontend regression passed with `301 passed`; the standalone
  standard-kline suite passed `21/21`.
- Final full repository regression passed with `1307 passed in 410.11s`.
- Real browser DOM measurement passed at 1600, 1440, 1280, and 390px: horizontal
  overflow was zero, order-button/toolbar intersection area was zero, desktop
  confirm height was 34px, and the mobile layout stayed single-column.
- Browser request capture after a normal reload observed nine DualTrack API
  requests and zero `mark_price` / `mark_source` query parameters. Console
  warnings and errors were empty.
- Browser-visible machine fill quantities were `2.4259`, `2.4308`, and `0.4852`
  instead of zero; the main OHLC row fit its container without truncation; the
  Binance WebSocket reached `已连接`.
- The live market endpoint reported `provider=binance_usdm`,
  `source_mode=requested_symbol`, `fresh=true`, and
  `max_age_minutes=3.0`.
- The live runtime endpoint reported all four checks `ok`, runtime status `ok`,
  `machine_fill_count=12`, and `machine_fills_hidden=false`.

## 2026-07-10 - 黄金飞书自动化切到双轨机器轨

### Decisions

- 飞书交易记录只接机器轨网格。
  - 新增 `dualtrack_machine_brief`：每个 12 小时周期输出机器方向、关键位、失效条件、网格观察位、趋势腿状态。
  - 新增 `dualtrack_trade_record`：只发送机器轨 entry / target / stop / final flatten 事件。
  - 继续使用 report/trade Feishu sender；健康告警不再作为交易记录内容。

- 早晚盘复盘只复盘机器轨网格。
  - 当 `schedule.profile=dualtrack_focus` 且存在 dualtrack artifact 时，`pm_portfolio_report` 不再读取旧 active strategy 的 performance/paper_orders。
  - 复盘口径改成：黄金 12 小时行情、机器轨开仓数、平仓数、TP/SL、已实现 PnL、open 估算未实现 PnL、下一周期机器方向。

- 交易记录发送必须防重复和防历史补发。
  - 发送账本为 `outputs/dualtrack_trade_notifications/<date>.json`。
  - 去重键为 `cycle_id + fill_id`。
  - 首次接入某个 cycle 时默认只建立 baseline，不补发已有 fills；只有 `--backfill-existing` 才显式补发历史。

- Intraday flatten 不是实际平仓。
  - 当前周期运行中，machine simulation 会用 `flatten` 对最后一根 bar 做临时结算。
  - Feishu 交易记录现在过滤掉周期未结束前的 `flatten`，避免把 mark-to-close 误报成真实平仓。

### Gotchas

- 本机 `dualtrack-cycle` launchd 每分钟运行。第一次把通知钩子接进 runner 后，后台 runner 立即扫描了当前周期已有 fills，并发出了 18 条交易记录；其中部分是 intraday flatten，语义上不应作为真实平仓。后续已改成首次 baseline + 过滤 intraday flatten，防止再次发生。

- `outputs/feishu_reports/<date>.json` 是发送回执，不代表交易语义一定正确；需要结合 `dualtrack_trade_notifications` 的 `suppressed` / `delivered` / `event` 字段判断。

- `trade_ticket_notifications` 仍是旧 ticket 审查链路；新的机器轨开/平仓不依赖旧 ticket。

### Evidence

- `tests/test_dualtrack_feishu.py` 覆盖机器轨作战单、entry/exit 交易记录、首次 baseline、intraday flatten 过滤。
- Focused regression passed:
  `python3 -m pytest tests/test_dualtrack_feishu.py tests/test_pm_portfolio_report.py tests/test_feishu_report_sender.py tests/test_dualtrack_dt8_cycle_runner.py`
  returned `42 passed`.
- Real current-cycle dry run after the flatten fix:
  `python3 -m pipelines.dualtrack_trade_notifications --cycle-id 2026-07-10_DAY --json`
  returned `sent=0 skipped=14 failed=0`.

## 2026-07-10 - DualTrack protective OHLC replay

### Decisions

- Human protective execution no longer evaluates only the newest close. The
  live tick replays every trusted 1m bar from the earliest open human trade and
  passes canonical open/high/low/close values through `ExecutionEngineAdapter`.
- Long stops use bar low and short stops use bar high. Targets use the opposite
  range edge. If one OHLC bar touches both TP and SL, the compatibility engine
  executes the stop first as the conservative deterministic rule.
- A bar that started before a trade entry may use only its current/closing mark,
  not its full high/low range. This prevents pre-entry price movement from
  closing a newly created trade retroactively.
- A real gap through a stop fills at the bar open only when a trusted OHLC event
  explicitly supplies that open. Point-price events preserve the existing stop
  price fill rule.
- The generated `dualtrack-live-tick` schedule now runs every 60 seconds instead
  of every 300 seconds, matching the GOLD 1m feed cadence.

### Gotchas

- OHLC replay fixes missed completed-bar wicks but does not reveal tick order
  inside a bar. The stop-first rule is intentionally pessimistic; it must not be
  described as exact exchange execution.
- The standardized datafeed service starts and its health endpoint passes, but
  its WebSocket upstream currently fails on this machine because `websockets`
  detects the configured SOCKS proxy and `python-socks` is not installed. This
  remains an explicit blocker to tick-level server streaming; no fallback or
  synthetic stream was substituted.
- The generated schedule is current, but the installed live-tick plist is still
  the old 300-second definition. `schedule_status` now reports exactly one
  mismatch instead of stale/unknown status. Replacing a loaded launchd job is
  separately acknowledgement-gated by the installer.

### Evidence

- Red-first tests prove an intermediate short-stop wick is executed after the
  final close recovers, same-bar TP/SL resolves to stop, and a pre-entry wick
  cannot close a new trade.
- Human engine, execution adapter, and cycle runner regression passed with
  `48 passed`.
- Generated schedule status on 2026-07-10 reports four of five jobs current and
  only `com.wendy.trading-orchestrator.dualtrack-live-tick` mismatched.

## 2026-07-10 - NautilusTrader isolated execution spike

### Decisions

- Installed NautilusTrader 1.230.0 only in `/tmp/dualtrack-nautilus-spike`; it is
  not a production dependency of trading-orchestrator.
- Ran a durable bracket fixture: market BUY 1 at 100, SL 95, TP 105, followed by
  a 1m bar with `O=100 H=101 L=94 C=100`. Nautilus filled the stop at 95,
  flattened the position, and reported `-5.03510000 USDT` realized PnL including
  fees.
- Did not implement or enable `NautilusExecutionAdapter`. datafeed currently
  lacks the instrument-definition fields needed to build an exchange-valid
  Nautilus instrument without hard-coded precision, multiplier, margin, and fee
  assumptions.

### Gotchas

- Passing one engine fixture proves matching/accounting semantics, not parity
  across scale-in, partial reduction, restart, duplicate replay, or the existing
  machine-fill fixture.
- Nautilus supports adaptive bar high/low ordering, while the compatibility
  ledger deliberately uses stop-first when both levels are touched. The parity
  suite must configure and document one rule rather than accepting unexplained
  differences.
- A BTCUSDT packaged test instrument was used only to exercise engine behavior.
  It must never appear in DualTrack GOLD snapshots or frontend widgets.

### Evidence

- `spikes/dualtrack_nautilus_fixture.py` asserts the two fills, prices, flat
  position, and fee-inclusive realized PnL.
- `docs/dualtrack-nautilus-spike-result.md` records the missing datafeed contract
  and cutover gates.

## 2026-07-10 - 机器轨开单记录改成截图式卡片

### Decisions

- 机器轨 entry 事件在 Feishu 里单独使用“黄金开单 · 自动成交”格式；target / stop / final flatten 继续走“黄金交易记录”格式。
- 策略中文名统一为“机器轨网格”；不再使用此前的误写名称。
- 开单卡展示用户真正需要确认的信息：作战单方向、人工/机器过滤、风险闭环、仓位/保证金、入场、TP、SL、证据路径。
- entry 自动通知使用绿色 Feishu interactive card；纯文字只作为发送审计和兼容性正文。
- 旧 ticket 体系里的 strength、quality gate、backtest win-rate/sample 不再硬塞进机器轨开单卡；机器轨只展示原生存在或可由 fill/plan/account 换算的字段。

### Gotchas

- “自动成交”在当前机器轨里表示 dualtrack simulation fill 已写入，不代表已经进入 demo/live 或券商实盘。
- 机器轨的 TP/SL 和盈亏比来自网格层级与作战单失效位，不能用旧 ticket 审查卡的 Rational Trigger / 回测样本口径解释。

### Evidence

- 本地渲染预览使用 `2026-07-10_DAY` 第一条 entry fill，展示出入场价、TP/SL、估算保证金、止损预估、止盈预估和 artifact 路径。
- 仅发送一条标题为“黄金开单 · 样本预览”的受控样本，回执 `delivered=true`、`code=0`；样本未写入自动交易通知去重账本。
- Focused regression passed:
  `python3 -m pytest tests/test_dualtrack_feishu.py tests/test_trade_ticket_card.py tests/test_pm_portfolio_report.py tests/test_feishu_report_sender.py tests/test_dualtrack_dt8_cycle_runner.py`
  returned `55 passed`.

## 2026-07-10 - DualTrack canonical execution boundary and shadow parity

### Decisions

- 所有将进入执行器的行情事件统一为 `dualtrack-market-event-v1`：必须具备 UTC 时间、正数价格、可信来源、`fresh=true`、`is_synthetic=false`，且 OHLC 如存在必须完整且自洽。
- 事件 ID 由规范化字段稳定生成；同一输入重放得到同一 ID，供未来双引擎对账和幂等排查使用。
- 双引擎对账采用精确比较，不允许先用 tolerance 掩盖差异；候选引擎缺失时产出 `blocked` 报告，而不是空报告或误报通过。
- Nautilus 仍然只处于影子接入准备阶段：没有 upstream、execution-venue 的 instrument definition 与候选 snapshot，不能启用。

### Gotchas

- 图表可展示的行情 payload 不等于执行级行情事件；后者必须同时满足来源、新鲜度和合成数据闸门。
- 只比较 PnL 会漏掉残余仓位或重复成交；对账还必须比较 fills、positions 和 open units。
- `blocked` 是安全状态，不是失败修复后的成功状态；它表示外部候选引擎尚未产生可审计的规范化结果。

- `pipelines.dualtrack_execution_reconcile` 只读 legacy snapshot 与候选 JSON，再原子写入 reconciliation artifact；它不初始化网络 client、不开新订单，也不改变 ledger。

## 2026-07-10 - Nautilus GOLD instrument bridge

### Decisions

- Added a lazy Nautilus instrument builder that accepts only
  `instrument-definition-v1` and does not add NautilusTrader to the production
  dependency set.
- The builder rejects synthetic, cached, non-execution, non-trading, inverse,
  incomplete, and unexplained-multiplier definitions.
- Public Binance instrument metadata does not include account maker/taker fee
  rates. Both rates must be supplied explicitly; the builder has no fallback
  fee.
- The builder is shadow-only. `legacy_paper` remains authoritative and the
  adapter factory still refuses to enable Nautilus.

### Gotchas

- XAUUSDT must use Nautilus `PerpetualContract` with commodity asset class, not
  a crypto perpetual merely because the venue API is Binance.
- Building a valid instrument proves contract semantics, not fill/accounting
  parity, restart persistence, or safe production cutover.
- Concurrent canonical-event and shadow-reconciliation work was left intact
  and excluded from this commit.

### Evidence

- A live datafeed response built `XAUUSDT-PERP.BINANCE` with tick `0.01`, size
  step `0.001`, multiplier `1`, and `1 XAU @ 4100 = 4100 USDT`.
- Instrument trust-gate plus execution-control focused regression passed with
  `25 passed`.

- `datafeed` 的 `instrument-definition-v1` 已通过真实本地 HTTP 调用验证。交易系统只接受 `require_execution_venue=true`、`served_from=upstream`、非合成定义；公开 instrument metadata 缺少 maker/taker fee 时，preflight 会保留 blocker，不能自行假设费用。

- `cost_per_side_bp=0.5` 是现有纸面成本假设，不等同于交易所 maker/taker fee 的外部证据；preflight 不可把它自动转换为 Nautilus 费率。注意 0.5 bp 的小数是 `0.00005`，不是 `0.000005`。

- 机器轨会过滤无法通过基本止损/止盈几何校验的模拟成交；过滤后的 ledger 可以保持干净，但 runtime 不能继续报 `ok`。此类事件现在会明确输出 `warn` 且 `valid_now=false`，阻止它被拿作切换样本。

- 同一根 OHLC bar 同时触及硬失效位和网格入场位时，无法从 OHLC 恢复真实先后顺序。为避免乐观开仓，硬失效优先：该 bar 不允许新开仓；人机模型与未来 Nautilus 对账都必须采用此保守语义。

- 纸面影子对账可使用已存在的 0.5bp/side 成本假设，但必须显式标记为 `paper_assumption` 与 `real_money_eligible=false`。这消除了 paper-shadow 的费率缺口，同时保留真实经纪商费率为单独授权和证据门槛。

- 第一条 Nautilus 对账场景固定为 long market entry 100、同一 1m bar low=94 的 stop 95。legacy 与 Nautilus 都使用同一份 upstream XAUUSDT instrument definition 和 paper-only 费率；先以精确 PnL、fill count、open units 对账，再扩展到其余九类场景。

## 2026-07-10 - Nautilus XAU first parity artifact and stricter comparison

### Decisions

- 首个真实 XAUUSDT shadow fixture 的结果写入
  `outputs/dualtrack/nautilus/parity/2026-07-10-xau-stop-loss.json`；它是
  纸面影子证据，不是 live-cycle candidate snapshot，也不能被拿来解除运行中
  reconciliation 的 `candidate_snapshot_missing` blocker。
- 对账不再只比较 PnL、成交数量和总残余单位；逐笔 `side/event/price/quantity`、
  仓位 `status/side/remaining_units`、标准账户金额和 reconciliation status 都
  必须精确一致。
- 当前情景仍限制在长期/short stop/target 的第一条固定情景；其余九类情景未完成前，
  Nautilus 不会被配置为 paper execution engine。

### Gotchas

- 两边的 `orders=[]` 不能被解读为订单生命周期也已对账；legacy compatibility
  engine 明确没有原生 order lifecycle。这是一个尚未解决的迁移能力缺口，不是通过。
- fixture 以临时 output root 构建 legacy ledger，避免任何测试成交写入真实 human
  fills。候选 snapshot 因此只能证明固定场景语义，不能替代真实 cycle 的 shadow
  execution artifact。
- 当前 Nautilus 版本会发出 Pandas4 的 `Timestamp.utcnow` deprecation warning；
  它不改变本次执行或对账结果，但升级运行时前应复核该警告。

### Evidence

- 使用真实 upstream XAUUSDT preflight、`paper_assumption` 的双边
  `0.00005` fee 和隔离 Nautilus 1.230.0 runtime，long 100 / stop 95 的结果为
  legacy 与 Nautilus 都 `realized=-5.00975`、2 fills、flat，精确对账 `pass`。
- Full repository regression: `1338 passed in 395.20s`。
- Focused boundary regression after strengthening comparison:
  `23 passed`。

## 2026-07-10 - Shadow cutover evidence gate

### Decisions

- 增加只读 `dualtrack_shadow_cutover_status` gate：必须连续 7 个按 cycle
  落盘的 `pass` reconciliation，才会显示
  `ready_for_attended_paper_switch`。
- gate 只写 operator status artifact；它永远不改变 configured engine、不开订单，且
  `real_money_eligible=false`。即使满足 7 次也只代表可进行人工批准的 paper switch。

### Gotchas

- `current.json` 不能作为连续性证据；它会被覆盖。gate 只读取每个 cycle 的独立
  reconciliation artifact。
- 一条 `blocked`、`drift` 或缺失候选 snapshot 都会把 trailing pass count 归零；
  不能用旧的通过记录跨越最近失败来凑够七次。

### Evidence

- 真实当前 gate 输出 `blocked`，`candidate_snapshot_missing`，
  `observed_consecutive_passes=0`；没有把首个固定 fixture 误当运行 cycle 通过。
- Gate 与 execution boundary focused regression: `15 passed`。
- Browser check against a disposable local server rendered `正常`、`执行对账 · 等待候选引擎`、
  `影子切换 · 未满足连续验证` and `人工交易窗口 · 开放`; all required API reads returned
  HTTP 200 and no execution-control endpoint was invoked.

## 2026-07-10 - Second real Nautilus parity direction

### Decisions

- 将 XAUUSDT fixture 参数化为 `long_stop` 与 `short_stop` 两个实际运行的固定情景；
  它们都使用同一份 upstream instrument preflight 和 paper-only fee model。
- 固定情景的单根 OHLC wick 均以 stop 为保守优先级：long 的 `low=94 < 95`，short
  的 `high=106 > 105`。这只验证单边 stop 行为，不宣称已覆盖同 bar TP/SL 冲突。

### Gotchas

- 这两个证据文件使用临时 legacy ledger，不能拼接到 live-cycle reconciliation
  history，也不能增加 seven-cycle gate 的 through count。
- `Pandas4Warning` 是 Nautilus runtime 的上游 deprecation warning；未改变双边
  fill、Pnl 或 flat result，但它不是可以静默忽略的长期依赖状态。

### Evidence

- `outputs/dualtrack/nautilus/parity/2026-07-10-xau-long_stop.json`:
  exact parity `pass`，双方 realized PnL `-5.00975`。
- `outputs/dualtrack/nautilus/parity/2026-07-10-xau-short_stop.json`:
  exact parity `pass`，双方 realized PnL `-5.01025`。
- Fixture declaration plus boundary regression: `14 passed`。

## 2026-07-10 - Target and same-bar priority Nautilus parity

### Decisions

- 扩展并实际运行 XAUUSDT fixture：long/short 的 target、以及 long 的
  `O=100 H=106 L=94 C=100` same-bar 双触及情景。
- Nautilus venue 显式使用 `bar_adaptive_high_low_ordering=true`。对 Open
  到 High/Low 等距的 same-bar fixture，该运行时顺序为 Open → Low → High，
  因而与 legacy 的保守 stop-first 语义一致。
- 候选 fill event 从 Nautilus 的实际成交价推导（SL=stop、TP=target），而不是
  从 fixture 的预期标签抄写，避免用期望值掩盖撮合顺序错误。

### Gotchas

- `bar_adaptive_high_low_ordering` 是一个针对 OHLC 模拟路径的显式模型选择，
  不是对真实 tick 路径的宣称；真实 shadow 仍需同一条 immutable event stream。
- 相同的 equality fixture 对优先级最敏感；若未来升级 Nautilus 改变该 tie-break，
  该 parity fixture 必须 drift，而不能静默重标记为 pass。

### Evidence

- 5 个隔离的真实 XAUUSDT fixture 都 exact `pass`：
  `long_stop=-5.00975`，`short_stop=-5.01025`，
  `long_target=4.98975`，`short_target=4.99025`，
  `long_same_bar_stop_first=-5.00975`（双方皆为 stop）。
- Fixture-level focused regression: `3 passed`。
- Final full repository regression after the five-scenario fixture expansion:
  `1346 passed in 400.05s`。

## 2026-07-10 - Immutable actual-cycle Nautilus shadow chain

### Decisions

- 每个 shadow cycle 必须先生成 `dualtrack-shadow-input-v1`：其中包含规范化的
  1m market events、legacy snapshot 的 immutable fill evidence、以及独立的
  accepted-command journal。候选运行时不得直接读取 legacy fills 文件。
- `GOLD`（存储/图表符号）到 `XAUUSDT`（Nautilus 执行合约）的映射显式写入配置；
  bundle 缺 provider、instrument ID、ready instrument preflight 或映射不一致时
  fail closed。
- `LegacyPaperExecutionAdapter` 仅在订单被成功接受后记录 command journal，按
  legacy fill ID 幂等。重试相同 source fill 不会制造第二条影子命令。
- 空订单 cycle 的 Nautilus market replay 可作为事件摄取证据，但
  `qualifies_for_cutover=false`，绝不能推进 seven-cycle execution-parity gate。

### Gotchas

- 过去的 fills 没有倒推成伪造的原始命令；历史命令不存在时，未来 candidate
  replay 必须明确 blocked，而不是把已知成交结果回灌给候选引擎。
- 当前设置中 data store symbol 为 `GOLD`，instrument definition 为 `XAUUSDT`；
  这个映射若被隐式处理，会重新引入 split-brain execution identity。
- 一个 `pass` reconciliation 不一定是切换证据。没有命令的 pass 只说明同一
  market stream 可被消费，不能说明订单、仓位或 PnL 语义已对齐。

### Evidence

- 真实 `2026-07-10_DAY` bundle:
  `input_id=shadow-fc7211e35547aa6ff797`，470 条 `binance_usdm` events，
  execution instrument `XAUUSDT`，legacy command count 0。
- 隔离 Nautilus runtime 成功消费该 bundle；reconciliation `pass`，但 cutover gate
  保持 `blocked/candidate_activity_insufficient/0`。
- Command-journal, bundle, replay, reconciliation and cutover focused tests:
  `15 passed`。

## 2026-07-10 - Legacy limit lifecycle parity repair

### Decisions

- Human-track `limit + entry` 不再由 compatibility engine 在提交时直接写 fill。
  它先写 `accepted` order artifact，只有可信 canonical event 的 high/low 真正触及
  限价后才生成 fill。
- 为避免 OHLC 内路径未知带来的乐观结果，已有仓位先处理本 bar 的 protective sweep，
  随后才接受新触及的 limit entry；新入场不能借同一根 bar 立即触发 TP/SL。
- 执行快照的 legacy `orders` 不再永远为空：已成交订单从 fill ledger 派生，挂单从
  order artifact 读取，capability 仍明确是 `derived_from_fill_ledger` 而非 native OMS。
- Dashboard POST 现在区分 `accepted` 与 `filled`；页面向用户显示“限价单已挂起 ·
  等待可信行情触及”。

### Gotchas

- 历史 `DualTrackHumanEngine` 直接调用仍保留为导入/同步路径；新的 pending lifecycle
  只在 execution adapter 边界生效，不能把外部 broker 已成交 fill 改写成挂单。
- Limit 的数量以提交时 `notional / limit price` 固定，实际触及后按该数量写入 fill；
  不能在触及时重新用 market price 反算数量。
- 同一根 OHLC bar 没有可验证的入场后路径。若需要 intrabar TP/SL，必须使用 quote/trade
  stream，而不是把 1m range 当 tick 序列。

### Evidence

- `limit_entry_waits_for_touch` 真实 Nautilus/XAUUSDT fixture 已 exact `pass`：双方都
  是 `accepted`、0 fills、0 positions，数量为 `100 / 95`。
- 固定 fixture gate 当前通过 market entry、limit wait、stop/target 和 same-bar priority；
  仍因 6 个未完成类而 blocked。
- Full repository regression: `1360 passed in 428.04s`。

## 2026-07-10 - Fixed parity suite complete; real-cycle qualification remains

### Decisions

- 固定 Nautilus parity gate 现在要求并验证十个明确类别，任何 `drift`、`missing`
  或 `not_run` 都会阻止 cutover，而不是仅凭少量 happy-path fixture 放行。
- 历史 `2026-07-09_DAY` machine fills 作为 raw evidence 保持不变；derived trade rebuild
  对无显式数量的同 rung machine stop/target 使用全部剩余 units，消除 exit-price
  反推数量造成的 phantom residual。
- Seven-cycle gate 还要求候选 snapshot 的 `qualifies_for_cutover=true`。一个真实
  market replay 但 command count 为 0 的 pass 仅证明摄取通路，不能计为执行 parity。

### Gotchas

- Legacy compatibility order lifecycle 是由 fills/order artifacts 派生的，不是 native
  OMS；它足以进行精确迁移对账，但不能被营销为 exchange queue / partial-fill model。
- Restart fixture 的模式是 `immutable_replay`：从 persisted input/output artifact
  重启后语义一致。它不是一个对 Nautilus 内存状态做未验证序列化的声明。
- 固定 suite 全绿并不授权 paper engine switch；仍须积累 7 个连续、实际含命令的
  paper cycles，且其中不得有 unexplained drift。

### Evidence

- `dualtrack_nautilus_parity_gate` 实际输出 `pass`，十类全部通过。
- 当前真实 `2026-07-10_DAY` 再次 replay：493 个 market events、0 accepted commands、
  reconciliation `pass`；cutover status 明确为
  `blocked/candidate_activity_insufficient/observed_consecutive_passes=0`。
- Historical residual check: 14 raw fills、7 rebuilt trades、0 residuals、
  `raw_fills_immutable=true`。
- Final full repository regression after lifecycle, accounting, fixture-gate and
  historical-rebuild changes: `1363 passed in 433.24s`。

## 2026-07-10 - M6 one-cycle shadow operation entrypoint

### Decisions

- `dualtrack_shadow_cycle` 把一个 cycle 的 prepare → isolated replay → exact
  reconciliation → cutover gate 串成一个无订单操作入口。它的成功状态为 `replayed`，
  不使用 `pass` 这个容易被误解为切换批准的词。
- 该入口即使 replay 成功也会保留 cutover gate 的独立状态；`candidate_activity_insufficient`
  仍返回为清晰的安全阻塞，而不改变 execution engine 或 real-money eligibility。

### Gotchas

- 目前 `2026-07-10_DAY` 没有已接受订单命令。它证明 531 条不可变 market events 的
  入库、Nautilus replay 和对账通路，但仍不代表订单执行语义已在生产 cycle 上发生。
- M6 的七次计数必须来自未来按 cycle 落盘的、命令非空的 replay artifacts，不能用
  同一日期反复运行无订单 cycle 累加。

### Evidence

- 实际 `dualtrack_shadow_cycle` 输出 `replayed`，prepare/replay exit code 均为 0；
  candidate evidence 为 `replayed_market_events=531`、`authoritative_command_count=0`、
  `qualifies_for_cutover=false`。
- Orchestration focused regression: `14 passed`。

## 2026-07-10 - Command-bearing shadow replay enabled

### Decisions

- Isolated Nautilus replay now consumes the immutable accepted-command journal,
  rather than refusing every nonempty command cycle. It supports market/limit
  entry, optional bracket TP/SL, and reduce-only exits; malformed, missing-time,
  invalid-side, invalid-quantity or unsupported order-type commands still fail
  closed.
- Candidate fill event labels are inferred from actual execution price against
  the command SL/TP, and a closed Nautilus net position is normalized back to
  the originating long/short side instead of leaking `flat` into the common
  contract.

### Gotchas

- The command journal records only successfully accepted legacy commands. It
  does not fabricate missing historical commands from fills.
- This is a paper-shadow path only; command replay neither routes to a broker
  nor changes the authoritative legacy ledger.

### Evidence

- Isolated command smoke: one market long at 100 with SL 95 replayed to two
  Nautilus fills, flat long position and realized PnL `-5.00975`; evidence
  records `authoritative_command_count=1` and `qualifies_for_cutover=true`.
- Replay/orchestration focused regression: `6 passed`.

## 2026-07-10 - Live API console evidence for cutover guard

### Decisions

- Cutover UI evidence must be captured through `pipelines.dashboard_server`, not
  a static-file server: the page depends on `/api/dashboard` to present the
  current shadow-gate state.
- The visual surface continues to expose the blocked state rather than implying
  readiness: `执行对账 · 已通过` is intentionally separate from `影子切换 · 需要真实订单样本`.

### Gotchas

- A static render showed `NO LIVE KLINE DATA`; it is a shell-only render and
  must not be retained as evidence of a working runtime console.
- The current dashboard also surfaces an unrelated `stale_installed` launchd
  warning. It does not change the M6 candidate-activity blocker, but it should
  not be concealed by the cutover display.

### Evidence

- Browser screenshot via local API server: `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/dualtrack-cutover-gate-live-2026-07-10.png`.
- Browser accessibility snapshot confirms `2026-07-10_DAY`, `执行对账 · 已通过`, and
  `影子切换 · 需要真实订单样本` in the live console.

## 2026-07-10 - Paper scheduler drift repaired

### Decisions

- Replaced the local `dualtrack-live-tick` LaunchAgent using the attended
  schedule installer after its generated 60-second cadence was found to differ
  from the installed 300-second cadence.
- The operation used a same-day, 15-minute takeover package plus the install
  acknowledgement. It changed local paper scheduling only; it did not open a
  broker client or submit an order.

### Gotchas

- An arbitrary package id and an expired package were correctly rejected. The
  installer requires a fresh package tied to the current run date before it
  will replace a loaded agent.
- The installer can take longer than a shell's initial output window because
  it sequentially backs up, bootouts, bootstraps, kickstarts, and verifies each
  generated agent. Completion must be read from `install_current.json` and the
  post-install verifier, not from an early empty stdout window.

### Evidence

- `pipelines.schedule_status`: `active`, with 5/5 generated agents installed,
  matching, and loaded; `dualtrack-live-tick` interval is now 60 seconds.
- `pipelines.schedule_post_install_verify`: `pass`; successful receipt at
  `2026-07-10T10:11:16+00:00`, five backups, and all five rollback entries
  restorable at `outputs/schedules/launch_agent_backups/20260710T101025Z/`.
- Before/after console proof is saved at
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/dualtrack-cutover-gate-live-2026-07-10.png`
  and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/dualtrack-after-scheduler-repair-2026-07-10.png`.

## 2026-07-10 - Persistent Nautilus paper adapter and account-cost parity

### Decisions

- Implement `NautilusExecutionAdapter` as a paper-only, event-sourced adapter.
  It durably stores accepted commands, trusted market events, replay inputs and
  outputs, normalized orders/fills/positions/accounts, and snapshots under an
  isolated `nautilus_paper` ledger.
- Persist market-event ingestion separately from replay acknowledgement. A
  process failure after event storage must retry replay on restart; it must not
  silently classify an unprocessed event as idempotent.
- Keep adapter construction behind attended approval, an explicit isolated
  runtime path, and `ready_for_attended_paper_switch`. The current `0/7`
  command-bearing cycle result cannot select Nautilus.
- Observe Binance maker/taker rates through the authenticated read-only account
  endpoint and funding through the public funding endpoint. Persist the exact
  environment and keep `real_money_eligible=false`.
- Apply the same observed fee model to both engines in parity fixtures. Market
  entry and stop are taker events; take-profit is a maker limit event. Exact
  parity remains required, with no tolerance widening.
- Move the conflicting TokenPulse share port to 8767 so the trading dashboard
  is the only listener on 8765. Keep the unrelated goldbot gateway on 8766.

### Gotchas

- The observed Binance rates are from the configured demo account. They are
  real account observations for paper modeling, not evidence of mainnet fees.
- A funding-rate sample is not a funding settlement. Do not alter realized PnL
  until a position is proven open at a real funding timestamp with the correct
  sign and notional.
- A replay with hundreds of market events but zero accepted commands proves
  ingestion only. It must remain `qualifies_for_cutover=false` and must not be
  rerun seven times to manufacture cycle evidence.
- Accepted orders submitted after an existing snapshot must be merged into the
  durable normalized order view immediately; waiting for the next market event
  would make the operator-facing order state temporarily false.
- The live Binance WebSocket host completes a handshake on this machine but
  sends no frames, while the demo host emits frames. Do not substitute demo,
  REST cache, fallback, or synthetic bars into the live path; report the live
  stream failure explicitly.
- TokenPulse port 8767 is a local untracked operator setting; it is not part of
  this repository commit.

### Evidence

- Fixed parity gate: all 10 classes exact `pass` using the account-observed demo
  maker/taker model.
- Persistent-adapter smoke: one command and one trusted market event persisted
  one order, one fill, one open position, exposure and margin across ten durable
  JSON projections including processed-event acknowledgement; restart
  reconciliation returned `ok`.
- Runtime status: schedule `active`, 5/5 jobs generated/installed/loaded and
  matching; `dualtrack-live-tick` interval 60 seconds; only the dashboard owns
  127.0.0.1:8765.
- Cutover gate: `blocked/candidate_activity_insufficient`, fixture gate `pass`,
  observed command-bearing cycles `0/7`, configured engine unchanged.
- Full repository regression after the persisted-event retry repair:
  `1378 passed in 401.21s`.
- Live browser proof: `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/dualtrack-persistent-adapter-gate-2026-07-10.png`
  and `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/dualtrack-cutover-status-0-of-7-2026-07-10.png`.
  The second screenshot shows reconciliation passed while the paper switch
  remains blocked for missing real order samples; browser warning/error log is
  empty.

## 2026-07-10 - Independent 12-hour machine decisions and explicit grid execution

### Decisions

- The machine track now has a dedicated AI-only plan accessor. Machine
  execution, runtime readiness, command center status, Feishu briefs, and the
  split dashboard must not inherit or fall back to a locked human plan.
- Every machine plan is immutable for one 12-hour cycle and records a complete
  direction (`long`, `short`, or `neutral`), expected range, explicit grid
  entry/target pairs, invalidation, rationale, and source provenance.
- The first production planner source is the dated finance daily newsletter's
  gold section plus trusted Binance bars. The planner may later add disclosed
  web sources, but it may not fabricate a source or use human-plan artifacts.
- Planner failure is represented as `decision_error`: persist a degraded,
  neutral, zero-order plan with the exact error. This is a fail-closed state,
  not a substitute forecast or synthetic market-data fallback.
- Explicit grid replay does not derive hidden bp-spaced levels. Intraday replay
  preserves open positions; only TP, SL, or the 12-hour cycle close can close
  them. OHLC ambiguity remains conservative: a touched stop wins before entry.
- Every closed cycle writes a machine review, including neutral and zero-fill
  cycles. The review records actual versus predicted range, touched/filled
  grid levels, PnL, and a concrete no-trade reason.
- The split dashboard exposes machine decisions without the former blind-state
  human fallback. It shows the AI range, explicit grid pairs, rationale, and
  machine review while keeping machine order controls read-only.

### Gotchas

- The installed Codex CLI inherited `gpt-5.6-terra` from user config and
  rejected the first planner call because the CLI version was too old. The
  planner now runs ephemerally with ignored user config and an explicit
  `gpt-5.4` model; the failed attempt remains in the audit trail.
- Existing cycle tests used human-only fixtures while asserting machine fills.
  Those fixtures had to seed AI plans explicitly; otherwise the new, correct
  behavior is machine stand-down.
- Legacy fixed-spacing AI plans remain readable for historical replay, but new
  autonomous plans are required to carry `grid_orders`. Keeping legacy replay
  compatibility does not authorize creating new implicit grids.
- The current DAY plan predates this contract and was not rewritten mid-cycle.
  The first new-format production plan is `2026-07-10_NIGHT`.
- The first NIGHT tick initially arrived before a cycle bar was available and
  correctly skipped with `cycle_bars_missing`. Once the first trusted bar was
  present, the next run armed both explicit levels. A missing first-minute bar
  is not permission to synthesize one.

### Evidence

- Full repository regression: 1385 passed. Follow-up sizing and dashboard
  checks passed in focused suites after clarifying that grid weights allocate
  the machine track's full nominal budget and may total at most 1.0.
- Production machine plan:
  `outputs/dualtrack/plans/2026-07-10_NIGHT_ai.json` (short, range 4092-4116,
  explicit levels 4108 and 4113, newsletter provenance).
- Planner trace:
  `outputs/dualtrack/planning/2026-07-10_NIGHT_machine.json`.
- Live runtime at 21:04 Beijing: all five checks `ok`; 60-second scheduler had
  processed four NIGHT bars, machine author was `ai`, layers were
  `decision:ai_independent` and `grid:ai_levels_armed_no_fill`, and fills were
  empty because neither planned entry had traded.
- Browser proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/dualtrack-independent-machine-plan-2026-07-10-night.png`
  and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/dualtrack-independent-machine-full-2026-07-10-night.png`.
  The live page shows human long versus machine short, the 4092-4116 range,
  explicit 4108/4113 entries, and $55k/$45k nominal allocation. Browser
  warning/error log was empty; machine top-card DOM intersections were zero.

## 2026-07-10 - 机器轨平仓与早晚复盘统一为 Feishu 卡片

### Decisions

- 机器轨 entry、target、stop、cycle flatten 和主动 exit 全部发送 Feishu interactive card；不再让平仓事件退回纯文字。
- 平仓卡按结果使用固定颜色：止盈绿色、止损红色、周期结束蓝色、主动平仓灰色。
- 平仓卡第一屏只展示交易闭环、开平仓价、TP/SL、净结果、R 倍数、名义金额和审计状态；原始文字仍保留为发送审计正文。
- `pm_morning` 和 `pm_evening` 在统一 sender 内自动生成复盘卡，第一屏只保留黄金行情、窗口已实现盈亏、机器轨表现、原因和下一步。
- 早盘复盘、晚盘复盘和 12 小时机器作战单三个 Codex automation 的中文策略名统一为“机器轨网格”，并明确不得额外补发纯文字副本。

### Gotchas

- `outputs/feishu_reports/2026-07-10.json` 和 `outputs/dualtrack_trade_notifications/2026-07-10.json` 中的旧名称属于历史回执，不应改写；新发送从代码和 automation prompt 两端统一使用“机器轨网格”。
- Feishu interactive card 发送时不会展示兼容性 `text` 正文，但该正文仍用于 source hash 和回执审计。
- 本次没有补发历史交易或复盘样本，避免再次刷屏；下一笔自然发生的平仓和下一次定时复盘才会在真实 Feishu 群中显示新卡片。
- Missing visual proof: 尚无“修改后真实 Feishu 投递”的截图，因为本次刻意不发送额外样本。现有 `outputs/feishu_reports/2026-07-10.json` 仅作为历史发送 trace，不作为新样式 Evidence。

### Evidence

- 本地卡片视觉证据：`/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-10-feishu-trade-review-cards.png`。
- 对应渲染 trace：`/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-10-feishu-trade-review-cards.html`。
- 真实数据预览使用 `2026-07-10_NIGHT` 止盈 fill（净结果 `+$101.61`）和 `outputs/pm_reports/2026-07-10-evening.md`（窗口已实现 `-76.67 USD`）。
- Focused regression：57 passed，覆盖止盈绿色、止损红色、盈利复盘绿色、亏损复盘红色、去重和 dualtrack runner 回归。

## 2026-07-13 - Neutral means a bilateral grid, not no trade

### Decisions

- A healthy machine `neutral` decision must carry explicit long orders in the
  lower half of the expected range and explicit short orders in the upper half.
  Only a degraded planner failure may remain neutral with zero orders.
- Neutral orders share the machine track's total notional budget. Their weights
  across both sides must total at most 1.0, and each side has its own declared
  range-boundary stop.
- A mid-cycle plan repair records `execution_start`; only bars at or after that
  timestamp are eligible for fills. Earlier touched levels remain missed
  opportunities and must never be relabeled as live paper trades.
- The live DAY plan was safely revised only because it had zero fills. The old
  zero-order plan was quarantined before replacement.

### Gotchas

- The planner prompt, plan validator, runner, review language, and dashboard all
  independently encoded `neutral = no orders`; changing only the UI would have
  left the engine inactive.
- A bilateral grid must preserve original plan rung indexes when long and short
  orders are simulated separately, otherwise review rows can be matched to the
  wrong fills.
- The 2026-07-13 repair uses information available at 14:49 Beijing and is valid
  only from that moment. It cannot be used to claim that the morning's large
  move was actually traded.

### Evidence

- Full repository regression: `1403 passed in 382.58s`.
- Live plan: `outputs/dualtrack/plans/2026-07-13_DAY_ai.json` with two long and
  two short grid orders and `execution_start=2026-07-13T06:49:45+00:00`.
- Quarantined legacy plan:
  `outputs/dualtrack/quarantine/machine_plans/2026-07-13_DAY_ai.legacy-neutral-20260713T064945Z.json`.
- Browser proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/dualtrack-neutral-bilateral-grid-2026-07-13.png`.

## 2026-07-13 - Recorded PnL, durable history, and 12-hour review visibility

### Decisions

- Recovery replay remains distinguishable from live-observed execution, but it
  is included in the operator-facing recorded PnL. Daily and weekly ledgers now
  expose live, recovery, and recorded totals separately.
- Historical daily ledgers are rebuilt only from durable fill evidence. Missing
  fills remain zero rather than being inferred from a chart or plan.
- The split canvas shows the full daily history for both tracks and keeps the
  latest completed 12-hour review visible during the active cycle.
- Every newly closed cycle writes human and machine review dimensions for
  direction, key levels, entry signal, and TP/SL geometry. Missing structured
  signal evidence is shown as missing and is not graded optimistically.
- The next machine planning cycle receives the previous machine four-dimension
  review and must persist a concrete `review_adjustment`; a plan that ignores an
  available review fails closed instead of claiming a learning loop.

### Gotchas

- When the current cycle already exists in a rebuilt daily ledger, the frontend
  must exclude that cycle before adding live API state. Falling back to the
  daily total after the exclusion produced a temporary double count that was
  caught in browser verification.
- Recorded PnL and live-execution performance are different claims. Recovery
  replay counts in the former but stays explicitly excluded from the latter.
- Unrealized PnL is not treated as zero when the live mark is unavailable. The
  UI now says that it is temporarily excluded from today and this week.
- The already locked 2026-07-13 DAY plan was not rewritten. Review feedback
  starts with the next naturally planned cycle, preserving plan immutability.

### Evidence

- Focused DualTrack regression: `247 passed`.
- Browser DOM verification: 9 dated rows from 2026-07-05 through 2026-07-13,
  no browser errors, and both review widgets visible in the mid-cycle phase.
- Screenshots:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-ledger-12h-review.png`,
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-machine-12h-review.png`, and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-ledger-12h-review-full.png`.

## 2026-07-13 - Weekend-safe range reference and independent chart timeframes

### Decisions

- A range boundary breach is recorded with its first timestamp and bar geometry.
  For a neutral bilateral grid, the breached side stops while the other side
  remains independently eligible; the UI states this policy instead of calling
  every breach a whole-plan failure.
- New machine plans use the median range of the latest 10 complete non-weekend
  12-hour samples as a transparent minimum width. Weekend and incomplete
  samples remain in `planning_context.excluded_samples` rather than silently
  influencing the range.
- An AI range below that floor is widened symmetrically and records the original
  range, adjusted range, reference method, and excluded weekend count. Existing
  locked plans and historical fills are never rewritten by this safeguard.
- Human and machine main charts now keep separate timeframe state. Human remains
  `1m/5m`; machine supports `1m/5m/15m/30m/1h/4h`. All four context charts keep
  their existing independent selectors.

### Gotchas

- The 2026-07-13 DAY revision did not directly use weekend bars: it used 350
  bars from that Monday before locking. The real weakness was the single
  previous-cycle range input and lack of a robust minimum-width contract.
- `4044-4078` is 34 price points, not less than one point. It was still narrower
  than the current non-weekend 12-hour median of 54.75, so the new floor would
  transparently widen the same candidate to `4033.625-4088.375`.
- A shared `state.mainTf` also coupled WebSocket subscriptions and live-trade
  refresh behavior. Splitting only the buttons would have left the data paths
  coupled, so storage, REST loads, subscriptions, labels, and refresh gating all
  had to become track-specific.

### Evidence

- Focused backend/frontend regression: `123 passed`.
- Full repository regression: `1408 passed in 364.40s`.
- Browser checks at 1565, 740, and 390 CSS pixels: no horizontal overflow or
  timeframe-button intersection; human remained `1m` while machine rendered
  `4h`, both with 240 Binance bars and zero console warnings.
- Screenshot:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-independent-timeframes-range-record.png`.

## 2026-07-13 - Equal main-chart timeframes and daily context

### Decisions

- Human and machine main charts expose the same independent timeframe set:
  `1m/5m/15m/30m/1h/4h`.
- Every context chart adds `1d` while preserving its own saved selection.
- Daily bars use the existing Binance USD-M native REST and WebSocket interval;
  no derived, fallback, cached, or synthetic data path was added.

### Gotchas

- Equal available options do not mean synchronized selection. Changing one
  track must not change the other track.
- A live daily candle keeps its UTC-open timestamp throughout the day, so the
  freshness bound follows the existing two-interval policy and is 2880 minutes.

### Evidence

- Static split-canvas regression: `20 passed`.
- Browser: human main `4h`, machine main `1m`, human context `1d` with 215
  Binance bars; no console warnings or errors.
- Mobile browser at 390 CSS pixels: zero horizontal overflow and zero
  timeframe-button intersections across all six selectors.
- Screenshot:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-equal-main-timeframes-daily-context.png`.

## 2026-07-13 - Confirmed range breach triggers a full machine replan

### Decisions

- This decision supersedes the earlier same-day rule that kept the unbreached
  side of a neutral grid eligible after a boundary breach.
- The first touch of either range boundary pauses every new entry from the old
  plan. Existing positions remain exit-only and can close only through their
  recorded TP/SL rules.
- A true range break requires three consecutive complete 1-minute closes
  strictly outside the boundary. Confirmation triggers a fresh AI decision for
  direction, the complete range, key levels, and all grid orders.
- The replacement plan starts no earlier than the next unseen bar. It cannot
  create fills from bars already available to the planner, and all fills from
  the superseded plan remain in the durable ledger.
- Range revisions are archived with both plan versions and the trigger bars.
  Replanning is limited to two revisions per cycle with a 60-minute cooldown.
- Planner failure is fail-closed: old entries remain paused, the error is
  visible, and the system retries after five minutes. Fewer than 30 minutes
  before cycle close, it waits for the next 12-hour decision instead.

### Live Result

- The old `4044-4078` neutral plan first touched its upper boundary at 16:18
  Beijing and confirmed the break with the 16:21-16:23 closes.
- Two existing short positions were genuinely stopped at 4078; this history
  was preserved and was not reclassified or rewritten.
- After the scheduler environment was repaired, the machine independently
  replaced the plan at 19:33 Beijing with a `short` plan, full range
  `4042-4098`, and three sell-grid entries at 4072, 4081, and 4089.5.
- The runtime returned to `ok`, the new plan was inside range on its next tick,
  and no historical level was backfilled as a new live trade.

### Gotchas

- The live scheduler initially failed to launch the planner because launchd's
  PATH did not include `/opt/homebrew/bin`, where `codex` and its Node runtime
  are installed. The generated schedule and installed live-tick job now carry
  an explicit deterministic PATH.
- A terminal `failed` state would have left the machine paused for the rest of
  the cycle even after the environment was fixed. Failed replans now retain the
  safety pause but retry every five minutes.
- Replanning from the latest bar timestamp itself can introduce look-ahead if
  that bar was already observed. `execution_start` is therefore later than the
  observed bar and makes the next bar the first eligible execution input.
- Recovery replay rows remain separate from live-observed fills. The four live
  fills from the old plan remain the runtime paper sample; recovery rows are
  still excluded from that claim.

### Evidence

- Focused range, scheduler, API, and split-canvas regression: `96 passed` and
  `88 passed` in the two post-change suites.
- Final full repository regression, including launchd PATH and failed-replan
  retry behavior: `1411 passed in 371.13s`.
- Live runtime: status `ok`, scheduler `active` with 4/4 matching and healthy
  jobs, current range `4042-4098`, and machine not stood down.
- Browser DOM: global status `运行`, direction `做空`, three visible short
  grid orders, complete reassessment explanation, zero console errors, and no
  horizontal overflow at 1280 CSS pixels.
- Screenshot:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-range-reassessment.png`.

## 2026-07-13 - Pending human limit orders stay visible and executable

### Decisions

- An accepted human limit order remains visible in the split canvas under
  `Current orders` until it is filled, cancelled, or rejected. The row exposes
  side, notional, quantity, limit price, TP/SL, and submission time.
- A limit order that is already marketable against the trusted current mark is
  filled immediately as taker liquidity while preserving its requested limit
  price for audit.
- Live-tick processing must continue when there are accepted entry orders even
  if no position is open. Position-exit checks and pending-entry checks share
  the same execution snapshot but have independent eligibility.
- A partial OHLC bar that began before order acceptance may use only its current
  trusted mark for post-order matching. Its earlier high and low cannot create
  a look-back fill.

### Gotchas

- The execution API already returned accepted orders; the split canvas simply
  never rendered them, so a persisted order looked lost to the user.
- The old protective-exit sweep returned early whenever there was no open
  position, which also skipped every pending entry order.
- The affected short limit at 4,056.9 was persisted and marketable. After the
  fix, the normal 60-second live tick filled it and the UI now shows the open
  short position instead of an invisible pending state.

### Evidence

- Focused execution, cycle-runner, API, and split-canvas regression:
  `83 passed`.
- Full repository regression: `1414 passed in 428.85s`.
- Live browser: `Current orders` shows `0` after the real fill; the position
  shows short `12.3247`, entry `4,056.9`, TP `4,036.7`, and SL `4,070.1`.
- Screenshot:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-pending-order-lifecycle.png`.

## 2026-07-13 - Twelve-hour review closes into one testable next-iteration change

### Decisions

- Every completed 12-hour cycle now records market regime, direction, range,
  key levels, signal, TP/SL geometry, execution evidence, and PnL separately.
- A neutral market is determined by directional efficiency: the absolute net
  move divided by the full high-low range. A slightly higher or lower close is
  still neutral when path efficiency is below 35%.
- No trade is a valid observed result. It is not automatically scored as a
  strategy failure when planned levels were not touched.
- One review can propose at most one changed dimension. It must have a durable
  change id, one expected metric, and a stated validation rule.
- Human-track output is advisory only and can never modify or lock the next
  human plan. Machine-track output enters a paper challenger only.
- A machine challenger needs at least 10 completed cycles and 30 trades before
  it can be shown as ready for operator review; 100 trades are preferred. It
  is never auto-promoted.
- The next machine planner must explicitly carry the same structured change id,
  dimension, mode, and metric. Legacy free-text reviews cannot silently alter
  the next plan.

### Gotchas

- Treating neutral as exact `close == open` misclassifies ordinary range-bound
  sessions as directional and creates false review feedback.
- One losing or breached cycle is evidence for a challenger, not permission to
  tune several parameters or declare a better strategy.
- Reading review prose is not self-evolution. The loop becomes auditable only
  when the proposed change is carried into the next plan and accumulated under
  the same id with explicit sample counts.
- Historical reviews lack the v3 evidence contract. They remain visible but are
  labeled insufficient and cannot produce an automatic next-iteration change.
- Meeting the minimum sample only means `ready_for_operator_review`; it never
  means automatic production promotion.

### Evidence

- Focused review, dashboard, scoring, and cycle regressions: `102 passed`.
- Full repository regression: `1418 passed in 406.24s`.
- Browser at 1280 and 740 CSS pixels: both review tracks are visible, zero
  horizontal overflow, and zero console errors.
- Current historical cycle is explicitly labeled as legacy evidence and does
  not claim a validated challenger.
- Screenshots:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-review-v3.png`
  and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-review-v3-mobile.png`.

## 2026-07-13 - Position protection remains visible and active across cycles

### Decisions

- The position, risk, and fills widgets must derive from the same durable open
  trades, including positions opened in an earlier cycle and still open now.
- The position widget lists every open trade separately with direction,
  quantity, notional, entry, holding time, exact TP, and exact SL.
- TP and SL shown for a position must come from that trade or its entry fill.
  The current plan is never a fallback because it may describe another trade.
- A position remains protected after the cycle changes. Protective exits and
  manual closes are executed against the position's origin ledger while also
  recording the current request cycle for audit.

### Gotchas

- The fills table already included prior same-day trades, while position and
  risk read only the current cycle. That made one page contradict itself.
- Current-cycle-only protective sweeps did more than hide the position: they
  also stopped monitoring the carried position's TP and SL.
- The user's short from the day cycle was still open when this was diagnosed.
  Once cross-cycle monitoring was restored, its trusted 1-minute bar touched
  TP and the engine closed it normally at 4,036.7.

### Evidence

- Live result: short entry `4,056.9`, TP `4,036.7`, SL `4,070.1`, closed at TP
  at Beijing time `22:09`; total realized PnL including costs is `+$243.97`.
- Focused dashboard, API, execution, and cycle regressions: `80 passed`.
- Full repository regression: `1422 passed in 413.73s`.
- Browser: human position shows `无持仓`, fills show the TP close at `4,036.7`
  and `+$243.97`, horizontal overflow is zero, and console errors are empty.
- Screenshot:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-position-protection.png`.

## 2026-07-13 - Daily cycle and visible two-second Binance market refresh

### Decisions

- New DualTrack cycles use one Beijing natural day: `00:00-24:00`, starting
  `2026-07-14`. Historical DAY/NIGHT cycle ids keep their original 12-hour
  meaning so stored trades, reviews, and ledger records do not move.
- The existing `2026-07-13_NIGHT` cycle is a one-time transition window from
  `21:00` to midnight. At midnight it closes and `2026-07-14_DAY` opens for 24
  hours.
- The split canvas refreshes all active chart timeframes from Binance XAUUSDT
  public REST every two seconds. There is no alternate provider, cached market
  fallback, or synthetic bar.
- Every chart uses the same latest Binance 1-minute close for its still-open
  candle. Historical OHLC remains the exchange response, while the current
  close and high/low envelope stay internally valid.
- Chart refresh is a display path. Protective exits and paper matching remain
  driven by the trusted 1-minute backend cadence, not by browser polling.

### Gotchas

- A WebSocket `open` event proved only that the handshake succeeded. Direct
  probes of XAUUSDT kline, mark-price, and aggregate-trade streams produced no
  messages, so the prior green `connected` state was a false health claim.
- Concurrent REST requests for different timeframes can finish at slightly
  different instants. Without one canonical live close, six individually valid
  responses still show contradictory prices on one screen.
- The 60-second full-page data reload briefly reintroduced divergent closes
  until it was required to await the same canonical two-second refresh before
  rendering.
- Reusing `2026-07-13_DAY` for a new natural-day cycle would collide with an
  already completed historical cycle. The midnight cutover preserves identity
  and audit history.

### Evidence

- Focused DualTrack regression: `299 passed`.
- Full repository regression: `1426 passed in 421.93s`.
- Live browser at 1280 CSS pixels: all six chart closes remained identical
  before and after the page's 60-second full reload; latest prices changed
  during the sample and horizontal overflow was zero.
- The data widget shows `2秒刷新`, a seconds-level refresh timestamp, and
  `Binance REST ... 无备用源`.
- Screenshots:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-24h-cycle-cutover.png`
  and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-13-dualtrack-2s-live-market.png`.

## 2026-07-14 - Unified daily PnL bars and cumulative NAV

### Decisions

- The operator-facing ledger has one PnL definition: every durable paper fill
  is recorded in that track's daily realized PnL. Recovery provenance remains
  in raw evidence for audit but is not a separate performance bucket or UI
  column.
- Daily ledger artifacts are rebuilt after every filled human order and every
  minute-level DualTrack live tick. The ledger GET also reconciles from durable
  fills so a stale derived ledger cannot hide a later cross-cycle exit.
- The split canvas replaces the four-column history table with an interactive
  daily PnL histogram and a cumulative NAV line starting at zero. Hover shows
  date, daily PnL, and cumulative NAV.

### Gotchas

- The July 13 human row showed `+$93.48` because it combined a stale DAY entry
  cost of `-$2.50` with the NIGHT trade's `+$95.98`. The DAY trade later closed
  at TP for `+$243.97`, but that cross-cycle exit had not rebuilt the DAY ledger.
- Hiding the recovery column without fixing ledger refresh would only conceal
  the accounting error. Durable fills must remain the source of truth and the
  daily/weekly ledgers must be treated as derived views.
- Lightweight Charts returns business-day objects from crosshair events even
  when input times are ISO date strings. Tooltip lookup must normalize that
  object back to `YYYY-MM-DD`.
- Page CSS color literals are token-gated; a tooltip shadow introduced an
  unauthorized RGBA literal and was removed before completion.

### Evidence

- July 13 human realized PnL now reconciles to `+$339.95`: DAY `+$243.97` plus
  NIGHT `+$95.98`.
- Browser hover on July 13 shows daily PnL `+$339.95` and cumulative NAV
  `+$432.49`; both tracks render daily bars plus NAV curves, with zero console
  errors and zero horizontal overflow.
- Focused DualTrack regression: `299 passed`.
- Full repository regression: `1426 passed in 384.18s`.
- Screenshot:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-14-dualtrack-daily-pnl-nav.png`.

## 2026-07-14 - Unified morning trading card closes review into next plan

### Decisions

- The 09:00 machine brief and the 10:00 morning review are one operator-facing
  morning trading card: previous-cycle result, one review-driven adjustment,
  the current cycle plan, and the evidence-chain status.
- The card reads the actual cycle window. It says `24 hours` after the natural-
  day cutover and still renders historical 12-hour cycles correctly.
- `计划链路正常` means a completed previous review was read, an adjustment was
  recorded, and the current AI plan is locked. Full lifecycle/runtime health
  remains an independent audit conclusion.
- The previous review is addressed through `previous_review_cycle_id`; missing
  or incomplete review evidence is shown as missing and PnL is not inferred.

### Gotchas

- Keeping `未来 12 小时` in automation prose after the 24-hour cutover would
  make a visually polished card factually wrong.
- A successful brief build does not prove full system health. The card must not
  turn plan availability into a global green status claim.
- Neutral plans carry explicit long and short grid sides. Orders without a side
  must fall back to the plan direction or they can appear in both grid columns.
- The existing 10:00 report is a duplicate user-facing surface once its prior-
  cycle result is incorporated into the 09:00 card.

### Evidence

- Relevant Feishu and machine-plan regression: `35 passed`.
- Live Feishu delivery verified at `2026-07-14T01:31:27+00:00`; the newest
  `dualtrack_machine_brief` receipt is `delivered=true`, channel `feishu`, and
  message format `interactive_card`.
- Desktop and mobile visual proofs:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-14-unified-morning-trading-card.png`
  and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-14-unified-morning-trading-card-mobile.png`.

## 2026-07-14 - Restore 12-hour strategy windows and expose machine rules

### Decisions

- Machine planning and review return to two Beijing-time windows:
  `09:00-21:00` and `21:00-09:00`. Daily PnL and NAV accounting remain on the
  Beijing natural day so strategy cadence and accounting cadence stay separate.
- The already-open `2026-07-14_DAY` cycle closes at `21:00` as a one-time
  21-hour transition window. Normal 12-hour DAY/NIGHT cycle ids resume from
  `2026-07-14_NIGHT`; recorded fills are not reassigned retroactively.
- The machine canvas shows the complete executable IF/THEN rule set and
  highlights the branch currently matched by live runtime state. Thresholds and
  prose are derived from the config API instead of being a second hard-coded
  strategy specification.
- Direction and range are re-evaluated on the 12-hour cadence or after a
  confirmed range breach. Signal execution remains continuous, and market-data
  or risk failures stop new entries immediately.

### Gotchas

- Reinterpreting the existing July 14 cycle as ending at `09:00` would move or
  orphan already-recorded fills. The explicit `21:00` transition preserves the
  audit trail.
- A conceptual `two 5-minute closes` rule was discussed but is not the current
  engine contract. The implemented rule remains the configured three
  consecutive 1-minute closes and must not be silently relabeled in the UI.
- A rules panel that is only explanatory copy will drift from execution. The
  displayed confirmation count, timeframe, cooldown, retry delay, replan cap,
  and minimum remaining time must all come from `/api/dualtrack/config`.
- The in-app browser runtime currently fails while loading its browser client
  with `Cannot redefine property: process`; source and API checks are trace, not
  a substitute for the screenshot required by the Evidence Contract.

### Evidence

- Focused DualTrack regression: `300 passed`.
- Full repository regression: `1427 passed in 387.79s`.
- Inline dashboard JavaScript syntax and touched-file whitespace checks pass.
- Missing visual proof: the machine rules panel on the localhost split canvas
  still requires a desktop and sub-760px browser screenshot after the in-app
  browser connection is restored.

## 2026-07-14 - Single production strategy console migration

### Decisions

- `dashboard-dualtrack-split.html` is now a single production-strategy
  console. It reads `strategy-production-console-v1`, not the previous
  human/machine canvas payloads. The legacy pages, APIs, plan files, fills and
  ledgers remain available as historical compatibility surfaces.
- `StrategyPlan` is the only active production plan per cycle. It has a stable
  ID, integer version, cycle, parameters, intraday rules, selected proposal
  IDs, and per-field origin (`human`, `ai`, or `confirmed`). A new console
  order carries `strategy_plan_id` and `strategy_plan_version` into the paper
  fill ledger.
- Old human and AI plans are read losslessly as same-schema `PlanProposal`
  records. The compatibility rule remains explicit: locked human proposal
  first, otherwise AI proposal, otherwise no production plan and no new entry.
  It never blends fields silently.
- `strategy_shadow` is a separate, deterministic historical counterfactual
  path. It consumes frozen plan parameters plus chronological market events,
  writes only `outputs/dualtrack/strategy_shadows/`, and reports orders,
  fills, positions, PnL and review metrics. `execution_shadow` remains the
  legacy ledger-vs-execution-engine parity surface and is not used for
  strategy-performance comparisons.

### Gotchas

- Current Tiger/COMEX data was real but stale at validation time. The console
  correctly rendered the chart while stopping all new entries; it must not be
  relabeled as a live-ready chart or replaced with synthetic prices.
- Historical dual-track paper fills cannot be retroactively rewritten into a
  single ledger without changing audit evidence. They are retained as legacy
  compatibility records; all new production-console orders have the new plan
  attribution fields.
- The first two recorded what-if variants (`atr-1-grid-8` and
  `atr-2-grid-16`) had zero closed trades against the exact frozen historical
  event window. Zero is a valid result, not missing data or a reason to invent
  outcomes.
- The preferred in-app browser connection failed with `Cannot redefine
  property: process`; visual proof was captured with local headless Chrome
  against the same localhost URL. This is browser-rendered evidence, not a
  source-only claim.

### Evidence

- Targeted production-console, plan-store and shadow regression: `40 passed`.
- Desktop, mobile, and Strategy Shadow browser-rendered evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-production-console-desktop-2026-07-14.png`,
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-production-console-mobile-2026-07-14.png`, and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-production-console-shadows-2026-07-14.png`.

## 2026-07-14 - Paper robot console controls

### Decisions

- The production console now exposes actual paper-control actions: start,
  stop, cancel all pending limit orders, revise range/grid as a new strategy
  plan version, and reset displayed statistics by recording a new baseline.
- Stop is execution-relevant: once control state has been configured, the
  intraday runner skips new production entries while it is stopped. Protective
  exits remain outside this gate.
- Cancel changes only `accepted` pending orders. Replanning supersedes the old
  plan but retains it; reset statistics retains every fill and ledger artifact.

### Gotchas

- These are paper controls, not live-broker controls. They never submit,
  cancel, or flatten a live broker account.
- Current market data remains stale, so start does not override the independent
  trusted-market gate. A started robot still cannot open a position until the
  data feed is fresh and canonical.

### Evidence

- Controls, plan history and runner regression: `65 passed`.
- Browser-rendered control surface:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-live-controls-2026-07-14.png`.

## 2026-07-14 - Baseline robot layout and live Tiger refresh

### Decisions

- The console layout follows the supplied grid-robot baseline literally: left
  side starts with trend recognition and strategy configuration, then the
  robot start / stop-and-clear controls; account summary, large price/grid
  chart, real-time status, and fills occupy the same reading order.
- MACD is no longer expanded by default. The default chart preserves the
  primary price/grid view with EMA overlays.
- Demo account equity is read from the real paper ledger (`ending_cash`), not
  a display placeholder. The validated value is `$9,906.84` from a `$10,000`
  starting balance and recorded paper PnL.

### Gotchas

- The stale quote was not a front-end refresh failure: the Tiger importer had
  not run since `2026-07-06T09:48:00Z`. A read-only Tiger import refreshed 500
  real MGC bars to `2026-07-14T04:18:00Z`; no fallback or synthetic quote was
  used.
- That one refresh does not prove a durable always-on ingestion schedule. The
  next control-plane pass must wire the importer into the recurring runtime
  before calling the feed continuously live.

### Evidence

- Layout, controls, plan-store, runner and API regression: `65 passed`.
- Live data browser proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-baseline-live-feed-2026-07-14.png`.

## 2026-07-14 - Reference-first grid robot shell

### Decisions

- The page is now rebuilt from the supplied grid-robot reference before any
  additional backend-driven layout work. Its first view is exactly the
  reference reading order: market/trend, strategy configuration, running
  adjustment; then account overview, price/grid, real-time state, and fills.
- Removed the `人工 / AI 提案差异` card from the operational first view. It
  remains represented in the production-plan evidence model and can return in
  a later history/detail surface, but it no longer competes with robot use.
- The reference controls now have real UI affordances: direction and style are
  selectable, the visible grid parameters are editable, start and
  stop/cancel/flatten retain their paper-control handlers, and the K-line
  period selector reads the existing canonical market-bars endpoint rather
  than resampling or fabricating data in the browser.

### Gotchas

- “Smart fill” deliberately only fills reviewable visible parameters in this
  phase; applying a changed direction/range/grid creates an explicit plan
  version. It is not an opaque automatic mutation.
- The 5-minute canonical response is reachable and has 240 real bars, but is
  currently `stale` (not synthetic). The visual console therefore shows the
  red fail-closed state; the missing recurring Tiger ingestion remains a
  backend runtime concern, not a front-end refresh substitute.
- Local headless Chrome constrains very narrow windows to a larger desktop
  layout width, so its 390px screenshot is browser-rendered but only
  conservative proof of mobile cropping. CSS switches the actual page to
  single-column left cards and single-column control fields below 1120px.

### Evidence

- Reference-shell focused regression: `65 passed`.
- Browser-rendered desktop and narrow-screen evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-reference-shell-desktop-2026-07-14.png` and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-reference-shell-mobile-2026-07-14.png`.

## 2026-07-14 - Reference parity and button acceptance

### Decisions

- The reference comparison is now enforced at component level: the header
  badges, left-card ordering, trend recommendation action, direction/style
  selectors, parameter block, start/stop controls, running adjustment card,
  four account metrics, large price/grid area, status and fills all use the
  same visual reading order as the supplied robot console.
- The chart remains the existing trusted `standard-kline` implementation as
  required by the trading console contract. Grid lines are generated from the
  active plan's real range and real grid count, not from screenshot values.
- A real DevTools browser pass clicked every visible application button, all
  six K-line period buttons, the period selector, every tab, and six chart
  library controls. Paper-control actions were then verified against the
  backend state rather than only checking their visual affordance.

### Gotchas

- The acceptance pass intentionally exercised the paper `adjust_plan` action.
  It preserved the prior plan and created auditable versions through v4, as
  designed; the final runtime state is `stopped`, with no new position opened.
- There was no open position in the ledger, so the conditional `手动平仓`
  control was correctly not rendered and could not be clicked. Its handler is
  retained for when a position exists.
- The market is still `stale`. Button validation did not bypass the market
  gate: start can set the paper desired state, but no fresh-market entry can
  be produced, and the final visible state remains stopped/new entries blocked.

### Evidence

- Focused regression and syntax validation: `65 passed`.
- Browser interaction pass: all app, timeframe, tab and chart buttons passed;
  start/stop, replanning and statistics reset all passed their backend-state
  assertions. Final state: `stopped`, active plan version `4`.
- Browser-rendered desktop, live button-check and actual mobile evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-reference-iteration-desktop-2026-07-14.png`,
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-browser-button-check-2026-07-14.png`, and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-reference-actual-mobile-2026-07-14.png`.

## 2026-07-14 - Console market source switched to Binance

### Decisions

- Per the explicit operator decision, the production-console default market
  reader now uses the existing Binance USD-M demo GOLD/XAUUSDT one-minute
  feed. It is the same feed refreshed by the installed 60-second
  `gold-1m-feed` heartbeat.
- This is a source selection, not a fallback chain: when the configured
  Binance source is absent or stale, the console blocks entries. It does not
  quietly fall back to Tiger or synthetic data.

### Gotchas

- The Tiger MGC data is still retained in the market database and historic
  receipts, but is no longer the console's default quote source. It was 39
  minutes old at diagnosis time because `tiger_futures_feed.enabled` is false
  and no Tiger heartbeat is installed.
- The Binance source is an execution-venue/demo feed and carries its explicit
  provenance flags (`public_proxy_feed`, `execution_venue_feed`,
  `crypto_perpetual`, `xauusdt`). It is not claimed to be COMEX broker data.

### Evidence

- Market feed, API, console static, strategy control and runner regression:
  `70 passed`.
- Live console API assertion: `GOLD`, `binance_usdm`, `ready`, `fresh`, and
  non-synthetic.
- Browser-rendered proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-binance-live-2026-07-14.png`.

## 2026-07-14 - Production console interaction repair

### Decisions

- The trend card is now strictly an operator-facing explanation: market,
  symbol, timeframe, bar count, update time, trend and confidence.  Provider
  implementation flags and raw operation metadata are retained in API
  provenance, not rendered as trading guidance.
- “根据当前行情智能填充参数” uses the already available, trusted one-minute
  Binance bars to calculate a reviewable ATR/EMA suggestion.  It changes only
  visible form fields and displays its calculation; applying the proposal
  still requires the separate “调整区间” operation and therefore creates an
  auditable plan version.
- Start and stop/cancel/flatten are real paper-control actions.  The console
  disables the active button during a request and preserves the outcome notice
  across normal live refreshes, so an operator can see that the command was
  accepted rather than mistaking a five-second repaint for a no-op.
- Production-plan grid levels are rendered as chart price lines with labels,
  using the existing `standard-kline` chart.  EMA is selectable with editable
  fast/slow periods and MACD is an opt-in lower pane; neither creates a new
  charting engine or invents market data.

### Gotchas

- A real-browser pass caught a render-path typo that static tests did not
  exercise.  It was fixed before the final visual capture; this is why the
  acceptance record includes user actions rather than only DOM-source checks.
- The active plan's lower grid levels may sit outside the current candle price
  viewport after the market moves.  They remain attached to the chart and are
  visible when the user zooms/pans; the compact grid summary always states the
  complete range, count and spacing.
- The production browser bridge could not initialise in this environment, so
  the interaction pass used a clean local Chrome session through DevTools.
  It is still a real browser rendering the served dashboard, not a source-only
  assertion.  The stop/start acceptance action restored the pre-test paper
  state: `running`.

### Evidence

- Final focused regression plus syntax check: `95 passed`.
- Real-browser journey passed: trusted fresh market, clean trend card, smart
  fill and refresh persistence, editable EMA, MACD rendering, chart grid
  lines, and stop/start backend control with persistent user feedback.
- Visual proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-production-ready-desktop-2026-07-14.png`,
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-production-ready-mobile-2026-07-14.png`, and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-macd-enabled-2026-07-14.png`.

## 2026-07-14 - Production paper-grid lifecycle closure

### Decisions

- Direction, style and parameter controls now call one authoritative backend
  preview contract before any order is placed. Neutral produces bilateral grid
  orders, long produces buy orders below market, short produces sell orders
  above market; steady uses a wider/lower-density grid and aggressive uses a
  narrower/higher-density grid. The exact preview is drawn on the standard
  K-line chart and becomes the committed plan only after Start is confirmed.
- Start is a real paper lifecycle action: it creates a new versioned
  `StrategyPlan`, submits accepted paper limit orders to the existing ledger,
  and records `strategy_plan_id` plus `strategy_plan_version` on every order.
  Identical repeated Start requests are idempotent. A changed configuration
  while running must use the explicit running-adjustment action.
- Runtime truth is represented by `desired_state` and `actual_state`. The UI
  derives its button emphasis from actual state: Start is enabled only while
  stopped; while running Start is dimmed and Stop is prominent, with the real
  accepted-order count shown in the header and status card.
- Stop is complete only after accepted orders are cancelled, open paper
  positions are flattened with a fresh trusted quote, and reconciliation
  passes. Running adjustment cancels and replaces grid orders without changing
  the production ledger into a shadow ledger or silently stopping the robot.
- A process-wide re-entrant control lock and a single activation helper enforce
  at most one active production plan even when browser refresh, legacy
  compatibility reads and Start arrive concurrently.
- For alternate K-line periods, a stale exact-period Binance series no longer
  masks fresh same-provider one-minute data. The server first prefers a fresh
  exact series, then deterministically derives the requested period from fresh
  Binance one-minute bars; it never falls back to another provider or
  synthetic prices.

### Gotchas

- Real-browser testing exposed a race where the compatibility reader could
  activate an older plan between Start and the next refresh. It was reproduced
  with a plan/order version mismatch and fixed with the shared control lock;
  a concurrent regression now asserts exactly one active plan and matching
  order versions.
- The browser pass also caught three source-level false positives: a stale 5m
  exact series was being returned ahead of fresh 1m derivation, the mobile
  account-grid CSS had an invalid `repeat()` value, and the preview hint could
  remain visible after the same plan became live. All three were fixed and
  rechecked in a real browser.
- Acceptance intentionally created auditable paper plan history through v23
  and then stopped the robot. Historical plans and cancelled orders were
  retained. Final acceptance state is stopped, zero accepted orders, zero open
  positions, reconciliation passed, and exactly one active plan record.
- This closes the paper-trading console lifecycle only. It does not migrate to
  live trading and does not switch execution to Nautilus.

### Evidence

- Real-browser journey passed 17 lifecycle and responsive assertions: fresh 5m
  switching, direction/style geometry, preview chart layers, versioned Start,
  running regrid, refresh persistence, idempotent Start, stop/cancel/flatten,
  Strategy Shadow isolation, mobile layout and zero browser runtime errors.
- Final repository regression: `1423 passed in 378.15s`; focused production
  strategy, market-feed, API, console and shadow regression: `52 passed`.
- The page also passes the repository design-token contract; operational state
  colours remain semantic while ungoverned colour literals and forbidden heavy
  font weights were removed.
- Running short/aggressive proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-production-grid-running-2026-07-14.png`.
- Running neutral/steady regrid proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-production-grid-regridded-2026-07-14.png`.
- Strategy Shadow comparison proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-production-shadows-2026-07-14.png`.
- Final mobile proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-production-grid-mobile-2026-07-14.png`.

## 2026-07-14 - Goldbot public dashboard v5 deployment

### Decisions

- The production strategy console is now published at
  `https://goldbot.park-ai-intel.com/dashboard-v5.html`. The existing v4 URL
  is retained for compatibility, and the domain root redirects to v5.
- Public v5 is an explicit observer surface. Live market data, account state,
  production orders, timeframe switching and strategy/grid preview are
  available; Start, Stop/cancel/flatten, running adjustment, statistics reset
  and manual close remain local-only. The gateway permits only the preview
  action and rejects every production mutation, so deployment cannot silently
  expand the trading-control boundary.
- The public route serves the same tested console implementation through a
  stable v5 alias. It does not copy or fork the page into a second source of
  truth.
- The existing named Cloudflare Tunnel remains the deployment path. Its
  connector was upgraded to cloudflared 2026.7.1 and pinned to QUIC after the
  previous connector stopped registering reliably over HTTP/2.

### Gotchas

- A local gateway process being healthy was not sufficient evidence that the
  public site was deployed: the initial public response was Cloudflare 1033
  because no tunnel connector was registered. Completion required both a
  persistent connector and successful external HTTP/browser checks.
- The public console displays the pre-existing running paper plan v24 and its
  five accepted orders. That plan started before this deployment; the public
  preview test did not start, stop, replace or otherwise mutate it.
- The public hostname continues to depend on the local Mac, gateway and named
  tunnel being online, matching the existing v4 hosting model.
- The bundled in-app browser bridge could not initialise because its process
  shim conflicts with the current desktop runtime. Final acceptance therefore
  used a clean, headless Google Chrome session with real layout, canvas,
  interaction and network assertions.

### Evidence

- Public desktop proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-dashboard-v5-public-desktop-2026-07-14.png`.
- Public strategy-preview/grid proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-dashboard-v5-public-preview-2026-07-14.png`.
- Public mobile proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-dashboard-v5-public-mobile-2026-07-14.png`.

## 2026-07-15 - Goldbot authenticated remote control

### Decisions

- Goldbot v5 will use Cloudflare Access on the existing Tunnel rather than
  moving the frontend to Vercel. The chosen identity is the current Cloudflare
  account member, additionally restricted at the gateway to
  `zinan92@hotmail.com`.
- The requested application session duration is seven days (`168h`). Access
  authentication unlocks Start, Stop/cancel/flatten, running adjustment,
  statistics reset and manual close; local `127.0.0.1:8765` control remains
  available independently.
- The public gateway validates the signed Access JWT against the application
  audience and Cloudflare team issuer before any production mutation. Preview
  remains non-mutating and available through the existing safe path.
- Authenticated mutation attempts and upstream results are written to a
  dedicated JSONL audit trail without logging the Access token.

### Gotchas

- The existing Wrangler OAuth and Tunnel certificate can manage deployment and
  connectivity but both receive `403` from the Access applications API. A
  one-time scoped Cloudflare token with `Access: Apps and Policies Write` is
  required to create the edge application and policy.
- Initializing Zero Trust through the signed-in dashboard reaches the free-plan
  activation checkout at `$0/month`, but Cloudflare requires an explicit
  authorization to charge the saved card if usage exceeds the free allowance.
  Activation is paused at that consent step; no billing authorization has been
  accepted by the agent.
- Before the Access application, team domain and AUD tag were installed, the
  gateway remained fail-closed and the public page stayed read-only. That
  intermediate state did not expose remote trading controls.

### Completed configuration

- Activated Cloudflare Zero Trust Free after Park personally accepted the
  dashboard billing authorization. Team name: `plain-pine-ac3d`.
- Created self-hosted Access application `Goldbot v5` for
  `goldbot.park-ai-intel.com` with application id
  `66b8a370-676d-4fcb-9b68-46bbbba384a2` and AUD tag
  `f591b72ad4a3d3bf1aa602a76ab991dbe4fb1b1802d6d513d0db167b8b403b5a`.
- Created allow policy `Goldbot operator` with policy id
  `04c3698f-4749-4809-97b6-1cdbb585e69a`, restricted to
  `zinan92@hotmail.com`. Both the Access application and gateway session
  contract use `168h`.
- Installed the Access team issuer and AUD in the persistent gateway LaunchAgent
  and restarted only the gateway. No strategy start, stop, order, position,
  statistics-reset or manual-close mutation was invoked during acceptance.

### Acceptance

- An unauthenticated public request now receives a Cloudflare Access `302` to
  the `plain-pine-ac3d.cloudflareaccess.com` login page.
- The signed-in public dashboard displays `已登录 · 可控制`; the existing runtime
  state controls render normally, with the active stop/cancel/flatten control
  available and the start control disabled while the UI reports the robot as
  running.
- Gateway authentication unit tests: 5 passed. Related dashboard/control-plane
  regression tests: 76 passed.

### Evidence

- Authenticated public dashboard and identity/control badge:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-v5-authenticated-control-2026-07-15.png`.
- Authenticated start/stop state and live runtime panel:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-v5-authenticated-start-stop-2026-07-15.png`.

## 2026-07-15 - Goldbot Access-session and trusted D1 startup recovery

### Decisions

- All dashboard API calls now identify themselves as AJAX with
  `X-Requested-With: XMLHttpRequest`. An expired Cloudflare Access session therefore returns
  a deterministic `401`; the page performs one top-level refresh to re-enter Access login
  instead of parsing the login HTML as JSON.
- Non-JSON API responses now fail with a user-readable service message. The raw
  `Unexpected token '<'` parser error is no longer exposed.
- Restored the startup prerequisite by adding native Binance USD-M `1d` candles in the
  independent datafeed adapter. Strategy planning still fails closed unless D1, 4H, 1H,
  15m and the 1m execution tape are trusted and non-synthetic.

### Gotchas

- The visible error looked like a Binance JSON failure, but the `<DOCTYPE>` response was a
  Cloudflare Access login page returned to a background request. Cloudflare documents this
  as an AJAX/session-expiry behavior.
- After isolating the login-layer symptom, a second blocker remained: the datafeed adapter
  declared Binance USD-M support only through `4h`, so a fresh 1m chart could coexist with
  a correctly blocked D1 strategy preview.
- No strategy start, stop, order, position, or ledger mutation was performed during this
  repair. Startup readiness was verified with the non-mutating preview action.

### Verification and evidence

- Access AJAX probe now returns `401` instead of redirecting to an HTML login page.
- Non-mutating production preview succeeds with 49 neutral/steady orders and fixed
  D1/4H/1m planning contexts.
- Visual proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-v5-access-recovery-market-2026-07-15.png`.
- Browser start-readiness proof after the Binance D1 repair:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-v5-start-ready-binance-d1-2026-07-15.png`.
- Focused dashboard, market-feed, strategy-control and recommendation regression:
  `82 passed`; gateway authentication regression: `5 passed`; browser acceptance had
  fresh/non-synthetic Binance data, a 49-order D1/4H/1m preview, an enabled Start button,
  and zero runtime errors.

### Login UX follow-up

- Replaced the Cloudflare-account OAuth step with Cloudflare Access One-time
  PIN as the application's only identity provider. `Accept all identity
  providers` is off, `onetimepin` is the sole selected provider, and instant
  authentication is on.
- The operator now enters an email address and receives a login code directly;
  no Cloudflare dashboard account login is required. The email allow policy and
  seven-day application session remain unchanged.
- Visual proof of the simplified login page:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-v5-email-otp-login-2026-07-15.png`.

### OTP delivery troubleshooting

- The Access code-entry page accepted `zinan92@hotmail.com` and one explicit
  resend was issued. The application policy remains exact-email allow and the
  Access authentication log shows `Allowed` entries with no `Blocked` entries.
- Do not treat the text `A code has been emailed to you` as a delivery receipt.
  Cloudflare deliberately shows it even when no message is sent, and its Access
  authentication log is not an outbound-email delivery log. A mailbox receipt
  is therefore still missing evidence.
- Cloudflare documents `noreply@notify.cloudflare.com` as the OTP sender and
  lists mailbox filtering or sender suppression after previous delivery failures
  as the remaining causes once policy denial is excluded. A newly requested PIN
  invalidates the previous PIN and expires after 10 minutes.
- Visual traces:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-v5-otp-resend-2026-07-15.jpg` and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-v5-access-auth-log-2026-07-15.jpg`.

## 2026-07-15 — Production console cycle rollover and ledger reconciliation

### Decision

- Runtime state is cycle-scoped. A `running` row from a previous 12-hour cycle
  is now exposed as `stopped + stale_cycle` for the current cycle; it can no
  longer create the false state `运行但无挂单` after rollover.
- Account totals and trade history use only fills traceable to a
  `strategy_plan_id`. Exit fills paired to those entries are included in the
  same production trade. Legacy human/machine results remain available through
  the historical ledger but are not silently added to production equity.
- An empty current-cycle paper ledger retains the configured 10,000 USDC paper
  account baseline. The current console therefore shows the versioned
  production history: 6 trades, 1,193.16 USDC cumulative notional, -4.38 USDC
  realized PnL and 9,995.62 USDC equity.
- New protective exit fills inherit `strategy_plan_id` and
  `strategy_plan_version` from the matched entry, closing the traceability gap
  without rewriting old source records.
- The recommendation block now names its direction source and explains that
  parameters use the latest 14-period ATR. The duplicated broker/timeframe/bar
  count sentence was removed. The opaque short plan hash was replaced with a
  human-readable production strategy label.

### Grid algorithm shown to the operator

- Steady: half-range = `max(2 * ATR14, latest * 0.25%)`, 8 intervals,
  per-grid notional = `max(10, equity * 1%)`, default leverage 1x.
- Aggressive: half-range = `max(1 * ATR14, latest * 0.125%)`, 12 intervals,
  per-grid notional = `max(10, equity * 1.5%)`, default leverage 2x.
- Neutral centers the range on the latest price. Long shifts 75% of total width
  below the latest price and only places buy entries; short mirrors that geometry
  above the latest price and only places sell entries. All controls generate a
  preview first and do not submit orders until explicit Start.

### Gotchas

- The prior page joined current cycle `2026-07-15_DAY` to runtime state from
  `2026-07-14_DAY`, then read only the empty current human execution file. This
  was a data-contract mismatch, not lost orders or lost history.
- One preserved historical production trade has an exit timestamp one minute
  earlier than its entry timestamp in the raw ledger. The compatibility reader
  preserves that evidence rather than silently correcting it; a separate ledger
  repair/migration should address historical timestamp quality.
- `A code has been emailed` in Cloudflare Access remains non-evidence of mail
  delivery; Park subsequently confirmed receipt of the OTP.

### Verification and evidence

- Focused backend, control-plane and static dashboard regression: 67 passed.
- Browser acceptance confirmed stopped current-cycle state, restored account
  equity, six historical production trades, explicit recommendation provenance,
  and both steady/aggressive preview geometry without placing orders.
- Visual proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-ledger-reconciled-2026-07-15.jpg` and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/strategy-console-aggressive-preview-2026-07-15.jpg`.

## 2026-07-15 — External operator user instruction contract

### Decisions

- The external user guide treats a robot as started only when the runtime is
  `running`, at least one paper order is accepted, the current-orders table is
  populated, and the chart is showing production orders rather than a preview.
- Every non-intuitive control is classified as explanation-required, dangerous,
  or a current limitation. The guide explains the exact ATR14 range geometry,
  direction bias, grid count, notional sizing, TP/SL and runtime state machine.
- The current safe distribution model is one designated operator with other
  users observing. Cloudflare-authorized users still share one paper robot and
  one paper ledger; the console does not provide per-user accounts or roles.
- The guide documents actual control-plane behavior even where the visible copy
  is currently inaccurate: running adjustment cancels and resubmits all pending
  grid orders from the complete current form state, while existing positions
  remain open.

### Gotchas

- `刷新趋势` currently reloads console and market data; it does not rerun AI,
  create a proposal or relock the production direction.
- A raw plan confidence of `1` is rendered as `100%`. That value is not a
  calibrated probability, win rate or profit guarantee and should not be
  exposed to external operators as certainty.
- `价格冲出区间时` is stored in the plan, but its three advertised behaviors
  have not been proven end to end in the new production control plane.
- `重置盈亏 / 交易量统计` writes `statistics_baseline_at`, but the production
  history/account aggregate does not yet apply that baseline to displayed
  totals. The external guide therefore marks the control as not reliable yet.
- This documentation task changed no dashboard surface. Per the Evidence
  Contract, the Markdown guide and decision log are trace material rather than
  new visual Evidence.

## 2026-07-15 — Grid timeframe, capital sizing and AI-confidence audit

### Decisions

- The chart timeframe is presentation only. The target strategy contract uses
  complete D1 bars for the persistent grid Range, complete 4H bars for spacing,
  and 1m only for execution and breach confirmation. A 12-hour cycle produces a
  review/proposal; it does not silently replace a continuously running grid.
- `刷新趋势` is defined as a fresh market evaluation that creates a new
  direction, style and grid-specification proposal. It must never directly
  mutate orders or the production plan.
- Capital sizing must start from `equity * leverage_limit`, then apply a margin
  utilization cap and a worst-case plan-loss cap. The leverage limit is an
  absolute ceiling, not a target utilization.
- The first Strategy Shadow matrix will test D1 ATR14 Range multipliers, 4H ATR
  spacing, 24-80 grids, 3x/5x/10x leverage, margin utilization and plan-loss
  caps. No single untested parameter set is promoted to production.
- The external operator guide is paused until the strategy specification is
  implemented and browser-verified.

### Current findings

- The machine planner runs Codex `gpt-5.4` with one program-built user prompt.
  There is no repository-owned custom system-prompt artifact, and the Codex
  internal system prompt is not persisted in the planning trace.
- The prompt currently receives cycle-local 1m OHLC/recent closes, a median of
  ten completed non-weekend 12h ranges, the previous machine review, a replan
  context and up to 16k characters of the finance newsletter. It does not
  receive D1/4H ATR, full multi-timeframe bars or deterministic trend features.
- Normal plan confidence is an uncalibrated 1-10 integer self-reported by the
  LLM. A decision-error plan hard-codes `confidence=1`.
- The current active production plan was locked from a 09:01 fail-closed plan.
  The later successful 09:02 neutral plan (`5/10`) and 10:20 short replan
  (`7/10`) did not replace it. The frontend then converted the error value `1`
  to `100%` by treating it as a 0-1 probability.
- The legacy DualTrack sizing contract uses a 100,000 U total notional ceiling
  for 10,000 U equity at 10x. The new production console independently added
  `equity * 1%` steady sizing, yielding 100 U per grid. That is an incomplete
  migration, not an approved product decision.
- A read-only audit of complete stored bars produced D1 ATR14 approximately
  100.50 and 4H ATR14 approximately 31.57. The current DualTrack market feed
  cannot yet aggregate `1d`; the target contract requires a trusted D1 feed and
  complete-bar filter before use.

### Gotchas

- Four 25,000 U grids do equal the full 100,000 U notional ceiling, but at 10x
  they also consume all 10,000 U initial margin when all four fill. A production
  default needs explicit free-margin and loss buffers.
- More grid levels do not mean every level can be sized by dividing the total
  budget by the displayed count. Sizing must use maximum simultaneously filled
  levels and the aggregate distance-to-stop loss.
- D1 ATR14 with the old `2 ATR` half-range may be materially wider than the
  current 12h planning range. It is a shadow candidate, not an automatic fix.
- Markdown audit artifacts are trace material, not new visual Evidence. No UI
  or runtime mutation was performed in this audit.

## 2026-07-15 — Fixed-timeframe grid and auditable AI proposal implemented

### Decisions

- Superseded the rejected chart-timeframe ATR behavior. Production previews now
  use complete D1 ATR14 for Range, complete 4H ATR14 for spacing and 1m for
  execution. The visible chart timeframe is presentation-only.
- Steady uses a `2 × D1 ATR14` half-range, `0.25 × 4H ATR14` target spacing,
  50% margin-utilization cap and 5% max-plan-loss cap. Aggressive uses `1 ×`,
  `0.125 ×`, 70% and 8%. Automatic grid count is clamped to 24–80.
- Direction no longer moves the market Range. Neutral arms both sides, long
  arms buy entries and short arms sell entries against identical geometry.
- Per-grid notional is the lower of the capital cap and distance-to-stop loss
  cap. `equity × leverage_limit` is an absolute ceiling, not target exposure;
  neutral risk uses maximum same-side exposure rather than summing mutually
  exclusive buy and sell books.
- `刷新趋势` now runs an auditable AI evaluation over fixed D1/4H/1H contexts.
  AI can propose direction and style only; deterministic code owns Range,
  spacing, grid count, leverage and notional. The prompt and evidence inputs are
  stored with the proposal.
- Removed the false probability display. The UI separates a deterministic
  rule score labelled `uncalibrated` from the model's 1–10 reasoning-material
  self-assessment labelled `not a probability`.
- AI refresh and adopting its recommendation create/preview a proposal only.
  They do not mutate the active production plan, cancel orders or place orders.

### Live verification

- Trusted strategy inputs were available: 22 complete non-weekend D1 bars and
  47 complete 4H bars. The inspected sample had D1 ATR14 about 100.50 and 4H
  ATR14 about 33.67.
- A real AI refresh recommended `short + steady`, produced rule score 74.4/100
  (uncalibrated) and AI self-assessment 8/10 (not probability), and returned
  `production_plan_unchanged=true`.
- With current paper equity 9,995.62 U, a steady preview produced about 47
  grids, about 800 U per grid, about 49,978 U capital budget and about 1.92x
  actual leverage. The 10x setting remained a ceiling because the 5% loss cap
  bound sizing first.
- Switching the chart from 1m to 30m preserved the exact Range, grid count and
  per-grid notional. Steady to aggressive narrowed Range and increased density;
  short to neutral preserved geometry while changing armed order sides.
- Full regression: `1438 passed in 367.40s`. Browser console had no errors;
  390px mobile viewport had no horizontal overflow.

### Gotchas

- A high leverage limit does not imply high actual leverage. The loss budget is
  intentionally allowed to bind before the capital ceiling.
- The rule score is not historical success probability. Its reserved historical
  calibration component remains zero until enough reproducible shadow outcomes
  exist.
- The active production plan is never silently replaced by a fresher AI plan.
  An operator must explicitly start or apply a running adjustment.
- The launchd environment could resolve the Codex wrapper but not Homebrew Node.
  The planner now uses an absolute Codex path and an explicit PATH; failures are
  returned as structured fail-closed API errors instead of dropped connections.
- Full-page mobile screenshot stitching was unreliable. Evidence therefore uses
  separate mobile viewport captures for the top and strategy-control sections.
- Current Strategy Shadow output is a smoke comparison. Its cost model remains
  zero and must not be treated as sufficient promotion evidence.

### Evidence

- Desktop AI/grid console:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-strategy-console-ai-grid-desktop.png`.
- Mobile top and strategy controls:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-strategy-console-ai-grid-mobile.png` and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-strategy-console-mobile-strategy.png`.
- Strategy Shadow comparison:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-strategy-shadow-comparison.png`.

## 2026-07-15 — AI trend evaluation Input/Output receipt

### Decisions

- Every `刷新趋势` run now creates a standalone
  `strategy-ai-evaluation-v1` receipt. Success and provider/validation failure
  attempts are both archived; a failed evaluation never disappears into an API
  error alone.
- AI input now includes complete, trusted D1/4H/1H/15m contexts. Each context
  records provider, bar window, latest completed OHLC, ATR14, EMA20, EMA50,
  standard MACD(12/26/9), trend and directional efficiency.
- 15m is confirmation evidence only. It cannot change the fixed D1 Range or 4H
  spacing contract, and the operator's visible chart timeframe remains excluded.
- The receipt stores the exact business prompt, raw model response, parsed
  decision, deterministic rule-score components, final recommendation and
  side-effect declaration. The server finalizes it with proposal/preview IDs,
  `production_plan_unchanged` and `orders_created`.
- Each AI refresh receives a unique evaluation-backed proposal ID, so repeated
  identical decisions do not replace the previous visible evaluation history.
- The current-trend card exposes a compact receipt entry. A responsive audit
  dialog shows Input and Output side by side, offers current-cycle evaluation
  history, and displays the local archive path plus SHA256.

### Local archive contract

- Path:
  `outputs/dualtrack/strategy_control/evaluations/<cycle_id>/<timestamp>_<evaluation_id>.json`.
- The archive is one atomic JSON record per file. `source_manifests` remains
  available even when context validation fails before a model call.
- The proposal also retains its `evaluation_receipt`, allowing the dashboard to
  render the exact evidence without reconstructing it from current market data.

### Gotchas

- “AI 检查 K 线图” means it reads completed OHLC numbers and indicator
  snapshots; it does not perform pixel-based visual chart interpretation.
- D1 may have too few completed bars for fully warmed EMA50 or MACD. Missing
  indicators are explicitly stored/displayed as insufficient sample, never
  silently synthesized.
- The complete prompt and response are intentionally local audit evidence. They
  may include current account and plan context and therefore should stay behind
  the existing authenticated dashboard boundary.
- A receipt proves what the model saw and returned. It does not prove that the
  recommendation is profitable or historically calibrated.

### Verification and Evidence

- A real Binance-backed refresh produced `short + steady`, rule score 73.3/100
  (uncalibrated), and AI self-assessment 8/10 (not probability). The receipt
  includes 22 D1, 63 4H, 95 1H and 159 15m completed bars; 15m EMA20 was
  4,037.999925 and MACD histogram was 0.339211 for that snapshot.
- The finalized receipt is 64,010 bytes and records
  `production_plan_unchanged=true` plus `orders_created=0` at:
  `outputs/dualtrack/strategy_control/evaluations/2026-07-15_DAY/2026-07-15T06-09-08.303402_00-00_ai-eval-0dc5545b91964186.json`.
- Full regression: `1440 passed in 384.18s`. Desktop and 390px mobile browser
  acceptance confirmed no console errors and no horizontal overflow.
- Visual Evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-ai-evaluation-receipt-desktop.png`,
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-ai-evaluation-receipt-mobile.png`, and
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-ai-evaluation-receipt-public-v5.png`.
- The public Cloudflare Tunnel briefly returned 1033 during acceptance. Its
  existing LaunchAgent was restarted, after which the authenticated public v5
  page loaded the new receipt UI successfully.

## 2026-07-15 — Runtime stop diagnosis and readable execution records

### Findings and decisions

- The robot did not stop because of stale market data, a cycle rollover, or an
  automatic fill rule. The authoritative runtime records `last_action=stop` at
  `2026-07-15T06:41:59.602095+00:00` (Beijing `2026-07-15 14:41`). The same
  control operation cancelled the remaining accepted production grid orders.
- The dashboard request log confirms a successful
  `POST /api/strategy-console/control` at that time. This is an explicit stop
  control path whose contract is “stop + cancel pending orders + flatten open
  positions + reconcile”.
- Stopped state is no longer shown without context. The header now says
  `已停止 · 收到停止指令`; the runtime card exposes the last action and its
  Beijing action time from the authoritative runtime record.
- Execution tables now render timestamps as `YYYY-MM-DD HH:mm` in
  `Asia/Shanghai`, with the column header explicitly marked `时间（北京）`.
- Asset quantity is rendered with at most six decimal places and trailing zeros
  removed. Ledger values remain unchanged; this is display formatting only.

### Gotchas

- The existing HTTP request log records endpoint, result and server time, but
  not the control request body or authenticated actor identity. Therefore the
  stopped action and time are proven, but attributing this historical action to
  a specific person or browser session would be speculation.
- `runtime.json` stores the latest authoritative runtime row rather than an
  append-only operator audit trail. The visible last action is sufficient for
  the current state explanation, but actor-level audit requires a separate
  authenticated control-event contract in a future change.

### Evidence

- Public v5 runtime/action and readable fills:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-v5-stop-action-and-readable-fills-detail.png`.
- Public v5 full-page capture:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-v5-stop-reason-beijing-time-readable-quantity.png`.
- Focused regression: `61 passed`; full regression: `1442 passed in
  400.43s`.
## 2026-07-15 - Independent datafeed cutover (in progress)

Objective: make the independent datafeed the trading framework's only market-data entry,
while keeping broker execution adapters separate.

### Decisions

- Added `DatafeedMarketClient` as the HTTP consumer of `kline-candles-v1`; it fails closed
  on unavailable, non-JSON, or non-200 datafeed responses.
- Enabled the independent datafeed at `127.0.0.1:8100` in pipeline config and declared
  per-instrument routes. GOLD uses `binance_usdm_futures`; MGCmain uses
  `tiger_openapi_comex`.
- Switched the dual-track chart market path to datafeed with `cache_policy=bypass`, strict
  quality, execution-venue required, and no fallback. The response exposes canonical GOLD,
  provider symbol XAUUSDT, provenance, freshness, and `reads_private_market_db=false`.
- Added `DatafeedMarketRepository` for strategy/replay read contracts. Normal
  `DualTrackCycleRunner` construction now uses datafeed; an explicitly supplied test DB is
  retained temporarily as a compatibility seam while remaining consumers migrate.

### Gotchas

- Using `cache_policy=allow` on the live dashboard initially returned an older cached bar.
  The chart path now bypasses cache and uses strict quality so it cannot quietly show stale
  history as the current market.
- The old market database mixed timezone-naive and timezone-aware timestamps. That produced
  duplicate logical minutes after migration; datafeed now canonicalizes and deduplicates UTC.
- The trading repository still contains ancillary/reporting modules that directly read
  `market_data.db`. The cutover is not complete until these are migrated or removed and a
  guard test proves production consumers cannot regress.
- Tiger credentials are not present in the current environment. Tiger adapter behavior is
  contract-tested and its 1,709 existing proven-source bars are migrated, but a fresh live
  Tiger request cannot yet be claimed.

### Verification so far

- datafeed: 65 tests passed; ruff passed.
- trading datafeed/chart/cycle targeted suites: 79 chart/client tests and 73
  repository/cycle tests passed.
- Live dashboard market API returned `ready`, `fresh=true`,
  `source_mode=binance_usdm_futures`, `is_synthetic=false`, and
  `reads_private_market_db=false`.
- Visual evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-datafeed-source-health-full.png`.

## 2026-07-15 — Public start-control 502 recovery

### Findings and decisions

- The failed public start did not create orders: the authoritative runtime
  remained `stopped`, with zero accepted orders and zero open positions.
- The public gateway used a fixed 20-second upstream timeout. A production
  control request validates trusted 1m plus fixed D1/4H/1H/15m market contexts
  before writing the paper ledger, so a transiently slow datafeed could outlive
  that proxy deadline and surface as HTTP 502.
- Authenticated control POSTs now receive a 90-second upstream deadline. Normal
  read requests retain the 20-second deadline.
- The gateway now forwards the upstream's bounded JSON error body for rejected
  controls. The dashboard can therefore show the real rejection reason instead
  of only `HTTP 400` or an empty response.
- A 5xx or broken control response is now treated as an unknown outcome, not an
  automatic failure. The browser polls the authoritative current-state endpoint
  and reports success only when running orders (or a fully stopped/flat state)
  are confirmed. It explicitly tells the operator not to repeat-click while
  reconciliation is in progress.
- The dashboard reuses the 1m market payload already returned by the current
  read model and pauses its five-second refresh while a control action is busy
  or the tab is hidden. This removes duplicate Binance reads and reduces
  self-inflicted contention from multiple open tabs.

### Gotchas

- A proxy timeout does not prove whether a mutation committed. Repeating a
  start/stop action before reading authoritative state can create ambiguous
  operator feedback even when the backend action is idempotent.
- The verification deliberately used `preview`, not `start`; it proved the
  deployed market-validation path, public JSON proxying, button availability
  and stopped/flat state without creating paper orders.
- The full-page in-app-browser capture repeats the sticky header at browser
  capture tile boundaries. The focused viewport evidence is the clearer visual
  proof for the actionable start/disabled stop controls.

### Verification and evidence

- Public gateway preview: HTTP 200 in 6.37 seconds, 49 grid orders, no ledger
  mutation. Invalid cycle requests returned the upstream JSON message through
  the public gateway.
- Public browser state: authenticated/control-enabled, market `实时·可信`, source
  `binance_usdm_futures`, start enabled, stop disabled, runtime stopped, no
  browser warnings/errors.
- Focused regression: 67 trading tests and 7 gateway tests passed; dashboard
  inline JavaScript parsed successfully.
- Visual evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/goldbot-v5-start-control-recovered-actions-2026-07-15.png`.

## 2026-07-15 - Datafeed becomes the production market-data boundary

Objective: strategies, charts, reports, replay, health, and schedulers consume one
auditable datafeed port; broker execution remains a separate port.

### Decisions

- Production market reads now construct `DatafeedMarketRepository`. The repository
  is used by dual-track cycles, charts, replay, market view, data trust/lineage,
  portfolio reports, health, system state, completion audits, and backtests.
- Scheduled Binance, Tiger, FRED, and OANDA market-data refreshes call datafeed HTTP.
  Trading scheduler modules no longer import exchange-specific candle collectors.
- The OANDA and Tiger connector catalog price-feed ports now identify datafeed
  adapters; execution adapters remain broker-owned and independent.
- Production CSV imports are rejected with an instruction to install a datafeed
  adapter. The old SQLite store is accepted only for system temporary paths used by
  isolated tests and migration rehearsals; non-temporary private paths fail closed.
- Added a CI architecture guard that freezes all remaining compatibility seams and
  rejects new scheduler imports, direct market-data URLs, SQLite reads, or exchange
  collectors outside those seams.

### Gotchas

- `TRADING_ORCHESTRATOR_MARKET_DB` is no longer a production routing mechanism.
  Setting it to a non-temporary alternate database does not bypass datafeed.
- Source availability is not credential readiness. Tiger/OANDA adapters must be
  enabled in datafeed config before their scheduled jobs become healthy.
- Datafeed downtime is a hard market-data failure. Trading does not silently fall
  back to the old database, synthetic candles, Yahoo, or another unnamed source.
- Daily snapshots no longer copy datafeed's private SQLite file. Trading archives its
  own artifacts and consumes the owner-side datafeed storage-integrity receipt.
- Existing legacy collector classes remain only to keep deterministic temporary-DB
  tests and one-time migrations reproducible. Their default production entrypoints
  delegate to datafeed.

### Verification and evidence

- Architecture boundary guard: 7 focused tests passed.
- Live scheduled Binance refresh returned 1,500 GOLD 5m candles with
  `market_data_backend=datafeed`, `provider_symbol=XAUUSDT`, fresh, non-synthetic.
- Fail-closed drill against an unavailable datafeed returned connection refusal with
  `fallback_used=false`.
- First full trading regression found five compatibility-output regressions; all five
  were fixed before the final rerun.
- Final full trading regression: `1455 passed` in 6m38s.
- Visual evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-datafeed-source-health-final.png`.
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-trading-datafeed-cutover-final.png`.

## 2026-07-15 - Datafeed outage and dashboard recovery

### Decision

- Keep trading fail-closed. The dashboard block was caused by no process listening
  on datafeed port `8100`, not by a Binance payload or downstream contract change.
- Fix service ownership in `/Users/wendy/datafeed` with a persistent LaunchAgent;
  no trading compatibility change or private market-data fallback was added.

### Gotchas

- A repo update and a running datafeed are separate facts. Connection refusal must
  remain visible as `行情 blocked` until the owner service and a fresh live candle
  are both verified.
- Datafeed recovery must not auto-resume a deliberately stopped robot. The dashboard
  is start-eligible again, but the runtime stays stopped until the user starts it.

### Verification

- Local and public v5 dashboards show `行情：实时·可信`, Binance USD-M provenance,
  no blocked-data state, and an enabled start control.
- Strategy-console current API reports fresh, non-synthetic GOLD/XAUUSDT 1m data.
- Focused downstream regression: `68 passed`.
- Visual proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-datafeed-restored-dashboard.png`.

## 2026-07-15 - Revalidate automatic grid notional at start

### Decision

- Distinguish automatic per-grid sizing from a manually fixed notional. Automatic
  sizing is now recalculated inside the start transaction from the latest trusted
  market/account snapshot; manual values remain subject to the unchanged hard cap.
- Persist `grid.notional_mode` in the preview and production plan so sizing intent is
  auditable. The dashboard labels automatic values as `自动风险上限`.

### Gotchas

- The safe per-grid notional changes when price crosses grid levels because the
  number of armed buy/sell levels and their cumulative loss to the shared stop
  change. A valid preview amount is therefore not a durable fixed quote.
- Treating an auto-generated amount as manual created a time-of-check/time-of-use
  failure: the start gate correctly rejected the stale number even though the user
  had selected automatic sizing.
- This fix does not relax risk limits and does not auto-start the robot. It only
  recomputes the automatic amount at the atomic start check.

### Verification

- Live preview with stale `774.23 USD` in auto mode returned `200` and resized to the
  current safe cap; the same stale value in manual mode remained blocked.
- Focused backend/dashboard regression: `82 passed`.
- Local and public v5 browser checks showed real-time trusted data, an enabled start
  button, and a preview explicitly labeled `自动风险上限`; no start was clicked.
- Visual proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-grid-auto-notional-revalidation.png`.

## 2026-07-15 - Datafeed provider contract and missed-fill recovery

### Decision

- Keep provider validation exact and fail-closed. Align the production DualTrack
  market-data provider with datafeed's canonical GOLD route,
  `binance_usdm_futures`; do not add a silent alias or fallback.
- Keep broker/execution identity `binance_usdm` separate. A market-data source
  name and an order-routing broker name are different contracts and must not be
  renamed together.
- Recover the missed current-cycle paper events only after pausing the one-minute
  scheduler, preserving pre-replay artifacts, and proving replay idempotency.

### Recovery result

- The accepted 4060.1089 buy limit was filled at the close of the 21:25 Beijing
  minute bar and recorded at 21:26.
- The 4067.9911 target was reached during the 21:58 minute bar and recorded at
  21:59. The trade is closed, so current position is correctly zero.
- Net trade PnL after both-side costs is `+1.32861937 USD`. The current cycle has
  2 fills, 1 closed trade, 50 remaining accepted orders, and clean execution
  reconciliation.
- A second replay processed the same history without changing the orders, fills,
  trades, or account hashes. The scheduler was restored and its next tick exited
  0 without `market_provider_mismatch`.

### Gotchas

- Zero current position does not mean an order failed. The UI must be checked
  against fills, closed trades, realized PnL, and pending-order state together.
- One-minute candle timestamps are interval starts; chronology-safe paper fills
  are recorded at the interval end, hence 21:25 touch becomes 21:26 execution
  time in the ledger.
- `runtime.accepted_order_count` is the original start receipt (51), while the
  live execution snapshot now has 50 pending orders. The dashboard correctly
  renders the latter as the actionable count.
- A pre-existing 2026-07-15 DAY human trade has an exit timestamp earlier than
  its entry timestamp. This is unrelated to the provider fix but remains a
  separate ledger-chronology debt to repair and backfill explicitly.
- The account panel is cumulative across versioned production-plan history. The
  recovered current-cycle trade is +1.33 USD, while the displayed cumulative
  realized PnL also includes earlier production trades.

### Verification and evidence

- Test-first contract reproduced the drift (`binance_usdm` versus
  `binance_usdm_futures`) before the fix.
- Focused provider, execution-adapter, cycle-runner, and runtime-dashboard
  regression: 74 passed.
- Full repository regression after fixture migration: 1475 passed in 547.71s.
- Pre-replay snapshot:
  `/Users/wendy/trading-orchestrator/outputs/dualtrack/recovery/2026-07-15-provider-contract-replay/pre/`.
- Visual proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-grid-fill-recovery-dashboard.png`.

## 2026-07-15 - Chronology-safe protective exits and current-cycle ledger repair

### Decision

- A protective market event whose end timestamp is not later than a trade's
  entry timestamp is ineligible for that trade. Skip it before price
  evaluation, even when the old candle close itself is beyond TP/SL.
- Enforce the same invariant at the ledger write boundary: every exit must be
  strictly later than its matched entry. This prevents any caller from
  bypassing the event-loop guard.
- Allocate new fill IDs after the highest persisted suffix, not from row count,
  because a controlled repair can leave intentional sequence gaps.
- Aggregate accepted-limit fills across the complete replay window so the
  scheduler receipt reports every real fill, not only the final market event.

### Recovery result

- Removed two impossible current-cycle targets whose timestamps preceded their
  entries: `2026-07-15_NIGHT_human_0004` and `_0006`.
- Replayed the same trusted Binance USD-M Futures 1m history. The 4044.3444
  long correctly reached 4052.2266 during the 23:41 Beijing bar and closed at
  23:42 for net `+1.33407949 USD`.
- The 4052.2266 long has not reached 4060.1088 and remains open. A later real
  touch opened the 4036.4622 grid long at 00:20 Beijing on 2026-07-16.
- Current authoritative state at visual capture: 47 pending orders, 2 open
  positions, 6 current-cycle fills, unique fill IDs, and local execution
  reconciliation `ok` with no chronology errors.
- The one-minute LaunchAgent was restored; its first post-repair run exited 0.

### Gotchas

- Filtering only the pre-entry OHLC range is insufficient. Falling back to the
  old candle close can still trigger a new trade, so the entire event must be
  rejected when its end timestamp is at or before entry.
- `len(rows) + 1` is not a safe durable identifier allocator after a repair;
  it can collide with a higher existing suffix.
- Per-event replay receipts must be aggregated. The final event often reports
  zero fills even though an earlier event in the same sweep legitimately
  filled an order.
- A historical 2026-07-15 DAY trade still has a pre-entry exit. It is outside
  this current-cycle repair and remains explicitly blocked from silent rewrite
  until its own historical replay evidence is approved.
- The local datafeed health endpoint exceeded its 10-second client timeout
  during full regression. Test-only market DBs are now propagated through the
  report/doctor dependency boundary instead of accidentally calling the live
  service; production remains fail-closed.

### Verification and evidence

- Focused chronology, ID allocation, replay summary, execution, provider, and
  dashboard regression: 102 passed.
- The isolated daily-review/report/doctor boundary regression: 3 passed.
- Corrupt-state pre-repair snapshot:
  `/Users/wendy/trading-orchestrator/outputs/dualtrack/recovery/2026-07-15-chronology-repair/pre/`.
- Visual proof:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-15-grid-chronology-recovery-dashboard.png`.

## 2026-07-16 - M0 freeze the Nautilus paper-engine cutover boundary

### Decision

- Keep `legacy_paper` as the sole authoritative paper engine while declaring
  `nautilus_paper` as the migration shadow. No configured engine changed.
- Route the scheduler, production strategy control plane, dashboard order
  endpoint, and execution snapshot endpoint through one configuration-aware
  adapter factory.
- Persistent config may express the desired engine but cannot self-authorize a
  Nautilus cutover. Attended approval and the isolated Nautilus runtime path
  must come from the service environment, and the existing shadow gate remains
  a separate mandatory check.
- Freeze the migration milestones and exit criteria in
  `docs/dualtrack-nautilus-migration-plan.md` before implementing continuous
  shadow delivery.

### Gotchas

- The existing Nautilus adapter and fixed parity pass did not mean production
  was wired to Nautilus; every production caller still relied on the factory's
  default `legacy_paper` selection.
- A config boolean such as `allow_paper_switch` is not attended approval. It is
  writable ahead of time and therefore cannot satisfy the cutover trust gate.
- The isolated runtime previously used under `/tmp` is not a durable production
  dependency. M1 must provision a stable isolated path before continuous
  shadow operation.
- The live cutover gate is correctly blocked at `0/7` because the only recorded
  parity cycle did not contain qualifying command activity.

### Verification

- Pre-change execution boundary baseline: `30 passed`.
- New selection contract first failed because the configured builder did not
  exist; after implementation, the production callers use the configured
  factory and keep `legacy_paper` authoritative.
- Nautilus instrument preflight: `ready_for_paper_shadow`.
- Fixed parity fixture gate: `pass`; paper cutover gate:
  `candidate_activity_insufficient`, `0/7`.

## 2026-07-16 - M1 continuous Nautilus paper shadow runtime

### Decision

- Install pinned NautilusTrader 1.230.0 in the stable isolated runtime
  `/Users/wendy/.local/share/trading-orchestrator/nautilus-1.230.0`; stop using
  `/tmp` as an execution dependency.
- Mirror the exact accepted production command and canonical market event into
  a separate Nautilus journal. Shadow failures are append-only evidence and do
  not alter or stop the authoritative legacy paper result.
- Defer Nautilus replay until the end of each live-tick event batch. Queue each
  immutable event once, replay the complete prefix once, then acknowledge all
  commands/events included in that successful replay.
- Idempotently backfill the current cycle's 51 pre-shadow production commands
  from the existing immutable `shadow_commands` journal. Do not ask the user to
  restart the robot and do not copy candidate fills into the legacy ledger.

### Gotchas

- The first continuous attempt called a full Nautilus subprocess replay for
  every bar in the legacy replay window. It exceeded 90 seconds and was
  manually terminated. Increasing the timeout would have hidden an O(n^2)
  integration error.
- A later successful event replay includes earlier unacknowledged events. The
  processed-event journal must acknowledge the entire replayed prefix, not only
  the event that triggered the retry.
- Importing commands after all market events were already acknowledged still
  requires a replay. A separate processed-command journal is therefore needed;
  event freshness alone is not a sufficient dirty-state signal.
- Fixed fixture parity is not live parity. The first command-bearing live
  comparison correctly reported drift rather than advancing the cutover gate.

### Verification

- Continuous-shadow unit and integration regression: `94 passed`.
- Real live tick after batching: `4.41 seconds`, exit 0, one flush, six newly
  processed events.
- Current shadow journal: 246 unique events / 246 processed; 51 unique commands
  / 51 processed.
- Current authoritative engine: `legacy_paper`; reconciliation `ok`, six fills,
  four positions, two open positions.
- Current fixed parity fixture gate remains `pass`; command-bearing live parity
  reports 49 explicit differences and cutover remains blocked at `0/7`.

## 2026-07-16 - M2-M5 Nautilus lifecycle, accounting, and attended-cutover rehearsal

### Decision

- Implement cancellations as durable commands, not mutable order-row edits. All
  commands sharing one timestamp are delivered through one Nautilus clock alert
  in journal order; this is required for complete 40-50 order batch cancellation.
- Preserve Nautilus HEDGING position identity on every close. Partial reduction
  targets the exact position ID and resizes the remaining TP/SL protection;
  regrid is two phase: cancel the old accepted set, verify it is gone, then arm
  the replacement set.
- Normalize the engine snapshot as the one read model for orders, fills,
  positions, account, realized/unrealized PnL, margin, exposure, fees and
  slippage. Production reconciliation now comes from the selected engine;
  candidate reconciliation is exposed separately as
  `execution_shadow_reconciliation`.
- Permit comparison rounding only when the candidate explicitly declares the
  venue price/quantity/money precision. Exact comparison remains exact; side,
  state, event and other semantic drift can never be rounded away.
- Use the authenticated demo account's observed paper fee contract (maker 0,
  taker 0.0004). Keep the older flat-fee historical drift unchanged. Only new
  command-bearing 12-hour cycles under the unified contract can count toward
  7/7; no historical evidence is rewritten.
- Isolate candidate and production persistence. Continuous shadow stays under
  `dualtrack/nautilus_paper`; an attended cutover writes only under
  `dualtrack/nautilus_authoritative`. Pre-cutover shadow orders therefore cannot
  become production orders through directory reuse.
- When Nautilus is authoritative, production history combines the preserved
  versioned Legacy archive with only `nautilus_authoritative` snapshots. Shadow
  fills are excluded. Market orders and manual closes are advanced with the
  server-validated market event before the API returns a fill.
- Keep `legacy_paper` authoritative. The real Nautilus cutover remains blocked
  until the live gate reaches seven consecutive qualifying cycles and the
  operator supplies attended service approval.
- Count only completed 12-hour cycles. A passing intracycle comparison is
  debugging evidence but never advances 7/7. The close path now re-runs the
  shadow comparison idempotently and requires a persisted close artifact,
  command activity, trusted configured-provider events, the current paper fee
  contract, and a pinned replay version.
- Add a read-only attended-cutover precheck which combines the 7/7 evidence gate
  with the operational flat-state boundary. It refuses Go while Legacy is
  running, accepted orders or positions remain, either ledger does not
  reconcile, service-environment approval is absent, or the isolated runtime
  and preflight are unavailable. It cannot edit config or submit an order.

### Gotchas

- Scheduling one clock alert per cancellation at the same nanosecond caused a
  real Nautilus batch to cancel only a prefix. Timestamp batching is execution
  semantics, not a performance-only optimization.
- A factory switch alone is unsafe if candidate state and authoritative state
  share disk paths. This was not visible in empty-directory unit tests; the
  cutover rehearsal now pre-seeds a historical shadow command to prove it stays
  excluded.
- Overwriting `production_execution.reconciliation` with the latest shadow
  comparison made a healthy Legacy ledger appear drifted. Active-ledger health
  and migration evidence must stay visibly separate.
- Existing shadow PnL differs from Legacy by the historical fee model. This is
  expected evidence and resets the streak; precision normalization must not be
  used to hide money-model differences.
- `re_arm_max` is a Strategy Lab parameter, not authorization for an automatic
  production re-arm loop. Production replacement behavior remains driven by
  explicit grid events and control actions.
- Browser full-page capture failed in the Codex in-app tab through both the
  high-level screenshot API and raw CDP. The same live local dashboard was then
  captured with the browser fallback; public DOM/error checks still used the
  authenticated public tab and reported zero browser errors.
- The first gate implementation could count an open cycle as soon as its current
  prefix matched. That did not prove 12 hours of behavior. `cycle_complete` is
  now cross-checked against the cycle attribution artifact before qualification.
- Excluding open cycles only at increment time was insufficient: after a 7/7
  completed streak, the next open cycle's provisional report would reset the
  gate to zero until its close. The gate now counts and resets only from strict
  v2 completed-cycle evidence. The newest open report remains visible through
  separate observation fields but cannot erase earned qualification. An open
  cycle with live drift can still block the attended switch without resetting
  the completed streak; an open passing prefix leaves a 7/7 gate ready.
- The updated datafeed reports the canonical provider name in `source_mode`
  instead of the older `requested_symbol` marker. The production order path
  already accepted both forms, but the legacy runtime-status endpoint did not,
  causing a false `blocked` message while the same payload was fresh and ready.
  Runtime diagnostics now use the same fail-closed contract: ready, fresh,
  explicit provider, non-synthetic, canonical source mode, and exact configured
  provider match.
- `test_official_feed_receipt_refreshes_from_current_local_state` was not truly
  local: when its temporary output root had no OANDA receipt it queried the live
  datafeed health endpoint, making the full suite depend on network timing. The
  fixture now persists the explicit local skipped-OANDA state it intends to
  test; production fail-closed behavior was not relaxed.

### Verification and evidence

- Fixed real Nautilus parity gate: all 10 lifecycle classes pass.
- Added the M5 transactional apply/rollback controller. It requires the read-only
  7/7 precheck and an exact acknowledgement, stops both adapter-owning services
  before any write, backs up the config and two installed LaunchAgent plists,
  persists service-environment approval/runtime, restarts and validates the
  selected engine, and automatically restores Legacy on failed validation.
  Explicit rollback separately requires stopped/flat/reconciled Nautilus and
  restores the apply backups byte-for-byte. Neither path starts a strategy or
  submits an order. The live controller has not been applied because M4 is 0/7.
- Exercised the real apply command against the current live state with the exact
  acknowledgement and isolated runtime path. It returned exit 2 with only the
  expected blockers (`shadow_gate_not_ready`, running Legacy runtime, 45 open
  Legacy orders), reported `config_write_performed=false`, and left the config
  plus both installed LaunchAgent files byte-identical. Legacy remained running.
- Automatic rollback is itself an audited state machine. A restore, restart, or
  Legacy revalidation failure now returns and persists
  `automatic_rollback_failed` instead of escaping without a receipt. Service
  quiescence is idempotent: a non-zero `bootout` is acceptable only when an
  immediate `launchctl print` proves the job is no longer loaded.
- Real isolated cutover rehearsal: start, regrid, cancel-all, manual market
  open/close, stop and final reconciliation pass; the historical shadow command
  remains untouched in the shadow directory.
- Isolated rollback selection restores `legacy_paper` after the Nautilus stop;
  the Legacy history bytes and Nautilus authoritative snapshot remain present
  and unchanged.
- The live attended precheck currently reports No-Go for exactly the expected
  reasons: 0/7, production runtime running, 45 accepted Legacy orders, and no
  cutover-only service approval/runtime environment. Legacy reconciliation is
  `ok`, so this is a controlled migration block rather than a production fault.
- Focused completed-cycle gate/dashboard/cutover regression: `71 passed`.
- Focused transactional M5 controller regression: `7 passed`.
- Final full repository regression after the transactional apply/rollback and
  failure-visible automatic rollback: `1531 passed` in 411.33 seconds.
- Current production check after tests: `legacy_paper`, runtime `running`,
  authoritative reconciliation `ok`; live-tick LaunchAgent running with last
  exit code 0. Runtime market diagnostics now report `ok / 行情新鲜`. Shadow
  gate remains blocked at `0/7`.
- Desktop evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-nautilus-migration-gate-desktop.png`.
- 390px mobile evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-nautilus-migration-gate-mobile.png`.

## 2026-07-16 - Nautilus paper authority cutover and contract-level drift audit

### Decision

- Complete the paper-only authority migration now at a stopped/flat cycle
  boundary. Keep the normal seven completed-cycle gate at `7/7`; do not lower or
  rewrite it. The operator-authorized acceleration is a separately acknowledged,
  receipt-backed override which is valid only when the deterministic Nautilus
  fixture gate passes, both ledgers are flat and reconciled, the isolated runtime
  is ready, and `real_money_eligible=false`.
- Normalize every new execution command before either engine accepts it. XAUUSDT
  executable prices now use `0.01`, quantities use `0.001`, and the command keeps
  its raw requested values plus immutable execution-contract and fee-contract
  hashes. Legacy and Nautilus therefore receive the same executable numbers.
- Make reconciliation economic and traceable, not count-only. It now checks fees,
  equity, fill cost/slippage/time, order time, position economics and
  StrategyPlan ID/version. Money remains eight-decimal exact; venue rounding is
  allowed only for price and quantity when explicitly declared by the candidate.
- Canonicalize spelling-only lifecycle aliases (`canceled` and `cancelled`) while
  continuing to treat semantically different states as drift.
- Preserve every historical Legacy fill/order/trade and every previous Nautilus
  shadow artifact byte-for-byte. Old cycles are classified as non-qualifying
  evidence rather than rewritten to look compatible.
- Treat the current migrated UI state explicitly. Once Nautilus is authoritative,
  the dashboard shows `已切换（Paper）`, fixed-fixture status, and the old-cycle
  streak separately instead of presenting an already-completed migration as a
  pending failed gate.

### Migration result

- Cutover receipt: `20260716T013003702625Z`; status `applied`.
- Authoritative engine: `nautilus_paper`; current cycle `2026-07-16_DAY` remains
  `stopped`, with zero accepted orders, zero positions, and reconciliation `ok`.
- The controller backed up `configs/dualtrack.yaml` plus both installed
  LaunchAgent plists, stopped both adapter-owning services, wrote the paper-only
  authority/runtime/override environment, restarted them, and validated the
  dashboard API. No order was submitted and no real-money path was enabled.
- Rollback remains available from the cutover receipt and restores the exact
  Legacy config/service bytes while preserving all three ledger namespaces.

### Full drift audit

- The historical `2026-07-15_NIGHT` engines still agree on lifecycle counts:
  57 orders, 12 fills and 6 positions on each side.
- The previously discussed `0.29247753 USD` net-PnL difference is fully explained:
  `0.28891059 USD` is the mixed historical fee-contract transition and
  `0.00356694 USD` is venue price/quantity precision. It is not missing cash.
- With the expanded schema, the old-cycle audit exposes 120 rows: 58 chronology,
  31 economic, 19 schema/semantic, and 12 StrategyPlan traceability differences.
  Most chronology/schema rows are missing fields in the old replay artifact;
  they are intentionally not backfilled. The earlier 45 `cancelled/canceled`
  rows disappear after canonicalization because they were spelling-only.
- New deterministic fixtures cover market long/short, untouched limit orders,
  scale-in weighted average, partial reduction, TP/SL, same-bar conservative
  priority, fees/slippage/margin/exposure/PnL, duplicate replay, restart and
  residual-unit reconciliation. All 10 fixture classes pass with the expanded
  schema.

### Gotchas

- Running the fixed parity fixtures does not refresh
  `cutover/shadow_gate_current.json` by itself. The shadow cutover status must be
  rebuilt after the fixture gate or the API/UI can display a stale blocker.
- Several tests implicitly loaded the live `configs/dualtrack.yaml` and therefore
  failed after a legitimate authority switch. Those tests now freeze a complete
  Legacy test config; test behavior no longer depends on the operator's current
  production engine.
- A candidate field must not be invented when Nautilus upstream reports omit it.
  Order/fill time and order type are emitted only when present; the deterministic
  fixture supplies the explicit engine-neutral evidence it is intended to test.
- StrategyPlan trace is absent from some historical persisted Legacy trade rows
  even when newer candidate artifacts contain it. Historical records remain
  immutable; all new commands carry the versioned trace.
- `Strategy Shadows` currently has no generated comparison result. The UI and
  saved screenshot correctly show this as missing historical what-if evidence;
  it is not evidence against the production cutover and was not fabricated.

### Verification and evidence

- Focused execution/cutover/UI regression: `135 passed`.
- Final full repository regression after the live cutover and test-isolation fix:
  `1538 passed in 479.98s`.
- Browser validation on the authenticated public v5 page: `Nautilus Paper（当前）`,
  stopped, zero orders, zero positions, preserved 10,005.35 equity and 13 fills,
  fresh Binance data, and zero browser console errors.
- Desktop:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-nautilus-cutover-desktop.png`.
- 390px mobile:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-nautilus-cutover-mobile.png`.
- Real-time grid:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-nautilus-cutover-realtime-grid.png`.
- Strategy Shadow empty-state evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-nautilus-cutover-strategy-shadows.png`.

## 2026-07-16 - Leverage-derived grid sizing, grid modes, and chart hierarchy

### Decision

- Define the operator-selected leverage as the worst-side notional ceiling. With
  10,000 USD equity and 10x leverage, the grid may deploy up to 100,000 USD of
  simultaneous same-side notional. For a neutral grid, the denominator is the
  larger of the buy-entry and sell-entry rung counts; it is not the total number
  of bilateral orders.
- Calculate automatic per-grid notional as
  `equity × leverage × capital_utilization_cap ÷ max_same_side_entry_levels`.
  `capital_utilization_cap` is now a single global value of 1.0. Steady and
  aggressive styles change range and spacing geometry only; they no longer
  carry hidden 50%/70% capital haircuts or different sizing risk budgets.
- Keep plan-loss math visible as a diagnostic and flag when it exceeds the
  configured advisory budget. It no longer silently reduces the order size.
  Lowering leverage is the explicit operator control for reducing deployment.
- Preserve the grid-count contract: D1 ATR14 determines the range, 4H ATR14
  determines the target spacing, and the resulting interval count is clamped to
  24-80 unless the operator enters a valid count explicitly.
- Add grid mode to the versioned preview/plan/order path. `arithmetic` uses equal
  absolute price differences; `geometric` uses equal price ratios. Both derive
  TP from the adjacent grid level, include fee-deducted minimum profit per grid,
  and produce deterministic preview hashes.
- Reduce chart noise by making ordinary chart reference lines faint, drawing
  grid/order levels as solid side-colored lines, removing the duplicate
  grid-plus-order line at the same price, and showing right-axis labels only for
  the six orders nearest the current price. Range boundaries remain emphasized.

### Binance feature inventory for later product review

- Candidate next: trigger price; open initial position on creation; TP/SL by
  price, PnL or ROI; explicit close-all-versus-keep-position behavior when the
  bot stops; fee-deducted profit/grid; estimated liquidation prices.
- Later / requires a separate execution design: trailing up/down with a movement
  limit, automatic margin addition on bracket change, live parameter
  customization, and copy-strategy workflows.
- Read-only reporting ideas: runtime, 24h/total matched trades, historical
  ROI/PnL curve, bot preview chart, range/grid/mode summary, and historical
  strategy comparison. These do not authorize production mutations.

### Gotchas

- The previous sizing formula took the minimum of a style-specific capital cap
  and a style-specific plan-loss cap. On the same live 53-grid preview this made
  steady use about 1.89x while aggressive used about 6.09x, despite both showing
  a 10x leverage limit. This was the source of the unexplained quantity change.
- A bilateral neutral grid must not divide capacity by all buy and sell orders.
  Only the maximum simultaneously accumulating side owns the worst-case margin
  denominator; using total order count would understate usable capacity by about
  half.
- Grid preview/start originally built all D1/4H/1H/15m contexts before sizing,
  so an unrelated 1H outage could block a calculation that only consumes D1 and
  4H. Preview/start now fetch only D1/4H; AI trend refresh remains fail-closed on
  all four required timeframes.
- Full-page screenshots of the canvas page can tile a sticky region in the
  in-app browser. Evidence therefore uses a desktop viewport capture for the
  chart and a separately scrolled 390px viewport capture for the controls.
- The authenticated public preview POST briefly returned Cloudflare 530 while
  the same local API remained healthy. No production mutation was attempted;
  interactive acceptance used the local authenticated control surface and the
  public page was rechecked read-only after the service settled.
- The final public read-only check then exposed Cloudflare Tunnel error 1033:
  the dashboard and gateway listeners were healthy, but the named tunnel had
  zero edge connections. Restarting its existing LaunchAgent restored two edge
  connectors; the authenticated page recovered without changing robot, order,
  position, or ledger state.

### Verification and evidence

- Live stopped-state preview, 10,005.35 USD equity, 10x leverage: steady and
  aggressive both calculate 3,705.68 USD per grid with 27 worst-side levels,
  or 100,053.36 USD total worst-side notional versus a 100,053.46 USD ceiling.
- Arithmetic and geometric previews both produced 53 deterministic orders; the
  displayed fee-deducted minimum profit/grid was 0.17%/0.18% for the steady
  snapshot and 0.08% for the aggressive snapshot.
- Full repository regression: `1543 passed in 519.31s`.
- Browser acceptance: trusted Binance USD-M data, Nautilus Paper authority,
  stopped, zero accepted orders, zero open positions, 390px horizontal overflow
  zero, no browser console errors, and no production-ledger writes from the
  preview interactions. Final public read-only state remained stopped with
  10,005.35 USD equity, 13 historical trades, zero orders, and zero positions.
- Desktop chart hierarchy:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-grid-visual-hierarchy.png`.
- Desktop sizing/modes:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-grid-sizing-modes-desktop.png`.
- 390px sizing/modes:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-grid-sizing-modes-mobile.png`.

## 2026-07-16 - Trusted strategy-timeframe retry and truthful grid-mode preview

### Decision

- Keep D1 and 4H strategy inputs fail-closed, but retry the exact same
  source/timeframe once when a non-synthetic snapshot is temporarily
  unavailable. The retry does not change provider, accept stale data, or allow
  synthetic data; a second failure still blocks preview and new positions.
- Preserve the last valid grid preview when a later preview request fails. A
  failed arithmetic/geometric switch now rolls the selected button back and
  explicitly says which grid remains on the chart, instead of showing a new
  selection over old grid geometry.
- Make mode geometry auditable in the chart summary. Arithmetic displays one
  fixed USD price gap; geometric displays the fixed percentage ratio plus its
  lower-to-upper USD price-gap range.

### Gotchas

- The previous grid-mode click changed the selected button before the async
  preview completed. On a transient D1 failure, the preview was then cleared
  and the chart fell back to the production plan without rolling back the
  button. The backend geometric calculation was correct, but the UI could
  falsely imply that the old chart was geometric.
- Equal-ratio levels do not have equal dollar gaps. On the accepted live
  53-grid preview, the ratio was about 0.19% while absolute gaps increased from
  about 7.15 to 7.87 USD across the range. Showing only the ratio made the
  visual difference unnecessarily hard to verify.

### Verification and evidence

- Focused strategy-timeframe, static UI, sizing, and control-plane regression:
  `47 passed`.
- Final full repository rerun: `1545 passed in 509.07s`. The first full run had
  one unrelated offline-runner isolation failure (`1544 passed, 1 failed`); that
  test passed alone and the clean full rerun did not reproduce it.
- Synthetic strategy data is rejected immediately and is never retried; the
  dedicated transient-retry/synthetic-rejection/UI-rollback check passed `4/4`.
- Authenticated public browser acceptance: arithmetic showed a fixed 7.5 USD
  gap; geometric showed a 0.19% ratio and a 7.15-7.87 USD variable gap, both
  with 53 deterministic preview orders. Preview created no production orders.
- Arithmetic chart:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-arithmetic-grid-chart-fixed.png`.
- Geometric chart:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-geometric-grid-chart-fixed.png`.

## 2026-07-16 - GRIDMIND visual restoration with the full production console contract

### Decision

- Keep the compact GRIDMIND visual system from the first Claude reskin: five
  account metrics, a wide chart/data column, a narrow decision/control rail,
  terminal typography, and restrained amber/green/red state accents.
- Restore missing capabilities by reusing the proven production-console logic,
  not by extending the first reskin's simplified handwritten implementation.
  The restored page retains clickable direction/style/grid-mode previews,
  editable range/grid/notional/leverage/out-of-range fields, preview risk,
  start/stop, running range adjustment, statistics reset, AI Input/Output
  receipts, EMA/MACD, six data tabs, manual close, operator audit, execution
  engine, and Nautilus cutover status.
- Continue using `standard-kline`; the first GRIDMIND draft's custom canvas is
  not a production chart contract. Include the complete active/preview range in
  price autoscaling so the grid geometry remains visible while all overlays
  still move with the chart's native scale and pan behavior.
- Make `/dashboard-v5.html` serve `dashboard-gridmind.html`. Keep
  `dashboard-dualtrack-split.html` unchanged as the compatibility entry point.

### Gotchas

- The earlier follow-up replaced `dashboard-gridmind.html` with the full legacy
  page to recover functionality. That restored behavior but also erased the
  compact visual hierarchy; the right fix was to preserve the full controller
  contract while replacing only the information architecture and skin.
- Price lines do not participate in Lightweight Charts autoscaling by default.
  Without an explicit `autoscaleInfoProvider`, a valid wide grid can exist but
  most levels remain outside the visible price scale. The GRIDMIND page now
  includes the selected production/preview range in autoscaling.
- The control rail is intentionally independently scrollable on desktop because
  the complete production controls cannot fit inside the screenshot draft's
  shorter read-only rail. At 1120px and below it returns to normal document flow.
- Automated public browsing reaches the Cloudflare Access login page without the
  operator session. Public authenticated visual proof is therefore not claimed;
  the local v5 route and the public gateway upstream were verified, while the
  edge remained fail-closed with HTTP 302 to Access.

### Verification and evidence

- Focused dashboard/server/chart regression: `70 passed`.
- Full repository regression: `1550 passed in 526.82s`.
- Browser acceptance: trusted Binance USD-M data, live 1m/5m switching, five
  populated account metrics, no horizontal overflow at 390px, AI receipt with
  Input/Output/archive, and zero browser console errors.
- Read-only preview acceptance: short + aggressive + geometric produced 53 grid
  intervals, 26 candidate orders, 3,848.21 USD per grid, and 10x estimated
  leverage; the production runtime remained stopped with zero accepted orders
  and zero open positions.
- Dashboard service restart preserved authoritative state exactly: cycle
  `2026-07-16_DAY`, `nautilus_paper`, stopped, zero orders, zero positions.
  The local gateway returned the new GRIDMIND page and the public edge returned
  the expected Cloudflare Access 302.
- Final desktop v5:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-gridmind-v5-final-desktop.png`.
- Geometric preview/grid visibility:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-gridmind-geometric-fit.png`.
- 390px full-page acceptance:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-gridmind-mobile-local.png`.

## 2026-07-16 - Chronological execution replay and immutable fill history

### Decision

- Treat execution lifecycle as four separate facts: submitted order, accepted
  order, immutable fill, and derived position. A filled order leaves the current
  order list but must remain in fill history; a position can change only through
  a later close fill.
- Sort both trusted market events and execution commands by event time before
  every Nautilus rebuild. Historical bars may be backfilled, but they can never
  be appended after a live control command and replayed as if they arrived later.
- Validate the complete start batch before advancing the market. A start is now
  all-or-zero: every planned order must be accepted or legitimately filled. On
  failure, the control plane cancels pending orders, flattens any positions
  created during the failed attempt, advances one cleanup event, and verifies
  zero remaining orders and positions before reporting failure.
- Use the durable client order ID as the business identity of a fill. Nautilus
  internal event IDs are replay-local random values and cannot be used for
  append-only guarantees. Persisted fill history rejects a missing or
  economically changed prior fill.
- Show position open/close time in Beijing time. Render the fill tab from the
  immutable production fill history rather than from the current derived trade
  snapshot, and label each event as open-long, close-long, open-short, or
  close-short.

### Root cause

- The 19:52 start did submit the complete 53-order batch. A live start event was
  persisted before older cycle bars, so Nautilus received event time in the
  order `11:52 -> 01:01 -> cleanup -> 01:02...`. This time travel caused two
  sell orders to appear filled transiently; the start verifier then observed
  only 51 accepted orders, rolled those 51 back, and left the transient state
  dependent on replay order.
- A later full replay reordered/recomputed that state and the temporary fill and
  position disappeared. It was not a user cancellation and was not valid fill
  lifecycle behavior; the UI was exposing a derived replay snapshot as if it
  were an immutable ledger.

### Gotchas

- Nautilus regenerates an internal event UUID during each replay. Comparing that
  UUID initially caused valid later partial-close replays to be rejected; the
  stable client order ID is the correct fill identity for this execution model.
- Commands with identical timestamps must keep insertion order. Python's stable
  sort is relied on so an entry remains before its associated cancel/exit at the
  same event time.
- The dashboard rolled from `2026-07-16_DAY` to `2026-07-16_NIGHT` during the
  investigation. The new cycle correctly displays stopped, zero orders, and
  zero positions; this rollover must not be described as manual cleanup of the
  previous cycle artifact.
- The transient 19:52 fill was never captured in an immutable intermediate
  ledger, so it cannot be reconstructed faithfully after the fact. Do not
  fabricate it from screenshots or current replay output.

### Verification and evidence

- Focused controller, adapter, dashboard, cycle-runner, and execution contract
  regression: `109 passed` plus real Nautilus runtime/cutover regression
  `43 passed`.
- Final clean full repository regression after the replay-version fixture was
  updated: `1555 passed in 698.82s`.
- Browser acceptance clicked current positions, current orders, fills, and the
  5m timeframe. The position table shows Beijing open/close time, fills remain
  visible as immutable lifecycle events, 5m loads trusted Binance USD-M data,
  and current-cycle orders remain zero. No dashboard-originated console errors
  were observed.
- Desktop lifecycle evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-gridmind-order-lifecycle-desktop.png`.
- 390px position-time evidence (zero horizontal page overflow):
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-16-gridmind-order-lifecycle-mobile.png`.

## 2026-07-18 - A0 versioned market-data trust envelope

### Decision

- Keep `schemas.market_data.Bar` as the only in-process OHLCV value object.
  Version the complete batch boundary with `market-data-envelope-v1` instead
  of adding a schema field to every candle or creating a fourth candle model.
- Translate the upstream `kline-candles-v1` response in exactly one adapter
  mapper. The mapper validates source identity, asset class, timeframe,
  response count, OHLC geometry, chronological timestamps, execution-venue
  evidence, and all trust-policy enums before returning an envelope.
- Define live execution readiness conservatively. Real data from an execution
  venue is not enough: the request and response must also prove strict quality,
  cache bypass, no fallback, exact source selection, freshness, no rejection,
  and no access issue.
- Add `DatafeedMarketRepository.load_envelope()` as an opt-in seam only. A0
  does not route the existing `load_bars*`, strategy, execution, dashboard, or
  Nautilus cutover paths through the new contract. Runtime migration belongs
  to A1 after shadow comparison against current live responses.
- Treat Binance XAUUSDT and Yahoo `GC=F` as structurally compatible market-data
  batches but not semantically substitutable instruments. The research fixture
  remains explicitly non-executable even though it contains real OHLCV data.

### Gotchas

- `fresh=null` is valid for some market-hours/research sources but can never
  satisfy the live execution-ready predicate. Datafeed currently also reports
  `fresh=null` for non-continuous sessioned execution venues, so A1 must add
  session-aware freshness before Tiger/COMEX can use this gate; A0 remains
  deliberately fail-closed.
- `execution_venue=true` alone is insufficient. A relaxed, cached, fallback,
  source-mismatched, rejected, or access-degraded response remains blocked.
- `DatafeedMarketClient` intentionally still returns the raw HTTP dict. Only
  `datafeed_market_mapper.map_candle_response` may convert that dict into the
  versioned domain envelope.
- `selected_source` is the registered adapter identity while `source_mode` is
  a path label. They are both required and preserved but are not required to
  have the same string value.
- Envelope-level trust fields and batch membership are frozen. The legacy
  `Bar.quality_flags` list remains mutable for compatibility in A0, so row
  flags are not the authoritative execution-readiness evidence and mutating
  them cannot alter the envelope header.
- The primary worktree contains unrelated Debug and grid-range changes. A0 was
  built in an isolated worktree and must not be merged by overwriting those
  changes or by copying whole files from the branch.

### Verification

- Clean isolated baseline before implementation: `28 passed` across the
  existing datafeed, market boundary, and execution-contract tests.
- Envelope, client-level HTTP 502 body preservation, repository, and
  architecture-boundary checks after implementation: `38 passed`.
- Focused upstream regression before review: `76 passed`.
- Full repository regression before review: `1577 passed, 6 skipped`.
- Opus adversarial review found no P0/P1 issue. Follow-up fixes separated
  `source_mode` from adapter identity, compared equivalent timestamp instants,
  cross-checked `age_seconds <= max_age_seconds`, and added the missing
  timeframe/latest/timezone contract tests.
- Final post-review focused regression: `84 passed`.
- Final post-review full repository regression: `1585 passed, 6 skipped in
  458.67s`.

## 2026-07-18 - A1 same-response market-data shadow cutover

### Decision

- Keep datafeed as the owner of provider selection, quality, freshness, and
  market-session truth. Trading Orchestrator accepts both `kline-candles-v1`
  and `kline-candles-v2` during migration, but v2 must carry explicit
  continuous/open/closed/unknown session evidence.
- Migrate `DualTrackMarketFeed` as the first consumer by mapping the same raw
  HTTP response twice. The HTTP client is called once; legacy and envelope
  projections are compared field by field without a second market request.
- Configure `datafeed.market_data_contract_mode=shadow`. Legacy remains the
  authoritative dashboard payload and receives only an additive shadow
  receipt. `authoritative` is implemented for testability but is not enabled.
- Fail closed in authoritative mode on an unavailable upstream, invalid
  schema, source mismatch, stale data, closed/unknown session, synthetic data,
  access issue, or unexpected projection failure. In shadow mode, an
  unexpected projection/comparison failure is sandboxed and cannot turn the
  legacy dashboard read into a 500.
- Revalidate session receipt age, current session end, and final-bar age at the
  consumer boundary. `MarketDataEnvelope.execution_ready` is necessary but is
  not by itself sufficient for a sessioned venue at a later wall-clock time.
- Compare every safety-relevant field for all bars. For batches above 5,000
  bars, skip only the duplicate diagnostic JSON digests; exact field comparison
  still covers the complete batch to avoid two extra 60k-bar copies.

### Gotchas

- The running local datafeed still emits `kline-candles-v1`. Real same-response
  parity is proven for v1 only; fixture coverage for v2 does not authorize a
  production cutover. Runtime remains `shadow` until live v2 parity is observed.
- Sessioned gap validation currently knows only the trading windows returned by
  one adapter receipt. A multi-day Tiger batch can contain a prior-session gap
  outside that receipt's coverage. Do not make Tiger/COMEX authoritative until
  calendar coverage spans the candle batch or unknown coverage fails closed.
- The legacy and datafeed freshness formulas currently match for supported
  timeframes; the added 5m parity test proves this beyond the original 1m
  fixture. A future policy change should intentionally surface as shadow drift,
  not be excluded from the gate.
- The primary Trading Orchestrator worktree still contains unrelated Debug and
  grid-range work. A1 was implemented only in the isolated worktree and must
  not be integrated by overwriting primary files.
- This milestone changes a backend contract and adds receipts; it introduces no
  new visible surface, so screenshot evidence is not applicable.

### Verification

- Datafeed post-review full suite: `86 passed`; Ruff: `All checks passed`.
- Trading Orchestrator post-review focused market/dashboard/execution suite:
  `152 passed`.
- Trading Orchestrator final full suite: `1615 passed, 6 skipped in 360.87s`.
- Read-only live probe against the running local datafeed returned real Binance
  USD-M XAUUSDT 1m data. The post-review shadow receipt reported `pass`, compared
  `40` fields, found `0` differences, and produced equal legacy/candidate
  digests. The receipt explicitly identified upstream schema
  `kline-candles-v1` and authority `legacy`.
- Two focused Opus reviews found no P0/P1 blocker. Accepted fixes sandboxed
  unexpected shadow/authoritative exceptions, removed session-blind response
  recomputation, failed closed on unwrapped adapter errors and naive timezone
  data, separated normal market closure from source health, and bounded large
  diagnostic digest cost. The proposed claim that 5m freshness must drift was
  rejected after formula inspection and an exact passing 5m parity test.

## 2026-07-18 - A2 single accounting truth

### Decision

- Make `accounting-snapshot-v1` the immutable, content-addressed read contract
  for orders, fills, positions, trade lifecycles, P&L, account values,
  completeness, and reconciliation. Execution engines and venue adapters keep
  ownership of their source facts; the accounting projector has no order or
  ledger write capability.
- Count one started position lifecycle as one trade. An entry is one trade;
  entry plus exit is still one trade; only a fully closed lifecycle increments
  `completed_trade_count`. Entry and exit executions remain separately visible
  in fill counts.
- Derive production-history compatibility totals from the canonical snapshot.
  Admit only versioned StrategyPlan Legacy facts and, when explicitly selected,
  Nautilus authoritative snapshots. Execution shadow, recovery replay, and
  unversioned Legacy records remain outside production P&L.
- Preserve unavailable venue facts as `null`, never zero. Binance user-trade
  evidence currently supports fill and account economics but not reliable
  entry/exit round-trip classification. Tiger prime-assets evidence supports
  aggregate account values but not order, fill, position, or trade counts.
- Keep broker accounting additive to reconciliation safety decisions. A
  malformed projection emits a full blocked canonical snapshot. If even the
  snapshot builder or serializer fails, a separate versioned unavailable
  receipt is attached so the authoritative reconciliation, closed-order
  reconciliation mark, and account-sync artifact still persist.
- Treat unobserved slippage as unknown. Explicit zero remains valid only when
  the engine supplies zero or an observed empty fill set proves it.
- Verify the Nautilus realized-P&L basis empirically rather than infer it from
  framework documentation. The pinned Nautilus 1.230.0 runtime produced
  `net realized = gross realized - commissions + funding`; this identity is now
  an integration-test gate.

### Gotchas

- Legacy entry fees are already represented in each fill's net realized P&L.
  Subtracting the same fee again would understate production results.
- Modern partially closed Legacy entries persist remaining units. Rebuilding
  those rows with the historical fill-only reducer double-counts the partial
  exit; the canonical path therefore uses `project_human_trades` for modern
  state and retains the historical reducer only for old row shapes.
- A blocked accounting projection must be visible but cannot silently rewrite
  the pre-existing broker confirmation, daily-loss, exposure, or order-routing
  decision. The outer unavailable receipt exists only to protect persistence.
- Nautilus authoritative history currently relies on the execution-contract
  invariant that fill IDs are globally idempotent within an engine ledger.
  A3 must include cross-cycle identity in the golden execution conformance
  scenarios before any broader authority change.
- Binance `userTrades` cannot distinguish every zero-realized entry from a
  breakeven exit using the current normalized fields. Do not manufacture
  round-trip counts from side or realized-P&L heuristics.
- This milestone adds backend/read-side contracts only. It has no new visible
  surface, so screenshot evidence is not applicable under the project Evidence
  Contract.
- The primary worktree still contains unrelated Debug and grid-range changes.
  A2 remains isolated and must not be integrated by overwriting primary files.

### Verification

- Clean A2 baseline: `115 passed, 5 skipped`.
- Canonical snapshot, execution, production-history, and broker implementation
  suite before review: `188 passed, 5 skipped`; related broker/guardrail suite:
  `94 passed`.
- Full repository suite before Opus review: `1633 passed, 6 skipped in 343.50s`.
- A read-only projection against existing production artifacts returned 26
  fills, 13 entry fills, 13 exit fills, 13 completed trade lifecycles, net
  realized P&L `5.34624747`, zero open trades, and a stable snapshot ID. No
  strategy, order, or external venue state was changed.
- The first Opus adversarial review found no P0/P1. Its valid P2 persistence
  finding was fixed; the claimed Nautilus gross/net ambiguity was rejected only
  after a real pinned-runtime replay proved net-of-commission semantics. The
  unknown-slippage inconsistency was fixed; cross-cycle ID hardening is carried
  into A3.
- The final focused accounting/reconciliation/guardrail suite after both Opus
  reviews: `124 passed`; pinned Nautilus runtime integration: `5 passed`; Ruff:
  `All checks passed`.
- Final post-hardening full repository regression: `1638 passed, 6 skipped in
  326.73s`.

## 2026-07-18 - A3 unified execution semantics kickoff

### Decision

- Keep Legacy as the current authoritative paper matcher. A3 removes the
  self-made matcher inside Strategy Shadow and delegates executable candidate
  replay to the existing Nautilus adapter; it does not switch paper authority.
- Use two separate evidence layers. Candidate-specific evidence is one Nautilus
  replay whose normalized inputs are content-bound and whose persisted snapshot
  passes `reconcile()`. Platform compatibility remains the existing exact
  Legacy-authoritative versus Nautilus-candidate parity gate. Comparing a
  Nautilus snapshot with itself is not conformance evidence.
- Extract the production StrategyPlan-to-command mapping once and reuse it in
  Strategy Shadow. Keep the fast Lab simulators as research filters, not as
  execution truth.
- Make the active Lab-to-paper gate fail closed unless it receives a verifier-
  approved candidate receipt token plus current platform parity evidence. Leave
  the separate legacy daily `StrategyPromotionGate` out of A3 to avoid widening
  the grid-only milestone.
- Emit a new `strategy-shadow-run-v2` contract. Historical v1 artifacts stay
  readable by the Dashboard but are permanently ineligible as promotion or
  conformance evidence.

### Gotchas

- The current Strategy Shadow calculates touches, protective exits, positions,
  P&L, and drawdown itself and hardcodes cost to zero. It must be replaced, not
  wrapped as another execution engine.
- At least one existing v1 artifact evaluates events before its plan `locked_at`
  while claiming no future-function leakage. A v2 scenario must bind an explicit
  `available_at` and reject every earlier event.
- A candidate receipt proves that one candidate is internally executable under
  Nautilus. It does not prove current Legacy paper equivalence by itself; the
  separate platform parity receipt is mandatory.
- A dedicated Shadow namespace is insufficient unless tests also prove no writes
  reach `dualtrack/reconciliation`, `dualtrack/nautilus/parity`, or
  `dualtrack/cutover`, because those directories control the attended seven-
  cycle authority gate.
- `pipelines/dashboard_server.py` reads the last row of each Strategy Shadow
  artifact. New writes must remain append-safe and v1/v2 compatible without
  rewriting historical files.
- The primary worktree contains unrelated Debug and range-drag work. A3 remains
  isolated and must not be integrated by overwriting primary files.

### Verification

- Focused pre-A3 Strategy Shadow, execution adapter/contract, parity, and
  promotion baseline: `55 passed`.
- Opus plan review verdict: sound direction with one P0 correction. The plan now
  forbids tautological Nautilus-versus-Nautilus comparison, names both evidence
  layers, protects the authority-gate directories, fails promotion closed, and
  preserves v1 Dashboard compatibility.

## 2026-07-18 - A3 unified execution semantics complete

### Decision

- Replace Strategy Shadow's local matcher with one explicit Nautilus replay
  port. The Shadow service now builds a versioned scenario, delegates execution,
  projects the returned snapshot through `accounting-snapshot-v1`, and derives
  display metrics only from that accounting truth.
- Reuse the same pure StrategyPlan-to-grid-command projection in the production
  control plane and Strategy Shadow. Normalized commands bind plan ID/version,
  venue precision, fees, starting cash, and leverage.
- Require `locked_at` on every executable StrategyPlan and reject commands or
  bars whose availability starts earlier than the locked plan or command batch.
- Keep candidate conformance and platform compatibility separate. A candidate
  receipt proves one content-addressed Nautilus replay and reconciliation;
  platform parity remains the exact Legacy-authoritative versus
  Nautilus-candidate ten-class fixture. Nautilus is never compared with itself.
- Bind both evidence layers to the current execution, accounting, projection,
  Shadow, and promotion source semantics; the runtime-reported and explicitly
  pinned Nautilus `1.230.0`; one fee/precision/capital/leverage contract; and
  child evidence no older than seven days. The aggregate timestamp is the oldest
  child timestamp, so an old fixture cannot be restamped as current.
- Make the active Lab promotion boundary accept only an integrity-valid token
  for the exact candidate. The token rechecks its embedded parity timestamp,
  code hash, runtime, and contracts on every call. Missing, v1, stale, drifted,
  or mismatched evidence remains `paper_eligible=false`.
- Preserve Legacy paper authority, the attended seven command-bearing cycle
  cutover gate, all real-money blockers, and historical
  `strategy-shadow-run-v1` trace artifacts.

### Gotchas

- The first isolated ten-class rerun exposed one real contract drift: every
  Nautilus fixture omitted `account.funding` while Legacy reported explicit
  zero. The fixed fixtures now report `funding=0.0` for windows with no funding
  settlement; exact parity then passed all ten classes.
- A platform hash must cover accounting and Shadow orchestration as well as the
  engine adapter. Otherwise an old candidate receipt can survive a changed
  accounting-pass rule. Opus found this as P1; the dependency list and a
  byte-drift test now enforce it.
- The existing primary `parity/current.json` predates the new bindings and is
  intentionally stale. It must be regenerated by the full fixture pipeline
  before any Lab candidate can become paper-eligible. A3 verified this only in
  an isolated output root and did not rewrite primary evidence.
- Changing the pinned runtime, account fee observation, starting cash, leverage,
  or any fingerprinted semantic path deliberately invalidates candidate and
  platform evidence. Divergent candidate/platform config surfaces as
  `platform_parity_contract_mismatch` rather than silently adapting.
- Missing fingerprint source files produce a structured blocked gate instead of
  crashing or preserving the previous pass.
- A3 has no new visible product surface, so screenshot evidence is not
  applicable under the project Evidence Contract. The primary worktree remains
  untouched because it contains unrelated Debug and range-drag changes.

### Verification

- Shared command projection, Shadow replay/read model, promotion, Dashboard,
  accounting, execution-adapter, and parity regression: `181 passed`.
- Final P1/P2 evidence-binding suite: `47 passed`; Ruff: `All checks passed`.
- Pinned real Nautilus integration: `6 passed`, including exact repeat/restart
  identity, two fills, one completed trade, non-zero fees, accounting identity,
  runtime version, and platform code hash.
- Full isolated Legacy↔Nautilus fixture rerun: all `10/10` classes exact `pass`,
  no blockers, runtime `1.230.0`, with one unanimous execution/fee contract.
- Final repository regression after all review fixes: `1662 passed, 7 skipped in
  342.98s`.
- First Opus adversarial implementation review found one P1 stale-parity
  evidence gap. The final Opus follow-up reported `NO P0/P1`; it confirmed that
  the original P1, accounting-code omission, runtime pin, and persisted-token
  freshness paths are closed. Its remaining config-coupling note is fail-closed
  with an explicit blocker; its structured-error P2 was fixed.

## 2026-07-18 - A4 unified Risk Port kickoff

### Decision

- Treat risk as a mutation-time precondition, separate from pure grid geometry
  and capital sizing. A range edit may recommend a lower per-grid notional, but
  the system must never apply that recommendation silently.
- Add one engine-neutral, content-bound `risk-decision-v1`. Strategy control,
  manual production entry, and broker composition roots consume it; Legacy,
  Nautilus, and venue adapters do not own separate risk meanings.
- Bind each decision to the exact candidate commands, StrategyPlan, trusted
  market, canonical account facts, current normalized execution state,
  evaluator version, and resolved policy. Rebuild and match it immediately
  before the first exposure-increasing submit.
- Fail new exposure closed on unknown equity, accounting/reconciliation drift,
  stale/tampered inputs, out-of-range market, unknown open-position protection,
  plan-loss excess, leverage excess, or margin excess.
- Preserve unconditional access to valid cancel, protective exit, flatten, and
  reduce-only actions. Entry-risk blockers must not become an exit trap.
- Bridge the mature `LiveMoneyGuardrails` implementation into the canonical
  decision instead of rewriting its tested limits or blocker precedence.

### Gotchas

- Current default auto-sizing uses full leverage capacity and can show maximum
  plan loss far above the 10% budget. A4 intentionally turns that diagnostic
  into an execution blocker while keeping preview values unchanged.
- `account_equity()` silently falls back to `$10,000`; mutation-time decisions
  cannot use that fallback.
- Local paper regrid stages new pending orders before cancelling old ones, but
  processes no market event inside the critical section. That is not a live
  venue atomic-replace guarantee and must stay explicitly out of scope.
- Existing positions survive regrid and keep their TP/SL. New exposure is
  allowed only when their remaining notional and stop risk are known.
- A persisted risk decision is audit evidence, not authority. It cannot be
  replayed as permission after market, account, execution state, code, or policy
  changes.
- A4 has no new visible UI surface; screenshot evidence is not applicable.
  The primary worktree remains untouched because it contains unrelated Debug
  and range-drag changes.

### Verification

- Focused pre-A4 baseline: `76 passed` across grid sizing, strategy control,
  live money guardrails, and dashboard server.
- Opus planning review completed with verified `claude-opus-4-8` receipt
  `20260717T205342Z_c7dd8846-01a1-4fb7-8907-65a948cc1832.json`.
- Accepted P0: never evaluate from preview/plan risk figures derived through the
  `$10,000` fallback; recompute from canonical account equity and exact commands.
- Accepted P0: primary risk gate precedes candidate-plan/runtime/order writes;
  the under-lock pre-submit recheck raises into transaction restoration.
- Accepted P1: derive action class server-side for every network entry, share
  the production mutation lock with manual orders, normalize execution risk
  state through canonical accounting, and keep live broker gates additive.
- Accepted P2: forbid ungated running `grid`/`risk_budget` adjustment, test the
  local-paper no-market-event regrid staging invariant, and bypass entry
  guardrails for reduce-only actions.
- Accepted P3: persisted receipts are audit-only and are never read as
  authorization. Instead of accepting auto-lock before a rejected start, A4
  requires an already-selected active StrategyPlan at the start boundary.

## 2026-07-18 - A4 unified Risk Port complete

### Value delivered

- A grid whose exact stop risk exceeds budget now stops at the mutation
  boundary. The preview still shows the requested geometry/notional and an
  explicit recommendation; the system never applies a smaller position
  silently.
- Grid start, running regrid, and every server-received manual entry share one
  content-bound decision contract and one in-process mutation lock. A changed
  execution snapshot between evaluation and submit raises stale with zero new
  submissions.
- Manual entry action is derived from `event`, so client `source` cannot grant
  a bypass. Valid close/flatten/reduce-only/cancel evaluates identity only and
  remains available when account, market, daily loss, or HALT blocks entry.
- Existing Binance/Tiger money guardrails remain policy authority. Their exact
  blockers and limits are wrapped into `risk-decision-v1`; broker requests now
  carry the canonical allow/block evidence without removing preflight,
  activation, reconciliation, attended, or lifecycle gates.
- Paper decisions persist under `dualtrack/risk_decisions`; venue decisions
  persist under `risk_decisions`. No runtime reads these receipts as authority.

### Gotchas

- HTTP risk enforcement is explicit at `/api/dualtrack/orders` after trusted
  server-market validation. The direct response builder keeps a compatibility
  default for historical/internal tests and is not a network security boundary.
- Local paper regrid is two-phase, not a live venue atomic replace. All new
  orders are accepted before old pending entries are cancelled, and no local
  market event occurs between those steps. Live grid routing remains out of
  scope.
- Risk policy percentages are fractions in `[0, 1]`. A display-style value such
  as `5` fails closed as invalid instead of being interpreted as 5%.
- The first final Opus attempt returned an API-error terminal state and failed
  receipt verification; none of its output was used. The bounded retry is the
  only final implementation-review evidence.
- A4 has no visible UI change. Under the Evidence Contract, visual proof is not
  applicable; test output, code, docs, and Claude receipts are trace only.

### Verification

- Focused risk/control/dashboard/broker suite: `223 passed, 1 skipped`.
- Full repository suite: `1693 passed, 7 skipped in 347.20s`.
- Ruff on all changed Python files: `All checks passed`.
- Verified final Opus implementation review: actual model
  `claude-opus-4-8`, session `e8e84226-e8c6-4430-8c09-29bb07e2722b`, receipt
  `20260717T214429Z_dcf548a2-be9e-49bb-9923-e64236de976b.json`, verdict
  `NO P0/P1`.

## 2026-07-18 - A5 Broker Port kickoff

### Decision

- Introduce one engine-neutral Broker Port and a registry composition root.
  Provider selection belongs at assembly; strategies, control, Dashboard
  commands, and Lab remain consumers of normalized execution intent.
- Preserve the proven Binance/Tiger order, live-money guardrail, activation,
  reconciliation, lifecycle, and protective recovery algorithms. A5 changes
  dependency direction before moving venue wire code.
- Model cancel, protective recovery, and reconciliation as explicit
  capabilities. Application code must stop guessing with `hasattr` or calling
  provider-private methods such as `_binance_symbol`.
- Keep `services.broker_adapter` as a compatibility facade while new code
  depends on `broker_port` and `broker_composition`. Existing import and
  monkeypatch seams remain supported during the strangler migration.
- Follow NautilusTrader's official adapter split: normalized execution client
  interface, venue-owned networking, configuration/factories at composition,
  and venue reports as reconciliation truth. Do not switch live authority in
  this milestone.

### Gotchas

- The 2,067-line `LiveBrokerAdapter` is also the utility base for Binance
  demo/testnet and Tiger paper. A mass file move would create high-risk semantic
  churn; A5 first establishes the port and routes constructors through it.
- Binance demo/testnet bypass only the real-money activation gate for fixed
  non-mainnet endpoints. Registry environment resolution must never carry this
  behavior into `environment=live`.
- Tiger paper can create network orders only behind its existing owner-only
  credential file, explicit paper TradeClient mode, confirmation,
  reconciliation, account, risk, and protection gates. Composition must not
  default or infer any arming flag.
- Inherited methods can make structural `hasattr` checks lie about a venue's
  supported capabilities. Capability declarations must be explicit and closed.
- Unknown providers may preserve historical dry-run artifacts for compatibility
  but can never resolve to an armed network path.
- A transport timeout or ambiguous acknowledgement remains recoverable venue
  uncertainty, not a rejection. Registry/facade code cannot collapse that
  state or bypass reconciliation.
- A5 has no new visible UI surface. Visual Evidence is not applicable; tests,
  code, docs, and review receipts remain trace material only.
- The primary worktree contains unrelated Debug and range-drag changes. A5
  remains isolated and must not be integrated by copying whole files.

### Baseline

- Focused broker, Binance demo/testnet, Tiger, multi-strategy, journal, live
  safety, mainnet canary, and broker-accounting regression: `105 passed`.
- Official Nautilus adapter guidance confirms separate execution clients,
  configuration/factories, venue networking, capability testing, and startup
  reconciliation as the mature boundary.

### Opus planning review

- Verified `claude-opus-4-8` review, session
  `212a4cb6-d50a-467b-8e6a-f2beb3900f6c`, receipt
  `20260717T220217Z_0a6d338b-06ce-40b0-8fcb-619e29a4878f.json`: no P0; safe
  to implement after the accepted corrections below.
- Accepted P1: capabilities default absent and are resolved from both concrete
  adapter and normalized provider. Tiger must not inherit Binance cancel or
  protective recovery, including plain `LiveBrokerAdapter(provider=tiger)`.
- Accepted P1: the Binance live registry entry must retain the existing
  `real_money_ready` activation-gated submit path. Demo/testnet builders cannot
  be reused or generalized into mainnet.
- Accepted P1: demo/testnet keep exact mode flags, guardrail semantics, request
  namespaces, and endpoints; reconciliation must use the same endpoint as the
  execution adapter.
- Accepted P2: unknown demo providers remain unarmed; builders are lazy and
  receive already-resolved config to preserve import/monkeypatch behavior;
  ambiguous lifecycle state passes through unchanged.
- Accepted P2: provider-neutrality tests are function-scoped. Existing
  `_execution_profile_for` and `_demo_reconciliation_block_reason` are
  explicitly classified as diagnostic read-model debt deferred to A6.

## 2026-07-18 - A5 final Opus review and hardening

### Review result

- Verified `claude-opus-4-8` review, session
  `61281269-e0b5-4f29-b7d3-64bad1e6efe3`, receipt
  `20260717T223756Z_79e46db2-2d1e-45be-925e-e43551447b1f.json`: explicit
  `NO P0 / NO P1`.
- Opus verified live activation, unknown-provider unarming, Tiger capability
  isolation, demo/testnet endpoint binding, secret safety, facade compatibility,
  and unchanged ambiguous-submit recovery.

### Accepted hardening

- Guard the legacy `cancel_binance_order` alias itself, not only the new public
  `cancel_order` port. Tiger is rejected before any signed request.
- Freeze adapter capabilities at construction so mutating `provider` later
  cannot grant Binance cancel/protection authority.
- Move active-demo profile normalization from the runner into broker
  composition. Only explicitly demo-capable plugins can be selected; OANDA,
  MT5, and unknown profiles fall back to the paper path instead of becoming
  armed through a demo toggle.
- Enforce equality between a plugin's declared execution capabilities and the
  port returned by its factory. Reconciliation remains a separate capability.
- Add hostile-config tests proving demo/testnet override a supplied Binance
  mainnet URL with their fixed non-mainnet endpoint.

### Verification so far

- Post-review targeted hardening: `28 passed`.
- Post-review focused broker/cycle regression: `131 passed`.
- Ruff on every changed Python file: `All checks passed`.

### Final closure

- Full repository regression after every hardening change:
  `1719 passed, 7 skipped`.
- Verified follow-up review used `claude-opus-4-8`, session
  `421c1d8e-3041-4ddd-9fec-756c519f3394`, receipt
  `20260717T230038Z_e68d8f4c-4bec-41c7-8805-e55909ce0064.json`: explicit
  `NO P0/P1 findings`; all five earlier hardening items were independently
  confirmed closed.
- A5 therefore meets its completion boundary without changing any active
  profile, credential, order semantics, live authority, or visible UI.

## 2026-07-18 - A6 stable Trading System Read Model kickoff

### Objective and value

- Give the production Dashboard one versioned answer for the current strategy,
  orders, positions, started trades, completed round trips, P&L, risk, broker,
  market trust, and runtime state.
- Make every GET observational: refreshing a page must never create a plan,
  evaluate an exit, mark P&L, submit/cancel an order, or write trading state.
- Keep the current visual design and all command/risk/broker authority intact;
  A6 changes the read boundary, not trading behavior.

### Decisions

- Reuse the existing Market Data, Execution, Accounting, Risk, Broker, and
  StrategyPlan contracts. Do not create a new P&L calculator, state database,
  event store, or frontend business-rule layer.
- Add `trading-system-read-model-v1` as a content-bound projection. Current
  orders/positions and all-plan trade lifecycle counts keep explicit scopes.
- Keep `GET /api/strategy-console/current` as a compatibility facade; switch
  GridMind to the new stable endpoint and keep every control as POST.
- Treat `trade_count` as started lifecycles and `completed_trade_count` as
  completed round trips. Fill count is separate.
- Remove hidden writes from `DashboardState.snapshot()` and
  `StrategyControlPlane.read_model()` rather than hiding them behind another
  GET helper.
- Assemble display price and execution marking from one request-scoped market
  observation.

### Gotchas

- The existing Dashboard GET masks stale scheduled state by evaluating exits
  and marking positions. Removing this makes the architecture correct but can
  reveal an operations/scheduling gap that must be reported honestly.
- The legacy Strategy Control Plane read silently materializes a plan. Explicit
  command paths already retain compatibility migration and must remain tested.
- Canonical production-history counts and current-cycle execution counts have
  different scopes; the schema must label both rather than adding unlike
  values.
- Risk `current.json` is display evidence only and cannot become reusable
  authorization.
- Browser code currently computes P&L, return, counts, and run consistency.
  Moving them to the backend must not weaken control-button safety.
- A6 has a visible GridMind change, so desktop and mobile visual Evidence is
  required before completion.
- The primary worktree contains unrelated Debug/range/product changes. A6 is
  isolated and must not be integrated by copying whole files.

### Baseline

- Focused read/control/accounting/provider-neutrality suite: `93 passed`.
- A5 full repository baseline: `1719 passed, 7 skipped`.

### Opus planning review

- Verified `claude-opus-4-8`, session
  `17644104-235a-4d4f-8080-9c523ba29b0b`, receipt
  `20260717T230955Z_35299a00-c6e8-4993-930e-16903cd2ec33.json`: no P0 and
  explicit `SAFE TO IMPLEMENT AFTER CORRECTIONS`.
- Accepted P1: GET-purity fingerprinting covers `/api/dashboard`,
  `/api/system/status`, `/api/trader/overview`, `/api/ops/status`, plus a
  seeded un-migrated legacy plan for the console endpoint.
- Accepted P1: one named pure assembler supplies both the new endpoint and the
  compatibility facade and passes one market observation into execution and
  accounting.
- Accepted P1: static browser tests must prove P&L, return, strategy labels,
  and authoritative counts are no longer calculated in JavaScript.
- Accepted P2: project broker/engine display labels and capture one safe
  control POST reflected through the new GET.

## 2026-07-18 - A6 stable Trading System Read Model complete

### Value delivered

- GridMind now reads one immutable `trading-system-read-model-v1` for the
  running strategy summary, market trust, runtime, orders, positions, trade
  lifecycles, fills, canonical P&L/return, risk, broker, and review/shadow
  references.
- Refreshing an operator GET no longer creates a compatibility plan, evaluates
  an exit, marks a position, writes trading artifacts, or calls broker
  preflight. The same request-scoped market observation feeds display,
  execution marking, and accounting.
- The operational tabs show authoritative counts. One entry starts one trade;
  entry plus exit remains one trade and becomes one completed round trip.
- The browser no longer calculates trading truth. Strategy direction/style,
  grid geometry, spacing, per-grid notional, counts, P&L, return, provider, and
  engine labels are backend projections.
- Runner broker diagnostics are adapter-neutral and secret-safe. Changing
  provider composition no longer requires Binance/Tiger presentation branches.

### Decisions

- Preserve the current execution snapshot for current orders, positions,
  exposure, and margin. Use the all-versioned-production-plan accounting
  snapshot for lifecycle totals, fills, cumulative notional, P&L, cash/equity,
  and return; expose both snapshot IDs and scopes.
- Treat `armed` as a local configuration fact only. Venue readiness remains a
  command-side preflight immediately before submission and is never triggered
  by a read model.
- Keep `/api/strategy-console/current` as a compatibility facade and every
  control as POST. Risk decisions displayed by GET remain non-authoritative
  observations and are never reused as permission.

### Gotchas

- The first final Opus review found that the stable endpoint still consumed
  current-cycle accounting for cumulative facts while the legacy facade showed
  production history. The defect was real even though the one-cycle browser
  fixture passed; a two-cycle regression now locks the scopes apart.
- The same review found a paper `preflight()` call in the stable GET. Although
  it was locally inert, its wall-clock `checked_at` changed the content hash and
  was a latent venue-call trap. The GET now projects descriptor/config only.
- Current cash/equity/P&L is cumulative while exposure/margin/slippage is the
  current execution overlay. This is intentional operator presentation, not a
  command or risk input.
- Full-repository Ruff has 167 pre-existing findings. A6 changed-file Ruff is
  clean; unrelated lint cleanup remains out of scope.
- The primary worktree's configured Nautilus path requires attended approval,
  so it was not forced for visual acceptance. Deterministic browser fixtures
  and a real temporary-paper POST-to-GET integration test provide safe proof;
  no production strategy, broker authority, credentials, or orders changed.

### Verification and evidence

- Focused post-review regression: `91 passed`.
- Full repository regression: `1735 passed, 7 skipped in 361.60s`.
- Ruff on all A6-changed Python files: `All checks passed`.
- Desktop evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-18-a6-read-model-desktop.png`.
- Mobile evidence:
  `/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence/2026-07-18-a6-read-model-mobile.png`.
- Initial verified Opus: actual model `claude-opus-4-8`, session
  `7179b6e7-dbe1-4553-9712-d585e347f717`, receipt
  `20260718T003143Z_50e6034f-a889-4a42-b983-5b28feb06fdf.json`.
- Verified follow-up Opus: actual model `claude-opus-4-8`, session
  `fcf715f6-e860-4141-a9ca-f4e91720d208`, receipt
  `20260718T005733Z_4e3cec78-a2e6-449a-a2e0-bb7a0eac0753.json`; `P1 CLOSED`,
  `P2 CLOSED`, no new P0/P1, `SHIP for A6`.

## 2026-07-18 - A7 end-to-end architecture fitness audit complete

### Value delivered

- The full trading system now has one evidence-backed ports-and-adapters map
  covering data download, cleaning/quality, analysis/strategy, backtest/replay,
  live execution/broker, risk/accounting/reconciliation, and dashboard/read
  model.
- The visible architecture score is `83%`. It measures modularity only; it does
  not claim real-money readiness, self-repair closure, or strategy evolution.
- Stable contract kernels now have executable dependency gates. Known inline
  Binance/Tiger projection debt is frozen so new provider imports or branches
  cannot silently enter those kernels.

### Decisions

- Score all seven product lines against the same four 25-point gates: contract,
  isolation, composition, and proof/cutover. Keep named seams visible instead
  of adding cosmetic interfaces.
- Use the conservative whole-system interpretation of backtest proof. Legacy
  mock `BacktestEvidence` prevents awarding full cutover points even though it
  is outside the authoritative DualTrack/Nautilus grid path and real money is
  disabled.
- Keep self-repair (`58%`) and strategy self-evolution (`62%`) outside the core
  architecture score. The shortest next architectural step is a Strategy
  Plugin Registry, followed by a first-class Backtest Port.

### Gotchas

- The first draft's `84%` overstated Backtest/replay proof. Opus found the
  `fallback_to_mock=True` legacy route; the final score is `83%` and the caveat
  is machine-frozen in the score test.
- The first fitness gate caught concrete imports but not all import aliases or
  inline dispatch forms. Follow-up hardening covers `from services import …`,
  direct comparisons, `match`, `startswith`/`endswith`, provider-key `get`, and
  provider-indexed mapping lookup.
- Passing a runtime-checkable Protocol proves public shape, not semantic
  conformance. Existing specialized A0-A6 suites remain the behavioral proof.
- A7 creates no visible product surface. Visual Evidence is not applicable;
  A6 desktop/mobile captures remain the latest UI proof.

### Verification

- Focused architecture and A0-A6 conformance pack: `118 passed`.
- Final repository regression: `1741 passed, 7 skipped in 371.95s`.
- Architecture-fitness tests: `6 passed`; changed-file Ruff and diff checks are
  clean.
- Initial verified Opus: `claude-opus-4-8`, session
  `6a00e210-39c9-4119-b70a-94f37acc1baf`, receipt
  `20260718T012149Z_0bf84316-62a6-4dd2-8a7d-97ca41329b67.json`; no P0/P1,
  `SHIP`, two accepted P2s.
- Verified follow-up Opus: `claude-opus-4-8`, session
  `36a1e86d-2935-4ac6-8007-ff8f56e0ead9`, receipt
  `20260718T013959Z_52d4ea83-6332-44d0-a820-1731f0d07ab4.json`; both P2s
  closed, no new P0/P1, `SHIP`.
- A7 changed no runtime config, execution authority, credentials, orders,
  positions, accounts, or production state.

## 2026-07-18 - A8 Strategy Plugin Registry kickoff

### Objective and value

- Remove the last application-level strategy engine branch so new grid variants
  and analysis engines plug into one explicit factory boundary.
- Preserve every current strategy's signal/filter behavior while making unknown
  plugins fail closed before a multi-strategy run creates trading artifacts.

### Decisions

- Follow NautilusTrader's configuration/factory separation with a repository-
  local `StrategyAnalysisPort` and explicit plugin composition root.
- Do not add Pluggy or auto-discover PyPA entry points. One factory call does
  not justify a new dependency, and loading arbitrary installed packages would
  expand trading-process code authority.
- Preserve missing `engine` as the legacy `ma` default. Reject explicitly
  unknown names instead of silently routing them to MA.
- Keep Chan imports lazy and keep filter composition outside the base-plugin
  registry.

### Gotchas

- Protocol shape is not semantic parity; all existing engine suites remain in
  the acceptance pack.
- Production config contains 25 strategies and 20 distinct engine names when
  the implicit MA default is included; every name must resolve before cutover.
- The primary worktree remains on an unrelated active branch. A8 is stacked on
  the clean A7 worktree and must not change live configuration or order state.

### Baseline

- A7 full suite: `1741 passed, 7 skipped`.
- Focused A8 baseline: `46 passed in 227.70s`.

## 2026-07-18 - A8 Strategy Plugin Registry complete

### Value delivered

- A new signal-analysis engine now enters through one explicit
  `StrategyAnalysisPort` factory registration. `Strategy`, Daily, Runner, Lab,
  Backtest, and Dashboard-facing summaries do not select concrete engines.
- The registry is frozen before use and publishes a content fingerprint plus
  safe descriptors. Runtime mutation cannot silently swap a strategy factory.
- Enabled strategies with unresolved plugins are reported and skipped before a
  per-strategy trading namespace is created.

### Decisions

- Keep plugin loading explicit. Do not auto-load installed Python packages or
  add Pluggy for a one-method boundary; separately distributed discovery can be
  added later behind a signed allowlist.
- Missing `engine` remains the compatible MA default. Explicit empty or unknown
  names fail closed; no explicit typo silently becomes MA.
- Preserve the legacy global report's disabled `gold_5m_v1` selection through
  an explicitly named compatibility method, but construct its engine through
  the same frozen registry.
- Keep Chan imports lazy and validate every factory result against both the
  base port and its declared capabilities.

### Gotchas

- All 25 research strategy entries are currently disabled for the
  multi-strategy fleet. The legacy global report nevertheless relies on
  `gold_5m_v1`; removing that behavior would have broken a real compatibility
  contract even though the production config appeared inactive.
- Registration availability is checked without instantiating every plugin in
  runner preflight, preserving Chan's optional interpreter/dependency boundary.
  Factory shape/capabilities are enforced when built and exhaustively covered
  for the trusted built-in composition.
- `TechnicalRuleSignalEngine` and signal filters retain internal dispatch, and
  `DualTrackMachinePlanner` is still hard-wired into the production grid cycle.
  A8 removes application-level signal selection; it does not pretend those
  separate seams are complete.
- A8 has no visible product surface, so visual Evidence is not applicable.

### Verification

- Registry audit: frozen, 64-character fingerprint, 20 registered plugins, 25
  configured strategies, zero enabled or resolution failures.
- Focused A0-A8 architecture/conformance: `139 passed`.
- Strategy behavior acceptance: `88 passed`; dedicated signal/filter/backtest
  parity: `42 passed`.
- Full repository regression: `1752 passed, 7 skipped in 491.24s`.
- Changed-file Ruff and diff checks: clean.
- Architecture progress: `86%`, reproducible from the canonical audit table.
- No config, strategy parameter, execution authority, credential, order,
  position, account, or production-state mutation occurred.

## 2026-07-18 - A9 Strategy Proposal Port kickoff

### Objective and value

- Make current production grid proposal generation replaceable without letting
  a new model/provider replace plan validation, persistence, or risk semantics.
- Enable deterministic and What-if planners to consume the same trusted context
  and produce the same validated machine-plan contract.

### Decisions

- Keep `DualTrackMachinePlanner` as the trusted lifecycle service. Extract only
  untrusted proposal generation behind `StrategyProposalPort`.
- The core owns range-floor policy, validation, degraded fallback, plan writes,
  revisions, traces, and audit. Plugins receive no broker/order/risk authority.
- Use an explicit frozen registry; do not auto-load installed Python packages.
- Preserve `decision_provider` injection by wrapping it in the same newsletter
  adapter used by the configured production path.

### Gotchas

- A superficial registry around the whole stateful planner would make plugins
  own persistence and safety. A9 must instead isolate proposal generation.
- Forced replan failure preserves the existing locked plan; initial failure
  creates a transparent degraded no-trade plan.
- The primary worktree remains unrelated and active. A9 is isolated on top of
  the clean A8 branch and changes no live configuration or state.

### Baseline

- A8 full suite: `1752 passed, 7 skipped`.
- Planner/DT8 baseline: `58 passed`.

## 2026-07-18 - A9 Strategy Proposal Port complete

### Value delivered

- Production grid proposal generation is now replaceable through a versioned,
  authority-limited `StrategyProposalPort`. A deterministic planner, another
  model, or a What-if challenger can enter without editing the cycle runner or
  trusted plan lifecycle core.
- Safety semantics do not move with the plugin. The core still owns range-floor
  policy, validation, degraded no-trade fallback, persistence, forced-replan
  preservation, revisions, traces, and audit.
- The configured Codex/newsletter implementation is now one explicit adapter;
  prompt bytes, subprocess restrictions, and current trading behavior remain
  unchanged.

### Decisions

- Keep proposal generation separate from the A8 signal-analysis port. A signal
  engine explains market state; a proposal plugin drafts a production grid;
  neither gets execution, risk, persistence, or promotion authority.
- Use an explicit frozen registry and safe content fingerprint. Do not load
  arbitrary installed Python packages into the trading process.
- Preserve the direct `decision_provider` compatibility seam through the same
  adapter/composition path, while production construction selects the frozen
  runtime before stores or execution services are created.
- Treat every proposal result as untrusted. Copy only prompt-declared proposal
  fields; core lifecycle, provenance, revision, execution-start, review-change,
  and fallback metadata are not plugin-owned.
- Give plugins only declared external context. A deterministic plugin that does
  not require newsletter research neither reads nor receives it.

### Gotchas

- A registry around the entire stateful planner would have made adapters own
  validation and persistence; the extracted boundary is proposal-only.
- Opus found that a raw dictionary merge could preserve plugin-supplied
  `degraded`, `planning_error`, `execution_start`, or unrelated review metadata.
  This could not create a trade, but it could mislabel lifecycle/provenance and
  bypass the explicit-grid error path. The final core now uses a proposal-field
  allowlist before range policy and validation.
- `force=True` proposal failure must preserve the previous locked plan and write
  no revision; initial failure must persist an explicit no-trade degraded plan.
  Both behaviors remain covered.
- A frozen Protocol proves shape, not strategy correctness. New planners still
  require behavioral, replay, risk, and promotion evidence before production
  selection.
- A9 has no visible product surface. Visual Evidence is not applicable; tests,
  commits, documents, and the Opus receipt are trace material only.

### Verification

- Focused proposal/planner/fitness regression after review: `76 passed`.
- Focused A0-A9 architecture/conformance regression: `212 passed`.
- Final repository regression after the accepted review fix:
  `1763 passed, 7 skipped in 378.62s`.
- Changed-file Ruff and diff checks: clean.
- Verified Opus: `claude-opus-4-8`, session
  `114afe14-6c34-4355-baa6-f6d9d56e8221`, receipt
  `20260718T030459Z_fac8bdbd-56f5-4c7d-9546-54fd629e2eab.json`; no P0/P1,
  `SHIP`. One valid P2 was fixed and regression-tested before completion.
- Architecture progress is now `87%`, reproducible from the canonical audit.
- No strategy parameters, execution/risk/broker authority, credentials, orders,
  positions, accounts, or production state were changed.

## 2026-07-18 - A10 Backtest Ports kickoff

### Objective and value

- Make backtest engine selection explicit across Daily signal evidence,
  historical strategy ranking, and Nautilus Strategy Shadow without conflating
  their different contracts.
- Remove production Daily's implicit remote/mock fallback so an unavailable
  service cannot fabricate historical-looking evidence.

### Decisions

- Use one frozen, kind-aware registry with three narrow ports rather than one
  untyped universal backtest interface.
- Preserve all simulation math. Wrap LocalBacktester, StrategyBacktester,
  remote evaluation, synthetic context, and Nautilus Strategy Shadow as adapters.
- Follow NautilusTrader's config/run separation. Keep Nautilus in its isolated
  runtime and retain Strategy Shadow receipts, parity, causal boundaries,
  accounting, and non-authoritative storage.
- Keep synthetic evidence only as an explicit compatibility plugin. It is always
  degraded and promotion-ineligible; production Daily selects Local explicitly.
- Do not add Pluggy or automatic package discovery. Five trusted factories do
  not justify a new dependency or arbitrary installed-code authority.

### Gotchas

- `analysis_fallback_to_mock` currently affects both Copilot and BacktestClient.
  The cutover must stop using it for backtests without changing Copilot behavior.
- Thin evidence remains context, not a paper-ticket blocker. This milestone must
  not silently change trade frequency or risk policy.
- Historical MA signals are an acknowledged price-only approximation and must
  remain labelled `faithful_signals=false`.
- The primary worktree is unrelated and active. A10 is isolated on top of the
  clean A9 branch and changes no live configuration or state.

### Baseline

- A9 full suite: `1763 passed, 7 skipped`.
- Backtest/local/ranking/Strategy Shadow behavior: `27 passed`.

## 2026-07-18 - A10 Backtest Ports complete

### Value delivered

- Signal evidence, historical strategy ranking, and Strategy Shadow execution
  replay now have three distinct versioned ports behind one kind-aware frozen
  registry. A replacement backtester no longer requires editing Daily, the
  leaderboard, Strategy Shadow, or the paper-only parameter experiment queue.
- Production Daily explicitly selects local historical evidence. Empty history
  stays transparent `thin`/`no_trade`; it cannot silently fall through to a
  remote service or fabricated sample.
- Every result records trusted plugin identity, evidence tier, content-bound
  input identity, registry fingerprint, degradation, and source eligibility for
  promotion. Core services discard plugin attempts to forge these fields or
  historical report identity.

### Decisions

- Keep three narrow contracts rather than a universal `run(dict)` interface:
  signal context, historical ranking, and execution replay have different
  inputs, outputs, and authority.
- Keep Local, remote, synthetic context, event-driven ranking, and Nautilus
  Shadow as explicit adapters. Do not add Pluggy or arbitrary installed-package
  discovery for five trusted factories.
- Preserve `BacktestClient` only as a compatibility facade. It may retain its
  explicit remote-to-synthetic fallback for old test/legacy callers, but no
  production application imports it.
- Treat `promotion_eligible` as evidence-source eligibility, not promotion
  approval. Verdict, sample threshold, regime, out-of-sample comparison, and
  human/automatic promotion policy remain independent gates.
- Preserve all Local, StrategyBacktester, cost, MA approximation, and Nautilus
  Shadow math. A10 changes selection and provenance, not strategy behavior.

### Gotchas

- `analysis_fallback_to_mock` still configures Copilot compatibility. Its name is
  historical; A10 removes it only from backtest selection and must not silently
  change Copilot behavior.
- An input hash that omitted local stop/target/hold settings would not reproduce
  a result. `SignalBacktestRequest` therefore binds the exact backtest config as
  well as signal, analysis, bars, and run context.
- Historical plugin output must be allowlisted. Spreading an arbitrary result
  mapping after core fields would let a plugin relabel strategy identity or
  provenance even without changing metrics.
- Opus found that `StrategyExperimentQueue` still constructed
  `LocalBacktester` directly. It is the same signal-evidence semantic, so its
  variant config was moved through the signal port rather than inventing a
  fourth port.
- A capable plugin can still be degraded by design; degradation always wins and
  prevents source eligibility. Zero-loss historical runs intentionally preserve
  infinite profit factor instead of treating it as malformed.
- A10 has no visible product surface. Visual Evidence is not applicable; tests,
  commits, documents, and the Opus receipt are trace material only.

### Verification

- Final repository regression: `1788 passed, 7 skipped in 435.68s`.
- Post-review Backtest/Shadow/fitness defense pack: `36 passed`; Local variant
  config round-trip and simulation parity are included.
- Changed-file Ruff, JSON config validation, architecture fitness, and diff
  checks: clean.
- Verified Opus: `claude-opus-4-8`, session
  `7fa956ce-3649-4397-9339-b2eac5a22e6d`, receipt
  `20260718T035024Z_9dfbdc05-b3c7-4dc1-b349-8833b83ef345.json`; no P0-P2,
  `SHIP`. Three P3 defense tests were added and the remaining direct Local caller
  was migrated before the final full regression.
- Architecture progress: `90%`, reproducible from the canonical audit table.
- No strategy parameters, risk rules, execution/broker authority, credentials,
  orders, positions, accounts, or production state were changed.

## 2026-07-18 - A11 Execution Plugin Registry kickoff

### Objective and value

- Make Legacy, Nautilus, and continuous Shadow execution composition replaceable
  through explicit factories without moving cutover or money authority into
  plugins.
- Remove concrete engine selection from production applications while preserving
  every current paper-execution and reconciliation behavior.

### Decisions

- Follow NautilusTrader's configuration-plus-factory registration pattern.
- Extract the port and Legacy adapter physically; retain the old module only as
  a compatibility facade.
- Keep attended approval, runtime path, parity/shadow gate, override
  acknowledgement, and paper-only policy in trusted composition.
- Register factories explicitly and freeze before use; do not auto-load installed
  packages.

### Gotchas

- Shadow is non-authoritative decoration and must never replace the Legacy return
  value or account truth when its runtime fails.
- Registry metadata is not cutover evidence and cannot bypass the existing
  seven-cycle/parity/fee/precision gates.
- The primary worktree remains unrelated and active. A11 is isolated on top of
  completed A10 and changes no live config or state.

### Baseline

- A10 full suite: `1788 passed, 7 skipped`.
- Execution boundary pack: `69 passed, 1 skipped`.

## 2026-07-18 - A11 Execution Plugin Registry complete

### Value delivered

- Paper execution is now a real replaceable boundary: one provider-free port,
  separate Legacy/Nautilus/Shadow adapters, a frozen content-hashed registry,
  and one trusted production composition root.
- Adding a paper engine is an explicit registration plus conformance task. The
  cycle runner, Dashboard commands, control plane, and attended cutover no
  longer need an engine-name branch or concrete-adapter import.
- The old `dualtrack_execution_adapter.py` path remains available, but contains
  only re-exports. No execution, selection, gate, or accounting logic remains
  there.

### Decisions

- Keep plugin behavior and money authority separate. Descriptors can describe
  capabilities, but config cannot self-approve Nautilus, supply authoritative
  runtime, bypass the cutover gate, or claim real-money eligibility.
- Keep Shadow as a wrapper around one authoritative port. Its missing or broken
  runtime is visible, but its return value, ledger, positions, P&L, and
  reconciliation never replace authoritative truth.
- Keep explicit registration instead of Pluggy/entry-point auto-discovery.
  Three trusted factories do not justify arbitrary installed code in an
  execution process.
- Treat the registry fingerprint as plugin provenance, not cutover evidence.
  Runtime, fee, precision, reconciliation, and parity gates remain independent.

### Gotchas

- A frozen registry prevents late mutation, but a future developer could still
  misdeclare a known adapter's safety flags at registration time. A core
  invariant now ties the known Nautilus implementation to attended approval,
  isolated runtime, and cutover-gate requirements.
- The first parity-path expansion covered every moved A11 file but omitted the
  pre-existing Shadow adapter which computes cutover qualification. Opus marked
  this P3; Shadow execution, reconciliation, and cutover-status semantics are
  now included so their changes invalidate stale parity evidence.
- `inspect_execution_engine_state()` intentionally constructs a candidate
  without granting authority. It returns only snapshot/reconciliation data; a
  defense test proves it neither exposes the adapter nor invokes order submit.
- Factory implementation-string checks protect accidental false provenance,
  not a malicious trusted registrant who controls Python class metadata. That
  is acceptable under the explicit trusted-registration threat model.
- A11 has no visible product surface and deploys no local service. Visual
  Evidence is not applicable; tests, commits, documents, and the Opus receipt
  are trace material only.

### Verification

- Final repository regression after Opus hardening:
  `1800 passed, 7 skipped in 404.82s`.
- Cross-layer execution/control/Dashboard pack before review:
  `179 passed, 1 skipped`; post-review policy/parity defense pack:
  `58 passed`; promotion/Shadow/runtime pack: `33 passed, 7 skipped`.
- Changed-file Ruff and `git diff --check`: clean.
- Verified Opus: `claude-opus-4-8`, session
  `58e32a97-c86f-4fd7-9d2f-9f463aa72d40`, receipt
  `20260718T044941Z_51c01cb4-c4a4-43ed-9c52-34c87b2866ad.json`; `SHIP`, no
  P0-P2. Three useful P3 defenses were applied before the final full run.
- Architecture progress: `91%`, reproducible from the canonical seven-row audit
  (`635 / 7 = 90.7%`, rounded to `91%`).
- No strategy parameters, risk rules, credentials, live configuration, orders,
  positions, accounts, or production state were changed.

## 2026-07-18 - A12 Accounting Adapter Registry kickoff

### Objective and value

- Extract Binance USD-M and Tiger accounting interpretation from the generic
  P&L core, then register each source explicitly behind one canonical snapshot
  port.
- Make a new broker/account source an adapter-plus-conformance change rather
  than a provider branch inside risk, Dashboard, or reconciliation code.

### Decisions

- Preserve `accounting-snapshot-v1`, exact snapshot IDs, rounding, issue order,
  completeness, and unknown-is-not-zero behavior.
- Reuse the explicit frozen-registry pattern already proven in A8-A11; do not
  add Pluggy or entry-point discovery.
- Keep execution-engine accounting provider-free and separate from broker
  source composition. Retain the old module as a compatibility facade only.

### Gotchas

- Binance user trades do not prove round-trip lifecycle, so trade and
  entry/exit counts must remain unknown.
- Tiger is aggregate account evidence; missing orders, fills, fees, funding,
  exposure, and ending cash must never become zero.
- Fail-honest accounting is additive to source persistence. Projection failure
  must remain visible without suppressing the reconciliation or sync receipt.
- The primary worktree remains unrelated and active. A12 is isolated on top of
  completed A11 and changes no live config or production state.

### Baseline

- A11 full suite: `1800 passed, 7 skipped`.
- Accounting/risk/reconciliation/Dashboard pack: `113 passed`.

## 2026-07-18 - A12 Accounting Adapter Registry complete

### Value delivered

- Broker accounting sources are now replaceable without editing risk,
  Dashboard, execution, reconciliation, or account-sync application modules.
  Binance USD-M and Tiger each own an isolated read-only mapping adapter behind
  one frozen content-hashed source registry.
- Execution-engine P&L projection is provider-free. It imports no venue mapper;
  the old `accounting_projection.py` path contains only compatibility re-exports.
- Exact economic behavior is preserved: Binance and Tiger retain their A11
  canonical payload hashes, while unknown/malformed evidence remains blocked
  and `None` rather than fabricated zero.

### Decisions

- Keep execution accounting generic because Legacy and Nautilus already emit
  the same versioned snapshot. Apply source registration only where venue field
  interpretation genuinely differs.
- Treat broker accounting plugins as trusted read-only anti-corruption
  adapters. The registry structurally validates implementation identity,
  source aliases, schema, capability, source name, and canonical snapshot type;
  it does not pretend to prove the economic honesty of trusted registered code.
- Keep fail-honest fallback in provider-neutral composition. Registered
  descriptor metadata supplies Tiger versus Binance source schema and default
  currency without restoring an inline provider branch.
- Preserve explicit registration and startup freeze; do not auto-discover
  installed code in a money-reporting process.

### Gotchas

- Snapshot IDs bind the entire canonical payload, including order, issue, and
  limitation ordering. The frozen A11 IDs therefore guard more than headline
  P&L values.
- Binance's present commission/funding map may legitimately contain numeric
  zero; an absent map remains `None`. These states must not be collapsed.
- Tiger's net liquidation is equity evidence, not ending cash. Orders, fills,
  positions, gross P&L, fees, funding, exposure, and lifecycle counts remain
  unknown.
- Opus found that the generic forbidden-import scanner did not name the new
  concrete accounting adapter module paths. No live dependency violation
  existed, but a future core leak could have escaped that guard. Both concrete
  module names are now explicitly forbidden and pinned by a test.
- Currency is intentionally not forced equal to descriptor default currency:
  adapters may observe a real balance asset. The registry validates source and
  schema provenance, while the registered adapter owns that observed label.
- A12 has no visible product surface and deploys no service. Visual Evidence is
  not applicable; tests, commits, diffs, documents, and the Opus receipt are
  trace material only.

### Verification

- Final repository regression after Opus hardening:
  `1813 passed, 7 skipped in 400.54s`.
- Pre-review accounting/risk/reconciliation/Dashboard/Shadow/parity pack:
  `181 passed`; post-review defense pack: `31 passed`.
- Frozen A11 IDs:
  - Binance: `accounting-07c106a8682cb8be79954801823763d97b0dccff5086e66a0ac9a2a48055b3cb`.
  - Tiger: `accounting-a8ba1a6cf165aa0fed9cf6f720d09033e512378adb1b86bfabd6d7a0ca201a68`.
- Git source relocation checks for execution core, Binance mapping, Tiger
  mapping, and broker counts each returned `exit=0` with zero diff output.
- Changed-file Ruff, architecture fitness, parity-path coverage, and
  `git diff --check`: clean.
- Verified Opus: `claude-opus-4-8`, session
  `76dfbe46-edcf-4315-8f72-dc6b3af13250`, receipt
  `20260718T053243Z_06b5df69-2399-41cc-82cb-4a65eaa59b85.json`; `SHIP`, no
  P0-P2. Its conditional verbatim-diff gate was independently closed with Git.
- Risk/accounting/reconciliation improved from `90/100` to `95/100`. Overall
  progress remains the honest rounded `91%` (`640 / 7 = 91.4%`).
- No strategy parameters, risk thresholds, credentials, live configuration,
  orders, positions, accounts, or production state were changed.

## 2026-07-18 - A16 Market Envelope authority cutover kickoff

### Decision

- Promote the existing versioned `MarketDataEnvelope` from shadow comparison to
  production authority only after real V2 same-response evidence, then route
  all `DatafeedMarketRepository` bar reads through the same mapper.
- Preserve explicit shadow mode as a diagnostic rollback, but make envelope
  authority the canonical and default path after cutover.
- Keep v1/v2 compatibility at the adapter boundary. Datafeed owns session and
  source truth; Trading Orchestrator validates and projects it without a second
  candle representation or duplicate HTTP request.

### User value

- A conforming datafeed adapter can replace Binance without changing strategy,
  backtest, risk, execution, or Dashboard interpretation of market bars.

### Live evidence

- Started datafeed commit `92e5e8c` on isolated port 8101 with a temporary DB;
  production port 8100 and trading processes were untouched.
- Six real Binance USD-M XAUUSDT 1m shadow responses passed 458 same-response
  comparisons each with zero differences. One real authoritative rehearsal
  also passed: `3,206` compared fields, zero drift in total.
- One separate authoritative request returned the known Binance upstream 502;
  the consumer returned `blocked` with zero bars. Availability remains a
  separate Debug concern and was not reclassified as contract success.

### Gotchas

- Historical and session-closed envelopes can be valid but not
  execution-ready; the repository must validate them without inventing
  freshness.
- A successful V2 sample proves schema/session compatibility, not continuous
  Binance uptime.
- Datafeed A1 is not deployed by this milestone; consumer v1/v2 compatibility
  and deployment sequencing remain explicit.
- A16 has no visible UI surface. Visual Evidence is not applicable unless the
  scope changes.

### Baseline

- A15 repository: `1852 passed, 7 skipped`.
- Market/envelope/DualTrack/architecture pack: `99 passed`.

## 2026-07-18 - A13 Binance USD-M transport extraction kickoff

### Decision

- Start the venue-package strangler with the highest-value, lowest-semantic-
  churn Binance boundary: endpoint resolution, instrument mapping, signing,
  public ExchangeInfo, HTTP request construction, and response decoding.
- Keep execution lifecycle, activation, canonical risk, same-cycle
  reconciliation, local accounting mirrors, protective policy, and ambiguous
  submission recovery in the proven adapter for this milestone.
- Preserve every existing private compatibility method as a thin delegate.
  Demo, testnet, canary, kill-switch, and direct operational callers therefore
  keep the same behavior while networking becomes venue-owned.
- Inject the existing opener and a clock into the transport. Do not introduce
  a second HTTP stack, background client, retry policy, or new plug-in system.

### User value

- Binance networking can be replaced or tested without editing the Broker Port
  or cross-venue orchestration, while the currently running grid's money and
  order-safety semantics remain unchanged.

### Gotchas

- HMAC input depends on parameter insertion order; signing parity includes the
  exact query/body shape, not only decoded values.
- Signed GET and signed mutation requests deliberately use different wire
  placement. The extraction must preserve both.
- `BinanceDemoBrokerAdapter` has a deliberate signed-GET response envelope and
  override. Base transport delegation must not shadow subclass overrides.
- Demo/testnet/mainnet share call names but not authority or default endpoint.
- The opener also serves reconciliation and is an established test seam.
- A13 is transport isolation, not complete Binance execution extraction. The
  remaining lifecycle/protection body stays explicit follow-on debt.
- No visible product surface changes in A13; Visual Evidence is not applicable
  unless the scope changes.

### Baseline

- A12 full repository: `1813 passed, 7 skipped`.
- Broker Port plus Binance mainnet/demo/testnet regression: `100 passed`.

## 2026-07-18 - A13 Binance USD-M transport extraction closure

### Outcome

- Binance USD-M endpoint ownership, base URL and instrument mapping, public
  ExchangeInfo normalization, credentials, HMAC signing, signed GET/mutation
  construction, timeouts, and response decoding now live in the isolated
  venue transport.
- `LiveBrokerAdapter` and `BinanceDemoBrokerAdapter` retain the old private call
  names only as compatibility delegates. No activation, risk, reconciliation,
  lifecycle, protective-order, emergency-close, or accounting behavior was
  moved or relaxed.
- Transport construction preserves the original dynamic seams: in-place
  config changes remain visible, while config or opener replacement rebuilds
  the immutable transport. The injected opener is never wrapped.
- Provider-free contract kernels explicitly forbid `services.venues` imports.
  Static tests also prevent Binance HMAC, timestamp/signature construction,
  base URLs, and broker endpoints from returning to the cross-venue facade.

### Opus adversarial review and hardening

- Verified `claude-opus-4-8`, session
  `d855d5a4-6301-432f-ae49-a43c8d1bf452`, receipt
  `20260718T060856Z_99325593-7454-4b5e-a18c-4d98548de002.json`: `SHIP`, no
  P0-P2.
- Closed P3: the public ExchangeInfo contract now proves its GET carries no
  API-key or Authorization header.
- Closed P3: position-risk endpoint ownership moved into the transport catalog;
  demo, testnet, and both operational kill switches call the semantic
  compatibility method rather than repeating the literal.
- Exact A12 parameter insertion-order parity is documented as inferred rather
  than independently byte-proved. Correctness is construction-safe because the
  transport signs the exact same query string it transmits; pre-A13 end-to-end
  tests independently anchor endpoints, methods, body-vs-URL placement,
  routing, and timeout.

### Verification

- Final repository regression after Opus hardening:
  `1822 passed, 7 skipped in 419.51s`.
- Pre-review transport/broker/Binance/architecture pack: `124 passed`;
  post-review mainnet/demo/testnet/kill-switch defense pack: `101 passed`.
- Changed-file Ruff and `git diff --check`: clean.
- Live execution/broker improves from `90/100` to `92/100`; overall architecture
  progress is now the reproducible rounded `92%` (`642 / 7 = 91.7%`).
- A13 has no visible product surface and deploys no service. Under the Evidence
  Contract, Visual Evidence is not applicable; tests, commits, docs, diffs, and
  the Opus receipt are trace material only.
- No strategy parameters, risk thresholds, credentials, live configuration,
  orders, positions, accounts, or production state were changed.

## 2026-07-18 - A14 OANDA and MT5 Broker Adapter extraction kickoff

### Decision

- Extract OANDA REST and MT5 file bridge as standalone
  `BrokerExecutionPort` adapters before moving the much larger Binance
  lifecycle/protection body. Removing the small cross-venue branches first
  reduces the final compatibility monolith with lower semantic risk.
- Give each provider an explicit lazy registry factory. Manual and unknown
  gateways alone retain the legacy factory; config still cannot grant an armed
  unknown path.
- Keep `LiveBrokerAdapter` direct/private seams as lazy compatibility delegates
  until operational callers have moved through composition. Do not keep a
  second OANDA or MT5 implementation in the facade.
- Preserve current gates and artifacts exactly. A14 changes dependency
  direction and physical ownership, not venue authority, order types, retries,
  risk, accounting, or reconciliation.

### User value

- OANDA and MT5 can be replaced independently without changing strategy, risk,
  Binance/Tiger, accounting, or Dashboard code, while existing safety runbooks
  keep working during migration.

### Gotchas

- OANDA dry-run intentionally needs no credentials; real submission needs both
  non-placeholder env values and `real_money_ready` before network I/O.
- OANDA bearer values must never enter descriptors, receipts, repr, or logs.
- MT5 non-dry outbox files are executable intent and remain activation-gated.
- MT5 preflight writes directories/templates and relative paths resolve from
  repository `ROOT`.
- Existing order IDs and truthiness-based quantity fallback are compatibility
  contracts in this milestone.
- Cached facade delegates must follow config/opener object replacement and
  in-place config mutation.
- No visible surface changes; Visual Evidence is not applicable unless scope
  changes.

### Baseline

- A13 repository: `1822 passed, 7 skipped`.
- OANDA/MT5/Broker/smoke/safety/audit pack: `63 passed`.

## 2026-07-18 - A14 OANDA and MT5 Broker Adapter extraction complete

### Outcome

- OANDA REST and the MT5 file bridge are now standalone
  `BrokerExecutionPort` adapters selected directly by the frozen broker
  registry. Replacing either no longer requires editing the cross-venue live
  broker implementation.
- Operational smoke, activation-safety, and completion-audit consumers resolve
  through composition. Existing `LiveBrokerAdapter` OANDA/MT5 construction and
  private helper names remain compatibility delegates over the same concrete
  instances, not duplicate implementations.
- OANDA owns its credential checks, practice/live endpoint, account quoting,
  instrument/payload/TIF translation, HTTP POST, response mapping, and durable
  request artifact. MT5 owns root resolution, readiness docs/templates,
  bridge-order creation, durable request artifact, and receipt correlation.

### Decisions

- Preserve activation, dry-run, idempotency, truthiness quantity fallback,
  request/receipt, and provider response behavior exactly. A14 changes physical
  ownership and composition authority, not trading semantics.
- Keep one explicit adapter per provider instead of inventing a generic
  transport abstraction: OANDA HTTP and MT5 executable filesystem intent have
  different trust and failure models.
- Keep legacy delegate seams until remaining callers are exhausted. AST tests
  now prove each named OANDA/MT5 compatibility helper contains only one return
  into its concrete adapter.

### Opus adversarial review and hardening

- Verified `claude-opus-4-8`, session
  `5264e18c-30d7-453a-b0b1-8ac5c6fb1245`, receipt
  `20260718T065100Z_e61f4fc6-a8f3-4404-b220-fff5a2d67a48.json`: verdict `SHIP`,
  no P0/P1.
- Closed P2 false confidence: the realistic OANDA fake response now includes
  `accountID`. The test proves the bearer token never persists while explicitly
  preserving the pre-existing verbatim broker-response contract instead of
  claiming account-ID redaction that the system does not provide.
- Closed P3: literal operational-import checks are now AST-based, all facade
  compatibility methods are structurally pinned as thin delegates, and the
  redundant outer live-env read was removed from provider-specific preflight.

### Gotchas

- OANDA's bearer token exists only in the Authorization header and is never
  persisted. A real OANDA response may echo the non-secret account identifier,
  and the durable request currently records that response verbatim. Redacting
  account identifiers would be a deliberate privacy-contract change, not an
  extraction parity fix.
- MT5 preflight intentionally creates directories and documentation, but no
  executable `*.json` order intent can be written before `real_money_ready`.
- Cached facade delegates share the config dictionary for in-place mutation
  visibility and rebuild on config/opener identity or live/dry state changes.
- A14 has no visible product surface and deploys no service. Under the Evidence
  Contract, Visual Evidence is not applicable; tests, commits, docs, diffs, and
  the Opus receipt are trace material only.

### Verification

- Final repository regression after Opus hardening:
  `1837 passed, 7 skipped in 401.49s`.
- Focused OANDA/MT5/Broker/architecture pack: `78 passed`; wider cross-provider
  broker/architecture pack: `171 passed`; post-review defense pack: `54 passed`.
- Changed-file Ruff, architecture fitness, and `git diff --check`: clean.
- Live execution/broker improves from `92/100` to `94/100`; overall architecture
  progress remains the honest `92%` (`644 / 7 = 92.0%`).
- No strategy parameters, risk thresholds, credentials, live configuration,
  orders, positions, accounts, or production state were changed.

## 2026-07-18 - A15 Binance execution adapter extraction kickoff

### Decision

- Finish the broker-facade strangler in dependency-safe order: first remove
  Tiger's inheritance from `LiveBrokerAdapter`, then move the shared Binance
  mainnet/demo/testnet lifecycle and protection body into a venue-owned adapter.
- Reuse the frozen Broker Port registry, Binance transport, canonical risk,
  lifecycle store, and compatibility-facade pattern. Introduce no second
  service locator, transport stack, or generic broker abstraction.
- Keep direct/private call seams during migration because canaries, kill
  switches, demo/testnet subclasses, and tests use them as operational seams.
  Production composition must nevertheless construct the concrete adapter.

### User value

- Binance and Tiger become independently replaceable without weakening the
  exact real-money, demo, testnet, protection, reconciliation, or recovery
  behavior already proven in production-facing workflows.

### Gotchas

- Tiger is physically separate by filename but still inherits the whole live
  broker facade and ten deterministic/helper behaviors from it.
- Direct legacy Tiger non-dry mode must remain fail-closed even though the
  explicitly armed Tiger paper adapter supports TradeClient submission.
- Demo/testnet share the Binance lifecycle but deliberately differ from
  mainnet authority and activation. Relocation must not collapse those gates.
- Private monkeypatch seams are part of current test and runbook compatibility.
- No visible surface changes are planned; Visual Evidence is not applicable
  unless scope changes.

### Baseline

- A14 repository: `1837 passed, 7 skipped`.
- Tiger/Binance/Broker/architecture pack: `159 passed in 167.63s`.

## 2026-07-18 - A15 Binance execution adapter extraction complete

### Outcome

- Tiger paper, Binance USD-M, OANDA REST, and MT5 file bridge now have
  independent concrete `BrokerExecutionPort` adapters. No venue inherits the
  cross-provider facade or another venue's implementation.
- `BinanceUsdmBrokerAdapter` owns mainnet/demo/testnet preflight, canonical
  risk and reconciliation gates, order lifecycle, ambiguous-submit recovery,
  TP/SL protection, emergency close, cancellation, and protective recovery.
  Mainnet composition returns it directly; demo/testnet inherit only this
  venue-owned base.
- `LiveBrokerAdapter` now uses composition instead of inheritance. It retains
  manual behavior and thin compatibility delegates only; it contains no
  Binance endpoint, signing, payload, lifecycle, protection, or recovery body.

### Decisions

- Preserve every order, receipt, journal, config/opener mutation, private
  monkeypatch, and direct-call compatibility seam while moving ownership.
  A15 changes dependency direction, not execution semantics or authority.
- Keep configured live composition separate from explicit demo/testnet
  composition. A stored non-mainnet environment label cannot authorize a
  non-dry order: the production base still requires `real_money_ready` before
  any POST.
- Freeze capabilities at facade construction. Mutating a provider label later
  cannot grant Tiger a Binance cancellation or protective-recovery capability.

### Opus adversarial review and hardening

- Verified `claude-opus-4-8`, session
  `abd8ceef-e3bd-4a07-8f7d-4d2a8e570339`, receipt
  `20260718T074050Z_7564bd48-7c30-461a-8b1a-5bb2d48c7c2e.json`: verdict
  `SHIP`, no P0/P1.
- Closed P2 architecture honesty: `LiveBrokerAdapter` no longer inherits
  `BinanceUsdmBrokerAdapter`; MRO and AST tests prove true composition and no
  lifecycle implementation in the facade.
- Closed P2 configured-path ambiguity with an explicit security contract and
  demo/testnet-label tests that prove the production activation gate blocks
  before network POST.
- Closed P3 proof gaps with both dry-run artifact parity and non-dry activation
  parity between the concrete adapter and legacy facade.

### Gotchas

- The configured live factory is a production control path, not a shortcut for
  starting demo/testnet. Dedicated non-mainnet runners must use explicit
  composition context.
- Legacy callers monkeypatch private Binance methods on facade instances. The
  compatibility adapter intentionally mirrors callable overrides into its
  cached concrete delegate until those callers migrate to public ports.
- `LiveBrokerAdapter` remains as cleanup debt, but it is now a facade rather
  than a cross-venue implementation owner. Deleting it is a separate low-risk
  consumer migration, not unfinished venue isolation.
- A15 has no visible product surface and deploys no service. Under the Evidence
  Contract, Visual Evidence is not applicable; tests, commits, docs, diffs, and
  the Opus receipt are trace material only.

### Verification

- Pre-review full repository: `1849 passed, 7 skipped in 390.89s`.
- Post-review cross-provider safety pack: `192 passed in 164.04s`.
- Final repository after all review hardening:
  `1852 passed, 7 skipped in 386.35s`.
- Changed-file Ruff and `git diff --check`: clean.
- Live execution/broker improves from `94/100` to `98/100`; overall architecture
  progress is now the reproducible rounded `93%` (`648 / 7 = 92.6%`).
- No strategy parameters, risk thresholds, credentials, live configuration,
  orders, positions, accounts, or production state were changed.

## 2026-07-18 - A16 Market Envelope authority cutover complete

### Outcome

- The canonical pipeline and omitted-mode default now make the versioned
  `MarketDataEnvelope` authoritative. Shadow remains an explicit diagnostic
  mode and cannot silently become production authority.
- `DatafeedMarketRepository` bar, range, latest, quote, and point-in-time reads
  all project through `load_envelope()` and the sole v1/v2 mapper. The duplicate
  raw candle-to-`Bar` interpreter was deleted.
- Invalid HTTP-200 contracts and upstream 502 failures propagate closed with one
  request and no local fallback. Existing provider, quality, ordering, and
  start/end behavior remains frozen by behavior-aware tests.

### Opus adversarial review and hardening

- The first Opus attempt ended in `api_error` and was not counted: actual model
  `claude-opus-4-8`, session `1593c5f8-d53e-404e-bc02-27d51f92b095`, receipt
  `20260718T083010Z_9f45d7ad-f84a-461c-a515-356f37828399.json`.
- Verified retry used `claude-opus-4-8`, session
  `70a1bdef-d104-4a01-9092-eacd5ced5958`, receipt
  `20260718T083929Z_466da7a5-defb-47ff-bb38-d82d9d624fa8.json`; verdict
  `SHIP WITH FIXES`, no P0/P1.
- Closed the in-scope proof gaps: the repository fake now honors range/end/limit
  semantics, point reads prove the last eligible bar rather than merely echoed
  request arguments, and a direct 502 test proves one-call/no-fallback behavior.

### Testing scope decision

- A16 had a small diff but medium/high semantic blast radius: it changed the
  default authority and every production datafeed bar projection. One full
  regression was proportionate and passed.
- Opus hardening changed tests and documentation only. A second full regression
  would have been over-testing, so closure used the 81-test market/envelope/
  DualTrack defense pack plus architecture fitness, Ruff, and diff checks.
- Future small compatibility or documentation changes in this area should keep
  using targeted upstream/downstream tests. Repeat the full suite only when a
  core authority, mapper, fail-closed, or cross-application behavior changes.

### Gotchas

- Production datafeed port 8100 still served V1 during the isolated rehearsal;
  datafeed A1 V2 deployment is a separate operational change. The consumer
  safely accepts both frozen versions.
- `load_bars_between()` still asks for one page capped at 60,000 bars. Opus
  correctly identified possible silent truncation for longer windows. Fix it
  with a versioned cross-repository continuation/pagination contract; do not
  hide a second fetch/mapping path in this repository.
- V1 infers continuous-market semantics when freshness is present. Keep that
  migration behavior only until V2 deployment is established, then retire it
  explicitly.
- The live V2 rehearsal proved contract parity on successful Binance reads, not
  upstream availability. The separate Binance 502 remained blocked and is not
  reclassified as a contract failure.
- A16 has no visible surface. Under the Evidence Contract, Visual Evidence is
  not applicable; tests, receipts, commits, and logs are trace material only.

### Verification

- Real isolated V2 rehearsal: six shadow responses and one authoritative
  response, 458 same-response comparisons each, `3,206` total with zero drift.
- Pre-review focused cutover pack: `168 passed`; datafeed A1 suite: `86 passed`.
- One full repository regression after production semantics changed:
  `1860 passed, 7 skipped in 390.67s`.
- Post-review defense pack: `81 passed in 0.34s`; changed-test Ruff and
  `git diff --check`: clean.
- Data download improves from `90/100` to `95/100`; data cleaning/quality from
  `85/100` to `95/100`. Overall architecture is the reproducible rounded `95%`
  (`663 / 7 = 94.7%`).
- No strategy parameters, risk thresholds, credentials, production process,
  order, position, account, or live state was changed.

## 2026-07-18 - A17 Risk policy and decision store extraction kickoff

### Decision

- Separate the canonical risk port, paper policy adapter, live guardrail bridge,
  and file audit store into distinct ownership boundaries.
- Select normalized risk policy through the same explicit frozen-registry and
  composition-root pattern already proven by strategy, backtest, execution,
  accounting, and broker modules.
- Bind every request to the actually composed evaluator metadata. An explicit
  empty, unknown, duplicate, late-mutated, or identity-mismatched plugin fails
  closed; there is no fallback from bad explicit configuration.
- Keep the audit store deliberately narrower than a repository: it may persist
  a validated decision but may never return reusable permission.

### User value

- A risk policy can be swapped without editing Strategy Control or execution,
  while a storage change cannot alter whether money is allowed to move.

### Mature pattern

- Official Nautilus execution documentation confirms its RiskEngine sits on
  submit/modify and owns order-level validation such as precision, balance,
  reduce-only, rate limits, and trading state. A17 keeps this mature defense and
  does not rebuild it; the application port remains responsible for grid and
  portfolio policy above execution.
- Reuse the repository's frozen plugin registries. Do not add a framework,
  service locator, event bus, or store registry with only one real adapter.

### Testing decision

- Focused baseline: `143 passed` across risk contract/port, Strategy Control,
  Binance/Tiger broker, Dashboard risk API, and architecture fitness.
- This is a money-safety composition change. Run targeted tests during work,
  obtain Opus review, then run at most one final full suite after valid core
  findings are closed. Test/document-only closure changes do not justify a
  second full run.

### Gotchas

- Moving the evaluator intentionally changes its source hash and derived IDs;
  this must not change metrics, limits, blockers, outcomes, or permissions.
- Safe reduce-only/cancel actions and stale-state rechecks are non-negotiable.
- `current.json` remains observability evidence and never authorization.
- A17 changes no visible UI and no production state. Visual Evidence is not
  applicable unless scope changes.

## 2026-07-18 - A17 Risk policy and decision store extraction closure

### Outcome

- `RiskDecisionPort` is now a canonical contract rather than a policy/store
  implementation container. Paper-grid policy, pure economics helpers,
  live-money translation, and file audit persistence have distinct ownership.
- One frozen `risk-policy-plugin-v1` registry binds configured name,
  implementation, capabilities, evaluator version, code hash, and source hashes.
  Production composes `paper_grid_risk`; explicit bad configuration and runtime
  descriptor drift fail before trading mutation.
- The file decision store implements validated append-only persistence only.
  It has no evaluate, authorize, or reusable-permission read surface.
- Strategy Control, Binance, and Tiger obtain policies, stores, and the live
  bridge from one composition root. Every grid request binds the policy and
  evaluator metadata returned by the actually selected port.

### Opus adversarial review and hardening

- Verified `claude-opus-4-8`, session
  `08a6d7b7-4cfe-4856-82e8-c06da714a9ea`, receipt
  `20260718T102007Z_5f754f88-a4d5-4dae-9b92-2a395686d314.json`; verdict
  `SHIP WITH FIXES`, no P0/P1/P2.
- Limited evaluator identity to files that actually determine evaluation,
  removed inert runtime descriptor copies, and required the live bridge to
  receive its store from composition instead of constructing a concrete store.
- Closed the suggested fail-open edge: `allows_new_order=true` grants exposure
  only with legacy `READY`, or the exact intentional paper-route `SKIPPED`
  reason. Any other status becomes `legacy_guardrail_status_invalid`.
- Kept the explicit capability-callability check. Runtime Protocol conformance
  checks structural presence; the registry additionally verifies that every
  capability promised by the descriptor is executable.

### Testing scope decision

- Focused implementation validation reached `150 passed`; a narrower
  intermediate subset reached `144 passed`. The repetition was more
  conservative than necessary and is recorded as light over-testing.
- Post-Opus core changes used the minimum affected pack: `41 passed`; the
  architecture/document score update used `17 passed`. No full suite was run
  during either small-fix iteration.
- Because A17 changes the composition that authorizes exposure across Strategy
  Control and both live brokers, exactly one final repository suite is allowed
  after all core hardening. Documentation-only closure after that suite does
  not trigger another full run.

### Gotchas

- A custom policy must register source-bound evaluator identity before registry
  freeze. Supplying a structurally valid object with a different implementation
  or evaluator is deliberately rejected.
- The live bridge's exact `SKIPPED` exception preserves the existing
  guardrail-disabled paper route. Broadly accepting `SKIPPED` would reopen the
  fail-open condition.
- Opus identified a pre-existing debug concern outside this extraction:
  Dashboard network-order preparation reads market state before canonical
  action classification. A stale market may therefore block cancel or flatten
  too early. Cancellation and flatten pricing have different needs, so resolve
  this through a dedicated safe-action market-gate audit, not an incidental
  risk-policy edit.
- No visible surface changed. Under the Evidence Contract, Visual Evidence is
  not applicable; commits, tests, docs, and the Opus receipt are trace material.

### Verification

- The one final repository suite passed: `1869 passed, 7 skipped in 389.50s`.
  It was not repeated after documentation-only closure.
- Changed-file Ruff and `git diff --check`: clean before the final suite.
- Risk/accounting/reconciliation improves from `95/100` to `100/100`; overall
  architecture is `668 / 7 = 95.4%`, still reported as `95%`.
- No strategy parameter, risk threshold, credential, production process,
  order, position, account, or live state changed.

## 2026-07-21 - Issue #49 explicit grid-line rearm

### Decision

- Make each AI-authored explicit grid line a fail-closed lifecycle with a stable
  line identity, monotonic generation, idempotent fill IDs, quantity accounting,
  and an auditable transition log.
- Re-arm a line only after a confirmed target close leaves zero exposure. A
  partially filled entry that has been closed remains blocked until the venue's
  residual-entry cancellation is also confirmed.
- Keep hard-stop, entry-cutoff, operator-cancel, and cycle-finalization paths
  terminal. They may close exposure but may not silently create a new entry.
- Give every repeated line cycle a distinct trade identity while retaining the
  stable line/position identity used for grid-level reconciliation.

### User value

- A 4000 -> 4010 -> 4000 -> 4010 oscillation can produce two independently
  reconcilable round trips on the same grid line instead of consuming that line
  after its first take-profit.

### Testing decision

- Focused grid, cycle-runner, scoring, and execution-contract regression:
  `113 passed`.
- Full repository suite: `1872 passed, 7 skipped in 369.22s`.

### Gotchas

- A fresh entry cannot target in its entry bar because OHLC does not reveal the
  intrabar path. This conservative ordering also prevents same-bar re-entry.
- The paper simulator currently emits full fills. Partial-fill and reconnect
  safety are enforced and tested at the grid-line state-machine boundary; this
  change does not enable a venue adapter or any real-money submission path.
- `dualtrack/grid_lifecycle/*_machine.json` is deterministic audit trace, not a
  venue receipt or authority to trade.
- No grid spacing, sizing, exchange key, live runtime configuration, risk-port
  behavior, production process, order, position, account, or live state changes.

## 2026-07-20 - Issue 40 paper-start readback race

### User outcome

- A paper grid start reflects the execution engine's real order state even when
  an order fills between submission and the first verification readback.

### Success criteria

- Every submitted order must still be present on the first readback.
- A submitted order may be `accepted` or already `filled` without causing a
  false startup failure.
- Missing orders, invalid states, or unrelated accepted orders still fail
  closed with diagnostic IDs.
- The submit-to-readback race has a deterministic regression test.
- The repository suite remains at or above the `1889 passed` issue baseline.

### Scope

- In scope: paper Strategy Control startup verification and its regression test.
- Out of scope: live/broker money paths, matching semantics, credentials,
  branch protection, and unrelated regrid behavior.

### Decision

- Verify submitted IDs against a single complete execution snapshot instead of
  comparing them only with the `accepted` projection. Preserve the prior
  fail-closed check for unexpected accepted orders.

### Gotchas

- A fill is evidence of forward progress, not evidence that submission failed.
- Historical terminal orders may coexist in a snapshot; only unrelated active
  `accepted` orders are startup blockers.
- This backend-only change has no visible surface; visual evidence is not
  applicable. Test output and the PR diff are trace evidence.

### Adversarial review

- Two independent read-only reviews checked race correctness, fail-closed
  behavior, scope, and test realism.
- The safety review reproduced ambiguous receipt identities and a late
  unrelated accepted order. Closure now rejects empty, whitespace-only, and
  duplicate IDs; validates both readbacks through one invariant; and derives
  counts from the same terminal snapshot.
- Duplicate detection uses a linear `Counter` pass so large historical
  snapshots do not create quadratic validation work.
- Final review found no remaining P0-P3 actionable findings and confirmed no
  live/broker, matching, credential, or branch-protection change.

### Verification

- Strategy Control plus Nautilus upstream/downstream pack: `39 passed`;
  independent reviewer expansion: `43 passed`.
- Changed-file Ruff and `git diff --check`: clean.
- Final repository suite: `1874 passed, 7 skipped in 402.65s`.
- The current `main` closure records `1869 passed, 7 skipped`; this issue adds
  five passing cases and removes none. The issue text's `1889 passed` baseline
  is not reproducible from the accessible remote, so the PR reports both the
  exact result and the positive delta instead of claiming that absolute count.

## 2026-07-20 - Issue 41 monotonic Dashboard order lifecycle kickoff

### User outcome

- One order remains visible in GridMind while its displayed state advances from
  accepted through partial fill to cancellation or fill; it never vanishes
  merely because it left the open-order subset.

### Success criteria

- The read model normalizes provider-neutral order states and emits a stable
  label, progression rank, open flag, and terminal flag.
- Total lifecycle-order count stays separate from the open-order count used by
  safety controls.
- GridMind renders the full order lifecycle collection while chart and control
  logic continue to use open orders only.
- An end-to-end projection sequence proves accepted -> partially filled ->
  cancelled remains one visible order with non-decreasing progression.
- Desktop/mobile browser evidence shows readable lifecycle states with no
  horizontal page overflow.
- Focused and full regression do not degrade the accessible repository base.

### Scope

- In scope: `trading-system-read-model-v1`, GridMind order-table presentation,
  static/projection/API tests, browser evidence, and this decision record.
- Out of scope: matching, execution adapters, order submission/cancellation,
  live/broker money paths, credentials, risk authority, and branch protection.

### Decision

- Reuse the execution snapshot's `orders` collection as current lifecycle
  truth and project canonical presentation phases in Python. GridMind retains
  the highest observed phase for each `(cycle_id, order_id)`. The two legally
  bidirectional protection states share one phase; only a strictly increasing
  revision derived from a complete, legal transition history may move between
  them. A delayed, lower-revision, or briefly incomplete poll therefore cannot
  make a displayed order regress or disappear. The cache resets on cycle
  rollover and never infers a state from fills.
- Preserve current-snapshot `open_orders` and `open_order_count` for charts,
  runtime text, and safety controls. Retained rows are table-only presentation
  memory, so a visibility fix cannot create command or risk authority.

### Gotchas

- A terminal order disappearing from the open-order subset is correct trading
  state but incorrect lifecycle presentation when the table promises to show
  the order's journey.
- Terminal outcomes branch (`filled`, `cancelled`, `rejected`, `expired`); a
  shared progression rank means terminal without pretending those outcomes are
  equivalent.
- A pure GET has no previous poll and therefore cannot enforce cross-request
  monotonicity by itself. A mutable server GET cache would violate the A6
  deterministic/pure boundary; the smallest stateful seam is a cycle-scoped,
  table-only browser cache driven entirely by backend phases and validated
  transition revisions.
- `protective_attached <-> protective_failed` is a legal recovery loop, so no
  total state rank can prove its direction. Both states use one phase and a
  positive transition revision. Same-state refreshes may never lower or erase
  a cached revision; otherwise two delayed snapshots could manufacture a
  false recovery.
- Fill presence alone cannot distinguish partial from full fill. The read model
  must never reconstruct an order state from fill rows; the execution snapshot
  remains authoritative.
- `order.state_label` is untrusted API text even when normally backend-authored.
  It must be escaped before entering the table's deliberately trusted HTML
  channel used by direction and action cells.
- An unknown provider state is not evidence that no order is open. The read
  model exposes `unknown_order_count`, degrades completeness, blocks Start, and
  keeps Stop available; it does not silently fold unknown into the closed set.
  Runtime presentation is also degraded, so an unknown order can never coexist
  with a green trustworthy run badge.

### Verification

- Focused read-model, API, GridMind, GET-purity, lifecycle, and real-DOM
  regression: `35 passed`; changed-file Ruff and `git diff --check` are clean.
- The Playwright regression executes the actual `dashboard-gridmind.html` and
  its JavaScript/DOM. It proves accepted -> partially filled -> cancelled,
  delayed accepted, missing rows, cycle reset, and the adversarial protection
  sequence failed rev8 -> stale failed rev6 -> stale attached rev7 -> attached
  rev9. Only the final rev9 recovery advances.
- A deterministic local browser fixture exercised five consecutive API polls:
  `accepted -> partially_filled -> cancelled -> accepted (regression) ->
  missing`. The table advanced `已接受 -> 部分成交 -> 已撤单` and then retained
  `已撤单`; the current-snapshot runtime count independently changed
  `1 -> 1 -> 0 -> 1 -> 0`, proving retained rows do not feed controls.
- A hostile unknown state beginning with `<img` rendered as `未知状态`; the
  order table contained zero `img` elements and the browser logged no runtime
  error. The runtime badge showed `异常` without the green class while Stop
  remained available. This is deterministic mock acceptance, not live market
  evidence.
- Desktop evidence:
  `/Users/wendy/.codex/visualizations/2026/07/20/019f7e15-978f-7831-bfb2-8c2b44eaea00/issue-41-order-lifecycle-desktop.png`.
- 390px evidence (zero page-level horizontal overflow):
  `/Users/wendy/.codex/visualizations/2026/07/20/019f7e15-978f-7831-bfb2-8c2b44eaea00/issue-41-order-lifecycle-mobile.png`.
- Final repository suite: `1881 passed, 7 skipped in 432.74s`. The accessible
  `main` baseline recorded immediately before these issue branches is `1869
  passed, 7 skipped`; #41 adds 12 passing tests and removes none. The supplied
  `1889 passed` reference is not reproducible from the current `main`, so this
  records the exact base/result instead of relabeling the count.
- Two independent adversarial reviews closed clean with no P0-P3 actionable
  finding after the revision-ordering and unknown-runtime badge fixes.

## 2026-07-20 - Issue #42 paper safe-action market-gate audit

### User outcome

- When the paper datafeed is unavailable or stale, an operator can still cancel
  pending orders, reduce an open position, or stop-and-flatten the strategy;
  every bypass of the entry freshness gate leaves bounded audit evidence.

### Success criteria

- Canonical server-side command classification runs before any entry-only
  market freshness rejection.
- Cancellation executes without a market price, including a fully blocked feed.
- Partial reduce and emergency flatten execute when the feed is fully blocked,
  using only gateway-bound server marks or trusted paper execution-ledger facts.
- A stale mark is never relabelled or injected as a fresh execution event.
- Stale entry remains fail-closed; synthetic, provenance-free, or wrong-provider
  marks cannot become safe-action pricing.
- API responses, persisted risk decisions, execution commands, and control
  audit JSONL expose the action class and stale-pricing evidence.

### Scope

- In scope: Dashboard manual paper orders, Strategy Control `cancel_all` and
  `stop`, Legacy paper, Nautilus paper replay settlement, tests, and this audit.
- Out of scope: every live/broker money path, credentials, branch protection,
  live-equivalent verification, strategy parameters, and risk thresholds.

### Decision

- Reuse `action_class_for_command()` as the only authority; client-supplied
  `action_class` and gate objects are discarded at the network boundary.
- Keep entry semantics unchanged. Only canonical `cancel` and `reduce_only`
  actions bypass the freshness requirement.
- Cancellation has no pricing dependency. Reduce/flatten pricing priority is a
  gateway-bound fresh/server mark, a persisted Nautilus execution event, the
  target position's last paper fill, then its paper cost basis. Every source,
  original timestamp, executable price, and operator request time is recorded.
- A gateway rejection of a provider, source mode, or missing synthetic flag
  cannot be bypassed by the later resolver. Client limit prices and gate objects
  never authorize the executable price.
- Fresh actions advance Nautilus with a validated new event. Stale safe actions
  use an explicit paper-only post-replay settlement step through `flush()`;
  cancellation changes only order lifecycle, and reduce-only settlement cannot
  exceed an open position. No stale or fabricated market event is appended.
- The same safe-action settlement flushes a configured Nautilus shadow behind
  Legacy authority, so its audit state does not wait for a future market event.
- Safe control actions skip planning timeframes and account-history construction
  because those read models do not authorize cancellation or flattening.

### Gotchas

- A reduce/flatten still requires one exactly identified open paper position and
  a trusted price already present in server or execution-ledger evidence. A
  corrupt position with no such evidence fails honestly instead of inventing a
  quote.
- A last-known mark, execution fill, or position cost basis is paper valuation
  evidence, not proof of current venue liquidity or a live execution price.
- Nautilus runs native replay first, then settles only canonical paper safe
  actions requested after the entry. This avoids time travel and avoids the fake
  fresh event that could also fill unrelated pending entries.
- This change has no visible UI. Visual evidence is not applicable; tests,
  JSONL audit rows, risk decisions, and persisted commands are trace evidence.

### Verification

- Focused paper safe-action, control, risk/API, shadow, and audit pack:
  `148 passed in 14.54s`.
- Isolated Nautilus 1.230.0 runtime pack: `8 passed`; the blocked-feed control
  test and the post-final-event cancel/reduce/flatten test both reconcile `ok`
  without adding a market event.
- Full repository regression: `1897 passed, 1 skipped in 416.67s`, exceeding the
  required `1889 passed` floor.
- Two independent adversarial reviews found no remaining P0, P1, or P2 issue;
  the exact wrong-provider, per-position fallback, and later-market recovery
  reproductions passed after hardening.
- No live/broker file, credential, branch protection, production process,
  strategy parameter, or risk threshold is changed.

## 2026-07-21 - Durable paper cycle rollover and terminal package

### User outcome

- A paper grid that was running at the 09:00/21:00 boundary closes safely,
  leaves one auditable terminal package, and starts the next trusted plan
  without requiring an operator to notice a stopped cycle.

### Decision

- Treat rollover as a persisted state machine: record intent, stop the old
  cycle, verify cancellation/flatten reconciliation, package immutable facts,
  then start the next cycle. Every failure writes a blocked fail-closed row.
- Bind rollover intent to the persisted runtime timestamp and compare it again
  inside the stop mutation and immediately before start. A later operator
  action cancels continuation instead of being overwritten by the scheduler.
- Carry the stopped-runtime token into `start` and recheck it after risk work,
  under a re-entrant cross-process file lock immediately before any
  plan/runtime or order write. Dashboard and scheduler therefore share one
  local production writer boundary, closing the planning/risk race window.
- Read the raw persisted runtime row at the boundary. The normal current-cycle
  read model intentionally masks an older cycle as stopped and cannot decide
  whether automatic continuation was authorized.
- Size the next cycle from the terminal post-flatten account in the package,
  never the pre-stop snapshot. Preserve explicit plan range, count and per-grid
  notional; a changed risk ceiling blocks instead of silently resizing.
- Keep optional Strategy Shadow evidence outside the production close gate.
  A shadow exception is captured once in the immutable package and only an
  explicit append-only revision may retry it.
- A new-cycle runtime row in stopped or error state is never automatically
  restarted. This protects an operator stop and prevents a failed start from
  looping every minute.
- If the process dies after a successful start but before the final rollover
  receipt, the persisted running state repairs the missing completed receipt.
  A crash leaving `starting` or `stopping` is blocked for inspection instead
  of being mistaken for a healthy running strategy.
- Verify every stored package hash before trusting `status=closed`. A mismatch
  creates a new fail-closed integrity incident without claiming to supersede
  the untrusted hash. The incident remains latched across scheduler ticks until
  an explicit actor/hash-bound acknowledgement; only then may fresh evidence
  create an append-only revision. A transient non-integrity blocked package is
  re-snapshotted and may close through a revision linked to its verified hash.
- Integrity acknowledgement requires a structured, non-empty actor id and
  transport plus the current incident hash; rejected acknowledgements leave
  the package journal byte-for-byte unchanged.
- Use the same cross-process production mutation lock for rollover receipts,
  terminal-package revisions, integrity acknowledgements and control-plane
  writes. This prevents scheduler and dashboard processes from interleaving
  state transitions or append-only evidence.
- Persist a rollover-specific transition owner in `starting`, `stopping` and
  error runtimes. Only scheduler-owned incomplete work is retried; an operator
  stop or unrelated failed start remains stopped and is never auto-restarted.
- Validate every package row and every supersedes link, not only the latest
  hash. An integrity incident records the exact historical row and remains
  latched until an actor/hash-bound acknowledgement.

### Gotchas

- Stop uses only the safe-action market contract, so unavailable planning
  timeframes cannot strand the old cycle. If no trusted execution-ledger price
  can flatten an open position, stop records a blocked receipt and resumes
  only after that safety evidence exists.
- A datafeed transport exception is converted into an explicit blocked market
  envelope so cancellation still runs. Flattening may use only trusted
  execution-ledger prices; the rollover never invents a quote.
- Cleanup must persist an error runtime even when its final adapter snapshot
  also fails. In that case `accepted_order_count_known=false` prevents a zero
  count from being mistaken for proof that no orders remain.
- The protective order sweep is also a production-ledger writer. Its snapshot,
  market-event processing and shadow flush must stay inside the same
  cross-process mutation boundary as dashboard controls and rollover.
- Once stop succeeds, package/start may resume from persisted evidence, but
  only while the runtime namespace still belongs to the previous cycle.
- Historical packages prove local paper execution and reconciliation facts;
  they are not evidence of live venue execution or liquidity.
- The new cycle still requires fresh, non-synthetic 1m data plus completed D1
  and 4H planning bars. Missing context blocks start rather than degrading to
  synthetic inputs.

### Verification

- Focused rollover, package, cycle-runner, dashboard and control-plane pack:
  144 passed, including an independent-process lock barrier test.
- Ruff and git diff checks passed. Full-suite execution was intentionally not
  used for this bounded lifecycle milestone.

## 2026-07-21 - Authoritative Beijing daily report and NAV

### User outcome

- The 24-hour report, daily realized PnL and NAV now agree with the terminal
  paper execution ledger instead of displaying zero when fills omit a PnL
  field.

### Decisions

- Define one report day as Beijing 00:00–24:00. Because the production grid
  rolls at 09:00/21:00, one complete calendar day requires three overlapping,
  terminal cycle packages and is publishable only after the last one closes.
- Source realized PnL exclusively from authoritative closed positions and
  reconcile their cycle total to execution.pnl.realized. Fills contribute only
  event count, entry-defined trade count and executed notional.
- Count one trade by its entry identity: an entry is one trade and its later
  close does not create a second trade.
- Persist one idempotent JSON report plus one readable Markdown report. Both
  carry the exact cycle IDs, package hashes and StrategyPlan identities used by
  the NAV projection; the dashboard exposes the same JSON artifact.
- Revalidate the report hash and every referenced closed package revision on
  dashboard read. A later append-only revision does not invalidate an older
  verified reference, but any package-chain tampering fails closed.
- Reject missing, non-finite, timezone-free, unreconciled or hash-invalid
  evidence. Unknown financial truth is never coerced to zero.

### Gotchas

- Beijing midnight cuts across the 21:00–09:00 trading cycle. Aggregating only
  two cycle reviews is not a Beijing natural day and can shift fills or PnL to
  the wrong date.
- Fill-level realized_pnl is intentionally ignored: adapters do not guarantee
  it is present or economically complete. A fill is still mandatory for event
  count and notional, so missing price/quantity also blocks publication.
- The daily NAV is a normalized one-day projection from the configured cycle
  starting equity. It is not an intraday mark-to-market curve.

### Verification

- Daily-report, cycle-package and dashboard focused pack: 54 passed.
- Full-suite execution was intentionally not used for this bounded financial
  reporting milestone.

## 2026-07-21 - Readable K-line viewport and quiet grid overlays

### User outcome

- GridMind opens on a readable 30-minute chart; a wide production Range no
  longer compresses the live candles into a flat line.

### Decisions

- Default only the operator's chart selector to 30m. Strategy planning and
  execution continue to consume their fixed backend timeframes unchanged.
- Restore native visible-candle autoscale. Price lines never expand the candle
  scale to the complete strategy Range.
- Derive the displayed price band from OHLC bars inside the current logical
  viewport and draw only Range, grid, order and position lines that fall inside
  that band. The complete Range remains in the production model and summary.
- Expose provider-neutral visible-range and price-line methods on the shared
  StandardKline adapter so the console does not reach into Lightweight Charts.
- Use low-opacity overlay lines and show one axis label: the pending order
  nearest the current price.
- Constrain all grid/flex ancestors at the 390px layout boundary; tables retain
  local scrolling while the page itself has no horizontal overflow.

### Gotchas

- Re-rendering the full chart on every visible-range event creates a feedback
  loop because live-edge restoration also changes that range. View events now
  update only the price-line overlay through the adapter.
- A 30m fetch is optional enrichment, not permission to blank the console.
  Render the trusted read-model market first, then replace it only when the
  requested chart timeframe succeeds.
- Hiding overflow alone does not make a grid responsive. Every minmax/flex
  ancestor of the chart and control rail must also allow min-width zero.

### Verification

- GridMind static suite: 14 passed.
- StandardKline Node suite: 21 passed.
- Inline dashboard JavaScript syntax check passed.
- Browser layout smoke test showed no page-level horizontal overflow at
  390x844 or 1440x900. Screenshots are archived under
  `/Users/wendy/.codex/visualizations/2026/07/21/trading-system-issue-57/`.
- The browser smoke used a static server, so it proves responsive layout only;
  it does not claim live API or candle-data acceptance.
- Adversarial review found no remaining P0-P2 defects after fixing timeframe
  overlay ordering and duplicate-price order labels.
- Full-suite execution was intentionally not used for this read-only UI
  milestone.

## 2026-07-21 - Auditable position, order and trade lifecycle counts

### User outcome

- The three trading tabs now answer three different questions without double
  counting: open positions, broker-accepted working orders, and unique trade
  lifecycles where entry plus later exit remains one trade.

### Decisions

- Keep canonical trade count and PnL in the accounting snapshot. The read model
  adds only presentation fields such as the verified close reason.
- Define a currently accepted order as `accepted`, `open`, `working`, or
  `partially_filled`; pre-acceptance and terminal states are not current
  委托. The existing monotonic browser lifecycle cache still prevents stale
  snapshots from resurrecting a terminal order.
- Display TP/SL only when the order and current StrategyPlan have the same
  non-empty plan ID. Missing protection may be completed from one uniquely
  matched deterministic plan order; missing, mismatched, ambiguous or
  incomplete lineage displays `未知`.
- Resolve TP, SL and manual close reason from authoritative exit-fill events,
  not from price proximity. Unknown evidence stays unknown.
- Count open positions from the projected open rows and render canonical entry
  quantity, entry/exit times and realized PnL for each unique trade lifecycle.

### Gotchas

- `open_order_count` includes pre-acceptance states and therefore cannot label
  the operator's current broker-accepted委托 count.
- A plan order with the same price and side is not sufficient when multiple
  candidates match; deterministic ambiguity must fail closed.
- The latest execution snapshot defines whether an order is still current;
  the monotonic cache defines only its latest valid phase. Without both rules,
  a disappeared accepted order becomes a permanent ghost or a terminal order
  can be resurrected by stale polling.
- `open_order_count` includes pre-acceptance states. It remains the broader
  stop/start safety gate. Start recovery trusts the persisted `running` runtime
  only when its last action and plan identity match, because a completely
  accepted grid may legitimately contain an immediately filled order.
- A `source_fill_id` is one complete identity. Parsing only its final preview
  segment can silently borrow TP/SL from the wrong plan.
- The 30m chart refresh is optional. The trusted read model must render before
  that request so a timeframe outage cannot blank these lifecycle tables.

### Verification

- Trading-system read model, API, GridMind static and real-DOM lifecycle pack:
  36 passed.
- Real-DOM lifecycle browser test: 1 passed.
- Inline dashboard JavaScript syntax check passed.
- Three adversarial passes ended with no remaining P0-P2 findings after
  resolving lifecycle membership, start-evidence and plan-lineage defects.
- Full-suite execution was intentionally not used for this bounded read-only
  projection milestone.

## 2026-07-21 - Trusted live market tape and runtime lamp

### User outcome

- The top bar now answers, at a glance, what is trading, the latest trusted
  price and today's venue move, and whether the paper grid is truly running.

### Decisions

- Derive the displayed pair only from the canonical market
  `provider_symbol`; `XAUUSDT` is rendered as `XAU / USDT` without inventing a
  symbol from a product label.
- Treat the canonical trusted 1m snapshot as the header ticker. A higher
  timeframe selected for the chart cannot replace or recolor the live header.
- Retain the previous price direction when two consecutive snapshots are
  equal. Only a strictly higher or lower trusted 1m price changes the color.
- Calculate today's percentage move from a trusted 1d open and the trusted 1m
  latest close only when provider, provider symbol, and UTC trading day match.
- Blink green only when runtime and actual state are both running, at least one
  broker-accepted order is visible, the market is trusted 1m, completeness is
  complete, the current risk decision still allows exposure with no blockers,
  and the runtime cycle is not stale.

### Gotchas

- A green data-source badge is not proof that the strategy is running. The run
  lamp has a separate conjunction of execution, market, and completeness gates.
- The chart can legitimately display 30m while the header must remain bound to
  canonical 1m; sharing `state.market` would make header price behavior depend
  on the operator's chart timeframe.
- A 1d bar from another provider or symbol is not a valid denominator. Missing
  lineage leaves the daily percentage unknown rather than blending venues.
- Equal ticks must not reset the last direction, or a quiet market would make
  the price flicker back to neutral between real moves.
- Price direction belongs to a provider plus provider-symbol identity. The
  first tick after a feed switch establishes a new baseline and must not be
  compared with the previous venue's price.
- A complete read model can still contain a currently blocked risk decision;
  completeness alone is therefore insufficient to authorize a green run lamp.

### Verification

- GridMind static and real-browser header pack: 19 passed.
- The browser test covered canonical pair formatting, same-venue daily change,
  up/equal/down tick behavior, feed-identity reset, the healthy run gate, and
  fail-closed behavior for blocked risk and an untrusted 1m snapshot.
- Inline dashboard JavaScript syntax check passed.
- Full-suite execution was intentionally not used for this bounded top-bar UI
  milestone.

## 2026-07-21 - Authoritative trade activity notifications

### User outcome

- New entries, partial reductions, take-profits, stop-losses, and final closes
  now appear as compact top-right notifications without replaying the account's
  historical fills whenever the page opens.

### Decisions

- Seed the browser tracker from the first complete, reconciled accounting
  snapshot and emit nothing for that baseline.
- Deduplicate a fill only with a stable lineage plus fill identity. Prefer
  `fill_id`, then source fill ID, then order ID; a fill without enough lineage
  evidence remains silent.
- Require an authoritative decrease in remaining units before announcing a
  partial close. Require a closed trade or zero remaining units before the one
  final completion notification.
- Require both historical and current accounting snapshots to be complete and
  reconciled before current position rows can authorize a notification.
- Keep trade tracking across current execution-cycle changes so a prior-cycle
  position can still emit its one eventual close notification without reviving
  it as a current position.
- Create Web Audio only after a pointer or keyboard gesture. Unsupported,
  suspended, or failed audio stays silent and cannot break polling or control.

### Gotchas

- A new exit fill is not by itself proof that the position is fully closed;
  fills and authoritative remaining units can arrive in different snapshots.
- The fill row and closed trade can become visible in the same snapshot. The
  tracker must coalesce them into one completion toast.
- Trade IDs alone may collide across cycles or plans. Missing lineage fails
  closed instead of risking a duplicate or wrong notification.
- Current positions join historical trades by the same lineage plus trade ID;
  a bare duplicate trade ID cannot overwrite a prior-cycle lifecycle.
- Browser refresh intentionally resets the in-memory tracker and establishes a
  new silent baseline; this feature is not a durable notification inbox.
- Audio autoplay policy varies by browser. Sound is an enhancement after user
  interaction, never evidence that a trade occurred.

### Verification

- Trade activity Node suite: 8 passed.
- GridMind static plus adjacent real-browser lifecycle/header pack: 21 passed.
- Covered silent authoritative baseline, degraded-snapshot rejection, fill
  deduplication, two fills on one order, partial/final close ordering,
  same-snapshot coalescing, degraded current-accounting rejection, colliding
  cross-cycle IDs, cross-cycle completion, and no-Web-Audio fallback.
- Inline dashboard JavaScript syntax and diff checks passed.
- Full-suite execution was intentionally not used for this browser-only
  notification milestone.

## 2026-07-21 - Same-cycle review ledger and Strategy Shadows

### User outcome

- The 12-hour review now reads as one paired ledger: what the locked production
  plan said, how that exact plan was judged, and what the same cycle actually
  produced.
- Strategy Shadows explain the human meaning of each scenario and compare only
  against a successful same-cycle replay of the locked production plan.

### Decisions

- Select only a closed cycle package for review and load its Shadows by the
  identical cycle ID; the current open cycle cannot be blended into history.
  Every displayed package must first pass its complete append-only hash-chain
  verification.
- Link a review track only through one unique source proposal referenced by the
  locked StrategyPlan. Missing or ambiguous lineage is displayed as unknown.
- Compare complete plan specifications, including direction, style, range,
  mode, spacing, grid count, per-grid notional, leverage, and out-of-range
  policy. A partial record cannot be called unchanged.
- Keep realized and unrealized PnL separate. A missing value remains unknown
  and is never normalized to zero.
- Accept only `variant_id=production` with `status=pass` as the counterfactual
  baseline, and require its plan identity to match the packaged production
  plan. Candidates must share its market-event hash, evaluation window, and
  execution and fee contracts.
- Project one selected compact review package plus summary-only package history
  into the five-second polling response. Raw replay events, commands, orders,
  and fills remain in immutable evidence rather than the dashboard payload.
- Present next-cycle output as advice. A record claiming automatic application
  is flagged for human verification rather than repeated as production truth.

### Gotchas

- A ledger review can contain both machine and human assessments. Choosing a
  default track would silently grade a different proposal than the locked plan.
- A row with `status=closed` is not trusted evidence by itself. A forged hash or
  broken supersedes link excludes the entire journal from review selection.
- A Shadow named `production` is still not usable if its replay was blocked or
  belongs to another cycle, has a different plan identity, or lacks replay
  input lineage.
- JavaScript numeric coercion turns `null` into zero. Review formatting must
  reject missing values before conversion.
- Range and grid values can match while the out-of-range policy differs; this
  is a materially different production specification.
- Key-level and TP/SL verdicts are dimension-specific. If those dimensions
  changed after proposal review, the old verdict is marked口径不一致.
- The packaged Shadow list can lag the dedicated read-model source. Same-cycle
  rows from the current source take precedence, then scenario IDs are deduped.
- Historical superiority is descriptive evidence for one replay window, not a
  forecast and not authorization to promote a strategy.

### Verification

- Review behavior Node suite: 8 passed.
- Cycle-package integrity, read-model, API, and GridMind static suite: 51 passed.
- Covered missing-value preservation, unique proposal linkage, track selection,
  complete specification comparison, direction mismatch, same-cycle Shadow
  filtering, selected-cycle alignment, dimension-specific plan matching,
  compact polling projection, deduplication, and strict same-input production
  baseline gating.
- Inline dashboard JavaScript syntax and diff checks passed.
- Full-suite execution was intentionally not used for this bounded review UI
  and read-model milestone.

## 2026-07-21 - Always-visible production strategy summary

### User outcome

- The chart now always states which locked production plan exists, whether it
  is actually running, and why its per-grid amount has the displayed value.

### Decisions

- Render the line only from `strategy.summary` and `runtime` in the canonical
  trading-system read model. Editable controls and preview state are excluded.
- Show plan version, bilateral or unilateral direction, style, grid mode and
  count, per-grid notional source, maximum plan loss, and leverage in one line.
- Keep the existing Range, spacing, and accepted buy/sell order summary as the
  separate line below it.
- Project `notional_mode`, its human label, and locked `max_loss` from the
  StrategyPlan so the UI does not reverse-engineer sizing intent.

### Gotchas

- A locked plan can exist while the runtime is stopped. Plan presence must not
  be presented as proof that the strategy is running.
- A draft preview may change direction, Range, count, or notional. It cannot
  replace the production summary before a new plan is actually locked.
- Missing notional provenance remains `来源未知`; the UI does not assume auto
  sizing merely because the amount resembles a risk-budget calculation.

### Verification

- Trading-system read-model and GridMind static tests: 36 passed.
- Covered locked sizing provenance, maximum loss, stopped copy, and exclusion
  of preview/form values from the production summary function.
- Inline dashboard JavaScript syntax and diff checks passed.
- Full-suite execution was intentionally not used for this small read-only UI
  milestone.

## 2026-07-21 - Running grid edge adjustment

### User outcome

- A running paper grid can add or remove whole price levels at either edge
  without stopping, flattening, changing its spacing/ratio, or resizing each
  grid order.
- Existing positions and their TP/SL evidence remain byte-for-byte equivalent
  across the adjustment; only unfilled entry orders outside a contracted range
  are cancelled.

### Decisions

- Keep edge geometry pure: arithmetic requests snap to the current absolute
  spacing and geometric requests snap to the current ratio. Grid count changes;
  per-grid notional and its auto/manual provenance do not.
- Require the current StrategyPlan identity in every request. A successful
  retry is recognized by a fingerprint of the original plan plus the direct
  requested range and performs no second mutation.
- Reuse the canonical `replace_pending` risk action, but bind accepted entries
  into disjoint retained and replaced ID sets. Retained orders appear as exact
  risk commands tied back to their engine order IDs, so the risk decision
  reflects the complete post-adjustment pending set without claiming they were
  cancelled.
- Stage only new edge commands, verify them as accepted or filled, then cancel
  only out-of-range entry IDs. Activate the new StrategyPlan version only after
  retained entries, positions, protection orders, and reconciliation pass.
- Persist the candidate plan as `staging` before the first order submit while
  leaving runtime on the old running plan. A retry with the same fingerprint
  resumes the same plan/version and submits only missing edge orders.
- Persist the canonical risk request, decision, and policy IDs on that staging
  plan so a crash after activation can repair runtime without losing the exact
  authorization evidence.
- Swap the old active and new staging statuses in one atomic same-cycle plan
  file write. The specialized edge path fails closed if another cycle is active
  rather than recreating the generic two-write activation gap.
- Flush Nautilus control commands only when there is no unprocessed market
  event. The replay uses the exact checked event snapshot, so an event arriving
  afterward remains pending for the normal market path.
- Resolve retained-order protection from the exact inherited StrategyPlan in
  the operator read model rather than treating every old plan ID as unknown.
- On pre-cancellation failure, cancel only the staged plan and leave the old
  plan running. Any failure after old-edge cancellation is non-rollbackable and
  moves runtime to error without cancelling the useful staged edges.

### Gotchas

- The older full-regrid risk contract assumed every accepted entry would be
  replaced. Supplying only the outside IDs would fail closed; supplying all IDs
  would create false audit evidence. Retained IDs therefore need an explicit
  binding to candidate economics.
- Accepted execution rows do not carry SL/TP in the canonical accounting view.
  Retained risk commands must resolve them from the exact originating plan;
  missing or ambiguous lineage blocks before mutation.
- A newly exposed edge already represented by an accepted order or open
  position is not submitted again. A fully completed entry/exit lifecycle is
  intentionally eligible to re-arm at that price with a deterministic new
  command identity; closed historical positions do not occupy the line.
- New edge limits are outside the trusted current mark. If an engine nonetheless
  creates a staged position during failure cleanup, the control plane fails
  visibly instead of flattening an unrelated prior position.
- Contraction can be snapped more aggressively than the pointer value. The
  trusted current market must remain inside the effective snapped range.

### Verification

- Focused geometry, sizing, risk, control-plane, inherited-plan read-model, and
  API, two-console static, and Nautilus adapter suite: 157 passed.
- Covered arithmetic and geometric snapping, fixed notional provenance,
  live-exposure deduplication plus completed-cycle re-arm, exact retained/replaced
  risk binding, pure contraction to zero pending entries, position and TP/SL
  preservation, atomic activation and runtime repair, completed-edge rearm,
  post-cancel failure preservation, inherited-plan protection, fail-closed
  Nautilus command-only flush, and staged-order-only cleanup.
- Full-suite execution was intentionally not used for this bounded paper-order
  milestone.

## 2026-07-21 - Read-only Range drag specification and risk preview

### User outcome

- A Range drag can be evaluated before any chart interaction or order mutation:
  whole-Range movement preserves width, count, and per-grid notional; one-edge
  movement fixes the opposite edge, preserves count and notional, and recomputes
  arithmetic spacing or the geometric ratio.
- The result is one explicit old-to-new specification with current canonical
  risk, order deltas, position/TP-SL statements, and a disabled confirmation
  state when current facts do not pass.

### Decisions

- Add `preview_range` as a read-only control action. It requires the exact
  active StrategyPlan ID and a running runtime, but does not append a control
  audit, risk decision, plan, order, fill, or runtime record.
- Keep pointer geometry validation pure and separate from market, account, risk,
  execution, and persistence adapters.
- Reuse the existing deterministic grid preview with fixed grid count and fixed
  manual notional. Only this read-only path may return an over-budget manual
  preview so the operator can see why confirmation is disabled.
- Evaluate the candidate through the canonical `replace_pending` risk port
  against the current trusted market, account, accepted entries, open positions,
  policy, and reconciliation. The decision is returned but not persisted.
- Evaluate the current grid through that same canonical port with every accepted
  entry retained. Old and new margin, actual leverage, maximum loss, and
  max-side notional therefore share current account/execution facts and one
  calculation basis.
- Never apply a sizing recommendation automatically. A second explicit
  `recalculate_notional_by_risk_budget` preview recomputes the candidate using
  the fresh server-side loss, projected leverage, and projected margin caps,
  then advertises the operation only if a trial candidate clears the complete
  canonical decision; production remains untouched.

### Gotchas

- Additive movement preserves arithmetic spacing because width and count remain
  fixed. On a geometric grid, additive movement preserves absolute width but
  necessarily changes the ratio; the old-to-new card exposes that change.
- Whole-Range equality uses a tight width-relative tolerance, then derives the
  upper boundary from the authoritative lower-boundary delta. A boundary drag
  returns the exact stored opposite edge; absolute-price-scaled tolerance must
  not introduce a tiny hidden width/spacing change.
- A local geometry risk estimate is not enough. Current open positions or an
  execution reconciliation problem can still block the canonical risk decision.
- A positive local cap is not a valid recommendation when open-position loss has
  consumed the canonical budget. Zero/unavailable canonical recommendations
  remain unavailable; they are never replaced by the local cap.
- An unsafe manual notional may be visible only as a read-only preview. Normal
  start/replace paths retain the strict manual-notional rejection and must run a
  new canonical risk decision before mutation.
- Order-delta counts describe a future full replacement. This milestone never
  performs that replacement and reports zero side effects explicitly.

### Verification

- Geometry, sizing, canonical risk, and control-plane focused suite: 93 passed.
- Covered whole-Range and fixed-edge geometry, arithmetic/geometric recompute,
  fixed count/notional, stale plan and current-price gates, exact order delta,
  over-budget visibility, explicit server-side risk recalculation, open-position
  old/new risk parity, projected margin/leverage clearance, zero remaining loss
  budget, high-price near-tolerance width drift, authoritative fixed boundaries,
  and byte-level proof that plan/runtime/risk/audit artifacts remain unchanged.
- Ruff and diff checks passed. Full-suite execution was intentionally not used
  for this read-only calculation milestone.

## 2026-07-21 - Chart Range draft interaction

### User outcome

- The production chart remains a normal pan/zoom chart until the operator
  explicitly enables `调整网格` on a running paper plan.
- Inside adjustment mode, the Range body moves as one fixed-width band while
  the upper and lower handles resize only their respective edge. Pointer
  release keeps a dashed draft and small confirm/cancel controls; it never
  opens a card or writes an order.
- Successive pointer releases accumulate. Only the small confirm control asks
  the server for the read-only old-to-new risk card; cancel removes the draft
  and restores the current production overlay.

### Decisions

- Put the interaction layer inside the standard K-line chart's existing
  coordinate adapter instead of adding another chart engine. The overlay is
  absent from pointer routing outside adjustment mode, avoiding a chart-pan
  conflict.
- Keep the production price lines authoritative throughout adjustment. Draft
  levels are a separate translucent dashed layer and never replace the current
  order/position overlay.
- Disable other production controls while a draft is active. A draft cannot be
  entered unless the authenticated paper runtime is running with an exact
  StrategyPlan.
- Treat a mixed sequence of individually constrained upper/lower/body gestures
  as one consolidated `draft` geometry for the read-only server preview. Count
  and per-grid notional remain fixed unless the operator explicitly requests
  risk-budget recalculation from the card.
- Validate the preview identity, requested bounds, fixed count/notional, and
  zero-side-effect receipt before displaying the card.

### Gotchas

- The visible edge can be outside the current candle window. Drag math must use
  price deltas from the gesture start, not snap to a currently visible price.
- Releasing the pointer is intentionally not confirmation. Network calls and
  modal opening occur only from the small `确认` button so repeated fine tuning
  stays uninterrupted.
- A polling render may redraw candles while a draft exists. The overlay is
  derived from the stored draft after every chart update, while the server
  still rejects a changed production plan ID on confirmation.
- `actual_state=running` is not sufficient identity evidence. Both UI entry and
  server preview require runtime plan ID/version to equal the active
  StrategyPlan; a polling mismatch cancels the local draft and requires a fresh
  operator review.
- Risk recalculation changes only the returned card. It does not silently
  rewrite the geometric draft or current production plan.

### Verification

- Dashboard static/browser, pure drag geometry, control-plane preview, and Node
  behavior/syntax suites: 67
  focused assertions passed.
- Covered explicit-mode gating, grab/ns-resize semantics, fixed-body and
  fixed-edge math, successive mixed gestures, no pointer-release request,
  outside release and pointer cancellation, cancel-to-production restoration,
  normal chart pan isolation, runtime/active-plan identity drift, zero-side-
  effect preview validation, required old-to-new card fields, and explicit
  risk-budget recalculation.
- Full-suite execution was intentionally not used for this bounded UI
  milestone.

## 2026-07-21 - Trusted historical K-lines and finalized execution bars

### Decision

- Only a bar whose start plus timeframe is at or before the current time may
  enter the execution adapter. The forming bar remains display-only.
- A historical page still passes through the typed market envelope. It may be
  `trusted_history=true` for display while always remaining `fresh=false` and
  therefore cannot authorize an entry.
- A failed live refresh keeps the last trusted candles visible but explicitly
  clears `trusted` and `fresh`; retained pixels never satisfy the trade gate.
- History uses an exclusive `end`, server `has_more`, timestamp deduplication,
  and logical-range restoration after prepend.

### Gotchas

- Treating a historical page as live-ready would let an old candle authorize a
  new order. Display trust and execution freshness are intentionally separate.
- Reusing a bar id while its high/low is still changing can permanently hide
  the final range behind idempotency. Event identity is cycle, timeframe, and
  bar start, and creation is delayed until close.
- A retained chart must set `trusted=false`; changing only its status text is a
  cosmetic block and is not a safety boundary.
- History dragging is disabled while Range adjustment mode owns the pointer;
  chart polling and prepend restoration are marked programmatic so neither can
  accidentally request another history page.
- A retained snapshot has `fresh=false`, so the ordinary five-second read-model
  refresh must reconcile it instead of treating it as disposable stale data;
  otherwise the next poll would erase the last trusted candles.
- Shadow contract mode still owns its legacy payload, but historical paging
  metadata must be attached after comparison so the dashboard can apply the
  same display-only trust contract in either cutover mode.

### Verification

- Revalidated on the post-#82 integration branch with the focused Python,
  Standard K-line, dashboard behavior, syntax, and conflict-marker checks.
- Full-suite execution is intentionally omitted for this medium integration;
  browser acceptance covers the joined Range-drag and history-drag surface.
## 2026-07-22 - 10x profit-targeted grid density

### Decision

- Keep direction, style, grid mode, and Range as the only operator strategy
  inputs. Leverage is a 10x ceiling and grid count/notional are derived.
- Let D1 ATR own Range and 4H ATR propose the densest grid, then search from
  at most 70 grids down to a floor of 30. Select the densest candidate whose
  minimum completed-grid profit is at least 10 USD within 10x capacity.
- Calculate profit after venue price/quantity rounding and modeled entry plus
  exit fees. Funding and realized slippage are excluded and must be labelled;
  this is a planned minimum, not a guaranteed fill outcome.
- Remove maximum stop loss from sizing and exposure blocking. Keep it as an
  advisory diagnostic while retaining market trust, margin, total leverage,
  order identity, protection geometry, and reconciliation gates.

### Gotchas

- A 10x leverage setting controls capital capacity; it does not multiply a
  fixed order's profit. The target is met by jointly solving grid density and
  per-grid notional under the same-side exposure ceiling.
- Neutral and directional grids have different maximum same-side counts. The
  solver uses the exact generated order set instead of assuming count / 2.
- Requested quantities are floored to the venue increment before profit is
  tested. Rounding a theoretical quantity up could otherwise pass the profit
  test while exceeding 10x capacity.
- If even 30 grids cannot clear 10 USD, preview/start fails closed. It never
  raises leverage, lowers the target, or invents a manual notional.

### Verification

- Focused sizing, exact-command canonical risk, policy registry, and selected
  start/range transaction tests: 60 passed.
- Full-suite execution was intentionally omitted for this bounded policy
  change; obsolete tests that encode 2x, 12/24-grid, and max-loss blocking are
  being replaced in the chained operator-surface milestone.
### Adversarial review corrections

- Auto sizing now evaluates 70 down to 30 directly. ATR explains the proposed
  spacing but cannot silently truncate the feasible density search.
- The 30–70 operating band is enforced by sizing, fixed-spacing edge changes,
  and the canonical paper risk decision, so neither UI nor a direct API call
  can create an out-of-band production grid.
- Range dragging intentionally keeps the active count and per-grid notional.
  A change that no longer clears the profit/capital gates is blocked instead
  of silently resizing the position; this preserves the operator contract.
- Persist actual leverage on the StrategyPlan and risk budget. The UI must not
  infer actual leverage from the 10x ceiling.

### Gotchas

- A narrower aggressive Range can make 70 grids too fine to clear the net
  profit target. In that case the densest valid answer can have fewer grids
  and a larger per-grid notional than the steady Range while using the same
  capital policy.
- The old risk-budget notional reduction can lower planned profit below 10 USD.
  It remains unavailable for this product contract; the response explains
  that geometry or capital must change instead.

## 2026-07-22 - Profit-targeted operator surface

### Decision

- The strategy card exposes only direction, style, grid mode, and Range.
  Grid count, per-grid notional, and leverage are derived outputs, not editable
  strategy inputs.
- The preview shows the selected 30–70 grid count, per-grid notional, minimum
  planned net profit versus the 10 USD target, estimated margin, and actual
  leverage versus the 10x ceiling.
- Maximum stop loss is absent from primary KPI and strategy summaries. The
  range replacement card may retain it only as `参考最大止损（不阻断）`.
- Copy states that modeled entry/exit fees are included while funding and
  realized slippage are excluded. A planned minimum is not presented as a
  guaranteed realized fill.

### Gotchas

- A currently running older plan can still display its historical grid count
  and notional beside a new derived preview. The UI labels one as production
  and the other as pending preview so they cannot be mistaken for one plan.
- Removing a control requires removing its event listeners and DOM reads too;
  leaving an old selector would fail at page initialization before any preview.
- Range drag intentionally preserves the running plan's count and notional.
  If the new geometry misses the 10 USD target, confirmation remains blocked;
  the old risk-budget downsize action cannot solve a profit shortfall and was
  removed from the card.
- Missing legacy metrics render as `--`. JavaScript's `Number(null) === 0`
  must not turn absent profit into zero or label the 10x ceiling as actual
  leverage; only persisted canonical actual leverage is displayed.

### Verification

- Static dashboard/read-model tests: 40 passed.
- Dashboard JavaScript, market retention, range interaction, and review tests:
  19 passed.
- Playwright operator-surface acceptance: 1 passed with screenshot saved at
  `docs/evidence/issue-84-profit-target-controls.png`.

## 2026-07-22 - Python 3.9 paper-control compatibility

### Decision

- Keep the installed paper LaunchAgents on the macOS system Python 3.9 runtime
  and remove the Python 3.10-only `zip(..., strict=True)` calls from the two
  exact-snapshot validation paths.
- Preserve the identity safety contract explicitly: the order-id list is
  derived one-for-one from the validated snapshot rows, then empty and
  duplicate IDs are rejected before the mapping is built.

### Gotchas

- Import-only Python 3.9 checks do not execute snapshot validation, so they did
  not expose this failure. The regression test replaces `zip` with a Python
  3.9-compatible signature and executes both start and replacement validators.
- The failed start briefly wrote terminal paper-order rows before cleanup, but
  the authoritative read model confirmed zero accepted orders and zero open
  positions. No live process or exchange credential was involved.
- A Nautilus cancel command can be visible briefly as an accepted command
  receipt before replay settles it. Start validation still rejects every
  unexpected accepted entry order, but does not misclassify a non-exposure
  `event=cancel` receipt as a second grid.

### Verification

- Six focused control-plane identity/readback tests passed.
- The exact validation path passed under `/usr/bin/python3` 3.9.6.

## 2026-07-22 - User-view paper acceptance and read-model repair

### Decision

- Validate the real local paper dashboard by operating it as Park would: start,
  refresh trend, draft and confirm a Range move, zoom, inspect every data tab,
  then stop/cancel/flatten. Backend receipts and screenshots accompany visual
  observations; neither one substitutes for the other.
- Keep the browser a projection of canonical evidence. Orders expose the exact
  matching plan's TP, SL, and planned net profit; positions expose normalized
  remaining quantity plus protection; plan versions stay in audit data but are
  removed from primary operator tables and the strategy summary.
- Fix older-history discovery at the fitted left edge. A deliberate right drag
  now requests the previous trusted page even when the chart cannot report a
  smaller logical `from` value because all 240 initial bars are already fitted.
- Build daily production P&L and Paper NAV from the actual machine ledger and
  starting balance instead of fields that do not exist in the read model.

### Gotchas

- A successful HTTP response is not button acceptance. The start and
  replacement checks wait for authoritative runtime, accepted-order count,
  exact plan identity, completeness, and reconciliation.
- The first Range translation was correctly rejected at 10.5x. A second move
  that preserved 40 grids and per-grid notional passed at 9.5x; the UI must not
  silently resize exposure during a drag.
- The previous history trigger required `range.from` to decrease. At a fitted
  oldest edge, the chart clamps that value even though the user's right-drag is
  unambiguous and emits no view-change event. Pointer direction plus the edge
  threshold is the minimal safe intent signal, checked again on pointer release.
- Strategy Shadows truthfully reports no comparable same-cycle scenarios. The
  requested 4-5 generated what-if variants are a separate product capability,
  not something this acceptance milestone may fabricate from production P&L.

### Verification

- Focused control-plane, sizing, edge-adjustment, read-model, and static
  dashboard tests: 141 passed.
- Dashboard JavaScript market/history and Range tests: 12 passed.
- Focused Playwright profit-control and Range acceptance: 2 passed.
- Real-browser acceptance evidence is recorded under
  `docs/evidence/issue-88/` with action latency and backend truth checks.

### Adversarial review corrections

- The auto-density solver now treats a venue precision failure as a rejected
  candidate, not the end of the 70-to-30 search. An explicit operator count
  remains strict and returns its exact precision error.
- Fixed-spacing edge orders now use the same modeled round-trip fee formula as
  initial sizing, persist `planned_net_profit_usd`, and fail closed if a new
  plan's 10 USD target would be violated.
- Range edge activation persists canonical projected actual leverage in both
  grid and risk budget; the dashboard no longer keeps the pre-adjustment value.
- Eighteen legacy control-plane assertions were brought onto the 30–70 grid,
  10x, profit-first contract. Lifecycle failure/recovery coverage remains and
  the focused module now passes all 69 tests.

### Gotchas

- A 70-grid candidate can be unrepresentable at venue price precision while a
  lower count is executable and profitable. Auto search must continue, but an
  explicit 70-grid request must not silently become a different strategy.
- Edge-only adjustment retains the existing notional. It cannot repair a
  profit shortfall by resizing; it rejects before staging any order.
- Edge prices, TP/SL, and downward-rounded quantities must pass through the
  same execution contract before profit, risk, dedupe, or submission. Raw
  geometry is retained as `requested_price` only for Range membership.
- Lifecycle tests need account headroom when intentionally adding edges. That
  is test setup for post-risk failure paths, not permission for production to
  bypass the 10x gate.

## 2026-07-22 - Paper manual Range risk acknowledgement

### Decision

- Treat a deliberate Paper Range replacement as an operator decision, not an
  automatic sizing decision. The final card shows every material old-to-new
  specification, projected leverage and margin, planned profit per grid, and a
  conservative maximum-loss scenario before execution.
- Require a separate explicit acknowledgement for the specification change,
  maximum loss, profit-target shortfall, leverage or margin excess, and market
  outside Range whenever each condition applies. The backend validates the
  exact acknowledgement set against the exact preview before staging orders.
- Permit acknowledged overrides only through the `nautilus_paper` adapter.
  Untrusted market data, reconciliation drift, and plan/order identity drift
  remain non-overridable fail-closed conditions.

### Gotchas

- A checkbox is not proof by itself. Preview identity and every required code
  are validated server-side. A digest binds the click to the displayed facts,
  and a candidate-risk digest is rechecked during launch and crash recovery so
  unchanged geometry cannot reuse consent after equity or policy facts move.
- The maximum-loss estimate is a bounded grid scenario: all same-side entries
  fill and exit at the planned stop one grid step outside the Range. New-plan
  loss excludes positions that the replacement transaction flattens first; it
  also excludes extreme gap slippage and funding, which the UI states.
- Leverage and margin confirmations use the same post-flatten candidate basis
  as the displayed new values. Pre-flatten exposure remains part of canonical
  reconciliation, but cannot create a checkbox that contradicts the card.
- Capital excess and profit shortfall are separate facts. A grid that still
  earns at least the target cannot be shown a profit-shortfall acknowledgement
  merely because its leverage or margin exceeds the automatic budget.
- The central risk decision remains truthfully `blocked`; a distinct Paper-only
  operator override authorizes execution. This avoids teaching other adapters
  that an exceeded limit is globally safe.

### Verification

- Focused control-plane, static dashboard, and Playwright tests: 99 passed.
- Dashboard Range JavaScript contract tests: 8 passed.
- Python 3.9 compilation and `git diff --check` passed.
- Browser evidence: `docs/evidence/issue-92/issue-92-risk-confirmation.png`.

## 2026-07-22 - Adaptive Paper grid parameter solver

### Decision

- Manual inputs are constraints, not suggestions. Editing Range, grid count,
  per-grid profit target, per-grid notional, or leverage locks that value; the
  solver changes only unlocked values.
- The default solution ranks all executable candidates by risk, profit-target
  shortfall, grid-band deviation, and distance from the current plan. A narrow
  Range may therefore produce fewer than 30 grids instead of an unusable
  preview.
- The 30–70 grid band, 10 USD target, and 10x leverage remain policy
  preferences for Paper. Deviations are rendered as explicit warnings and are
  executable only after exact server-bound acknowledgements.
- Invalid geometry, stale or synthetic market data, venue precision failures,
  account/execution reconciliation drift, and execution identity conflicts
  remain non-overridable hard stops.

### Formula

- Arithmetic spacing: `(high - low) / grid_count`; geometric spacing ratio:
  `(high / low) ** (1 / grid_count)`.
- Per-grid net profit uses venue-rounded quantity and modeled round-trip fees:
  `quantity * (abs(tp - entry) - fee_rate * (entry + tp))`.
- For every candidate count, the solver scales notional from a venue-rounded
  reference candidate until it meets the locked profit target, compares
  same-side exposure with locked/recommended leverage capacity, and selects the
  highest-density feasible candidate.
- When count and leverage are both locked, notional is capped by that leverage
  and any resulting profit shortfall is shown instead of silently changing a
  locked value.
- The preview exposes labeled `保格数` / `保收益` / `保杠杆` alternatives using
  the same venue-rounded economics so the selected trade-off is inspectable.

### Gotchas

- A displayed `10.00` can be internally below target after venue quantity
  flooring. The solver applies a deterministic 0.1% sizing cushion after each
  proportional solve so display rounding cannot invert the risk classification.
- The backend does not trust browser checkboxes alone. Consent binds the exact
  preview, displayed facts, canonical risk snapshot, and complete code set;
  start re-evaluates all of them before any plan or order mutation.
- Manual policy overrides are accepted only through the Nautilus Paper adapter.
  This change does not weaken live/real-money eligibility or exchange-key
  boundaries.
- Adaptive start requires the exact preview ID and zero accepted orders/open
  positions. Its confirmation uses full projected exposure; only replacement
  flows may use post-flatten candidate risk.
- Preview evaluates canonical state but does not persist a risk decision,
  activate a plan, submit an order, cancel an order, or alter a position.

### Adversarial review corrections

- Unlocked density now skips an unrepresentable high-count precision candidate
  and continues to lower executable counts; a locked count remains strict.
- Auto-sized notional is capped at the 20x Paper manual capacity. If locked
  values imply more than 20x actual leverage, preview remains explanatory but
  final confirmation is unavailable because that is the configured hard
  manual capacity, not a preference checkbox.
- Start no longer reuses replacement-only post-flatten risk logic and rejects
  an omitted/stale preview ID before plan or order mutation.
- Fractional grid counts are rejected instead of silently rounded.
- Preview identity now binds the normalized lock set and locked input values;
  Auto and Locked cannot share consent merely because their current economics
  happen to match.
- Start derives the zero-order/zero-position gate from one execution snapshot,
  preserving the existing independent pre-submit drift recheck.

### Verification

- Focused sizing, control-plane, static dashboard, and Playwright tests:
  127 passed.
- Python compilation and `git diff --check` passed.
- Browser evidence:
  `docs/evidence/issue-97/issue-97-parameter-controls.png` and
  `docs/evidence/issue-97/issue-97-adaptive-risk-confirmation.png`.

## 2026-07-22 - V5 history pagination input parity

### Decision

- Keep the fast 240-bar first paint. Historical depth remains an explicit,
  read-only pagination flow using the existing exclusive `end` cursor.
- Treat mouse-wheel and trackpad navigation as first-class history intent,
  alongside the existing right-drag gesture. A short-lived input token binds
  pagination to a real user gesture instead of chart initialization events.
- Continue merging fresh 240-bar snapshots into the accumulated dataset so
  five-second polling updates the live edge without deleting loaded history.

### Gotchas

- `subscribeVisibleLogicalRangeChange` also fires during render, resize, and
  viewport restoration. Loading solely from `range.from <= 24` can create an
  automatic page-fetch loop; the input token and `chartProgrammatic` guard are
  both required.
- The existing backend and deduplicating prepend path were still present on
  main. The regression was input-modality coverage: only a captured pointer
  drag could trigger it, while Park uses wheel/trackpad scrolling.
- History remains display-only. A trusted historical page never becomes a
  fresh execution event and cannot reopen the new-entry gate.

### Adversarial review correction

- A wheel event alone is not sufficient proof of older-history intent. The
  token now records the pre-input logical range and is consumed only when the
  viewport actually moves toward older candles; reverse wheel/trackpad input
  cannot spend a history request.
- Browser coverage uses real vertical wheel and horizontal trackpad deltas in
  both directions. Toolbar clicks are no longer an accidental proxy for wheel
  intent.

### Verification

- Market/history JavaScript tests: 5 passed.
- Static dashboard plus real-browser wheel/trackpad tests: 27 passed.
- The browser starts at 240 bars, rejects reverse-direction input without a
  request, then loads 320 trusted older bars and renders 560 total bars while
  retaining the live market gate. It also completes a live refresh while the
  history request is pending and crosses the real five-second polling interval;
  both paths retain all 560 bars without treating history as fresh execution
  data.
- Browser evidence:
  `docs/evidence/issue-99/issue-99-history-beyond-240.png`.
- `git diff --check` passed.

## 2026-07-22 - Crosshair date-time on the bottom time axis

### Decision

- Remove the duplicate date-time text from the top toolbar and render one
  TradingView-style label at the bottom of the chart, aligned to the vertical
  crosshair's X coordinate.
- The label uses the chart's configured timezone and locale, includes year,
  date, hour, and minute, and leaves the top toolbar dedicated to OHLC/source.
- Keep this behavior in the provider-neutral `standard-kline` package so every
  consuming K-line view gets the same coordinate-axis interaction.

### Gotchas

- Lightweight Charts reports crosshair X in chart-container coordinates, not
  page coordinates. The label must live inside the chart canvas and use that
  local X value directly.
- Near either edge, an exactly centered label would be clipped. Its center is
  clamped inside a 72px safe inset while remaining visually attached to the
  crosshair.
- When the pointer leaves a valid time point, both the label and its stale
  text must be cleared; OHLC safely returns to the latest candle.

### Verification

- `standard-kline` focused unit tests: 21 passed.
- Browser interaction test: 1 passed; the full date-time label follows the
  crosshair on the bottom axis, the top duplicate is absent, and the label
  clears after pointer exit.
- Browser evidence:
  `docs/evidence/issue-100/issue-100-crosshair-time-axis.png`.
- `git diff --check` passed.

## 2026-07-22 - Paper start candidate handshake

### Decision

- Treat one click on `启动机器人` as one explicit server-side preparation plus
  one commit of that exact candidate. The preparation receipt binds the active
  plan version, preview ID, grid orders, and a five-minute lifetime without
  creating a plan, order, position, or persisted risk decision.
- Permit ordinary trusted price ticks while the prepared candidate remains
  economically executable. The final start still rechecks current market
  trust, zero existing exposure, current plan identity, and the full risk
  policy before submitting any Paper order.
- Surface both success and failure in the existing top-right notification
  region. A failed start now states that production was not changed instead of
  leaving the only explanation below the fold.

### Gotchas

- Refreshing the preview immediately before start is insufficient: auto-range
  geometry can change again between two requests. Freezing browser inputs is
  also insufficient because a live tick can change which side owns a grid
  level. The server therefore prepares and later validates one exact candidate.
- A tick that crosses any prepared entry level makes an order marketable. That
  candidate is rejected fail-closed and the next click prepares a fresh one;
  the receipt never turns stale geometry into a market order.
- `preview` remains read-only and unaudited. `prepare_start` persists only a
  bounded staging receipt and is audited as an explicit operator start intent.
- Manual risk acknowledgement remains bound to its exact risk snapshot. This
  fix does not bypass confirmations, live/real-money eligibility, or exchange
  credential boundaries.

### Adversarial review corrections

- The final market check now compares the prepared and current grid-cell index,
  not only whether emitted orders became marketable. This closes the one-sided
  long-up/short-down case where crossed levels were filtered from the order
  list.
- Preparing a start cancels the debounce timer and invalidates any in-flight
  parameter preview, so a late response cannot overwrite the prepared
  candidate or its confirmation card.
- The receipt token binds the complete candidate, canonical market snapshot,
  active plan, lifetime, and execution adapter. Start also recomputes the
  canonical preview ID and normalizes damaged receipt data to a fail-closed
  `prepared_start_changed` response.
- Provider, source mode, symbol, timeframe, and Paper execution-adapter identity
  must remain unchanged between prepare and start.

### Verification

- Control-plane, audit, read-model API, static dashboard, and Playwright
  start-flow tests: 125 passed.
- Python compilation and `git diff --check` passed.
