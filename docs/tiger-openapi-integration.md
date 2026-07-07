# Tiger OpenAPI Integration

This track adds Tiger as a first-class market-data and future broker venue without changing the existing Binance demo path.

## Current Boundary

- M0/M1 are read-only: QuoteClient futures metadata and bars only.
- The Tiger RSA/config file must stay outside git and be referenced by `TIGER_OPENAPI_CONFIG_PATH`.
- The Tiger RSA/config file must be owner-only (`chmod 600`); the feed fails closed if the file is group/other accessible.
- The feed stores Tiger contracts under their own local symbols such as `MGCmain`, `MGC2608`, `1OZmain`, or `1OZ2608`; it does not overwrite the existing `GOLD`/Binance series.
- Continuous contracts such as `MGCmain` are acceptable for research data. Real order placement later must use dated tradable contracts such as `MGC2608`.
- Tiger paper TradeClient support exists behind an explicit profile gate, but the checked-in profile remains `dry_run=true`, `network_order_submission=not_implemented_fail_closed`, and `confirm_tiger_paper_orders=false`.
- Tiger kill-switch network cancel/close exists only behind an additional explicit flag. The checked-in profile keeps `enable_tiger_kill_switch_network_actions=false`.
- Tiger account/balance sync is read-only and uses `get_prime_assets`; it feeds money guardrails but does not enable order placement.
- Tiger network entries require same-path attached protection by default: a limit entry with both stop-loss and take-profit in the parent order.
- Tiger paper order drill uses local fake TradeClient/SDK objects only; it never creates a real Tiger network client.
- Tiger attended paper-order readiness is artifact-only and still requires explicit operator authorization before any real Tiger paper preview/place.
- Tiger attended paper-order canary now has a guarded CLI entry point. The default action is a ticket-specific artifact-only check; network submit requires an explicit submit flag, confirm flag, and fixed acknowledgement phrase.
- Futures live-money guardrails now include the Tiger contract multiplier when calculating candidate notional.
- Tiger has an attended paper canary risk package, but it is only used when the canary command explicitly passes `--use-attended-canary-risk-limits`; default checks still use the global live-money limits.
- Tiger paper-order approval packages are artifact-only runbooks. They generate the refresh commands, submit command, and manual kill path for an attended paper canary, but they do not authorize or submit orders.
- On mounted volumes, file modes may report group/other access even after local chmod. Use an owner-only local runtime copy of the Tiger properties file for approval/submission commands.
- The dual-track Tiger venue dashboard reads the approval package as a redacted status summary only. It does not expose the generated submit command and still has no Tiger order-control endpoint.
- Connector catalog is read-only: it reports venue/source capabilities and credential readiness without exposing credential values or opening broker/feed clients.
- Connector onboarding dry-run is also read-only: it validates requested connector roles and credential readiness booleans, rejects raw secret fields, and does not mutate runtime config.
- Connector activation plan is preview-only: it generates a config patch preview for selected roles, rejects raw secret fields, and does not write runtime config, open network clients, or create order-control endpoints.
- The dual-track browser chart uses backend `GET /api/dualtrack/market/bars`; the browser no longer opens a direct Binance websocket.
- Tiger realtime validation can be enforced with `--require-market-hours-pass`; that mode exits 0 only after a market-hours `status=pass` result and exits 75 for `pending_market_open`.
- The dual-track Tiger venue dashboard shows both realtime validation status and the market-hours gate status; it still has no Tiger order-control endpoint.
- Tiger price-feed readiness is evaluated by an artifact-only gate that requires both the historical feed import and the market-hours realtime gate to pass.
- Tiger price-feed acceptance is the one-command operator receipt for price-feed promotion. It may refresh read-only realtime validation, then writes readiness, connector catalog, and acceptance artifacts. It cannot enable broker orders.
- The dual-track Tiger venue dashboard shows the latest price-feed acceptance receipt as display-only `验收收据`.
- Connector catalog includes Tiger price-feed acceptance metadata for future onboarding UI, while `price_feed.status` remains based on the underlying readiness gate.
- Connector onboarding and activation preview now include Tiger price-feed acceptance evidence when the price-feed role is blocked.
- The connector panel renders onboarding blockers and activation warnings so operators can see the market-hours acceptance next action in the browser.
- Live readiness now has Tiger broker feedback based on Tiger price-feed readiness/acceptance artifacts.
- `DataSourcePreflight` now has an `MGCmain`/`1m` Tiger execution-venue config. Tiger bars can support paper/research, but `ready_for_live` requires `tiger_price_feed_readiness.ready_for_price_feed=true`.
- Non-default data-source preflight runs are namespaced under `outputs/data_source_preflight/<symbol>_<timeframe>/` so MGC checks do not overwrite the legacy global GOLD/5m current file.
- The Tiger venue read model and dashboard now show the `MGCmain`/`1m` data-source preflight as display-only `数据源预检`.
- One-command Tiger price-feed acceptance now refreshes the `MGCmain`/`1m` data-source preflight artifact after readiness/catalog evaluation.
- The Tiger venue read model and dashboard now show display-only operator timing for price-feed acceptance: wait, rerun now, accepted, blocked, or expired.
- Live readiness now selects market-data preflight and gap checks by broker provider; Tiger uses `MGCmain`/`1m` namespaced artifacts instead of legacy `GOLD`/`5m`.
- Live activation and live switch planning now use the same broker-aware market-data identity, so Tiger final gates read `outputs/data_source_preflight/MGCmain_1m/current.json`.
- Connector catalog/onboarding/activation/acceptance load local live env before credential readiness checks, but still expose only redacted readiness booleans and file mode.
- Tiger `MGCmain`/`1m` data-source freshness is COMEX-session-aware while price-feed acceptance is pending: closed-market bars can support paper/research visibility, but live readiness still waits for market-hours validation.
- Tiger price-feed acceptance receipts now include `operator_next_action`, so CLI/dashboard/future connector UI can all read the same wait/rerun/expired/accepted/blocked guidance.
- Tiger price-feed acceptance `operator_next_action` now uses the validation reference time rather than unconditional wall-clock time, so historical `as_of` runs and artifact-only aggregation remain deterministic.
- The Tiger venue read model now prefers acceptance receipt `operator_next_action`; local time-based calculation is only a legacy fallback.
- Connector catalog, onboarding dry-run, activation preview, and the browser connector panel now pass through the same acceptance `operator_next_action` summary as display-only guidance.
- Connector activation preview now includes a display-only `activation_gate` so the future platform-switch UI can distinguish blocked, preview-ready-with-warnings, and preview-ready states before any config-write milestone.
- Connector activation preview now also includes a display-only `activation_runbook` with blocker resolution, separate config-write boundary, post-apply validation, and rollback-boundary phases.
- Connector activation preview now includes an artifact-only `config_apply_package` that binds patch digest, operator acknowledgement, rollback requirements, and post-apply validation commands without applying config.
- Connector activation preview now includes `switch_audit`, a top-level read model for whether the selected connector can proceed to a separate config-write milestone.
- Dualtrack operational accounting now has an optional Tiger/MGC cost mode: one rung can be represented as whole MGC contracts, not continuous notional, and each side uses the fixed-per-contract fee model from `configs/risk_rules.yaml`.
- Dualtrack now has an optional COMEX futures session mask for Tiger/MGC operation. It keeps the legacy fixed 12h cycles by default, but a Tiger/MGC profile can skip runner activity during COMEX daily breaks/weekends and filter closed-session bars from cycle inputs.
- Tiger filled-order artifacts can be imported into the dualtrack human ledger, and an applied Tiger/MGC profile can run that import before close.
- Connector activation now previews the full Tiger/MGC dualtrack profile: MGC bars, COMEX session mask, integer-contract cost model, rung cap, and human-fill sync.
- Connector config apply/rollback is implemented as an attended, reversible boundary; the dashboard can produce dry-run receipts but does not send config-write acknowledgements.
- Dualtrack live tick is a generated local schedule job, separate from the boundary-cycle job, so the current and next human plans can stay synced while intraday machine sampling advances.
- The checked-in dualtrack default is still not switched to Tiger/MGC. Applying the profile remains a separate explicit config-write decision and still does not authorize Tiger broker orders.

## Milestones

1. M0 - Safety and baseline
   - Config path exists, is owner-only, and is not committed.
   - Tiger SDK access is treated as optional for tests.
   - No Tiger TradeClient code exists in the M0/M1 path.

2. M1 - Price feed
   - `python3 -m pipelines.tiger_futures_feed --contract MGCmain --limit 500` imports Tiger 1m bars into the local SQLite market database.
   - Historical backfill uses `python3 -m pipelines.tiger_futures_feed --contract MGCmain --backfill-start <iso> --backfill-end <iso> --backfill-total <n> --page-size <n> --time-interval <seconds>`.
   - Import artifacts are written under `outputs/tiger_futures_feed/`.
   - Bars carry `official_broker_feed`, `execution_venue_feed`, `exchange_futures`, and `tiger_openapi` quality flags.

3. M2 - Data quality and sessions
   - Use Tiger/CME trading-time metadata for COMEX session masks.
   - `python3 -m pipelines.tiger_futures_sessions --contract MGC2608 --trading-date YYYY-MM-DD` writes the raw trading/bidding windows under `outputs/tiger_futures_sessions/`.
   - Validate missing-minute behavior around daily breaks, weekends, and holidays.
   - Keep research outputs separate from production runner outputs.

4. M3 - Lab decision gate
   - Re-run grid/R5 experiments on Tiger COMEX bars with per-venue cost and integer-contract sizing.
   - Tiger MGC cost is represented as fixed USD per contract side in `configs/risk_rules.yaml`; `requires_bill_confirmation` remains true until matched against the real Tiger bill.
   - M3 exploratory artifact: `outputs/lab/reports/R5_tiger_mgc_contract_grid.md`.
   - Decide between MGC loop engine and 1OZ grid based on realized fee and liquidity evidence.

## M3 Exploratory Result - 2026-07-05

- Isolated research DB: `outputs/lab/tiger_m3/tiger_market.db`.
- MGCmain 1m bars: 33,900 rows, `2026-06-01T00:00:00+00:00` -> `2026-07-03T16:59:00+00:00`.
- 1OZmain 1m bars: 3,747 rows, `2026-07-01T00:00:00+00:00` -> `2026-07-03T16:59:00+00:00`.
- Tiger MGC integer-contract grid report: `outputs/lab/reports/R5_tiger_mgc_contract_grid.md`.
- Decision gate: fail for the random-direction arm; no valid random cell had positive mean net PnL per cycle.
- Diagnostic bound: oracle arms were strongly positive, anti/random/always-long were negative. This means the economics are not blocked by MGC fees alone, but require a direction edge; the grid shape by itself is not enough.
- This is not promotion evidence: no holdout was consumed and the sample is short. It is a first sizing/fee sanity check only.

5. M4 - Broker port recognition
   - Add a `tiger_openapi_paper` broker profile behind the existing broker port.
   - `tiger_openapi` preflight validates the local props file, checks owner-only permissions, and reports `network_order_submission=not_implemented`.
   - Non-dry-run Tiger order submission remains fail-closed.

6. M5 - Local Tiger paper order intent
   - Tiger dry-run requests write a Tiger futures-specific order intent under `outputs/tiger_order_requests/`.
   - The local intent includes `submission_intent=tiger_tradeclient_future_order`, `network_order_created=false`, COMEX contract, FUT security type, side, order type, limit price, stop loss, targets, and integer contract quantity.
   - Quantity is rejected unless it is a positive whole-contract integer.
   - No Tiger TradeClient network order is sent in M5.

7. M6 - Optional Tiger paper TradeClient adapter
   - `TigerOpenApiPaperBrokerAdapter` can build a Tiger futures order through an injected `TradeClient`.
   - Network submission is available only if the operator explicitly sets the profile to paper TradeClient mode (`dry_run=false`, `network_order_submission=paper_tradeclient`, `confirm_tiger_paper_orders=true`).
   - Before `place_order`, the adapter writes an order lifecycle intent, calls account prechecks, optionally previews the order, and blocks non-flat accounts or existing open orders.
   - Attached stop/target protection is allowed only on limit entry orders, matching Tiger's documented attached-order constraint.
   - Continuous contracts such as `MGCmain` are blocked for network orders; dated contract specs such as `MGC2608` must be present.

8. M7 - Read-only reconciliation and kill-switch dry-run
   - `TigerOpenApiPaperReconciliation` fetches Tiger paper positions and open orders through TradeClient read-only calls.
   - Reconciliation writes `outputs/tiger_reconciliation/` and blocks new Tiger paper orders unless the Tiger account is `confirmed_flat`.
   - `TigerOpenApiPaperKillSwitch` writes `outputs/tiger_kill_switch/` and plans cancel/close actions without sending cancel/close/place requests.
   - Confirming the Tiger kill-switch activates the shared HALT only; cancel/close remains manual/not implemented.
   - `live_readiness` and `live_switch_plan` now recognize `tiger_openapi` as a supported provider only when broker preflight is ready.

9. M8 - Order/fill attribution and gated kill-switch network path
   - `TigerOpenApiOrderSync` reads Tiger paper futures open orders and filled orders into `outputs/tiger_order_sync/`.
   - Sync uses read-only TradeClient methods only: `get_open_orders` and `get_filled_orders`.
   - The normalized artifact redacts account/key fields and gives the dual-track console a Tiger attribution fact surface.
   - `TigerOpenApiPaperKillSwitch` can execute cancel/close through an injected/future TradeClient path only when all gates are explicit: non-dry-run paper TradeClient mode, `confirm_tiger_paper_orders=true`, `enable_tiger_kill_switch_network_actions=true`, and runtime `confirm_tiger_kill`.
   - Default checked-in config remains non-network and creates no Tiger cancel/place requests.

10. M9 - Dual-track Tiger venue status
   - `TigerVenueStatus` reads local Tiger artifacts only: reconciliation, order sync, and kill-switch current JSON.
   - Dashboard API exposes `GET /api/dualtrack/venue/tiger`.
   - The dual-track console renders a Tiger paper venue card with reconciliation status, order/fill sync counts, kill-switch status, and the new-order gate.
   - No Tiger SDK client is opened by the dashboard request path.
   - No dual-track control endpoint is added; POST endpoints remain limited to plan, paper order, and verdict.

11. M10 - Dated contract rollover and money-guardrail fail-closed
   - `TigerContractResolver` maps research/continuous symbols such as `MGCmain` to explicit dated execution contracts through `execution_contract_map`.
   - Network Tiger orders are blocked unless the resolved execution contract is dated and outside the configured rollover window.
   - `python3 -m pipelines.tiger_contract_status --date YYYY-MM-DD --symbol MGCmain --json` writes `outputs/tiger_contracts/`.
   - The dual-track Tiger venue card now includes the execution contract and rollover status.
   - Tiger paper TradeClient network submission now requires live-money guardrail evidence before preview/place unless explicitly disabled for low-level fake-client tests.
   - At M10, Tiger reconciliation did not include balance/accounting/daily-PnL proof, so the guardrail failed closed with `BLOCKED_MONEY_GUARDRAIL_UNKNOWN` instead of treating missing evidence as zero loss.

12. M11 - Account/balance evidence for money guardrails
   - `TigerOpenApiAccountSync` reads paper account assets through Tiger TradeClient `get_prime_assets`.
   - `python3 -m pipelines.tiger_openapi_account_sync --date YYYY-MM-DD --json` writes `outputs/tiger_account_sync/`.
   - The Tiger paper adapter merges account sync evidence into reconciliation before `LiveMoneyGuardrails` and before any `preview_order`/`place_order`.
   - Missing or failed account sync remains fail-closed. Fresh finite account sync lets the daily-loss part of the guardrail become evidence-based, but existing notional/trade-limit gates still apply.
   - The dual-track Tiger venue card shows account evidence status from local artifacts only.

13. M12 - Same-path protective order invariant
   - `require_attached_protection_before_entry=true` is part of the default Tiger paper profile.
   - Network-capable Tiger entries are blocked before opening a Tiger client unless the ticket has a LIMIT entry, `stop_loss`, and at least one take-profit target.
   - Blocked protection failures write `outputs/tiger_order_requests/` artifacts with `network_order_created=false`.
   - Low-level fake-client drills may explicitly disable the requirement, but the checked-in operational default keeps it enabled.

14. M13 - Local fake-client paper order drill
   - `python3 -m pipelines.tiger_openapi_paper_order_drill --date YYYY-MM-DD --json` runs the Tiger paper order path in an isolated local fake-client runtime.
   - Scenario `default_guardrail_block` proves a real MGC one-contract notional is blocked by live-money limits before fake preview/place.
   - Scenario `simulated_green_order` uses small synthetic notional to prove contract resolution, reconciliation, account sync, money guardrails, parent-order attached TP/SL, fake preview/place, and lifecycle acceptance.
   - Artifacts are written under `outputs/tiger_paper_order_drill/`; per-scenario request/lifecycle files live under `outputs/tiger_paper_order_drill_runtime/<run_id>/`.
   - The dual-track Tiger venue card shows local paper order drill status from artifacts only.

