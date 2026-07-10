# Decision Log

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