15. M14 - Attended paper-order readiness gate
   - `python3 -m pipelines.tiger_openapi_paper_order_readiness --date YYYY-MM-DD --json` evaluates whether the evidence surface is ready for a human-authorized Tiger paper-order canary.
   - The gate reads artifacts only: venue status, dated contract, reconciliation, account sync, order/fill sync, kill-switch, local paper-order drill, and checked-in Tiger profile safety.
   - It writes `outputs/tiger_paper_order_readiness/` with `ready_for_attended_paper_order=true/false`.
   - It always sets `can_submit_without_explicit_operator_authorization=false` and `real_tiger_network_call_attempted=false`.
   - The dual-track Tiger venue card shows the readiness gate as `授权门`.

16. M15 - Attended paper-order canary entry point
   - `python3 -m pipelines.tiger_openapi_paper_order_canary --date YYYY-MM-DD --ticket-id <id> --asset <dated-contract> --side buy|sell --quantity <whole-contracts> --entry-price <p> --stop-loss <p> --take-profit <p> --json` performs a ticket-specific artifact-only check.
   - The check requires M14 readiness, one canary per day, a protected dated LIMIT ticket, and ticket-specific money guardrails.
   - The submit path exists but is fail-closed unless all three operator controls are present: `--submit-tiger-paper-canary`, `--confirm-tiger-paper-canary`, and `--acknowledge-tiger-paper-network-submission I_UNDERSTAND_TIGER_PAPER_TRADECLIENT_WILL_PREVIEW_AND_PLACE_AN_ORDER`.
   - The service writes `outputs/tiger_paper_order_canary/` and never submits without explicit operator authorization.
   - The 2026-07-05 read-only MGC2608 check stayed blocked before Tiger preview/place because 1 MGC at 4186 has contract-multiplier notional 41860, above the current live-money guardrail limits.

17. M16 - Attended paper canary risk package
   - `broker_profiles.tiger_openapi_paper.attended_paper_canary` defines a scoped paper-canary package: 1 contract, 45000 notional cap, and 2.5% max candidate stop-loss as a share of observed paper equity.
   - The package is not active by default. The canary command must include `--use-attended-canary-risk-limits` to evaluate or submit with these limits.
   - `LiveMoneyGuardrails` now accepts broker-level `live_money_guardrails` overrides so the canary can use scoped limits without changing `configs/risk_rules.yaml`.
   - The 2026-07-05 default MGC2608 check remains blocked by the global 10 notional cap.
   - The 2026-07-05 MGC2608 check with `--use-attended-canary-risk-limits` returned `ready_for_operator_authorization`, `submit_requested=false`, and `real_tiger_network_call_attempted=false`.

18. M17 - Operator approval runbook
   - `python3 -m pipelines.tiger_openapi_paper_order_approval --date YYYY-MM-DD --ticket-id <id> --asset <dated-contract> --side buy|sell --quantity <whole-contracts> --entry-price <p> --stop-loss <p> --take-profit <p> --operator <label> --props-path <owner-only-file> --json` builds an artifact-only operator approval package.
   - The approval package requires a ready ticket-specific canary check, an owner-only Tiger properties file, and an operator label.
   - It writes `outputs/tiger_paper_order_approval/` with pre-submit refresh commands, the exact submit command, post-submit read-only checks, and the manual kill path.
   - It always sets `submit_requested=false`, `can_submit_without_explicit_operator_authorization=false`, and `real_tiger_network_call_attempted=false`.
   - It exits non-zero when the package is blocked.

19. M18 - Approval visibility in dual-track venue
   - `TigerVenueStatus` reads `outputs/tiger_paper_order_approval/current.json` as a redacted status summary.
   - `GET /api/dualtrack/venue/tiger` includes `paper_order_approval` with status, ticket id, blocker count, canary risk numbers, and safety flags.
   - The API intentionally omits the generated `submit_command` and does not add any Tiger order-control POST endpoint.
   - The dual-track Tiger venue card shows an `授权包` row so the operator can see whether the M17 runbook is ready.
   - If an approval artifact ever indicates `submit_requested=true` or `real_tiger_network_call_attempted=true`, the venue status becomes `manual_review`.

20. M19 - Connector catalog foundation
   - `ConnectorCatalog` exposes a read-only catalog of supported connectors: Binance USD-M, Tiger OpenAPI, Yahoo Chart, and gold-api.com.
   - `python3 -m pipelines.connector_catalog --date YYYY-MM-DD --json` writes `outputs/connector_catalog/current.json` and the dated artifact.
   - `GET /api/connectors/catalog` returns connector roles, ports, capabilities, credential variable names, and readiness booleans.
   - The catalog never returns API key values, private key contents, or the Tiger properties file path.
   - The dual-track Tiger venue card shows a `接入目录` row with ready price-feed and broker counts.

21. M20 - Connector onboarding dry-run
   - `ConnectorOnboardingDryRun` validates a requested connector, roles, and credential readiness confirmations without storing credential values.
   - `python3 -m pipelines.connector_onboarding --connector-id tiger_openapi --role price_feed --role broker_order --environment paper --json` writes `outputs/connector_onboarding/current.json`.
   - `POST /api/connectors/onboarding/dry-run` accepts only connector id, roles, environment, and readiness booleans.
   - Raw fields such as `api_key`, `private_key`, `token`, `license`, or `tiger_id` are rejected before any artifact is written.
   - `dashboard-dualtrack-v5.html` adds a `接入 CONNECTOR` panel for dry-run validation; it has no API-key text field, does not save credentials, and does not add a broker order endpoint.

22. M21 - Connector activation plan preview
   - `ConnectorActivationPlan` builds a preview of config changes needed to activate selected connector roles.
   - `python3 -m pipelines.connector_activation_plan --connector-id tiger_openapi --role price_feed --role broker_order --environment paper --json` writes `outputs/connector_activation_plan/current.json`.
   - `POST /api/connectors/activation/plan` accepts the same safe payload shape as onboarding and returns `config_patch_preview`.
   - Tiger price-feed preview enables `tiger_futures_feed.enabled`; Tiger broker preview selects `broker.provider=tiger_openapi`, `broker.profile=tiger_openapi_paper`, `broker.environment=paper`, `broker.dry_run=true`, and `demo_trading.broker_profile=tiger_openapi_paper` while removing Binance-specific broker overrides from the preview.
   - The preview explicitly warns that Tiger paper TradeClient network submission remains unarmed.
   - The dashboard `接入 CONNECTOR` panel adds `预览激活`; this still does not write `configs/pipeline.yaml`, open SDK clients, submit orders, or add a broker order-control endpoint.

23. M22 - Demo runner broker-profile adapterization
   - `MultiStrategyRunner` now resolves the active `demo_trading.broker_profile` before choosing the execution adapter.
   - `binance_usdm` profiles still route to `BinanceDemoBrokerAdapter`, preserving the current production demo behavior.
   - `tiger_openapi` paper profiles route to `TigerOpenApiPaperBrokerAdapter` through the existing broker adapter factory.
   - Active Tiger demo reconciliation uses `TigerOpenApiPaperReconciliation`, which is read-only and writes local reconciliation artifacts without placing/cancelling/closing orders.
   - Tiger execution profile summaries expose only env variable names and guarded flags; they do not expose the Tiger properties path or secret contents.
   - Any unsupported demo broker profile routes through a fail-closed adapter with `live_trading_enabled=false`.

24. M23 - Dual-track backend market bars
   - `GET /api/dualtrack/market/bars` returns a read-only chart snapshot from the local market DB.
   - Source priority is Tiger/COMEX bars first, then cached Binance USD-M 1m bars, then explicit display-only synthetic seed bars if the DB is missing or empty.
   - The dashboard no longer contains a Binance websocket URL or browser-side exchange socket.
   - The endpoint is not part of `_DUALTRACK_POST_ENDPOINTS` and does not open broker, order, or feed SDK clients.

25. M24 - First Tiger MGCmain feed into the shared market DB
   - Ran `python3 -m pipelines.tiger_futures_feed --contract MGCmain --limit 20` with `TIGER_OPENAPI_CONFIG_PATH` set to the local owner-only runtime properties file.
   - Result: `status=pass`, `imported_rows=20`, `latest_timestamp=2026-07-03T16:59:00+00:00`, `latest_price=4186.9`.
   - Shared market DB coverage now includes `MGCmain`/`1m`/`tiger_openapi:COMEX` from `2026-07-03T16:40:00+00:00` to `2026-07-03T16:59:00+00:00`.
   - `GET /api/dualtrack/market/bars?limit=5` now returns `source_mode=tiger_openapi`, `symbol=MGCmain`, and `provider=tiger_openapi:COMEX`.
   - The feed receipt does not include the properties file path or credential values.

26. M25 - Standard Tiger MGCmain dashboard window
   - Ran `python3 -m pipelines.tiger_futures_feed --contract MGCmain --limit 500` against the shared market DB.
   - Result: `status=pass`, `imported_rows=500`, `latest_timestamp=2026-07-03T16:59:00+00:00`, `latest_price=4186.9`.
   - Shared market DB coverage now includes 500 `MGCmain`/`1m`/`tiger_openapi:COMEX` rows from `2026-07-03T08:40:00+00:00` to `2026-07-03T16:59:00+00:00`.
   - Dashboard default query `GET /api/dualtrack/market/bars?limit=96` now returns 96 Tiger/COMEX bars from `2026-07-03T15:24:00+00:00` to `2026-07-03T16:59:00+00:00`.
   - Tiger quote permission metadata in this run reported `aStockQuoteLv1` and `has_futures_realtime=false`; historical/recent bars worked, but futures realtime behavior still needs a market-hours validation pass.
   - The feed receipt does not include the properties file path or credential values.

27. M26 - Tiger realtime validation harness
   - Added `python3 -m pipelines.tiger_realtime_validation --contract MGCmain --poll-seconds <seconds>`.
   - The validator is read-only and uses Tiger QuoteClient only; it does not write the market DB, open TradeClient, or submit orders.
   - It first checks Tiger trading windows. If COMEX is closed, it writes `status=pending_market_open` instead of pretending realtime validation passed.
   - Current real run at `2026-07-05T14:49:22+00:00` returned `status=pending_market_open`.
   - Next Tiger trading window from the artifact: `2026-07-05T22:00:00+00:00` to `2026-07-06T21:00:00+00:00`, `trading_date=2026-07-06`.
   - Quote permission metadata still reports `aStockQuoteLv1` and `has_futures_realtime=false`; rerun during that window with a nonzero poll interval to prove whether recent 1m bars advance in market hours.
   - Artifact path: `outputs/tiger_realtime_validation/current.json`.

28. M27 - Tiger realtime validation status in the venue dashboard
   - `TigerVenueStatus` now reads `outputs/tiger_realtime_validation/current.json` and returns a redacted `realtime_validation` summary.
   - Dashboard `老虎 TIGER · PAPER VENUE` now shows `行情验证`.
   - `pending_market_open` and missing realtime-validation artifacts do not degrade the Tiger venue; market-hours `warn` or `fail` validation does degrade the venue and surfaces the validation message in the headline.
   - The venue summary exposes status, timestamps, next trading window, freshness/advance fields, quote permission names, and safety flags. It does not expose the Tiger properties file path or credential values.

29. M28 - Market-hours realtime validation gate
   - `python3 -m pipelines.tiger_realtime_validation --contract MGCmain --require-market-hours-pass` writes the same redacted validation artifact and adds `market_hours_gate`.
   - Default validation remains compatible and exits 0 after writing the artifact.
   - Gate mode exits 0 only when `status=pass`, exits 75 when the market is not open yet, and exits 2 for `warn`, `fail`, or unknown validation states.
   - The gate is read-only: it opens no TradeClient, writes no market DB bars, creates no order-control endpoint, and does not authorize Tiger paper submission.
   - Real pre-open M28 run at `2026-07-05T15:21:57+00:00` returned `status=pending_market_open`, `market_hours_gate.exit_code=75`, `operator_action=rerun_after_next_trading_window`, and next window `2026-07-05T22:00:00+00:00` to `2026-07-06T21:00:00+00:00`.

30. M29 - Market-hours gate visibility in venue dashboard
   - `TigerVenueStatus` now exposes `realtime_validation.market_hours_gate` as a redacted summary: required, ready flag, market-hours observed flag, exit code, operator action, and next trading window.
   - Dashboard `老虎 TIGER · PAPER VENUE` now shows `行情门禁` next to `行情验证`, so operators can distinguish "not open yet, retry later" from a market-data failure.
   - M29 is display-only. It does not change venue degradation rules, does not open Tiger SDK clients, and does not add any Tiger order-control endpoint.

31. M30 - Tiger price-feed readiness gate
   - `python3 -m pipelines.tiger_price_feed_readiness --date YYYY-MM-DD --json` reads local artifacts only: `outputs/tiger_futures_feed/current.json` and `outputs/tiger_realtime_validation/current.json`.
   - The gate requires feed import `status=pass`, `ready=true`, at least 500 imported bars, realtime validation `status=pass`, and `market_hours_gate.exit_code=0`.
   - It writes `outputs/tiger_price_feed_readiness/current.json` and the dated artifact, but opens no Tiger SDK client, writes no market DB bars, creates no order-control endpoint, and cannot enable broker orders.
   - Real M30 run at `2026-07-05T15:44:29+00:00` returned `status=blocked`: feed import passed with 500 rows, but `realtime_market_hours_gate` remained blocked because the market-hours gate was still `pending_market_open` with exit code 75.

32. M31 - Price-feed readiness visibility in venue dashboard
   - `TigerVenueStatus` now reads `outputs/tiger_price_feed_readiness/current.json` and exposes a redacted `price_feed_readiness` summary.
   - Dashboard `老虎 TIGER · PAPER VENUE` now shows `价格源门`, including readiness status and blocker count.
   - M31 is display-only. A blocked price-feed readiness artifact does not change the paper venue/order status, does not open Tiger SDK clients, and does not add any order-control endpoint.

33. M32 - Connector catalog uses Tiger price-feed readiness
   - `ConnectorCatalog` now reads `outputs/tiger_price_feed_readiness/current.json` for Tiger `price_feed` capability status.
   - Tiger credential missing or non-owner-only still returns `needs_credentials`; credential ready plus blocked price-feed readiness returns `blocked`; only `ready_for_price_feed=true` returns `ready`.
   - `ConnectorOnboardingDryRun` and `ConnectorActivationPlan` pass their `output_root` into the catalog, so dry-run/preview decisions use the same local readiness evidence as the dashboard.
   - Real M32 catalog run with the owner-only Tiger properties file returned Tiger `broker_order=ready`, Tiger `price_feed=blocked`, `ready_price_feed_count=3`, and `ready_broker_count=1`; no credential value or properties path was returned.

34. M33 - One-command Tiger price-feed acceptance
   - `python3 -m pipelines.tiger_price_feed_acceptance --date YYYY-MM-DD --contract MGCmain --poll-seconds 75 --json` refreshes the read-only realtime gate, evaluates `tiger_price_feed_readiness`, refreshes `connector_catalog`, and writes `outputs/tiger_price_feed_acceptance/current.json`.
   - Status vocabulary is `accepted`, `pending_market_open`, or `blocked`. Exit code is 0 for accepted, 75 for pending market open, and 2 for blocked.
   - The command has `--skip-realtime-run` for artifact-only aggregation when the operator does not want to open a QuoteClient.
   - Safety boundary: the acceptance command opens no TradeClient, submits no orders, writes no market DB bars, creates no order-control endpoint, and cannot enable broker orders.
   - Acceptance only promotes Tiger as a price source for research/dashboard use. It does not authorize Tiger paper-order submission or real-money routing.

35. M34 - Price-feed acceptance visibility
   - `TigerVenueStatus` reads `outputs/tiger_price_feed_acceptance/current.json` and exposes a redacted `price_feed_acceptance` summary.
   - Dashboard `老虎 TIGER · PAPER VENUE` now renders `验收收据` with accepted/pending/blocked state, exit code or blocker count, and no credential details.
   - M34 is display-only. It does not open Tiger SDK clients, run realtime validation, write market bars, change broker readiness, or add any order-control endpoint.

36. M35 - Connector catalog acceptance metadata
   - `ConnectorCatalog` now includes `capabilities[].acceptance` for Tiger `price_feed`.
   - The acceptance metadata includes status, ready flag, exit code, blocker count, operator action, next trading window, and checked-at timestamp.
   - The catalog still does not expose credential values or the Tiger properties path.
   - Important boundary: Tiger `price_feed.status` still uses credential readiness plus `tiger_price_feed_readiness`; it does not circularly depend on `tiger_price_feed_acceptance`, because the acceptance command itself refreshes the catalog.

37. M36 - Acceptance-aware connector onboarding and activation
   - `ConnectorOnboardingDryRun` includes Tiger readiness and acceptance summaries in `role:price_feed` evidence.
   - If Tiger price feed is blocked by `pending_market_open`, onboarding summaries now say to rerun after the next market-hours window instead of only saying the role is not ready.
   - `ConnectorActivationPlan` emits `tiger_price_feed_acceptance_not_ready` warnings with acceptance status, exit code, and next-window evidence.
   - M36 still does not run acceptance, open Tiger SDK clients, store credentials, write runtime config, or create order-control endpoints.

38. M37 - Connector panel blocker visibility
   - Dashboard `接入 CONNECTOR` now renders `验证阻塞` from onboarding blockers and `预览警告` from activation warnings/blockers.
   - For Tiger price-feed acceptance pending market open, the row includes the next-window timestamp returned by the backend evidence.
   - M37 is frontend display only. It does not call Tiger SDK clients, run realtime validation, write runtime config, or create order-control endpoints.

39. M38 - Tiger live-readiness broker feedback
   - `LiveReadiness._broker_feedback` now has a `tiger_openapi` branch.
   - Tiger broker feedback passes only when `outputs/tiger_price_feed_readiness/current.json` has `ready_for_price_feed=true`.
   - If the latest acceptance receipt is `pending_market_open`, live readiness reports the next market-hours window instead of a generic missing feedback message.
   - M38 reads local artifacts only. It does not run acceptance, open Tiger SDK clients, write market bars, submit orders, or turn price-feed readiness into broker-order authorization.

40. M39 - Tiger execution-venue data preflight gate
   - `configs/pipeline.yaml` now defines `market_data_sources.mgcmain_1m` for Tiger/COMEX `MGCmain` 1m bars.
   - `DataSourcePreflight(symbol="MGCmain", timeframe="1m")` treats `tiger_openapi:COMEX` as an execution-venue provider, but live readiness also requires the Tiger price-feed readiness artifact to pass.
   - If Tiger bars exist while acceptance is `pending_market_open`, preflight can still report paper/research usability, but `ready_for_live=false` with the next market-hours window in evidence.
   - M39 reads local readiness/acceptance artifacts only. It does not run acceptance, open Tiger SDK clients, write market bars, submit orders, or authorize broker order routing.

41. M40 - Namespaced non-default preflight artifacts
   - `python3 -m pipelines.data_source_preflight --symbol MGCmain --timeframe 1m` is now supported.
   - Shared-output non-default runs write `outputs/data_source_preflight/MGCmain_1m/current.json` and the dated file instead of overwriting `outputs/data_source_preflight/current.json`.
   - `DataSourcePreflight` falls back to the SQLite latest bar when `outputs/clean_bars/<date>/<symbol>_<timeframe>.json` is absent, which matches Tiger feed imports.
   - The legacy flat preflight path remains compatible for `GOLD`/`5m` and for callers using a scoped strategy output root.

42. M41 - MGC preflight visibility in Tiger venue
   - `TigerVenueStatus` now reads `outputs/data_source_preflight/MGCmain_1m/current.json` and returns a redacted `data_source_preflight` summary.
   - Dashboard `老虎 TIGER · PAPER VENUE` now shows `数据源预检`, including pending market-open evidence when the Tiger MGC data source is not live-ready.
   - This row is display-only. It does not run data preflight, open Tiger SDK clients, write market bars, change venue readiness, submit orders, or authorize broker routing.

43. M42 - Acceptance refreshes MGC data-source preflight
   - `TigerPriceFeedAcceptance` now refreshes `DataSourcePreflight(symbol="MGCmain", timeframe="1m", write_legacy_artifacts=false)` as part of the one-command acceptance run.
   - The acceptance receipt includes a `steps.data_source_preflight` summary and an evidence path to `outputs/data_source_preflight/MGCmain_1m/current.json`.
   - `--skip-realtime-run` still does not open QuoteClient; it can refresh the local data-source preflight from SQLite/artifacts only.
   - M42 does not write market bars, open TradeClient, submit orders, or authorize broker routing.

44. M43 - Acceptance operator timing visibility
   - `TigerVenueStatus.price_feed_acceptance` now includes `operator_status`, `operator_summary`, and a safe `next_command`.
   - Operator status distinguishes `waiting_market_open`, `rerun_acceptance_now`, `window_expired`, `accepted`, and `blocked`.
   - Dashboard `老虎 TIGER · PAPER VENUE` now shows `验收下一步`.
   - This is display-only. It does not auto-run acceptance, schedule SDK work, write market bars, open TradeClient, submit orders, or authorize broker routing.

45. M44 - Broker-aware live-readiness market data
   - `LiveReadiness.run()` now resolves the broker provider before market-data checks.
   - For `tiger_openapi`, live readiness runs `DataSourcePreflight(symbol="MGCmain", timeframe="1m", write_legacy_artifacts=false)` and `DataGapDoctor(symbol="MGCmain", timeframe="1m")`.
   - `DataGapDoctor` can fall back to SQLite market bars when clean-bars artifacts are absent, matching Tiger feed imports.
   - Non-default gap checks are namespaced under `outputs/data_gaps/MGCmain_1m/` and do not overwrite legacy `outputs/data_gaps/current.json`.
   - M44 does not import market bars, open Tiger SDK clients, submit orders, or authorize broker routing.

46. M45 - Broker-aware activation and switch planning
   - `LiveActivationGate` now resolves the active provider from broker preflight/config and, for `tiger_openapi`, reads `outputs/data_source_preflight/MGCmain_1m/` before evaluating the market-data gate.
   - `LiveSwitchPlan` now uses the same Tiger `MGCmain`/`1m` scoped artifact path in its evidence and safety order.
   - Legacy `outputs/data_source_preflight/current.json` can remain the `GOLD`/`5m` surface without blocking or passing Tiger's final gate.
   - M45 does not run Tiger SDK clients, import bars, submit orders, mutate runtime config, or authorize broker routing.

47. M46 - Connector readiness loads local live env
   - `ConnectorCatalog` now has an explicit `load_live_env` option. CLI/dashboard usage loads the local live env before checking credential presence; explicit test/config usage remains isolated unless the caller opts in.
   - `ConnectorOnboardingDryRun`, `ConnectorActivationPlan`, and `TigerPriceFeedAcceptance` propagate that behavior for real operator-facing runs.
   - Real local M46 artifact check returned Tiger `broker_order=ready`, Tiger `price_feed=blocked`, and blocker summary "waiting for the next market-hours validation window"; this replaced the earlier false `needs_credentials` state.
   - Output remains redacted: credential values and the Tiger properties file path are not emitted; only key name, present/file-exists/owner-only booleans, and mode are returned.
   - M46 does not open Tiger SDK clients, write market bars, submit orders, mutate runtime config, or authorize broker routing.

48. M47 - COMEX-session-aware Tiger data-source freshness
   - `DataSourcePreflight` now suspends current-session freshness enforcement only when the Tiger execution-venue gate is explicitly `pending_market_open` and the recorded next COMEX trading window has not started.
   - Before that next window, stale-looking Friday MGC bars can be `ready_for_paper=true` for research/dashboard visibility.
   - `ready_for_live` remains false because the Tiger price-feed readiness gate still blocks until market-hours realtime validation passes.
   - Once the recorded window starts, stale bars are enforced again and preflight returns blocked until fresh market-hours evidence is imported/validated.
   - Real local M47 artifact check returned `data_source_preflight.status=warn`, `ready_for_paper=true`, `ready_for_live=false`, and `gate_status=pending_market_open`.
   - M47 does not open Tiger SDK clients, write market bars, submit orders, mutate runtime config, or authorize broker routing.

49. M48 - Acceptance receipt operator next action
   - `TigerPriceFeedAcceptance` now writes `operator_next_action` into the acceptance receipt.
   - Status vocabulary is `waiting_market_open`, `rerun_acceptance_now`, `window_expired`, `accepted`, and `blocked`.
   - The field includes a summary, the next safe command when retrying is appropriate, and the next trading window when available.
   - Real local M48 artifact check returned `operator_next_action.status=waiting_market_open` and the next command for `python3 -m pipelines.tiger_price_feed_acceptance ...`.
   - This is guidance only. M48 does not auto-run acceptance, open TradeClient, write market bars, submit orders, mutate runtime config, or authorize broker routing.

50. M49 - Venue read model consumes acceptance operator guidance
   - `TigerVenueStatus.price_feed_acceptance` now prefers `operator_next_action` from `outputs/tiger_price_feed_acceptance/current.json`.
   - Legacy acceptance receipts without `operator_next_action` still fall back to local time-window calculation.
   - Real local M49 venue snapshot returned `operator_status=waiting_market_open` and the same next command as the acceptance receipt.
   - M49 does not auto-run acceptance, open Tiger SDK clients, write market bars, submit orders, mutate runtime config, or authorize broker routing.

51. M50 - Connector surfaces consume acceptance operator guidance
   - `ConnectorCatalog` includes acceptance `operator_status`, `operator_summary`, and safe `next_command` metadata for Tiger price-feed capability.
   - `ConnectorOnboardingDryRun` and `ConnectorActivationPlan` propagate that same guidance into role evidence and activation warnings.
   - `dashboard-dualtrack-v5.html` now renders connector blockers from `acceptance.operator_summary` when present.
   - M50 does not auto-run acceptance, open Tiger SDK clients, write market bars, write runtime config, submit orders, mutate broker routing, or authorize broker execution.

52. M51 - Connector activation gate
   - `ConnectorActivationPlan` now returns `activation_gate` with status, operator status, summary, safe next command, and explicit non-mutating safety flags.
   - The gate reports `blocked` while Tiger price-feed acceptance is waiting for market open, `preview_ready_with_warnings` when a patch preview exists but warnings remain, and `preview_ready` only when there are no blockers or warnings.
   - `dashboard-dualtrack-v5.html` renders this as `切换门` inside the connector panel.
   - M51 does not apply config patches, open Tiger SDK clients, run acceptance, write market bars, submit orders, mutate broker routing, or authorize broker execution.

53. M52 - Connector activation runbook
   - `ConnectorActivationPlan` now returns `activation_runbook` with ordered phases for resolving the current gate, a separate config-write boundary, post-apply validation, and rollback requirements.
   - When Tiger is blocked by market-hours acceptance, the first phase carries the same safe acceptance command from `operator_next_action`.
   - `dashboard-dualtrack-v5.html` renders this as `切换步骤`.
   - M52 does not apply config patches, run listed commands, open Tiger SDK clients, write market bars, submit orders, mutate broker routing, or authorize broker execution.

54. M53 - Connector config-apply approval package
   - `ConnectorActivationPlan` now returns `config_apply_package` with patch digest, pre-apply checks, required acknowledgement, config-write boundary, rollback requirement, and post-apply validation commands.
   - `dashboard-dualtrack-v5.html` renders this as `审批包`.
   - While Tiger price-feed acceptance is pending market open, the package remains `blocked`; when only warnings remain, it reports `operator_review_required`.
   - M53 does not apply config patches, run listed commands, open Tiger SDK clients, store credentials, expose credential values, write market bars, submit orders, mutate broker routing, or authorize broker execution.

55. M54 - Connector switch audit
   - `ConnectorActivationPlan` now returns `switch_audit`, which aggregates onboarding, activation gate, runbook, config-apply package, and broker-order network status into one read-only switch receipt.
   - `dashboard-dualtrack-v5.html` renders this as `切换审计`.
   - The audit always keeps `can_switch_connector_from_this_endpoint=false`, `can_apply_config_now=false`, and `can_enable_broker_orders=false`.
   - M54 does not apply config patches, run listed commands, open Tiger SDK clients, store credentials, expose credential values, write market bars, submit orders, mutate broker routing, or authorize broker execution.

56. M55 - Acceptance operator guidance reference time
   - `TigerPriceFeedAcceptance.operator_next_action` now uses explicit `as_of` when provided, loaded realtime artifact `checked_at` for artifact-only aggregation, and current acceptance time otherwise.
   - This fixes a wall-clock drift bug where historical pending-market-open receipts could flip to `rerun_acceptance_now` when tests or re-aggregation ran after the recorded window opened.
   - M55 does not open Tiger SDK clients, write market bars, submit orders, mutate runtime config, or authorize broker execution.

## M5 Broker Boundary - 2026-07-05

- Config profile: `broker_profiles.tiger_openapi_paper`.
- Request artifact directory: `outputs/tiger_order_requests/`.
- Current status: local paper order intent only; `network_order_created=false`.
- Explicit blocker for real API order placement: Tiger TradeClient submission, protective order attachment, reconciliation, kill-switch, and daily-loss guardrails are not implemented for Tiger yet.

## M6 Paper TradeClient Boundary - 2026-07-05

- Adapter: `services.tiger_openapi_broker_adapter.TigerOpenApiPaperBrokerAdapter`.
- Factory path: `services.broker_adapter.resolve_broker_config` + `build_live_broker_adapter`.
- Default checked-in state remains non-network: `dry_run=true`, `confirm_tiger_paper_orders=false`.
- The tested network-capable paper path uses injected fake SDK/client objects and proves call order: account positions -> open orders -> preview -> place.
- No real Tiger TradeClient order was submitted during implementation or tests.

## M7 Reconciliation Boundary - 2026-07-05

- Reconciliation artifact directory: `outputs/tiger_reconciliation/`.
- Kill-switch artifact directory: `outputs/tiger_kill_switch/`.
- The Tiger adapter now requires read-only Tiger reconciliation before any paper TradeClient network submission.
- `confirmed_flat` is required before preview/place; open positions, open orders, drifts, unknown state, or suspected naked positions block new orders.
- The Tiger kill-switch is intentionally dry-run/manual: it can activate HALT, but it does not call Tiger cancel/close/place APIs.
- Real read-only smoke on 2026-07-05:
  - `python3 -m pipelines.tiger_openapi_reconciliation --date 2026-07-05 --json` returned `confirmation_status=confirmed_flat`, `exchange_positions=[]`, `exchange_open_orders=[]`.
  - `python3 -m pipelines.tiger_openapi_kill_switch --date 2026-07-05 --json` returned `status=dry_run`, `network_order_created=false`, `network_cancel_created=false`.

## M8 Attribution Boundary - 2026-07-05

- Order/fill sync service: `services.tiger_openapi_order_sync.TigerOpenApiOrderSync`.
- Pipeline: `python3 -m pipelines.tiger_openapi_order_sync --date YYYY-MM-DD --json`.
- Artifact directory: `outputs/tiger_order_sync/`.
- Sync status vocabulary: `synced` or `cannot_sync`.
- The sync path is read-only and does not import or call Tiger order placement/cancel APIs.
- The kill-switch network path is implemented for paper drills and fake-client tests, but the default profile keeps it disabled with `enable_tiger_kill_switch_network_actions=false`.
- Real read-only smoke on 2026-07-05 returned `sync_status=synced`, `open_order_count=0`, and `filled_order_count=0`.
- Tiger SDK filled-order reads require a time window; the pipeline defaults to `run_date 00:00:00` through `run_date 23:59:59`.

## M9 Dual-Track Venue Boundary - 2026-07-05

- Read model: `services.tiger_venue_status.TigerVenueStatus`.
- Dashboard endpoint: `GET /api/dualtrack/venue/tiger`.
- Frontend: `dashboard-dualtrack-v5.html` loads the endpoint and renders `老虎 TIGER · PAPER VENUE`.
- Payload status vocabulary: `ready`, `blocked`, `manual_review`, `degraded`, or `unknown`.
- Dashboard safety invariant: this endpoint reads artifacts only and does not open Tiger TradeClient or QuoteClient.
- Dual-track POST endpoints remain unchanged: `/api/dualtrack/plan`, `/api/dualtrack/orders`, `/api/dualtrack/verdict`.

## M10 Contract / Guardrail Boundary - 2026-07-05

- Contract resolver: `services.tiger_contracts.TigerContractResolver`.
- Contract status pipeline: `python3 -m pipelines.tiger_contract_status --date 2026-07-05 --symbol MGCmain --json`.
- Current resolver output: `MGCmain -> MGC2608`, `status=ready`, `days_to_contract_month=27`.
- Config keys: `execution_contract_map`, `rollover_policy.rollover_days_before_contract_month`, and `require_live_money_guardrails_before_entry`.
- Checked-in Tiger profile keeps `require_live_money_guardrails_before_entry=true`.
- Low-level fake-client tests can set `require_live_money_guardrails_before_entry=false` to prove order construction; that is not the operational default.
- At this boundary, before M11 account sync, an armed Tiger paper TradeClient order was expected to stop before preview/place at the money-guardrail gate.

## M11 Account / Guardrail Evidence Boundary - 2026-07-05

- Account sync service: `services.tiger_openapi_account_sync.TigerOpenApiAccountSync`.
- Pipeline: `python3 -m pipelines.tiger_openapi_account_sync --date YYYY-MM-DD --json`.
- Artifact directory: `outputs/tiger_account_sync/`.
- Read-only Tiger SDK method: `TradeClient.get_prime_assets(base_currency="USD", consolidated=True)`.
- Normalized fields include `exchange_balance.balance`, `exchange_balance.available`, `exchange_accounting.net_realized_pnl_estimate`, `exchange_accounting.unrealized_pnl_estimate`, and an exact UTC trading-day window.
- The Tiger adapter now refreshes account sync after flat reconciliation and before live-money guardrails. It still calls no `preview_order` or `place_order` if account sync is missing, stale, non-finite, or if notional limits block the order.
- Real read-only smoke on 2026-07-05 returned `sync_status=synced`, `segment_key=C`, `balance_present=true`, `accounting_observed=true`, and zero realized/unrealized PnL for the observed paper account.
- The dashboard Tiger venue read model now includes `account_sync`; the dashboard path remains artifact-only and does not open a Tiger SDK client.

## M12 Protective Order Boundary - 2026-07-05

- Config key: `broker_profiles.tiger_openapi_paper.require_attached_protection_before_entry`.
- Checked-in default: `true`.
- The Tiger paper adapter now performs `protective_order_precheck` after dated-contract resolution and before creating a Tiger TradeClient.
- Required ticket shape for network-capable Tiger entries: LIMIT entry, `stop_loss`, and `targets[0]`.
- Missing protection or non-LIMIT entries are recorded as blocked Tiger order requests before any Tiger `preview_order` or `place_order` call.
- This keeps the known protection invariant fail-closed while still allowing explicit low-level fake-client drills to disable the requirement.

## M13 Paper Order Drill Boundary - 2026-07-05

- Drill service: `services.tiger_openapi_paper_order_drill.TigerOpenApiPaperOrderDrill`.
- Pipeline: `python3 -m pipelines.tiger_openapi_paper_order_drill --date 2026-07-05 --run-id m13-local-fake --json`.
- Artifact directory: `outputs/tiger_paper_order_drill/`.
- Runtime directory: `outputs/tiger_paper_order_drill_runtime/<run_id>/`.
- Real run on 2026-07-05 with `run_id=m13-local-fake` returned `status=pass` and `real_tiger_network_call_attempted=false`.
- Scenario coverage:
  - `default_guardrail_block`: confirms one MGC contract at normal price is blocked by `BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT` before fake preview/place.
  - `simulated_green_order`: confirms the fake-client order path reaches fake preview/place only after dated contract resolution, flat reconciliation, account sync, money guardrails, and same-path attached TP/SL pass.
- The drill creates a dummy local props file inside the isolated runtime root and injects fake TradeClient/SDK objects. It does not instantiate Tiger's real TradeClient.
- The dashboard Tiger venue read model includes `paper_order_drill`; failed drill evidence degrades the venue card, missing drill evidence does not block the read-only venue status.

## M14 Attended Paper-Order Readiness Boundary - 2026-07-05

- Readiness service: `services.tiger_openapi_paper_order_readiness.TigerOpenApiPaperOrderReadiness`.
- Pipeline: `python3 -m pipelines.tiger_openapi_paper_order_readiness --date 2026-07-05 --json`.
- Artifact directory: `outputs/tiger_paper_order_readiness/`.
- Real run on 2026-07-05 returned `status=ready_for_attended_paper_order`, `ready_for_attended_paper_order=true`, `can_submit_without_explicit_operator_authorization=false`, and `real_tiger_network_call_attempted=false`.
- Required evidence checks:
  - Tiger venue status is ready.
  - Dated execution contract is ready.
  - Reconciliation is flat with no positions/open orders.
  - Account sync has balance/accounting evidence.
  - Order/fill sync is current with no open orders.
  - Kill-switch has no network modification.
  - Local fake-client paper order drill passed.
  - Checked-in Tiger profile remains non-network by default.
  - Operational invariants are configured: reconciliation, money guardrails, same-path protection, and kill-switch network actions disabled.
- This is not authorization to submit an order. It is the precondition artifact for a future explicitly attended paper-order canary.
- The dashboard Tiger venue read model includes `paper_order_readiness`; a blocked readiness artifact degrades the venue card, missing readiness evidence does not block the read-only venue status.

## M15 Attended Paper-Order Canary Boundary - 2026-07-05

- Canary service: `services.tiger_openapi_paper_order_canary.TigerOpenApiPaperOrderCanary`.
- Pipeline: `python3 -m pipelines.tiger_openapi_paper_order_canary --date 2026-07-05 --ticket-id tiger_m15_mgc2608_readonly --asset MGC2608 --side buy --quantity 1 --entry-price 4186 --stop-loss 4170 --take-profit 4200 --operator codex --json`.
- Artifact directory: `outputs/tiger_paper_order_canary/`.
- Default mode is artifact-only: `submit_requested=false`, `can_submit_without_explicit_operator_authorization=false`, and `real_tiger_network_call_attempted=false`.
- Required submit acknowledgement: `I_UNDERSTAND_TIGER_PAPER_TRADECLIENT_WILL_PREVIEW_AND_PLACE_AN_ORDER`.
- Real read-only setup on 2026-07-05:
  - Installed official `tigeropen` Python SDK into the user Python environment.
  - Tightened the mounted Tiger properties file from world-readable/writeable to owner-only `0600`.
  - Re-ran Tiger read-only reconciliation, account sync, and order sync with `TIGER_OPENAPI_CONFIG_PATH` set only for those commands.
  - M14 readiness returned `ready_for_attended_paper_order=true`.
- M15 ticket-specific result: blocked before Tiger preview/place by `BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT` and `BLOCKED_TOTAL_NOTIONAL_LIMIT`.
- Guardrail evidence now reports `candidate.notional_multiplier=10.0` and `candidate.notional=41860.0` for `MGC2608` at 4186 x 1 contract.
- This means the next decision is not API plumbing; it is whether to adjust paper canary live-money limits for an explicitly attended paper test, use a smaller contract if available, or keep the current fail-closed limit.

## M16 Attended Paper Canary Risk Package - 2026-07-05

- Config: `broker_profiles.tiger_openapi_paper.attended_paper_canary`.
- Package scope: explicit Tiger paper canary only.
- Limits:
  - `max_contracts=1`
  - `single_order_max_notional=45000.0`
  - `total_account_max_notional=45000.0`
  - `max_candidate_stop_loss_pct_of_equity=2.5`
  - `max_daily_entry_orders=1`
- Default behavior remains blocked for one MGC at 4186 because the canary does not use the package unless `--use-attended-canary-risk-limits` is present.
- Explicit package check command:
  - `python3 -m pipelines.tiger_openapi_paper_order_canary --date 2026-07-05 --ticket-id tiger_m16_mgc2608_risk_package --asset MGC2608 --side buy --quantity 1 --entry-price 4186 --stop-loss 4170 --take-profit 4200 --operator codex --use-attended-canary-risk-limits --json`
- Real read-only result:
  - `status=ready_for_operator_authorization`
  - `submit_requested=false`
  - `real_tiger_network_call_attempted=false`
  - candidate notional: `41860.0`
  - candidate stop-loss: `160.0`, or `2.14242579%` of observed paper equity
- This still is not permission to submit. Actual Tiger paper TradeClient preview/place remains behind `--submit-tiger-paper-canary`, `--confirm-tiger-paper-canary`, the exact acknowledgement phrase, fresh read-only Tiger evidence, and operator attendance.

## M17 Operator Approval Runbook - 2026-07-05

- Approval service: `services.tiger_openapi_paper_order_approval.TigerOpenApiPaperOrderApproval`.
- Pipeline: `python3 -m pipelines.tiger_openapi_paper_order_approval --date 2026-07-05 --ticket-id tiger_m17_mgc2608_approval --asset MGC2608 --side buy --quantity 1 --entry-price 4186 --stop-loss 4170 --take-profit 4200 --operator codex --props-path <owner-only-runtime-properties-file> --use-attended-canary-risk-limits --json`.
- Artifact directory: `outputs/tiger_paper_order_approval/`.
- Runtime properties file used for generated commands: owner-only local Tiger properties file, mode `0600`.
- The original mounted Tiger properties file reported mode `0777` on the mounted volume, so it is not used in the M17 approval runbook.
- Real M17 result:
  - `status=ready_for_operator_approval`
  - `submit_requested=false`
  - `can_submit_without_explicit_operator_authorization=false`
  - `real_tiger_network_call_attempted=false`
  - `use_attended_canary_risk_limits=true`
- The generated submit command remains dormant until an operator explicitly chooses to run it. M17 itself does not call Tiger `preview_order`, `place_order`, cancel, close, or modify APIs.

## M18 Approval Visibility Boundary - 2026-07-05

- Read model: `services.tiger_venue_status.TigerVenueStatus`.
- Frontend: `dashboard-dualtrack-v5.html`.
- Test coverage:
  - `tests/test_tiger_venue_status.py` covers ready approval summaries, blocked approval degradation, and `manual_review` when approval artifacts indicate submit/network activity.
  - `tests/test_dashboard_dualtrack_static.py` verifies the `授权包` row exists and the static dashboard does not include `submit_command`.
  - `tests/test_dualtrack_api_contracts.py` continues to verify `/api/dualtrack/venue/tiger` is not a POST control endpoint.
- Redaction boundary: the dashboard API includes only approval status metadata and canary risk summaries; it does not include the generated submit command or the Tiger properties file path.
- Real local API check after M18 returned Tiger venue `ready`, `confirmation_status=confirmed_flat`, open orders `0`, and `paper_order_approval.status=ready_for_operator_approval`.

## M19 Connector Catalog Boundary - 2026-07-05

- Catalog service: `services.connector_catalog.ConnectorCatalog`.
- Pipeline: `python3 -m pipelines.connector_catalog --date 2026-07-05 --json`.
- Dashboard endpoint: `GET /api/connectors/catalog`.
- Artifact directory: `outputs/connector_catalog/`.
- Real local result with `TIGER_OPENAPI_CONFIG_PATH` pointing to the owner-only local properties file:
  - `connector_count=4`
  - `ready_price_feed_count=4`
  - `ready_broker_count=1`
  - Tiger OpenAPI `price_feed=ready`
  - Tiger OpenAPI `broker_order=ready`
  - Tiger credential check: present, file exists, owner-only, mode `0600`
- Redaction boundary:
  - Environment variable names such as `TIGER_OPENAPI_CONFIG_PATH`, `BINANCE_API_KEY`, and `BINANCE_API_SECRET` may appear.
  - Actual env values, API keys, private key contents, and the Tiger properties file path are not returned.
- This is the foundation for a future connector-onboarding UI. It does not store credentials, mutate config, open network clients, or add order-control endpoints.
- M32 note: the M19 ready price-feed count was credential-based. Current catalog semantics are stricter for Tiger: Tiger `price_feed` is `blocked` until `tiger_price_feed_readiness` is `ready_for_price_feed`.

## M20 Connector Onboarding Dry-Run Boundary - 2026-07-05

- Dry-run service: `services.connector_onboarding.ConnectorOnboardingDryRun`.
- Pipeline: `python3 -m pipelines.connector_onboarding --connector-id tiger_openapi --role price_feed --role broker_order --environment paper --json`.
- Dashboard endpoint: `POST /api/connectors/onboarding/dry-run`.
- Frontend panel: `dashboard-dualtrack-v5.html` `接入 CONNECTOR`.
- Artifact directory: `outputs/connector_onboarding/`.
- Real local result:
  - `connector_id=tiger_openapi`
  - `requested_roles=[price_feed, broker_order]`
  - `status=ready_for_operator_setup`
  - `blockers=0`
  - `stores_credentials=false`
  - `credential_values_exposed=false`
  - `writes_runtime_config=false`
  - `opens_network_clients=false`
  - `creates_order_control_endpoint=false`
- Browser check showed the connector panel can run the dry-run and returns `ready_for_operator_setup`.
- Redaction boundary: the API/artifact does not include raw key values or the Tiger properties file path.

## M55 Dualtrack Tiger/MGC Operational Cost Boundary - 2026-07-06

- Scope: dualtrack machine and human ledgers can now represent MGC as integer contracts with fixed USD side costs.
- Shared helper: `services.dualtrack_costs`.
- Machine path:
  - `services.dualtrack_grid_core.simulate_conditional_grid` accepts an optional `execution_cost_model` and `cost_rules`.
  - In Tiger/MGC mode, each fill records `contracts`, `quantity`, actual contract notional (`price * contracts * contract_multiplier`), side `cost`, and a venue cost model.
  - Existing bp/notional mode remains the default path.
- Human path:
  - `services.dualtrack_human.DualTrackHumanEngine` accepts `contracts`/`quantity` in venue mode and records the same fixed-per-contract cost model.
  - In venue mode, manual fills require explicit contract quantity; notional-only payloads are rejected to avoid guessing real Tiger fill size.
- Config shape for a future Tiger/MGC dualtrack switch:
  - `execution_cost_model.venue=tiger_mgc`
  - `execution_cost_model.quantity_mode=integer_contracts`
  - `execution_cost_model.contract_multiplier=10`
  - `execution_cost_model.contracts_per_rung=1`
  - optional `grid.max_rungs` to cap contract inventory.
- Tests:
  - `tests/test_dualtrack_dt2_machine_runner.py::test_machine_runner_tiger_mgc_mode_uses_integer_contracts_and_fixed_side_cost`
  - `tests/test_dualtrack_dt3_human_track.py::test_human_tiger_mgc_mode_records_contracts_and_fixed_side_cost`
  - `tests/test_dualtrack_dt3_human_track.py::test_human_tiger_mgc_mode_requires_explicit_contracts`
- This milestone does not claim strategy edge, market-hours realtime readiness, or broker-order authorization. It only removes the false continuous-notional/bp-fee assumption from the operational dualtrack layer.

## M56 Dualtrack COMEX Session Mask Boundary - 2026-07-06

- Scope: dualtrack can now distinguish COMEX open time from daily breaks and weekends when a profile enables the session mask.
- Shared clock functions: `services.dualtrack_clock.is_comex_futures_open`, `comex_futures_session_status`, `next_comex_futures_open`, and `filter_bars_for_market_session`.
- Session rule:
  - Sunday 18:00 New York time through Friday 17:00 New York time.
  - Daily 17:00-18:00 New York time break.
  - Uses Python `zoneinfo` for `America/New_York`, so summer/winter UTC offsets are handled by timezone conversion rather than hard-coded UTC hours.
- Runner behavior:
  - `DualTrackCycleRunner.auto()` returns `status=skipped`, `reason=market_closed` when the configured session is closed.
  - `DualTrackCycleRunner.intraday_tick()` skips during closed COMEX sessions.
  - `_cycle_bars()` and `previous_cycle_range()` filter out closed-session bars when the session mask is enabled.
- Compatibility:
  - The default dualtrack config keeps `market_session.enabled=false`, so existing GOLD/Binance-style behavior is unchanged.
  - A future Tiger/MGC dualtrack profile should enable `market_session.venue=comex_futures`.
- Tests:
  - `tests/test_dualtrack_clock.py`
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_comex_session_auto_skips_during_daily_break`
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_comex_session_intraday_skips_during_weekend_close`
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_comex_session_mask_filters_closed_break_bars_from_cycle_and_previous_range`

## M57 Tiger Fill Import Into Human Ledger - 2026-07-06

- Scope: Tiger filled-order artifacts can now be imported into the dualtrack human ledger for attribution.
- Import service: `services.dualtrack_tiger_human_sync.DualTrackTigerHumanSync`.
- Pipeline: `python3 -m pipelines.dualtrack_tiger_human_sync --date YYYY-MM-DD --json`.
- Input artifact: `outputs/tiger_order_sync/current.json`.
- Output artifacts:
  - `outputs/dualtrack/fills/<cycle_id>_human.json`
  - `outputs/dualtrack/accounts/<cycle_id>_human.json`
  - `outputs/dualtrack/tiger_human_sync/current.json`
  - `outputs/dualtrack/tiger_human_sync/<date>.json`
- Behavior:
  - Imports only normalized Tiger MGC filled orders with side, whole-contract quantity, average fill price, and timestamp.
  - Maps Tiger `filled_quantity` into dualtrack `contracts`/`quantity`.
  - Computes Tiger/MGC fixed-per-contract side cost when broker-reported commission is absent.
  - Uses broker-reported `commission` as the human fill cost when present, while retaining the estimated cost in `cost_model.estimated_cost`.
  - Keeps `source_fill_id` on each human fill and skips duplicates on repeated runs.
  - Preserves existing human plan attribution through `out_of_plan`.
- Safety boundary:
  - This pipeline reads an existing order-sync artifact and writes dualtrack attribution files only.
  - It does not open Tiger SDK clients, submit orders, cancel orders, close positions, write runtime config, or authorize broker routing.
- Tests:
  - `tests/test_dualtrack_tiger_human_sync.py`
  - `tests/test_dualtrack_dt3_human_track.py`

## M58 Runner Human-Fill Sync Before Close - 2026-07-06

- Scope: `DualTrackCycleRunner.close_cycle()` can run human-fill sync before machine recomputation and scoring when explicitly enabled in config.
- Config:
  - `human_fill_sync.enabled`
  - `human_fill_sync.provider=tiger_openapi`
  - `human_fill_sync.run_before_close`
  - `human_fill_sync.refresh_order_sync_before_import`
  - `human_fill_sync.require_success_before_close`
- Default behavior:
  - Checked-in dualtrack config keeps `human_fill_sync.enabled=false`.
  - Therefore the legacy GOLD/Binance-style runner remains unchanged.
- Tiger/MGC behavior when enabled:
  - Before closing a cycle, the runner imports Tiger filled orders into the human ledger through `DualTrackTigerHumanSync`.
  - If `refresh_order_sync_before_import=true`, it first runs read-only `TigerOpenApiOrderSync` for that date.
  - If sync is blocked and `require_success_before_close=true`, close returns `status=skipped`, `reason=human_fill_sync_blocked`, and does not write closed attribution.
  - Successful sync metadata is included in the close result and runner state.
- Safety boundary:
  - Default runner config does not open Tiger SDK clients.
  - Artifact-only import remains available without network.
  - Read-only order-sync refresh requires explicit config and still uses the existing `TigerOpenApiOrderSync` read-only boundary.
- Tests:
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_tiger_human_fill_sync_runs_before_close_and_scoring`
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_tiger_human_fill_sync_blocks_close_when_artifact_missing`
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_tiger_human_fill_sync_can_refresh_order_sync_before_import`

## M59 Tiger/MGC Dualtrack Profile Preview - 2026-07-06

- Scope: connector activation planning now includes a `dualtrack_profile_preview` for Tiger/MGC.
- User-facing purpose:
  - Future platform/API-key onboarding can show what choosing Tiger changes in the dualtrack operating layer before any config write.
  - The preview keeps broker/feed switching and dualtrack runtime assumptions in the same review package.
- Profile ID: `tiger_mgc_dualtrack_paper`.
- Previewed dualtrack config:
  - `market_data.symbol=MGCmain`
  - `market_data.timeframe=1m`
  - `market_data.provider=tiger_openapi:COMEX`
  - `market_session.enabled=true`
  - `market_session.venue=comex_futures`
  - `market_session.timezone=America/New_York`
  - `execution_cost_model.venue=tiger_mgc`
  - `execution_cost_model.quantity_mode=integer_contracts`
  - `execution_cost_model.contract_multiplier=10`
  - `execution_cost_model.contracts_per_rung=1`
  - `grid.max_rungs=2`
  - when `broker_order` is requested, `human_fill_sync` is planned on with Tiger order-sync refresh before close.
- Runner support:
  - `DualTrackCycleRunner` now reads default `market_data.symbol/timeframe` from dualtrack config.
  - Checked-in defaults remain `GOLD/1m`, so existing flows remain unchanged until a separate profile write.
- Safety boundary:
  - The activation endpoint remains preview-only and writes no config.
  - The profile does not open Tiger SDK clients, submit orders, cancel orders, close positions, or authorize broker routing.
  - Applying this profile later would still require price-feed acceptance, explicit config-write approval, and strategy-edge approval.
- Tests:
  - `tests/test_connector_activation_plan.py::test_connector_activation_plan_previews_tiger_feed_and_broker_without_writing_config`
  - `tests/test_connector_activation_plan.py::test_connector_activation_plan_surfaces_tiger_acceptance_pending_market_open`
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_cycle_runner_uses_configured_market_data_symbol_by_default`

## M60 Dashboard Tiger/MGC Profile Preview Surface - 2026-07-06

- Scope: `dashboard-dualtrack-v5.html` now renders `dualtrack_profile_preview` from connector activation planning.
- Frontend rows:
  - `双轨档案`: profile status and dualtrack change count.
  - `MGC运行层`: planned market symbol/timeframe and provider/session.
  - `MGC费用/粒度`: planned per-side MGC fee and integer-contract quantum.
  - `人工成交同步`: whether close-time Tiger fill sync is planned.
  - `策略/下单门`: strategy approval and broker-order gating state.
- Safety boundary:
  - The rows render only after the operator requests activation preview.
  - The dashboard does not run activation automatically.
  - It does not render `next_command` or validation commands as buttons.
  - It does not write config, open Tiger SDK clients, submit orders, cancel orders, close positions, or authorize broker routing.
- Tests:
  - `tests/test_dashboard_dualtrack_static.py::test_dualtrack_v5_matches_locked_visual_contract_sections`
  - `tests/test_dashboard_dualtrack_static.py::test_dualtrack_v5_uses_dualtrack_api_contracts_and_backend_market_bars`
  - `node` syntax parse of the dashboard script.

## M61 Connector Config Apply And Rollback Boundary - 2026-07-06

- Scope: add an attended config writer for connector activation plans.
- Service: `services.connector_config_apply.ConnectorConfigApply`.
- CLI:
  - Dry-run: `python3 -m pipelines.connector_config_apply apply --plan outputs/connector_activation_plan/current.json --json`
  - Write: `python3 -m pipelines.connector_config_apply apply --plan outputs/connector_activation_plan/current.json --write --accept-warnings --acknowledgement I_UNDERSTAND_CONNECTOR_CONFIG_WRITE_IS_SEPARATE_AND_REVERSIBLE --json`
  - Rollback: `python3 -m pipelines.connector_config_apply rollback --receipt outputs/connector_config_apply/<apply_id>.json --acknowledgement I_UNDERSTAND_CONNECTOR_CONFIG_ROLLBACK_WILL_OVERWRITE_CURRENT_CONFIG --json`
- API boundary:
  - `POST /api/connectors/config/apply`
  - `POST /api/connectors/config/rollback`
  - Neither route is part of `_DUALTRACK_POST_ENDPOINTS`.
- Behavior:
  - Default `apply` mode is dry-run and writes only a receipt.
  - `write=true` requires the exact activation-plan acknowledgement.
  - Activation warnings require explicit `accept_warnings=true`.
  - Blocked activation plans cannot be applied.
  - Before writing, the service backs up every targeted config file under `outputs/connector_config_apply/backups/<apply_id>/`.
  - Rollback restores config files from the recorded backup and writes a rollback receipt.
- Tiger/MGC write scope when applied from the current profile:
  - `configs/pipeline.yaml`: Tiger futures feed enablement, broker provider/profile/environment/dry-run, demo broker profile, and removal of legacy Binance broker overrides.
  - `configs/dualtrack.yaml`: MGC market data, COMEX session mask, Tiger/MGC execution cost model, rung cap, and human-fill sync.
- Safety boundary:
  - The service writes local config files only.
  - It does not open Tiger SDK clients, run price-feed acceptance, write market bars, submit orders, preview orders, cancel orders, close positions, store credentials, expose credential values, or authorize broker routing.
- Tests:
  - `tests/test_connector_config_apply.py`
  - `tests/test_dualtrack_api_contracts.py::test_connector_config_apply_post_route_is_not_order_control`
  - `tests/test_dualtrack_api_contracts.py::test_connector_config_rollback_post_route_is_not_order_control`

## M62 Dashboard Connector Config Receipt Surface - 2026-07-06

- Scope: expose config apply/rollback receipt status in the dualtrack connector panel.
- API boundary:
  - `GET /api/connectors/config/status`
  - Reads `outputs/connector_config_apply/current.json` and `outputs/connector_config_apply/rollback/current.json`.
  - Returns compact `latest_apply`, `latest_rollback`, `backup`, and safety summaries.
- Frontend:
  - Loads the status endpoint with the other dashboard data.
  - Adds a `写入预检` button after activation preview.
  - The button posts only `{activation_plan: state.connectorActivationPlan}` to `/api/connectors/config/apply`, so it produces a dry-run receipt by default.
  - Renders `配置写入收据`, `备份位置`, and `回滚收据` rows.
- Safety boundary:
  - The dashboard does not send `write=true`, `accept_warnings`, or acknowledgement strings.
  - The dashboard does not expose a rollback button.
  - The status endpoint is a receipt read. It does not open Tiger SDK clients, fetch prices, write config, submit orders, cancel orders, close positions, or authorize broker routing.
- Tests:
  - `tests/test_connector_config_apply.py::test_connector_config_status_summarizes_apply_backup_and_rollback`
  - `tests/test_dualtrack_api_contracts.py::test_connector_config_status_get_route_is_read_only_receipt_surface`
  - `tests/test_dashboard_dualtrack_static.py`

## M63 Dualtrack Live Tick Schedule Boundary - 2026-07-06

- Scope: make the dualtrack live heartbeat reproducible from the repo's schedule generator.
- User-facing purpose:
  - A dashboard being visible is not enough proof that the machine track is sampling live bars.
  - The system needs a five-minute heartbeat that syncs the operator's Obsidian plan and advances the intraday machine track between the 09:00/21:00 boundary jobs.
- Runner behavior:
  - `python3 -m pipelines.dualtrack_cycle_runner --event live-tick`
  - Calls `sync_obsidian_human_plans(include_next=True)` first.
  - Then calls `intraday_tick()` for the active cycle.
  - Writes normal dualtrack runner artifacts under `outputs/dualtrack/runner/<cycle_id>.json`.
- Schedule behavior:
  - `ScheduleManager` now generates `com.wendy.trading-orchestrator.dualtrack-live-tick.plist`.
  - The job runs every 300 seconds with `RunAtLoad=true`.
  - The existing `dualtrack-cycle` job remains every 60 seconds for boundary/auto orchestration.
  - `ScheduleStatus` and `CompletionAudit` now treat `dualtrack-live-tick` as a required job.
- Safety boundary:
  - Generating the plist does not install or load it.
  - The live tick uses existing dualtrack paper paths; it does not open Tiger SDK clients by default, write config, submit orders, cancel orders, close positions, or authorize broker routing.
  - If a future applied profile enables read-only Tiger human-fill refresh, that still happens only through the close-cycle sync boundary, not through this schedule generator.
- Tests:
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_obsidian_plan_sync_imports_current_draft_and_next_locked`
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_live_tick_syncs_obsidian_plan_and_runs_intraday`
  - `tests/test_schedule_manager.py`

## M64 Schedule Current-Version Audit - 2026-07-06

- Scope: make local schedule status distinguish "installed/loaded" from "installed from the current generated repo artifacts."
- User-facing purpose:
  - When the operator asks whether the system is in place for market open, loaded old launchd jobs should not be mistaken for current-version automation.
- Schedule status behavior:
  - `installed_count`: generated job labels whose plist exists in `~/Library/LaunchAgents`.
  - `loaded_count`: installed generated job labels that launchd reports as loaded.
  - `matching_generated_count`: installed jobs whose plist exactly matches the generated plist under `outputs/schedules/launch_agents`.
  - `active_current_count`: installed, matching, and loaded jobs.
  - `stale_installed`: status when installed jobs exist but at least one plist differs from the generated schedule.
- OPS surface:
  - OPS dashboard shows `current / active` separately from `installed / loaded`.
  - OPS dashboard lists stale installs by job label.
- Safety boundary:
  - This audit reads generated/installed plists and launchd print status only.
  - It does not install, bootstrap, bootout, kickstart, or modify launchd jobs.
  - It does not touch broker config, Tiger SDK clients, market bars, or orders.
- Tests:
  - `tests/test_schedule_manager.py::test_schedule_status_detects_stale_loaded_launch_agents`
  - `tests/test_dashboard_v3_static.py::test_ops_dashboard_links_back_to_trader_console`

## M65 Schedule Install Dry-Run Plan - 2026-07-06

- Scope: add a reviewable launchd install plan before any operator modifies local LaunchAgents.
- User-facing purpose:
  - When schedule status reports stale installed jobs, the operator can see exactly what would be replaced/restarted before approving a real install.
- CLI:
  - `python3 -m pipelines.schedule_install --dry-run --date YYYY-MM-DD --json`
  - Non-JSON output prints per-job dry-run `action` values instead of requiring install-only `status` fields.
- Receipt artifacts:
  - `outputs/schedules/install_plan_current.json`
  - `outputs/schedules/install_plan_<date>.json`
- Plan behavior:
  - Status is `noop` when every generated job is already installed, current, and active.
  - Status is `ready` when one or more jobs require reinstall and no generated plist is missing.
  - Status is `blocked` when a generated plist is missing.
  - Per-job actions distinguish already-current jobs, stale loaded jobs that need restart, no-restart staging, missing installs, and blocked missing-plist cases.
- OPS surface:
  - Dashboard state exposes `schedule_install_plan`.
  - OPS dashboard renders an `install plan` row with replacement and blocker counts.
- Safety boundary:
  - Dry-run emits planned commands only.
  - It does not copy plists, bootstrap, bootout, kickstart, open Tiger SDK clients, fetch prices, write broker config, submit orders, cancel orders, or close positions.
  - It does call `launchctl print` through schedule status and writes receipt artifacts.
- Gotchas:
  - `planned_commands` are receipts, not executed commands.
  - `--no-restart-loaded` can copy a current plist in a later real install while launchd keeps running the already loaded old definition until restarted.
- Tests:
  - `tests/test_schedule_manager.py::test_schedule_installer_plan_reports_stale_loaded_job_without_modifying_launchd`
  - `tests/test_schedule_manager.py::test_schedule_installer_plan_warns_no_restart_only_stages_loaded_stale_job`
  - `tests/test_dashboard_state.py`
  - `tests/test_dashboard_v3_static.py::test_ops_dashboard_links_back_to_trader_console`

## M66 Schedule Attended Install Gate - 2026-07-06

- Scope: make the non-dry-run launchd install path fail closed unless the operator provides an explicit acknowledgement.
- User-facing purpose:
  - After the dry-run plan says which stale jobs would be replaced, a real install should still require an attended confirmation and leave a backup trail.
- Required acknowledgement:
  - `I_UNDERSTAND_SCHEDULE_INSTALL_WILL_REPLACE_OR_RESTART_LOCAL_LAUNCHD_JOBS`
- CLI:
  - Blocked/no-write default: `python3 -m pipelines.schedule_install --date YYYY-MM-DD --json`
  - Attended install: `python3 -m pipelines.schedule_install --date YYYY-MM-DD --acknowledgement I_UNDERSTAND_SCHEDULE_INSTALL_WILL_REPLACE_OR_RESTART_LOCAL_LAUNCHD_JOBS --json`
- Behavior:
  - `ScheduleInstaller.install()` runs `ScheduleInstaller.plan()` before any write.
  - If the plan is blocked, install writes a blocked receipt and does not modify LaunchAgents.
  - If the plan requires changes and acknowledgement is missing/wrong, install writes a blocked receipt and does not modify LaunchAgents.
  - If the plan is `noop`, install writes a no-op receipt and does not modify LaunchAgents.
  - If acknowledgement is correct, install backs up existing plists before replacing them.
- Receipt artifacts:
  - `outputs/schedules/install_current.json`
  - `outputs/schedules/install_<date>.json`
  - `outputs/schedules/launch_agent_backups/<install_id>/` when existing plists were backed up.
- OPS surface:
  - Dashboard state exposes `schedule_install`.
  - OPS dashboard renders an `install apply` row for the latest apply/noop/blocked receipt.
- Safety boundary:
  - The acknowledgement authorizes only local launchd plist replacement/restart.
  - It does not authorize broker config writes, Tiger SDK order clients, market data writes, order preview, order submission, cancellation, or position close.
  - A blocked install can call `launchctl print` through the plan/status path and write local receipt artifacts; it does not create LaunchAgents, copy plists, bootout, bootstrap, or kickstart.
- Gotchas:
  - Backups are file-level plists only; they do not automatically roll launchd runtime state back.
  - `--no-restart-loaded` on a later attended install can still leave the loaded job running the previous definition until restarted.
- Tests:
  - `tests/test_schedule_manager.py::test_schedule_installer_blocks_apply_without_acknowledgement`
  - `tests/test_schedule_manager.py::test_schedule_installer_backs_up_existing_plists_before_replacing`
  - `tests/test_schedule_manager.py::test_schedule_installer_copies_plists_and_records_receipt`
  - `tests/test_schedule_manager.py::test_schedule_installer_no_restart_does_not_kickstart_jobs`
  - `tests/test_dashboard_state.py`
  - `tests/test_dashboard_v3_static.py::test_ops_dashboard_links_back_to_trader_console`

## M67 Schedule Rollback Gate - 2026-07-06

- Scope: add a reviewable and acknowledgement-gated rollback path for local launchd schedule installs.
- User-facing purpose:
  - If an attended install replaces stale LaunchAgents, the operator should be able to verify and execute a controlled plist-level rollback from the install backup receipt.
- CLI:
  - Rollback dry-run/default receipt: `python3 -m pipelines.schedule_install --rollback --dry-run --date YYYY-MM-DD --json`
  - Rollback from explicit receipt: `python3 -m pipelines.schedule_install --rollback --dry-run --receipt outputs/schedules/install_<date>.json --date YYYY-MM-DD --json`
  - Attended rollback: `python3 -m pipelines.schedule_install --rollback --date YYYY-MM-DD --acknowledgement I_UNDERSTAND_SCHEDULE_ROLLBACK_WILL_RESTORE_LOCAL_LAUNCHD_JOBS_FROM_BACKUP --json`
- Required acknowledgement:
  - `I_UNDERSTAND_SCHEDULE_ROLLBACK_WILL_RESTORE_LOCAL_LAUNCHD_JOBS_FROM_BACKUP`
- Behavior:
  - `ScheduleInstaller.rollback_plan()` reads the install receipt and validates each job's backup plist path.
  - If the install receipt is missing or has no backup records, rollback plan is blocked.
  - If any backup file is missing, rollback plan is blocked.
  - `ScheduleInstaller.rollback()` runs the plan first and blocks apply when the plan is not ready or acknowledgement is missing/wrong.
  - Successful rollback backs up the current target plists before restoring the older backup plists.
- Receipt artifacts:
  - `outputs/schedules/rollback_plan_current.json`
  - `outputs/schedules/rollback_plan_<date>.json`
  - `outputs/schedules/rollback_current.json`
  - `outputs/schedules/rollback_<date>.json`
  - `outputs/schedules/rollback_target_backups/<rollback_id>/` when current target plists were backed up before restore.
- OPS surface:
  - Dashboard state exposes `schedule_rollback_plan` and `schedule_rollback`.
  - OPS dashboard renders `rollback plan` and `rollback apply` rows next to schedule install state.
- Safety boundary:
  - Rollback acknowledgement authorizes only local LaunchAgent plist restoration/restart.
  - It does not authorize broker config writes, Tiger SDK order clients, market data writes, order preview, order submission, cancellation, or position close.
  - A blocked rollback can call `launchctl print` through the plan/status path and write local receipt artifacts; it does not copy plists, bootout, bootstrap, or kickstart.
- Gotchas:
  - A blocked install receipt has no backup records, so rollback plan is expected to be blocked until a successful attended install exists.
  - Rollback restores plist files; it does not prove the runtime is healthy, market data is fresh, or Tiger broker order paths are ready.
- Tests:
  - `tests/test_schedule_manager.py::test_schedule_rollback_plan_blocks_when_install_receipt_has_no_backups`
  - `tests/test_schedule_manager.py::test_schedule_rollback_blocks_apply_without_acknowledgement`
  - `tests/test_schedule_manager.py::test_schedule_rollback_restores_backup_with_acknowledgement`
  - `tests/test_dashboard_state.py`
  - `tests/test_dashboard_v3_static.py::test_ops_dashboard_links_back_to_trader_console`

## M68 Schedule Post-Install Verification Gate - 2026-07-06

- Scope: add a no-write verifier that proves whether the current generated schedule has actually taken over after an attended install.
- User-facing purpose:
  - After replacing stale launchd jobs, the operator should have one receipt that says whether the system is current-version active, rollback-ready, and still producing a runner heartbeat.
- CLI:
  - `python3 -m pipelines.schedule_post_install_verify --date YYYY-MM-DD --json`
- Receipt artifacts:
  - `outputs/schedules/post_install_verify_current.json`
  - `outputs/schedules/post_install_verify_<date>.json`
- Checks:
  - `schedule_current_active`: `ScheduleStatus` must show every required generated job installed, matching generated, and loaded.
  - `install_receipt`: latest `install_current.json` must be successful/no-op, not blocked.
  - `rollback_ready`: latest install backup records must be restorable when backups are expected.
  - `runner_heartbeat`: `runner_status/current.json` must be fresh enough to prove the runner is still alive.
- Current real result on 2026-07-06:
  - `blocked`
  - `schedule_current_active=fail` because 9/9 installed jobs are stale relative to generated plists.
  - `install_receipt=fail` because latest install receipt is blocked by missing acknowledgement.
  - `rollback_ready=fail` because no successful install backup records exist yet.
  - `runner_heartbeat=pass`, which is not sufficient to prove current-version takeover.
- OPS surface:
  - Dashboard state exposes `schedule_post_install_verify`.
  - OPS dashboard renders `post-install verify`.
  - ops-status contract includes `schedule_post_install_verify` under runner diagnostics.
- Safety boundary:
  - The verifier calls `launchctl print` through schedule status and writes local receipt artifacts.
  - It does not copy plists, bootstrap, bootout, kickstart, open Tiger SDK clients, fetch prices, write broker config, submit orders, cancel orders, or close positions.
- Gotchas:
  - A fresh runner heartbeat can come from stale launchd jobs. It is necessary evidence, not sufficient evidence.
  - Rollback readiness is part of takeover proof; a successful install without backup evidence should not be treated as fully operationally reversible.
- Tests:
  - `tests/test_schedule_manager.py::test_schedule_post_install_verifier_blocks_when_schedule_is_stale`
  - `tests/test_schedule_manager.py::test_schedule_post_install_verifier_passes_after_successful_install_with_fresh_runner`
  - `tests/test_dashboard_state.py`
  - `tests/test_dashboard_v3_static.py::test_ops_dashboard_links_back_to_trader_console`
  - `tests/test_dashboard_server.py::test_ops_status_contract_keeps_diagnostics_outside_trader_contract`

## M69 Schedule Takeover Package - 2026-07-06

- Scope: create a durable no-write handoff package for attended launchd schedule takeover.
- User-facing purpose:
  - Before the operator authorizes real launchd replacement/restart, the system should provide one artifact with current evidence, exact commands, acknowledgements, verification, and rollback commands.
- CLI:
  - `python3 -m pipelines.schedule_takeover_package --date YYYY-MM-DD --json`
- Receipt artifacts:
  - `outputs/schedules/takeover_package_current.json`
  - `outputs/schedules/takeover_package_<date>.json`
- Current real result on 2026-07-06:
  - `ready_for_attended_install`
  - Install plan: `ready`, `requires_reinstall_count=9`, `blocked_count=0`.
  - Post-install verifier: `blocked`, because current generated schedule has not taken over yet.
  - Rollback plan: `blocked / missing_backup_records`, expected until a successful attended install creates backup records.
- Package commands:
  - Review install plan.
  - Attended install with `I_UNDERSTAND_SCHEDULE_INSTALL_WILL_REPLACE_OR_RESTART_LOCAL_LAUNCHD_JOBS`.
  - Post-install verification.
  - Review rollback plan.
  - Attended rollback with `I_UNDERSTAND_SCHEDULE_ROLLBACK_WILL_RESTORE_LOCAL_LAUNCHD_JOBS_FROM_BACKUP`.
- OPS surface:
  - Dashboard state exposes `schedule_takeover_package`.
  - OPS dashboard renders `takeover package`.
  - ops-status contract includes `schedule_takeover_package` under runner diagnostics.
- Safety boundary:
  - The package writes local receipt artifacts and calls `launchctl print` through component checks.
  - It does not copy plists, bootstrap, bootout, kickstart, open Tiger SDK clients, fetch prices, write broker config, submit orders, cancel orders, or close positions.
- Gotchas:
  - `ready_for_attended_install` means the dry-run install plan is unblocked and commands are available. It does not mean install has happened.
  - The acknowledgement in the attended install command authorizes only local launchd schedule replacement/restart.
- Tests:
  - `tests/test_schedule_manager.py::test_schedule_takeover_package_prepares_attended_install_commands_without_writes`
  - `tests/test_dashboard_state.py`
  - `tests/test_dashboard_v3_static.py::test_ops_dashboard_links_back_to_trader_console`
  - `tests/test_dashboard_server.py::test_ops_status_contract_keeps_diagnostics_outside_trader_contract`

## M70 Schedule Takeover Package Binding - 2026-07-06

- Scope: bind attended launchd install authorization to the fresh takeover package that was just reviewed.
- User-facing purpose:
  - The operator should not be able to copy an old acknowledgement command and accidentally install against drifted schedule evidence.
- Package fields:
  - `package_id`: stable hash of the reviewed install/verify/rollback evidence.
  - `valid_for_minutes`: current validity window.
  - `expires_at`: UTC expiry time for the package.
- CLI behavior:
  - `python3 -m pipelines.schedule_takeover_package --date YYYY-MM-DD --json` emits an attended install command containing `--package-id <package_id>`.
  - `python3 -m pipelines.schedule_takeover_package --check-current --date YYYY-MM-DD --json` validates the current package without creating a new install attempt.
  - `python3 -m pipelines.schedule_install --date YYYY-MM-DD --acknowledgement ... --package-id <package_id> --json` is required for non-noop attended install.
- Blockers:
  - `missing_package_id`
  - `missing_takeover_package`
  - `stale_or_mismatched_package`
  - `takeover_package_not_ready`
  - `expired_package`
- OPS surface:
  - The `takeover package` row shows the package id short code next to the replacement count.
  - The `takeover package` row shows expiry and marks stale packages as `expired`.
  - The `package check` row shows the current package usability check and next operator action.
  - The `install apply` row treats package-gate blocked receipts as authorization-gate state and shows `gate <blocker>` rather than implying a failed launchd mutation.
- Safety boundary:
  - Package-id failures write a blocked local install receipt only.
  - They do not copy plists, bootstrap, bootout, kickstart, open Tiger SDK clients, fetch prices, write broker config, submit orders, cancel orders, or close positions.
- Gotchas:
  - The install acknowledgement is still required, but it is no longer sufficient when the plan needs replacement.
  - Regenerate the takeover package if schedule evidence changes or the package expires.
  - Run `--check-current` immediately before attended install; it should return `operator_next_action.action=authorize_attended_install`.
- Tests:
  - `tests/test_schedule_manager.py::test_schedule_installer_blocks_apply_without_package_id_after_acknowledgement`
  - `tests/test_schedule_manager.py::test_schedule_installer_blocks_apply_with_stale_package_id`
  - `tests/test_schedule_manager.py::test_schedule_installer_blocks_apply_when_takeover_package_missing`
  - `tests/test_schedule_manager.py::test_schedule_installer_blocks_apply_when_takeover_package_expired`
  - `tests/test_schedule_manager.py::test_schedule_installer_blocks_apply_when_takeover_package_not_ready`
  - `tests/test_schedule_manager.py::test_schedule_takeover_package_prepares_attended_install_commands_without_writes`
  - `tests/test_schedule_manager.py::test_schedule_takeover_package_check_current_reports_usable_package_without_writes`
  - `tests/test_schedule_manager.py::test_schedule_takeover_package_check_current_blocks_expired_package`
  - `tests/test_dashboard_v3_static.py::test_ops_dashboard_links_back_to_trader_console`

## M71 Tiger/MGC Market-Hours Price Feed Accepted - 2026-07-06

- Scope: run the real market-hours Tiger price-feed acceptance gate and refresh local MGC bars.
- Result:
  - `python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-06 --contract MGCmain --poll-seconds 75 --json`
  - `status=accepted`
  - `exit_code=0`
  - `ready_for_price_feed=true`
  - realtime validation `status=pass`; bars advanced during the polling window.
- Local data refresh:
  - `python3 -m pipelines.tiger_futures_feed --date 2026-07-06 --contract MGCmain --limit 500`
  - imported 500 rows.
  - latest local MGC bar: `2026-07-06T05:31:00+00:00`.
  - scoped data-source preflight `outputs/data_source_preflight/MGCmain_1m/current.json` now reports `status=pass`, `ready_for_live=true`, `live_data_mode=execution_venue`.
- Safety boundary:
  - Acceptance opened only read-only QuoteClient.
  - Feed import wrote local market bars only.
  - Neither path opened TradeClient/order clients, submitted orders, wrote broker config, or authorized broker routing.
- Gotchas:
  - `can_enable_broker_orders_from_this_gate=false`; price-feed acceptance is not order authorization.
  - Permission metadata still labels quote permission as `aStockQuoteLv1` with `has_futures_realtime=false`; the promotion evidence is the market-hours acceptance receipt plus fresh MGC bars.

## M72 Tiger/MGC Connector Activation Preview Ready - 2026-07-06

- Scope: after price-feed acceptance, run the connector activation and config-apply dry-run for Tiger `price_feed` + `broker_order`.
- Activation preview:
  - `python3 -m pipelines.connector_activation_plan --connector-id tiger_openapi --role price_feed --role broker_order --environment paper --json`
  - `status=preview_ready`
  - blockers: 0
  - warning: `paper_network_order_not_armed`
  - pipeline changes: 15
  - dualtrack profile changes: 16
  - activation gate: `preview_ready_with_warnings`
- Config apply dry-run:
  - `python3 -m pipelines.connector_config_apply apply --plan outputs/connector_activation_plan/current.json --json`
  - `status=dry_run_ready`
  - mode: `dry_run`
  - total changes: 31
  - target files: `configs/pipeline.yaml`, `configs/dualtrack.yaml`
- Safety boundary:
  - The preview endpoint wrote no runtime config.
  - The dry-run wrote no runtime config and created no real backup.
  - Neither path opened Tiger network clients, submitted orders, cancelled orders, closed positions, stored credentials, or enabled broker orders.
- Remaining attended step:
  - Real config write requires explicit config-write acknowledgement and `--accept-warnings`.
  - The warning `paper_network_order_not_armed` is intentional: TradeClient submission remains fail-closed until a later operator-approved paper-order milestone.

## M73 Tiger/MGC Config Apply Package Id - 2026-07-06

- Scope: bind the future attended config write to the exact Tiger/MGC activation plan that was reviewed.
- New package field:
  - `outputs/connector_activation_plan/current.json config_apply_package.package_id=631b8ed6cb3310de`
  - the package's attended apply command includes `--package-id 631b8ed6cb3310de`.
- Config apply behavior:
  - dry-run remains no-write and does not require a package id.
  - non-dry-run `connector_config_apply apply --write` now requires:
    - the exact acknowledgement string.
    - `--accept-warnings` when activation warnings exist.
    - the exact `--package-id` from the reviewed package.
- Current no-write receipt:
  - `outputs/connector_config_apply/current.json`
  - `status=dry_run_ready`
  - `mode=dry_run`
  - `package_id=631b8ed6cb3310de`
  - `change_counts.total=31`
  - `safety.writes_runtime_config=false`
- Safety boundary:
  - Missing or mismatched package id blocks before any runtime config write.
  - The package id binds only the config write plan; it does not authorize Tiger paper orders, real orders, launchd takeover, or strategy promotion.

## M74 Tiger/MGC Config Package Visible In Dualtrack UI - 2026-07-06

- Scope: make the reviewed config package visible in the operator-facing connector card.
- UI additions:
  - `审批包ID`: the current activation package id.
  - `写入边界`: whether the plan requires a separate package-bound write.
  - `预检包ID`: the package id attached to the latest config-apply dry-run receipt.
- Current visible package:
  - `631b8ed6cb3310de`
- Safety boundary:
  - The UI remains display-only for config writes.
  - It does not include write-mode payloads, warning acceptance flags, acknowledgement strings, rollback controls, Tiger network clients, or broker order controls.
- Tests:
  - `tests/test_dashboard_dualtrack_static.py`
  - `tests/test_connector_config_apply.py`

## M75 Tiger/MGC Config Package Full-Content Binding - 2026-07-06

- Scope: strengthen the attended config-write package so it binds the exact reviewed changes, not just their paths.
- Problem found:
  - The previous package digest included path/risk metadata, but not every `current`, `planned`, or `reason` value.
  - The config writer also compared the supplied `--package-id` to the id embedded in the submitted activation plan, but did not independently recompute it.
- Fix:
  - `config_apply_package.patch_digest` now includes `current`, `planned`, and `reason` for each config patch.
  - `config_apply_package.dualtrack_patch_digest` does the same for `configs/dualtrack.yaml` changes.
  - `ConnectorConfigApply` recomputes the package id from the submitted plan before any non-dry-run write.
- Current package:
  - `631b8ed6cb3310de`
- Negative proof:
  - Tampering `config_patch_preview[0].planned` while reusing the old package id returns `status=blocked`, blocker `config_apply_package_id_stale`, and `safety.writes_runtime_config=false`.
- Safety boundary:
  - Dry-run still does not write runtime config.
  - A stale, missing, mismatched, or content-inconsistent package id blocks before writing config.
  - This still does not authorize broker orders, paper TradeClient submission, launchd takeover, strategy promotion, or real-money execution.

## M76 Tiger/MGC Config Package Check Current - 2026-07-06

- Scope: add a final read-only package check before any future attended config write.
- Command:
  - `python3 -m pipelines.connector_config_apply check --plan outputs/connector_activation_plan/current.json --json`
- Current result:
  - `outputs/connector_config_apply/check_current.json`
  - `status=ready_for_attended_config_write`
  - `package_id=631b8ed6cb3310de`
  - `usable_for_attended_config_write=true`
  - `operator_next_action.action=authorize_attended_config_write_with_warning_acceptance`
  - `operator_next_action.requires_accept_warnings=true`
- What it validates:
  - activation package id exists.
  - package id matches the recomputed full-content digest.
  - latest config apply dry-run exists.
  - latest dry-run is `dry_run_ready`.
  - latest dry-run package id matches the activation package.
  - latest dry-run safety proves `writes_runtime_config=false`.
- UI surface:
  - dualtrack connector card shows `包检查`.
  - dualtrack connector card shows `授权下一步`.
- Safety boundary:
  - Check writes only a local check receipt.
  - It does not write runtime config, create backups, open Tiger clients, submit/cancel/close orders, switch broker routing, arm Tiger paper orders, or authorize real-money execution.

## M77 Tiger/MGC Post-Apply Validation Boundary - 2026-07-06

- Scope: make the post-config-write validation and rollback boundary explicit before any attended write.
- Current check receipt:
  - `outputs/connector_config_apply/check_current.json`
  - `status=ready_for_attended_config_write`
  - `package_id=631b8ed6cb3310de`
  - `post_apply_validation_commands` count: 4
  - `rollback_boundary.required=true`
- Post-apply validation runbook:
  - `python3 -m pipelines.connector_catalog --json`
  - `python3 -m pipelines.tiger_price_feed_acceptance --date <YYYY-MM-DD> --contract MGCmain --poll-seconds 75 --json`
  - `python3 -m pipelines.live_readiness --date <YYYY-MM-DD> --json`
  - `python3 -m pipelines.live_switch_plan --date <YYYY-MM-DD>`
- UI surface:
  - dualtrack connector card shows `写后验收`.
  - dualtrack connector card shows `回滚边界`.
- Safety boundary:
  - These commands are listed, not executed.
  - Rollback becomes actionable only after an attended config write creates backups.
  - This milestone still does not switch broker routing, submit orders, arm Tiger paper orders, or authorize real-money execution.

## M78 Tiger/MGC Operator Handoff Artifact - 2026-07-06

- Scope: create a single human-readable handoff for the current attended config-write package.
- Command:
  - `python3 -m pipelines.connector_config_apply handoff --plan outputs/connector_activation_plan/current.json --json`
- Current artifacts:
  - `outputs/connector_config_apply/handoff_current.json`
  - `outputs/connector_config_apply/handoff_current.md`
- Current result:
  - `status=ready_for_operator_review`
  - `package_id=631b8ed6cb3310de`
  - current runtime broker: `binance_usdm`
  - current dualtrack symbol: `GOLD`
  - post-apply validation count: 4
  - rollback boundary: `required=true`
- Handoff contents:
  - exact attended config apply command.
  - current runtime state before switch.
  - post-apply validation runbook.
  - rollback boundary.
  - not-authorized list.
- Safety boundary:
  - Handoff writes only local handoff artifacts.
  - It does not write runtime config, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M79 Tiger/MGC Handoff Evidence Chain - 2026-07-06

- Scope: bind the operator handoff to the exact evidence artifacts and current config fingerprints used to generate it.
- Handoff evidence now includes:
  - activation plan artifact existence/SHA256 and payload SHA256.
  - latest config dry-run receipt existence/SHA256.
  - latest persisted package check existence/SHA256.
  - computed check payload SHA256.
  - current `configs/pipeline.yaml` and `configs/dualtrack.yaml` existence/SHA256.
- Connector config status now includes:
  - `latest_handoff.status`
  - `latest_handoff.handoff_id`
  - `latest_handoff.package_id`
  - `latest_handoff.broker_provider`
  - `latest_handoff.dualtrack_symbol`
  - `latest_handoff.evidence_count`
- UI surface:
  - dualtrack connector card shows `交接单`.
  - dualtrack connector card shows `运行现状`.
- Safety boundary:
  - Evidence fingerprints are provenance, not permission.
  - The handoff and dashboard still do not write configs, accept warnings, expose acknowledgements, run rollback, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, or authorize real-money execution.

## M80 Tiger/MGC Handoff-Bound Config Write - 2026-07-06

- Scope: make any future attended config write depend on a current operator handoff, not only acknowledgement and package id.
- Write-mode config apply now requires:
  - activation plan acknowledgement.
  - exact package id.
  - explicit warning acceptance when warnings exist.
  - latest handoff status `ready_for_operator_review`.
  - latest handoff package id matching the reviewed activation package.
  - latest handoff activation payload SHA matching the submitted plan.
  - activation/dry-run/check artifacts still matching handoff SHA256 evidence.
  - current `configs/pipeline.yaml` and `configs/dualtrack.yaml` still matching handoff SHA256 evidence.
- New blockers include:
  - `config_handoff_missing`
  - `config_handoff_plan_mismatch`
  - `config_handoff_*_stale`
- Current refreshed handoff:
  - `outputs/connector_config_apply/handoff_current.json`
  - `handoff_id=handoff_20260706T061747115551Z`
  - `package_id=631b8ed6cb3310de`
  - current runtime broker: `binance_usdm`
  - current dualtrack symbol: `GOLD`
- Safety boundary:
  - This is a guard for a future attended config write, not a config write itself.
  - It does not switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M81 Tiger/MGC Check Write Guard Visibility - 2026-07-06

- Scope: make read-only check report whether the handoff-bound write preflight is actually ready.
- `connector_config_apply check` now separates:
  - package/dry-run consistency.
  - current handoff write preflight.
- Check statuses:
  - `ready_for_handoff`: package/dry-run are consistent, but a current handoff is missing or stale.
  - `ready_for_attended_config_write`: package/dry-run and handoff-bound write preflight are ready.
  - `blocked`: package/dry-run validation failed.
- Current refreshed evidence:
  - `outputs/connector_config_apply/handoff_current.json`
  - `handoff_id=handoff_20260706T062840024487Z`
  - `outputs/connector_config_apply/check_current.json`
  - `status=ready_for_attended_config_write`
  - `write_preflight.status=ready`
  - `write_preflight.blockers=[]`
- UI surface:
  - dualtrack connector card shows `写入守卫`.
- Gotcha:
  - `persisted_config_check` remains evidence in the handoff but is not a hard write gate, because running a read-only check updates `check_current.json` and should not self-invalidate the handoff.
- Safety boundary:
  - This milestone does not write runtime config, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M82 Tiger/MGC Sandbox Config Rehearsal - 2026-07-06

- Scope: prove the attended config write and rollback path against sandbox config copies before any real runtime config write.
- Command:
  - `python3 -m pipelines.connector_config_apply rehearse --plan outputs/connector_activation_plan/current.json --json`
- Current result:
  - `rehearsal_id=rehearsal_20260706T063827387627Z`
  - `package_id=631b8ed6cb3310de`
  - `status=passed`
  - `runtime_config.unchanged=true`
- Rehearsal sequence:
  - dry-run: `dry_run_ready`
  - pre-handoff check: `ready_for_handoff`
  - sandbox handoff: `ready_for_operator_review`
  - write check: `ready_for_attended_config_write`
  - sandbox apply: `applied`
  - sandbox rollback: `rolled_back`
- UI surface:
  - connector config status includes `latest_rehearsal`.
  - dualtrack connector card shows `切换演练`.
- Safety boundary:
  - Rehearsal writes only sandbox config copies under `outputs/connector_config_apply/rehearsals/`.
  - It does not write runtime config, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M83 Tiger/MGC Rehearsal-Bound Config Write - 2026-07-06

- Scope: require a passed current sandbox rehearsal before any future attended config write can proceed.
- Write-mode config apply now requires:
  - exact acknowledgement.
  - exact package id.
  - warning acceptance.
  - current handoff.
  - passed current rehearsal.
- Check progression:
  - `ready_for_handoff`
  - `ready_for_rehearsal`
  - `ready_for_attended_config_write`
- New blockers include:
  - `config_rehearsal_missing`
  - `config_rehearsal_not_passed`
  - `config_rehearsal_runtime_config_stale`
  - `config_rehearsal_*_status`
- Current refreshed evidence:
  - handoff `handoff_20260706T064853774956Z`
  - rehearsal `rehearsal_20260706T064853829778Z`
  - package `631b8ed6cb3310de`
  - `outputs/connector_config_apply/check_current.json status=ready_for_attended_config_write`
  - `write_preflight.status=ready`
- Safety boundary:
  - This is still pre-authorization proof only.
  - It does not write runtime config, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M84 Tiger/MGC Operator Authorization Package - 2026-07-06

- Scope: create one final read-only evidence package for the operator before any attended config write.
- Command:
  - `python3 -m pipelines.connector_config_apply authorization --plan outputs/connector_activation_plan/current.json --json`
- Package contents:
  - current runtime config summary.
  - current check/write-preflight summary.
  - current handoff summary.
  - current sandbox rehearsal summary.
  - local attended apply command.
  - post-apply validation commands.
  - rollback boundary.
  - explicit not-authorized list.
- Artifacts:
  - `outputs/connector_config_apply/authorization_current.json`
  - `outputs/connector_config_apply/authorization_current.md`
  - `outputs/connector_config_apply/authorization_history.json`
- UI surface:
  - connector config status includes `latest_authorization`.
  - dualtrack connector card shows `最终授权包` with status and blocker count only.
- Safety boundary:
  - This milestone does not write runtime config, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M85 Tiger/MGC Final Readiness Audit - 2026-07-06

- Scope: summarize current state against the original user-value objective before any manual switch.
- Artifact:
  - `outputs/connector_config_apply/final_readiness_audit_current.md`
- Current classification:
  - `pre-switch authorization-ready`
  - progress `99.99%`
  - active config is still not switched.
- Current evidence:
  - Tiger price-feed acceptance: `accepted`.
  - Tiger realtime validation: `pass`.
  - Shared market DB: 952 `MGCmain/1m/tiger_openapi:COMEX` bars through `2026-07-06T05:31:00+00:00`.
  - Config authorization: `ready_for_operator_authorization`, package `631b8ed6cb3310de`, blocker count `0`.
  - Runtime config: `broker.provider=binance_usdm`, `broker.dry_run=true`, `market_data.symbol=GOLD`, `market_session.enabled=false`, `human_fill_sync.enabled=false`.
- Remaining decision gates:
  - explicit attended config-write authorization.
  - strategy edge approval before treating the machine track as tradeable.
  - separate paper-order authorization before Tiger TradeClient submission.
  - real-money readiness remains out of scope.
- Safety boundary:
  - This audit does not write runtime config, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M86 Tiger/MGC Repeatable Go/No-Go Readiness Audit - 2026-07-06

- Scope: make the final readiness audit repeatable from local evidence instead of relying on a hand-written snapshot.
- Command:
  - `python3 -m pipelines.connector_config_apply readiness-audit --plan outputs/connector_activation_plan/current.json --json`
- Current result:
  - `status=go_for_attended_config_switch`
  - `current_stage=pre_switch_authorization_ready`
  - `package_id=631b8ed6cb3310de`
  - `can_switch_config_with_operator_authorization=true`
  - `can_trade_machine_track=false`
  - `can_submit_tiger_orders=false`
  - `blocker_count=0`
  - market coverage: 952 `MGCmain/1m/tiger_openapi:COMEX` rows through `2026-07-06T05:31:00+00:00`
- Artifacts:
  - `outputs/connector_config_apply/final_readiness_audit_current.json`
  - `outputs/connector_config_apply/final_readiness_audit_current.md`
  - `outputs/connector_config_apply/final_readiness_audit_history.json`
- UI/status surface:
  - connector config status includes `latest_readiness_audit`
  - dualtrack connector card shows `最终审计`
- Safety boundary:
  - This audit reads local artifacts and the local SQLite market DB only.
  - It does not write runtime config, write market DB, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M87 Tiger/MGC Post-Switch Validation - 2026-07-06

- Scope: add the read-only validator that proves a future attended config write actually switched runtime to Tiger/MGC while keeping orders disabled.
- Command:
  - `python3 -m pipelines.connector_config_apply post-switch-validate --package-id <package-id> --json`
- Valid post-switch requirements:
  - latest config apply receipt is `applied`.
  - package id matches the reviewed package.
  - rollback backup is available.
  - active broker provider is `tiger_openapi`.
  - active broker remains `dry_run=true`.
  - dualtrack market data is `MGCmain` from `tiger_openapi:COMEX`.
  - COMEX session mask is enabled.
  - execution cost model is `tiger_mgc` with `integer_contracts`.
  - human-fill sync is enabled.
  - broker order submission remains closed.
  - Tiger price-feed acceptance and realtime validation still pass.
  - local market DB has at least 500 `MGCmain/1m/tiger_openapi:COMEX` bars.
- Current pre-switch result:
  - `status=blocked`.
  - This is expected because active runtime config is still `binance_usdm` / `GOLD`.
- Artifacts:
  - `outputs/connector_config_apply/post_switch_validation_current.json`
  - `outputs/connector_config_apply/post_switch_validation_current.md`
  - `outputs/connector_config_apply/post_switch_validation_history.json`
- UI/status surface:
  - connector config status includes `latest_post_switch_validation`.
  - dualtrack connector card shows `切后验收`.
- Safety boundary:
  - This validator reads local artifacts, active config, and local SQLite only.
  - It does not write runtime config, write market DB, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M88 Tiger/MGC Rehearsal Includes Post-Switch Validation - 2026-07-06

- Scope: make sandbox rehearsal prove the full attended switch lifecycle: authorize, Go/No-Go, sandbox apply, post-switch validation, and rollback.
- Command:
  - `python3 -m pipelines.connector_config_apply rehearse --plan outputs/connector_activation_plan/current.json --json`
- Current result:
  - `status=passed`
  - `package_id=631b8ed6cb3310de`
  - `steps.authorization.status=ready_for_operator_authorization`
  - `steps.readiness_audit.status=go_for_attended_config_switch`
  - `steps.sandbox_apply.status=applied`
  - `steps.post_switch_validation.status=validated_post_switch`
  - `steps.sandbox_rollback.status=rolled_back`
  - `runtime_config.unchanged=true`
- Implementation notes:
  - sandbox support artifacts copy local price-feed acceptance/readiness/realtime/futures-feed receipts.
  - sandbox market DB is copied with SQLite backup semantics so WAL-backed MGC rows are preserved.
  - the rehearsal-only authorization bypass remains private; public authorization still requires the current passed rehearsal.
- Safety boundary:
  - Rehearsal writes sandbox config copies under `outputs/connector_config_apply/rehearsals/` only.
  - It does not write runtime config, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## M89 Tiger/MGC Dualtrack Same-Venue Scoring Guard - 2026-07-06

- Scope: protect the user-facing human-vs-machine comparison from silently mixing venues, fee models, or sizing modes after the Tiger/MGC dualtrack profile is applied.
- Guard test:
  - `python3 -m pytest tests/test_dualtrack_dt8_cycle_runner.py::test_mgc_dualtrack_close_scores_machine_and_human_with_same_tiger_contract_cost_model -q`
- Covered path:
  - local `MGCmain` bars in the market DB;
  - COMEX session mask enabled;
  - machine grid close-cycle execution through `DualTrackCycleRunner`;
  - Tiger filled-order import through `DualTrackTigerHumanSync`;
  - final attribution and daily ledger write.
- Required invariants:
  - machine and human fills both use `cost_model.venue=tiger_mgc`;
  - both tracks use `quantity_mode=integer_contracts`;
  - guarded fills carry whole-contract `contracts` and `quantity`;
  - side notional equals `price * 10`;
  - side cost equals $2.70 per MGC contract;
  - attribution and daily ledger realized PnL are computed from the same fills.
- Current runtime note:
  - active config remains pre-switch (`binance_usdm` / `GOLD`) until the operator explicitly authorizes the attended config write.
  - M89 proves the future Tiger/MGC profile path is covered; it does not switch runtime config and does not claim strategy edge.
- Safety boundary:
  - No runtime config write.
  - No Tiger SDK client opened.
  - No order submit/cancel/close.
  - No strategy promotion or real-money authorization.

## M90 Tiger/MGC Operator Stage Status - 2026-07-06

- Scope: provide a single read-only answer for the operator question "what stage is Tiger integration in right now?"
- Command:
  - `python3 -m pipelines.connector_config_apply status --json`
  - Add `--output-root <path>` when inspecting sandbox/test receipts instead of the repo's active `outputs/`.
- New status fields:
  - `current_runtime`: active broker, symbol, provider, session mask, cost model, human-fill sync state.
  - `price_feed`: acceptance, readiness, and realtime-validation summaries with age/freshness metadata.
  - `operator_stage`: synthesized stage, next action, switch/trade/order booleans, and explicit not-authorized list.
- Freshness rule:
  - price-feed evidence must be no older than 900 seconds for the attended-switch stage.
  - `price_feed_status_ready=true` means the last receipts passed.
  - `price_feed_evidence_fresh=true` means those receipts are still recent enough.
  - `price_feed_ready=true` requires both.
- Current real local result after the original acceptance receipt aged out:
  - `operator_stage.stage=price_feed_evidence_stale_refresh_acceptance`
  - `operator_stage.summary="Previous Tiger price-feed checks passed, but the evidence is no longer fresh enough for an attended config switch."`
  - `operator_stage.price_feed_status_ready=true`
  - `operator_stage.price_feed_evidence_fresh=false`
  - `operator_stage.price_feed_ready=false`
  - `operator_stage.next_action=rerun_tiger_price_feed_acceptance_then_readiness_audit`
  - `operator_stage.refresh_commands[0].command=python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-06 --contract MGCmain --poll-seconds 75 --plan-only --json`
  - `operator_stage.refresh_commands[1].command=python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-06 --contract MGCmain --poll-seconds 75 --json`
  - `operator_stage.refresh_commands[2].command=python3 -m pipelines.connector_config_apply readiness-audit --plan outputs/connector_activation_plan/current.json --json`
  - `operator_stage.refresh_commands[3].command=python3 -m pipelines.connector_config_apply status --json`
  - `operator_stage.can_switch_config_with_operator_authorization=false`
  - `operator_stage.can_trade_machine_track=false`
  - `operator_stage.can_submit_tiger_orders=false`
  - `operator_stage.runtime_switched_to_tiger_mgc=false`
  - `current_runtime.broker_provider=binance_usdm`
  - `current_runtime.dualtrack_symbol=GOLD`
- Current refreshed readiness audit:
  - `status=blocked`
  - blocker: `tiger_price_feed_evidence_stale`
  - `can_switch_config_with_operator_authorization=false`
  - market coverage still exists: 952 `MGCmain/1m/tiger_openapi:COMEX` rows through `2026-07-06T05:31:00+00:00`
- Interpretation:
  - The current system has passed the historical price-feed and switch-readiness gates.
  - The live switch decision now needs a fresh price-feed acceptance run and a refreshed readiness audit.
  - The first refresh command is plan-only. It writes `outputs/tiger_price_feed_acceptance_plan/` and does not open Tiger SDK clients.
  - The second refresh command opens a read-only Tiger QuoteClient if run; it does not open TradeClient, submit/cancel/close orders, or write runtime config.
  - It is not switched yet.
  - It is not strategy-approved.
  - It cannot submit Tiger paper or real orders.
- Safety boundary:
  - Status is read-only.
  - It does not run Tiger realtime validation, refresh market bars, write config, open Tiger SDK clients, submit/cancel/close orders, take over schedules, promote strategy, or authorize real-money execution.

## M91 Tiger Price-Feed Acceptance Plan-Only - 2026-07-06

- Scope: let the operator preview the Tiger price-feed refresh sequence before any command opens a Tiger QuoteClient.
- Command:
  - `python3 -m pipelines.tiger_price_feed_acceptance --date <YYYY-MM-DD> --contract MGCmain --poll-seconds 75 --plan-only --json`
- Output:
  - `schema_version=tiger-price-feed-acceptance-plan-v1`
  - `status=plan_ready`
  - `outputs/tiger_price_feed_acceptance_plan/current.json`
  - command sequence: price-feed acceptance -> final readiness audit -> status.
- Safety boundary:
  - The plan-only command does not open QuoteClient, TradeClient, or order clients.
  - It does not run realtime validation, refresh readiness/catalog/data-source preflight, write acceptance artifacts, write market DB bars, submit/cancel/close orders, write runtime config, take over schedules, or authorize broker routing.
  - The actual acceptance command remains explicit and separate; if run, it opens a read-only QuoteClient only.

## M92 Tiger/MGC Operator Stage Frontend Rows - 2026-07-06

- Scope: make the browser connector card show the same operator-stage state as `connector_config_apply status`.
- Source:
  - `GET /api/connectors/config/status`
  - `status.operator_stage`
- Display rows:
  - `接入阶段`: synthesized stage plus switched/not-switched state.
  - `阶段下一步`: next operator action.
  - `刷新预览`: plan-only refresh guidance, including that it opens no Tiger client and writes only a plan artifact.
  - `真实验收`: actual acceptance guidance, including that it may open QuoteClient but does not write config or submit orders.
  - `交易权限`: machine-track and Tiger-order submission remain closed unless separate gates approve them.
- Safety boundary:
  - These rows are display-only.
  - They do not add a run button for plan-only, Tiger price-feed acceptance, config apply, rollback, schedule takeover, or Tiger orders.
  - They do not expose acknowledgement strings, raw write payloads, raw credentials, Tiger properties path, order controls, or rollback controls.

## M93 Tiger/MGC Price-Feed Refresh Runbook - 2026-07-06

- Scope: produce one durable operator package for refreshing stale Tiger/MGC price-feed evidence at market open.
- Command:
  - `python3 -m pipelines.connector_config_apply price-feed-refresh-runbook --json`
- Artifacts:
  - `outputs/connector_config_apply/price_feed_refresh_runbook_current.json`
  - `outputs/connector_config_apply/price_feed_refresh_runbook_current.md`
  - `outputs/connector_config_apply/status_current.json`
  - `outputs/connector_config_apply/price_feed_refresh_runbook_history.json`
- Current real local output:
  - `schema_version=connector-price-feed-refresh-runbook-v1`
  - `status=ready_for_operator_refresh`
  - `operator_stage.stage=price_feed_evidence_stale_refresh_acceptance`
  - `operator_stage.runtime_switched_to_tiger_mgc=false`
  - `operator_stage.can_switch_config_with_operator_authorization=false`
  - `operator_stage.can_trade_machine_track=false`
  - `operator_stage.can_submit_tiger_orders=false`
- Runbook command sequence:
  - `preview_tiger_price_feed_acceptance_refresh`: plan-only, opens no Tiger SDK client, writes only a plan artifact.
  - `refresh_tiger_price_feed_acceptance`: opens read-only QuoteClient, does not open TradeClient, does not submit orders, does not write runtime config.
  - `refresh_final_readiness_audit`: local artifact/DB audit only.
  - `show_connector_switch_status`: local status only.
- Safety boundary:
  - Runbook generation does not run Tiger price-feed acceptance.
  - It writes the status snapshot used to build the runbook, so the evidence path is durable and auditable.
  - It does not open QuoteClient, open TradeClient, write market DB bars, write runtime config, submit/cancel/close orders, expose credentials, take over schedules, promote strategy, or authorize broker routing.
  - The runbook being `ready_for_operator_refresh` does not mean the config switch is ready; the current readiness audit remains blocked until fresh price-feed evidence is collected.

## M94 Tiger/MGC Refresh Runbook Status Surface - 2026-07-06

- Scope: show the latest refresh runbook in status and the dashboard without generating a new runbook from read paths.
- Source:
  - `outputs/connector_config_apply/price_feed_refresh_runbook_current.json`
  - `outputs/connector_config_apply/status_current.json`
- Status field:
  - `latest_price_feed_refresh_runbook`
- Current real local summary:
  - `status=ready_for_operator_refresh`
  - `operator_stage=price_feed_evidence_stale_refresh_acceptance`
  - `command_count=4`
  - `status_snapshot_exists=true`
  - `writes_runtime_config=false`
  - `opens_network_clients=false`
  - `opens_quote_client=false`
  - `opens_trade_client=false`
  - `submits_orders=false`
- Dashboard rows:
  - `刷新操作包`: shows runbook status and step count.
  - `操作包证据`: shows whether the bound status snapshot exists.
- Safety boundary:
  - `connector_config_apply status` only summarizes an existing runbook artifact.
  - The dashboard only renders the status summary.
  - Neither path creates a runbook, runs Tiger price-feed acceptance, opens Tiger clients, writes market DB bars, writes runtime config, submits/cancels/closes orders, exposes credentials, takes over schedules, promotes strategy, or authorizes broker routing.

## M95 Tiger/MGC Refresh Runbook Read-Only API - 2026-07-06

- Scope: expose the existing market-open refresh package to the dashboard and operator tools without turning a read path into a refresh action.
- Endpoint:
  - `GET /api/connectors/config/price-feed-refresh-runbook`
- Source:
  - `outputs/connector_config_apply/price_feed_refresh_runbook_current.json`
- Behavior:
  - returns the latest runbook receipt when the artifact exists;
  - adds `served_from` so the returned package is auditable;
  - adds `endpoint_safety` with `generates_runbook=false`, `opens_network_clients=false`, `opens_quote_client=false`, `opens_trade_client=false`, `submits_orders=false`, and `writes_runtime_config=false`;
  - returns `status=missing` plus an empty `command_sequence` when the artifact does not exist.
- Dashboard:
  - `dashboard-dualtrack-v5.html` fetches this endpoint directly;
  - the connector card still renders only `刷新操作包` and `操作包证据`;
  - the browser does not expose run buttons for plan-only preview, Tiger acceptance, readiness audit, config apply, rollback, or orders.
- Safety boundary:
  - This endpoint must only read the existing JSON artifact.
  - It must not call `price_feed_refresh_runbook()`, generate JSON/Markdown artifacts, run Tiger price-feed acceptance, open QuoteClient, open TradeClient, refresh market bars, write runtime config, submit/cancel/close orders, expose credentials, take over schedules, promote strategy, or authorize broker routing.

## M96 Tiger/MGC Refresh Runbook Step Display - 2026-07-06

- Scope: make the market-open refresh sequence visible in the dashboard without adding an execution surface.
- Dashboard rows:
  - `刷新步骤 1`: `预览验收计划`
  - `刷新步骤 2`: `运行只读行情验收`
  - `刷新步骤 3`: `刷新最终审计`
  - `刷新步骤 4`: `查看当前阶段`
- Safety markers:
  - each row shows whether the step opens QuoteClient;
  - each row shows that TradeClient, order submission, and runtime config writes remain closed unless a future artifact explicitly says otherwise;
  - unsafe markers render as negative if a future runbook ever includes TradeClient, order submission, or config write flags.
- Endpoint hardening:
  - `GET /api/connectors/config/price-feed-refresh-runbook` now writes `endpoint_safety` from server code, not from the runbook artifact.
  - A stale or malformed artifact cannot make the endpoint claim it generates runbooks, opens clients, submits orders, or writes config.
- Safety boundary:
  - The dashboard rows are display-only.
  - They must not become clickable run buttons, hidden command runners, terminal automation, config-write controls, rollback controls, Tiger SDK calls, or order controls.

## M97 Tiger/MGC Refresh Window Gate - 2026-07-06

- Scope: tell the operator whether the read-only Tiger price-feed acceptance step should be run now or after the next COMEX open, without opening Tiger SDK clients.
- Runbook field:
  - `refresh_window_gate`
- Possible statuses:
  - `wait_for_comex_open`: COMEX is closed by the local session calendar; plan-only preview is safe, but the QuoteClient acceptance step should wait.
  - `ready_to_run_acceptance_now`: COMEX is open by the local session calendar; the operator may run the explicit read-only acceptance command.
  - `not_required`: current operator stage has no price-feed refresh command sequence.
- CLI:
  - `python3 -m pipelines.connector_config_apply price-feed-refresh-runbook --as-of <ISO8601> --json`
  - `--as-of` exists for deterministic previews and tests; omit it for the current local check.
- Dashboard:
  - row `验收窗口`;
  - shows `COMEX open · run read-only acceptance` when locally open;
  - shows `wait · <next_open>` while closed;
  - safety markers remain local-calendar/no TradeClient/no orders/no config write.
- Current regenerated local artifact:
  - `outputs/connector_config_apply/price_feed_refresh_runbook_current.json`
  - `runbook_id=price_feed_refresh_runbook_20260706T091234720737Z`
  - `refresh_window_gate.status=ready_to_run_acceptance_now`
  - `refresh_window_gate.can_run_quote_client_step=true`
  - `operator_stage.stage=price_feed_evidence_stale_refresh_acceptance`
  - `operator_stage.can_switch_config_with_operator_authorization=false`
- Safety boundary:
  - The gate uses `services.dualtrack_clock.comex_futures_session_status`.
  - It does not run Tiger price-feed acceptance, open QuoteClient, open TradeClient, refresh market bars, write runtime config, submit/cancel/close orders, expose credentials, promote strategy, or authorize broker routing.
  - `ready_to_run_acceptance_now` is not an acceptance pass and does not make `can_switch_config_with_operator_authorization` true by itself.

## M98 Tiger/MGC Real Price-Feed Acceptance Refresh - 2026-07-06

- Scope: refresh the actual Tiger/MGC market-hours evidence and move the connector state to the human-reviewed config-switch gate.
- Commands run:
  - `python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-06 --contract MGCmain --poll-seconds 75 --json`
  - `python3 -m pipelines.tiger_futures_feed --date 2026-07-06 --contract MGCmain --limit 500`
  - `python3 -m pipelines.data_source_preflight --date 2026-07-06 --symbol MGCmain --timeframe 1m`
  - `python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-06 --contract MGCmain --skip-realtime-run --json`
  - `python3 -m pipelines.connector_config_apply readiness-audit --plan outputs/connector_activation_plan/current.json --json`
  - `python3 -m pipelines.connector_config_apply price-feed-refresh-runbook --json`
- Real-time acceptance result:
  - `tiger_realtime_validation.status=pass`
  - `bar_advanced=true`
  - `fresh=true`
  - `opens_quote_client=true`
  - `opens_trade_client=false`
  - `submits_orders=false`
  - `tiger_price_feed_acceptance.status=accepted`
  - `ready_for_price_feed=true`
- Local market DB refresh:
  - imported 500 `MGCmain/1m` bars from `tiger_openapi:COMEX`;
  - coverage rows moved to 1177;
  - latest local timestamp moved to `2026-07-06T09:16:00+00:00`.
- Scoped data-source preflight:
  - `outputs/data_source_preflight/MGCmain_1m/current.json`
  - `status=pass`
  - `ready_for_paper=true`
  - `ready_for_live=true`
  - `live_data_mode=execution_venue`
- Final switch gate:
  - `operator_stage.stage=ready_for_attended_config_switch`
  - `can_switch_config_with_operator_authorization=true`
  - `price_feed_ready=true`
  - `readiness_audit_fresh=true`
  - `runtime_switched_to_tiger_mgc=false`
  - `can_trade_machine_track=false`
  - `can_submit_tiger_orders=false`
- Current refresh runbook:
  - `status=not_required`
  - `command_count=0`
  - `refresh_window_gate.status=not_required`
- Gotcha:
  - Tiger real-time acceptance does not write local market bars. If the dashboard/data-source preflight needs current bars, run `tiger_futures_feed` after acceptance.
  - The final `tiger_price_feed_acceptance/current.json` was refreshed with `--skip-realtime-run`, so its safety shows `opens_quote_client=false`; the actual QuoteClient evidence is in `outputs/tiger_realtime_validation/current.json`.
- Safety boundary:
  - No TradeClient/order client was opened.
  - No order was submitted, cancelled, or closed.
  - No runtime config was written.
  - The system is ready for explicit attended config-switch review only; it is not switched and not trade-enabled.

## M99 Tiger/MGC Attended Switch Review Surface - 2026-07-06

- Scope: make the final pre-switch state visible in the dashboard without exposing the raw write acknowledgement or adding a write button.
- Status field:
  - `operator_stage.attended_switch_review`
- Key fields:
  - `status=ready_for_operator_review` when all pre-switch evidence is current;
  - `package_id=631b8ed6cb3310de`;
  - `can_switch_config_with_operator_authorization=true`;
  - `requires_operator_command=true`;
  - `rollback_required=true`;
  - `post_apply_validation_count=4`;
  - `runtime_config_writes_from_status_endpoint=false`;
  - `opens_network_clients_from_status_endpoint=false`;
  - `submits_orders_from_status_endpoint=false`;
  - `can_trade_machine_track_after_switch=false`;
  - `can_submit_tiger_orders_after_switch=false`.
- Dashboard rows:
  - `人工切换`
  - `切换包ID`
  - `切后验收`
  - `切后交易`
- Safety boundary:
  - The dashboard remains display-only.
  - It does not expose the raw acknowledgement string.
  - It does not run `connector_config_apply apply`.
  - It does not open Tiger clients, write runtime config, submit/cancel/close orders, promote strategy, or bypass post-apply validation.

## M100 Tiger/MGC Redacted Attended Switch Review API - 2026-07-06

- Scope: provide a stable read-only endpoint for frontend/operator tools to inspect the final switch review state without parsing the whole connector status.
- Endpoint:
  - `GET /api/connectors/config/attended-switch-review`
- Schema:
  - `connector-attended-switch-review-api-v1`
- Current state:
  - `status=ready_for_operator_review`
  - `package_id=631b8ed6cb3310de`
  - `operator_stage=ready_for_attended_config_switch`
  - `can_switch_config_with_operator_authorization=true`
  - `runtime_switched_to_tiger_mgc=false`
  - `current_broker_provider=binance_usdm`
  - `current_dualtrack_symbol=GOLD`
  - `can_trade_machine_track=false`
  - `can_submit_tiger_orders=false`
  - `requires_operator_command=true`
  - `requires_acknowledgement=true`
  - `rollback_required=true`
  - `post_apply_validation_count=4`
  - `after_switch_gates.can_trade_machine_track=false`
  - `after_switch_gates.can_submit_tiger_orders=false`
- Redaction:
  - `raw_acknowledgement_exposed=false`
  - `attended_apply_command_exposed=false`
  - `credential_values_exposed=false`
- Endpoint safety:
  - `runs_config_apply=false`
  - `opens_trade_client=false`
  - `submits_orders=false`
  - `writes_runtime_config=false`
- Dashboard:
  - fetches this endpoint directly;
  - falls back to `operator_stage.attended_switch_review` if unavailable;
  - prefers the endpoint's top-level current runtime/order-gate facts for `接入阶段` and `交易权限`;

## M101 Tiger Paper Order Readiness Same-Day Evidence Gate - 2026-07-06

- Scope: prevent a future Tiger paper-order canary from passing on stale prior-day artifacts.
- Rule:
  - `tiger_openapi_paper_order_readiness` now requires same-`run_date` evidence for:
    - dated contract status;
    - reconciliation flat check;
    - account sync;
    - order/fill sync;
    - kill-switch evidence;
    - local fake-client paper-order drill.
- Current behavior:
  - Running the readiness gate for `2026-07-06` with `2026-07-05` reconciliation/order/account evidence returns `status=blocked`.
  - Blocked checks include `required_run_date` and `current_for_run_date=false`.
  - `TigerVenueStatus.can_open_new_orders` can still be true from the latest flat reconciliation artifact.
  - `TigerVenueStatus.can_enter_attended_paper_order` is false until the stricter paper-order readiness gate passes.
  - `TigerVenueStatus.paper_order_readiness.operator_next_action` summarizes whether to refresh stale evidence, how many stale blockers exist, and whether refresh steps use read-only TradeClient evidence.
  - The dashboard shows this as `下单证据` and `刷新路径`.
  - `python3 -m pipelines.tiger_openapi_paper_order_readiness --date YYYY-MM-DD --refresh-runbook --json` writes an artifact-only refresh runbook at `outputs/tiger_paper_order_readiness/refresh_runbook_current.json`.
  - `TigerVenueStatus.paper_order_refresh_runbook` summarizes the runbook for the dashboard row `证据刷新包`.
  - `paper_order_refresh_runbook.matches_current_readiness=true` means the runbook was generated from the current readiness receipt.
  - If `matches_current_readiness=false`, the dashboard labels it as a stale package and the operator should regenerate it before using its command sequence.
  - `GET /api/dualtrack/venue/tiger/paper-order-refresh-runbook` exposes a redacted read-only API for the refresh package.
  - The API returns step labels and safety flags, but not raw executable `command` text.
  - `GET /api/connectors/config/price-feed-refresh-runbook` follows the same dashboard redaction rule: local artifacts keep commands; API responses strip raw command text.
- Rationale:
  - `TigerVenueStatus` may display the latest known account/order state.
  - Paper-order readiness is stricter because it is the gate immediately before an attended TradeClient canary.
- Safety boundary:
  - This gate is still artifact-only.
  - It does not open QuoteClient, open TradeClient, refresh account/order state, submit/cancel/close orders, or write runtime config.
  - Dashboard refresh-path rows are display-only labels; they do not run the refresh commands.
  - Refresh-runbook generation writes local JSON/Markdown only; it does not run the refresh commands embedded in the runbook.
  - still renders only display rows, not an execute button.
- Safety boundary:
  - The exact write command remains local-only in `outputs/connector_config_apply/authorization_current.md`.
  - The endpoint must not expose that command or the raw acknowledgement string.
  - It must not write config, open clients, submit/cancel/close orders, or bypass post-apply validation.
