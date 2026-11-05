# Strategy Lab Implementation Notes

## Product Purpose

Strategy Lab exists to reject unreliable strategy promotion. Its job is not to
find the prettiest historical curve; it must make strategy evidence auditable:
fixed rules, fixed sizing, explicit costs, out-of-sample judgment, registered
trials, and fail-closed invalidation when evidence is thin or corrupted.

## Verified Baseline

- Base branch before work: `feat/autonomy-remaining` at `c53aeff`.
- Work branch: `codex/strategy-lab-research`.
- Spec read: `docs/strategy-lab-spec.md`.
- Existing candidate lab seed: `services/strategy_backtester.py`.
- Existing non-lab backtest surfaces:
  - `services/local_backtester.py` is a production signal-evidence helper.
  - `services/backtest_client.py` wraps remote/local evidence for production.
  - `pipelines/backtest_strategies.py` writes legacy backtest reports under
    `outputs/backtests/`.
- 1m backfill entry point: `pipelines/backfill_gold_1m.py`, which calls
  `services.binance_futures_feed.run_binance_usdm_1m_backfill`.
- Historical direction artifacts:
  - `outputs/market_views/`
  - `outputs/direction_bias_decisions/`

## Conservative Decisions

- The lab engine will use `services/strategy_backtester.py` semantics as the
  seed because it already models one-position-at-a-time stop/target/timeout
  outcomes and existing paper-execution costs.
- `local_backtester.py` and `backtest_client.py` remain production evidence
  surfaces. Strategy Lab will not delegate promotion-grade research decisions
  to them.
- Research sizing is fixed notional. It will not inherit production
  `position_size_pct` or leverage semantics.
- Signal-on-close execution is no earlier than the next bar open.
- If stop and target are both touched in the same bar, the stop wins.
- Missing bars, NaN metrics, or zero-trade evaluation windows are invalid
  results, not neutral or zero-valued results.

## Deviations And Reality Checks

- Current local `data/market_data.db` contains `GOLD` `1m` bars starting at
  `2025-12-11T08:05:00+00:00`, which is less than 12 calendar months as of
  this task.
- User decision on 2026-07-05: using the full available XAUUSDT history is an
  acceptable substitute for the original 12-month requirement because Binance
  official metadata shows the contract did not exist earlier.
- `pipelines/backfill_gold_1m.py` defaults to `2025-12-11T00:00:00+00:00` and
  describes that as the XAUUSDT listing-date area.
- Binance public USDⓈ-M `exchangeInfo` confirms `XAUUSDT` has
  `onboardDate=2025-12-11T08:05:00+00:00`, `contractType=TRADIFI_PERPETUAL`,
  and `status=TRADING`.
- `outputs/clean_bars/` has 44 day directories, but current 1m research data
  lives in SQLite, not `outputs/clean_bars/GOLD_1m.json`.
- Historical `DirectionBiasGate` evidence is thin: market views exist only for
  a few dates, and direction-bias decisions begin around 2026-06-25. E2 must
  report both the retrospective sample size and prospective-logging status.
- R1 prompt described 21 strategies, but current `configs/strategy.yaml`
  contains 22 registered strategies. R1 scanned all 22 current strategies so no
  current registry entry was silently omitted.
- R1 5m replay resamples 1m SQLite bars with UTC epoch-floor 5-minute buckets,
  matching `MarketStore.load_aggregated_bars_between`: first open, max high,
  min low, last close, summed volume, bucket-start timestamp.
- Chan strategies expose `historical_signals`, but the engine docstring states
  that its fast historical path has mild lookahead versus the causal live
  detector. Because Strategy Lab §3 forbids lookahead, R1 marks chan variants
  `not_replayable` with `historical_signals_noncausal_lookahead_risk` instead
  of using those metrics.
- `gold_5m_v1` uses the legacy MA/macro `SignalEngine`, which has no
  deterministic `historical_signals` interface and depends on live event/macro
  context. R1 marks it `not_replayable` with `missing_historical_signals`.
- R1 technical-rule replay uses each engine's configured `min_bars` plus a
  conservative bounded causal rolling window for indicator warmup. This avoids
  full-history recomputation while keeping replay deterministic and aligned
  with live engines seeing finite candle buffers.
- R3 ran on a moving local SQLite backfill during the session; unfrozen reruns
  can change only `data_range` as fresh 1m bars arrive. Determinism was verified
  by freezing `--end 2026-07-05T02:22:00+00:00`; two fixed-input R3 runs
  produced the same scrubbed artifact hash
  `9f75b0c0053e926a06ee6a8a02a168702506c137ff587fd53773d7dcb037b4c9`.
- R3 recommended the R2 primary-label horizon from the selected gross/risk cell:
  `gold_5m_psych_level_rejection`, hold `8x`, stop/target scale `1.0x`, horizon
  `960` minutes.
- R2 features use causal rolling regime proxies (`causal_regime_vol`,
  `causal_regime_trend`) instead of precomputed E3 regime labels. Full-sample E3
  buckets would introduce avoidable lookahead risk into model features.
- R2 `gbt_depth3` is fixed to a light triage configuration
  (`n_estimators=24`, `max_depth=3`, `max_features="sqrt"`, `subsample=0.8`,
  seeded) because full calibrated GBT sweeps were the wall-clock bottleneck. The
  spec pins depth and determinism, not tree count.
- Historical chan live signal records reference
  `outputs/clean_bars/YYYY-MM-DD/GOLD_1m.json` source artifacts that are no
  longer present locally. R2 therefore reports chan parity as
  `source_artifacts_missing` instead of claiming a match from reconstructed
  SQLite bars.
- The chan engine's fast `historical_signals` path remains noncausal per its own
  docstring, and the true causal detector is too slow for the 296k-bar lab range.
  R2 registers all four chan-family addendum trials as `not_replayable` with an
  explicit bounded-replay reason rather than using the noncausal fast path.
- Owner decision on 2026-07-05 for R4: funding costs are excluded because the
  target venue will not be a perpetual and simplicity is preferred. R4 reports
  carry the required footnote: "Funding excluded (owner decision); revisit before
  real-money on any perpetual venue."
- Owner decision on 2026-07-05 for R4: pass/fail verdicts use a lab-scoped
  primary cost basis of `0.5` bp/side, while the Binance-maker reference basis of
  `2` bp/side is reported as secondary information only. Production
  `paper_execution_costs` remain untouched.
- R4 pins its evaluation input to the R3 candidate artifacts' shared
  `data_range.end` (`2026-07-05T02:22:00+00:00`) before reproducing anchors.
  This keeps the pre-registered R3 cells stable even as local SQLite backfill
  continues to receive newer 1m bars.
- R4 regime-slice gates reuse E3's deterministic 4h `volatility_bucket` labels
  as the fixed three-slice battery (`low`, `mid`, `high`), with a primary pass
  requiring non-negative expectancy in at least two of the three slices.
- R4 promoted `gold_5m_psych_level_rejection_swing` to lab-scoped
  `paper_eligible`; `gold_5m_ema50_position_swing` failed the pre-registered
  holdout expectancy criterion and is not paper-eligible.
- R4 adds disabled strategy config entries for the two swing variants. The
  current production runner ignores disabled entries, but the live/paper
  execution path does not yet express R4's 8x hold and scaled stop/target
  geometry as runtime exit semantics. Before any manual enablement, the minimal
  production change would be to add strategy-scoped exit-geometry support to the
  paper/live execution path and prove parity with the lab replay.

## Backfill Safety

The identified 1m backfill path writes market data to the local SQLite market
database and receipts under `outputs/binance_usdm_1m_backfill/`. It does not
submit, approve, close, cancel, or route orders.

## Tiger OpenAPI M0-M55

- Tiger OpenAPI M0/M1 is read-only and uses QuoteClient only. The Tiger
  RSA/license properties file is referenced through `TIGER_OPENAPI_CONFIG_PATH`
  and must be owner-only (`chmod 600`); the feed fails closed if the file is
  group/other accessible.
- The original props file on the mounted volume reported mode `0777` on that
  volume. The local runtime copy used for M17 commands is owner-only, mode
  `0600`; the exact local path is intentionally kept out of repo docs.
- Tiger historical futures bars can be imported with
  `pipelines.tiger_futures_feed`; session windows can be queried with
  `pipelines.tiger_futures_sessions`. Both are read-only and do not import
  TradeClient or order APIs.
- Tiger price-feed acceptance is now a one-command receipt:
  `pipelines.tiger_price_feed_acceptance`. It can refresh the read-only
  realtime validation, then evaluates local price-feed readiness and connector
  catalog artifacts. It writes `outputs/tiger_price_feed_acceptance/` and exits
  0 only when Tiger is accepted as a price feed; exit 75 means wait for market
  open; exit 2 means blocked. It cannot authorize or submit broker orders.
- The dual-track Tiger venue dashboard now surfaces that receipt as
  `验收收据` through `TigerVenueStatus.price_feed_acceptance`. This is a
  read-only display field over local artifacts; it does not run validation or
  create any control path.
- `ConnectorCatalog` now carries the Tiger price-feed acceptance summary for
  future onboarding UI. Catalog status still comes from credential readiness and
  `tiger_price_feed_readiness`, not acceptance, because acceptance refreshes the
  catalog as one of its steps.
- Connector onboarding and activation preview now propagate Tiger acceptance
  context into their role checks and warnings. Pending market open is surfaced
  as a next-window action, while these surfaces remain dry-run only.
- The dual-track connector panel now renders the first onboarding blocker and
  activation warning/blocker as `验证阻塞` and `预览警告`. This is display only;
  the panel still does not run acceptance or write config.
- `LiveReadiness._broker_feedback` now handles `tiger_openapi` by reading local
  `tiger_price_feed_readiness` and `tiger_price_feed_acceptance` artifacts. It
  passes only after `ready_for_price_feed=true`; pending market open is reported
  with next-window evidence.
- `DataSourcePreflight` now has a Tiger-specific execution-venue gate for
  `MGCmain`/`1m`. Local `tiger_openapi:COMEX` bars can support paper/research,
  but they do not make the source live-ready unless
  `tiger_price_feed_readiness.ready_for_price_feed=true`. Pending market-hours
  acceptance is carried into the data-source preflight evidence instead of
  being treated as a generic stale/missing bar.
- Non-default shared-output preflight runs are namespaced. `MGCmain`/`1m`
  writes `outputs/data_source_preflight/MGCmain_1m/current.json` and no longer
  overwrites the legacy global `outputs/data_source_preflight/current.json`
  consumed by GOLD/5m dashboards and risk gates. When a clean-bars artifact is
  absent, preflight falls back to the SQLite latest bar so Tiger feed imports
  can be evaluated directly.
- `TigerVenueStatus` and `dashboard-dualtrack-v5.html` now expose the
  namespaced `MGCmain`/`1m` preflight as display-only `数据源预检`. Real local
  snapshot after M41 showed Tiger paper venue `ready`, while MGC data-source
  preflight remained `fail` with `gate.status=pending_market_open`, preserving
  the separation between broker venue status and price-feed acceptance.
- `TigerPriceFeedAcceptance` now refreshes the namespaced MGC data-source
  preflight and embeds a `steps.data_source_preflight` summary in the acceptance
  receipt. The real local `--skip-realtime-run` aggregation after M42 returned
  `pending_market_open`, `exit_code=75`, `steps.data_source_preflight.source_key=MGCmain_1m`,
  and safety flags `opens_quote_client=false`, `opens_trade_client=false`,
  `submits_orders=false`.
- `TigerVenueStatus.price_feed_acceptance` now computes display-only operator
  timing from the local acceptance artifact. The real local snapshot after M43
  reported `operator_status=waiting_market_open` and summary
  `Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance.`
  Dashboard renders that as `验收下一步`; it does not auto-run acceptance.
- `LiveReadiness.run()` now chooses the market-data identity from the broker
  provider before running data checks. For `tiger_openapi`, it evaluates
  `MGCmain`/`1m` data-source preflight and gap checks with
  `write_legacy_artifacts=false`, so Tiger readiness does not overwrite
  legacy `GOLD`/`5m` dashboard state. `DataGapDoctor` can use SQLite bars when
  clean-bars artifacts are absent, matching Tiger feed imports.
- `LiveActivationGate` and `LiveSwitchPlan` now use the same broker-aware
  market-data identity at the final gates. When broker preflight reports
  `tiger_openapi`, they read the scoped
  `outputs/data_source_preflight/MGCmain_1m/` artifact instead of the legacy
  `outputs/data_source_preflight/current.json` `GOLD`/`5m` surface.
- Connector catalog/onboarding/activation/acceptance now load the local live
  env before credential readiness checks in real operator-facing runs. The
  M46 local artifact state is now accurate: Tiger broker-order credentials are
  present and owner-only, while Tiger price-feed promotion is blocked only by
  market-hours acceptance. Artifacts still redact credential values and the
  Tiger properties file path.
- Tiger `MGCmain`/`1m` data-source freshness is now aware of the pending COMEX
  session gate. While acceptance is `pending_market_open` and the next recorded
  window has not started, the last exchange bar can support paper/research
  visibility even if its wall-clock age is large. Once that window starts,
  freshness is enforced again. The M47 local acceptance refresh returned
  `data_source_preflight.status=warn`, `ready_for_paper=true`, and
  `ready_for_live=false`.
- Tiger price-feed acceptance receipts now include `operator_next_action` with
  wait/rerun/expired/accepted/blocked guidance. The M48 local artifact state is
  `pending_market_open` plus `operator_next_action.status=waiting_market_open`,
  pointing at the next safe acceptance command after the recorded COMEX window
  opens. This is artifact guidance only; it does not schedule or run SDK work.
- Tiger price-feed acceptance operator guidance now uses the validation
  reference time instead of unconditional wall-clock time: explicit `as_of`
  first, loaded realtime `checked_at` for artifact-only aggregation, otherwise
  current acceptance time. This keeps historical and skip-realtime receipts
  deterministic.
- `TigerVenueStatus` now consumes that acceptance guidance directly and only
  computes wait/rerun/expired locally for legacy receipts. The M49 local venue
  snapshot returned the same `waiting_market_open` next command as the
  acceptance artifact.
- Connector catalog, dry-run onboarding, activation preview, and the browser
  connector panel now propagate the same acceptance `operator_next_action`
  summary. This keeps the future "choose a platform and preview activation"
  flow aligned with the one-command acceptance receipt while remaining
  display-only and non-mutating.
- `ConnectorActivationPlan` now emits an explicit `activation_gate` for the
  future platform-switch UI. The gate distinguishes blocked, preview-ready with
  warnings, and preview-ready states, while keeping config writes, network
  clients, and broker orders outside this endpoint.
- `ConnectorActivationPlan` now also emits an artifact-only
  `activation_runbook`. It gives the future platform-switch UI an ordered path
  through blocker resolution, a separate config-write milestone, post-apply
  validation, and rollback requirements without executing any of those steps.
- `ConnectorActivationPlan` now emits an artifact-only
  `config_apply_package`. It ties patch digest, operator acknowledgement,
  backup/rollback requirements, and post-apply validation commands together,
  but still cannot apply configs or enable broker orders from the preview
  endpoint.
- `ConnectorActivationPlan` now emits `switch_audit`, a top-level read model
  that answers whether the selected connector can proceed. It aggregates the
  onboarding, activation gate, runbook, config package, and broker-order
  network status while keeping connector switching and broker-order
  authorization outside this endpoint.
- M3 uses an isolated research DB at `outputs/lab/tiger_m3/tiger_market.db`.
  Current coverage: `MGCmain` 33,900 rows from `2026-06-01T00:00:00+00:00` to
  `2026-07-03T16:59:00+00:00`; `1OZmain` 3,747 rows from
  `2026-07-01T00:00:00+00:00` to `2026-07-03T16:59:00+00:00`.
- M3 added `R5_tiger_mgc_contract_grid`, an exploratory integer-contract
  variant of R5: one MGC contract per rung, max inventory capped at 1 or 2
  contracts, and fixed per-contract Tiger costs from `configs/risk_rules.yaml`.
- The 2026-07-05 M3 exploratory run wrote
  `outputs/lab/reports/R5_tiger_mgc_contract_grid.md` and failed its random-arm
  decision gate: no valid random-direction cell had positive mean net PnL per
  cycle. Oracle arms were strongly positive, so the blocker is direction edge /
  strategy shape, not fee math alone. This result is not promotion evidence
  because no holdout was consumed and the sample is short.
- M4/M5 added `broker_profiles.tiger_openapi_paper` and routed
  `tiger_openapi` through the existing BrokerAdapter port in a fail-closed
  state. Preflight checks the Tiger props path and owner-only file permissions.
  Dry-run mode writes a Tiger futures order intent under
  `outputs/tiger_order_requests/`, including COMEX contract, FUT security type,
  side, order type, limit price, stop/targets, and positive whole-contract
  quantity. Non-dry-run network order submission is still explicitly
  `not_implemented`; no Tiger TradeClient order path exists yet.
- M6 added an optional `TigerOpenApiPaperBrokerAdapter` and broker profile
  resolver. The checked-in `tiger_openapi_paper` profile is still non-network
  (`dry_run=true`, `network_order_submission=not_implemented_fail_closed`,
  `confirm_tiger_paper_orders=false`). Tests use an injected fake TradeClient
  and fake SDK to prove the network-capable paper path without touching a real
  Tiger account: positions precheck, open-order precheck, preview, place,
  lifecycle intent, artifact recording, whole-contract sizing, dated-contract
  enforcement, and attached stop/target legs for limit entries. Live Tiger
  order submission remains out of scope.
- M7 added Tiger read-only reconciliation and kill-switch dry-run surfaces.
  `TigerOpenApiPaperReconciliation` writes `outputs/tiger_reconciliation/` from
  TradeClient positions/open-orders reads and uses the same safety vocabulary as
  the Binance reconciliation path (`confirmed_flat`, `confirmed_drift`,
  `cannot_confirm`, `suspected_naked_position`, `can_open_new_orders`). The
  Tiger paper adapter now requires `confirmed_flat` before preview/place.
  `TigerOpenApiPaperKillSwitch` writes `outputs/tiger_kill_switch/`, plans
  cancel/close actions, and can activate shared HALT when explicitly confirmed,
  but it does not call Tiger cancel/close/place APIs. `live_readiness` and
  `live_switch_plan` recognize `tiger_openapi` only when broker preflight is
  ready.
- Real read-only Tiger smoke on 2026-07-05 confirmed the paper account was flat
  for managed Tiger symbols: no Tiger positions, no Tiger open orders, no drift,
  and kill-switch dry-run created no network order/cancel.
- M8 added `TigerOpenApiOrderSync`, a read-only order/fill attribution surface
  that writes `outputs/tiger_order_sync/` from TradeClient `get_open_orders` and
  `get_filled_orders`. It redacts account/key fields and does not submit,
  cancel, modify, or close orders.
- M8 also added a paper kill-switch network path for tests and future operator
  drills. The checked-in profile remains disabled
  (`enable_tiger_kill_switch_network_actions=false`). Network cancel/close only
  runs when the broker profile is non-dry-run paper TradeClient mode, Tiger
  paper order confirmation is true, the kill-switch network flag is true, and
  runtime confirmation is passed. Default runs still create no Tiger network
  cancel/place calls.
- Real read-only M8 smoke on 2026-07-05 wrote
  `outputs/tiger_order_sync/current.json` with `sync_status=synced`,
  `open_order_count=0`, and `filled_order_count=0`. The SDK required an
  explicit filled-order time window, so the pipeline defaults to
  `<run_date> 00:00:00` through `<run_date> 23:59:59`.
- M9 added `TigerVenueStatus` as a dashboard read model over local Tiger
  artifacts only. It reads reconciliation, order-sync, and kill-switch current
  JSON files, redacts raw order fields from frontend summaries, and exposes
  status vocabulary `ready`, `blocked`, `manual_review`, `degraded`, and
  `unknown`.
- M9 added `GET /api/dualtrack/venue/tiger` and a Tiger paper venue card in
  `dashboard-dualtrack-v5.html`. The dashboard path does not open Tiger SDK
  clients and does not add any dual-track control POST endpoint; the POST
  surface remains plan, paper order, and verdict only.
- M10 added `TigerContractResolver` and `pipelines.tiger_contract_status`.
  Contract execution is now explicit: continuous/research symbols such as
  `MGCmain` can map to a dated tradable contract only through
  `execution_contract_map`; dated contracts are blocked inside the configured
  rollover window (`rollover_days_before_contract_month=14` in the checked-in
  Tiger profile). The 2026-07-05 local check wrote
  `outputs/tiger_contracts/current.json` with `MGCmain -> MGC2608`,
  `status=ready`, and `days_to_contract_month=27`.
- M10 also put Tiger paper TradeClient network submission behind
  `LiveMoneyGuardrails` before `preview_order`/`place_order`. The default Tiger
  profile has `require_live_money_guardrails_before_entry=true`. At that
  boundary, Tiger reconciliation confirmed positions/open orders but did not
  include balance/accounting/daily-PnL evidence, so an armed Tiger network order
  failed closed at `BLOCKED_MONEY_GUARDRAIL_UNKNOWN` instead of assuming zero
  loss.
- M11 added `TigerOpenApiAccountSync` and
  `pipelines.tiger_openapi_account_sync`, a read-only account/balance evidence
  surface backed by TradeClient `get_prime_assets`. It writes
  `outputs/tiger_account_sync/`, normalizes account observation, balance,
  available funds, realized/unrealized PnL, and an exact UTC trading-day window.
- The Tiger paper adapter now refreshes account sync after flat reconciliation
  and merges it into the guardrail reconciliation before `preview_order` or
  `place_order`. Missing account sync still fails closed; successful account
  sync only resolves the daily-loss evidence gap and does not bypass notional or
  daily-trade limits.
- Real read-only M11 smoke on 2026-07-05 returned
  `sync_status=synced`, `segment_key=C`, `balance_present=true`,
  `accounting_observed=true`, and zero realized/unrealized PnL. No Tiger order,
  cancel, close, or preview call was made.
- M12 added a Tiger same-path protection invariant. The checked-in
  `tiger_openapi_paper` profile now has
  `require_attached_protection_before_entry=true`; the adapter blocks before
  opening Tiger TradeClient unless a network-capable entry is a LIMIT order with
  both `stop_loss` and `targets[0]`. Blocked failures write
  `outputs/tiger_order_requests/` with `network_order_created=false`. Low-level
  fake-client drills can explicitly disable the requirement, but the
  operational default remains fail-closed.
- M13 added `TigerOpenApiPaperOrderDrill` and
  `pipelines.tiger_openapi_paper_order_drill`, a local fake-client paper order
  drill. It writes `outputs/tiger_paper_order_drill/` and isolates per-scenario
  runtime artifacts under `outputs/tiger_paper_order_drill_runtime/<run_id>/`.
  The 2026-07-05 `m13-local-fake` run passed with
  `real_tiger_network_call_attempted=false`: the default MGC one-contract path
  blocked on notional before fake preview/place, while a small synthetic-notional
  path reached fake preview/place with dated contract, flat reconciliation,
  account sync, money guardrails, attached TP/SL, and accepted lifecycle
  evidence. `TigerVenueStatus` and `dashboard-dualtrack-v5.html` now surface the
  drill status as artifact-only evidence.
- M14 added `TigerOpenApiPaperOrderReadiness` and
  `pipelines.tiger_openapi_paper_order_readiness`, an artifact-only gate for a
  future explicitly attended Tiger paper-order canary. It reads venue status,
  dated contract, flat reconciliation, account sync, order/fill sync,
  kill-switch, paper-order drill, and checked-in Tiger profile safety, then
  writes `outputs/tiger_paper_order_readiness/`. The 2026-07-05 run returned
  `ready_for_attended_paper_order=true` while preserving
  `can_submit_without_explicit_operator_authorization=false` and
  `real_tiger_network_call_attempted=false`. `TigerVenueStatus` and
  `dashboard-dualtrack-v5.html` now surface this as the `授权门` row.
- M15 added `TigerOpenApiPaperOrderCanary` and
  `pipelines.tiger_openapi_paper_order_canary`, a ticket-specific attended
  canary entry point. Default mode is artifact-only; submit requires
  `--submit-tiger-paper-canary`, `--confirm-tiger-paper-canary`, and the exact
  acknowledgement phrase
  `I_UNDERSTAND_TIGER_PAPER_TRADECLIENT_WILL_PREVIEW_AND_PLACE_AN_ORDER`.
  The service writes `outputs/tiger_paper_order_canary/` and cannot enter the
  adapter without explicit operator authorization.
- M15 also made `LiveMoneyGuardrails` candidate notional multiplier-aware for
  futures. With the Tiger profile, `MGC2608` at 4186 x 1 contract is evaluated
  as 41860 notional, not 4186. The 2026-07-05 read-only M15 check passed M14
  readiness and ticket-shape checks but stayed blocked before Tiger
  preview/place by the current 10 notional live-money limits.
- Local runtime note: official `tigeropen` 3.6.0 was installed into the user
  Python environment before read-only Tiger SDK calls were rerun.
- M16 added an attended paper canary risk package under
  `broker_profiles.tiger_openapi_paper.attended_paper_canary` and broker-level
  `live_money_guardrails` overrides. The package is scoped to explicit Tiger
  paper canary checks/submissions only and is used only when
  `--use-attended-canary-risk-limits` is present. Default MGC checks still use
  the global 10 notional cap and remain blocked.
- The M16 package allows one MGC paper canary up to 45000 notional and requires
  candidate stop-risk to be no more than 2.5% of observed paper equity. The
  2026-07-05 artifact-only run for `MGC2608` at 4186, stop 4170, target 4200
  returned `ready_for_operator_authorization` with `submit_requested=false` and
  `real_tiger_network_call_attempted=false`; candidate notional was 41860 and
  candidate stop-loss was 160 (2.14242579% of observed paper equity).
- M17 added `TigerOpenApiPaperOrderApproval` and
  `pipelines.tiger_openapi_paper_order_approval`, an artifact-only approval
  package for the final attended paper canary step. It regenerates the
  ticket-specific canary check, requires an owner-only Tiger properties file,
  writes `outputs/tiger_paper_order_approval/`, and records pre-submit refresh
  commands, the exact submit command, post-submit read-only checks, and the
  manual Tiger UI kill path. It never opens Tiger SDK clients and always records
  `submit_requested=false`,
  `can_submit_without_explicit_operator_authorization=false`, and
  `real_tiger_network_call_attempted=false`.
- The 2026-07-05 M17 run for `MGC2608` at 4186, stop 4170, target 4200, using
  the owner-only local Tiger properties file, returned
  `ready_for_operator_approval` with no blockers. This is still not order
  authorization; the generated submit command remains dormant until the
  operator explicitly chooses to run it.
- M18 made the M17 approval package visible in the dual-track Tiger venue read
  model and dashboard. `TigerVenueStatus` now reads
  `outputs/tiger_paper_order_approval/current.json` and returns a redacted
  `paper_order_approval` summary: status, ticket id, safety flags, blocker
  count, and canary risk numbers. It intentionally omits the generated
  `submit_command` and Tiger properties path. `dashboard-dualtrack-v5.html` adds
  an `授权包` row with no action button and no new Tiger POST endpoint. If an
  approval artifact ever records `submit_requested=true` or
  `real_tiger_network_call_attempted=true`, the venue read model moves to
  `manual_review`.
- M19 added `ConnectorCatalog` and `pipelines.connector_catalog` as the first
  productized connector registry surface. It reports supported connectors
  (Binance USD-M, Tiger OpenAPI, Yahoo Chart, gold-api.com), roles, ports,
  capabilities, credential variable names, and credential readiness booleans.
  It never returns secret values or the Tiger properties file path and never
  opens broker/feed clients. `GET /api/connectors/catalog` exposes the same
  read-only payload, while `dashboard-dualtrack-v5.html` shows a `接入目录` row
  with ready price-feed and broker counts. The 2026-07-05 artifact run with the
  owner-only Tiger properties file returned 4 connectors, 4 ready price feeds,
  and 1 ready broker capability.
- M20 added `ConnectorOnboardingDryRun` and `pipelines.connector_onboarding`.
  It validates a selected connector, requested roles, and credential readiness
  booleans without storing credentials, writing runtime config, or opening
  broker/feed clients. The dashboard exposes
  `POST /api/connectors/onboarding/dry-run` and a `接入 CONNECTOR` panel, but the
  payload only carries connector id, roles, and readiness confirmations. Raw
  secret fields such as `api_key`, `private_key`, `token`, `license`, or
  `tiger_id` are rejected before artifacts are written. The 2026-07-05 Tiger
  dry-run returned `ready_for_operator_setup` with `blockers=0` and no Tiger
  properties path in the API/artifact.
- M21 added `ConnectorActivationPlan` and
  `pipelines.connector_activation_plan`. It turns the selected connector roles
  into a preview-only `config_patch_preview` and writes
  `outputs/connector_activation_plan/current.json`, but does not mutate
  `configs/pipeline.yaml`, store credentials, open broker/feed clients, or add
  an order-control endpoint. The Tiger preview sets up the safe profile shape:
  enable `tiger_futures_feed`, select `broker.provider=tiger_openapi`, select
  `broker.profile=tiger_openapi_paper`, keep `broker.environment=paper`, and
  keep `broker.dry_run=true`. M22 extended the preview to also set
  `demo_trading.broker_profile=tiger_openapi_paper`, so the active machine-track
  runner would follow the same selected broker profile after an approved config
  write.
- M22 adapterized the active demo runner path. `MultiStrategyRunner` now resolves
  `demo_trading.broker_profile` before choosing a broker adapter: `binance_usdm`
  still uses `BinanceDemoBrokerAdapter`, while `tiger_openapi` paper profiles
  route through `TigerOpenApiPaperBrokerAdapter` via the existing broker adapter
  factory. Active Tiger demo reconciliation uses
  `TigerOpenApiPaperReconciliation`, which is read-only. Tiger execution-profile
  summaries expose only env variable names and guarded flags, not credential
  values or the Tiger properties path. Unsupported demo profiles route through a
  fail-closed adapter with `live_trading_enabled=false`.
- M23 moved the dual-track chart off browser-direct Binance websocket data.
  `GET /api/dualtrack/market/bars` now serves read-only chart bars from the
  local market DB, preferring Tiger/COMEX `MGCmain` 1m bars, then cached Binance
  `GOLD` 1m bars, then explicit display-only synthetic seed bars. The dashboard
  frontend reads this backend endpoint and no longer contains the Binance
  websocket URL. The endpoint is GET-only and not part of the dualtrack POST
  control surface.
- M24 completed the first real Tiger MGCmain import into the shared market DB.
  `pipelines.tiger_futures_feed --contract MGCmain --limit 20` returned
  `status=pass` and imported 20 `MGCmain` 1m bars from `tiger_openapi:COMEX`;
  latest timestamp `2026-07-03T16:59:00+00:00`, latest price `4186.9`. The
  dashboard market-bars builder now resolves to `source_mode=tiger_openapi`
  against the shared DB. The output receipt did not include the Tiger properties
  file path or credential values.
- M25 expanded the shared DB Tiger dashboard window with
  `pipelines.tiger_futures_feed --contract MGCmain --limit 500`. The shared DB
  now has 500 `MGCmain` 1m rows from `2026-07-03T08:40:00+00:00` to
  `2026-07-03T16:59:00+00:00`; dashboard `limit=96` reads 96 Tiger/COMEX bars
  from `2026-07-03T15:24:00+00:00` to `2026-07-03T16:59:00+00:00`. This run's
  quote permission metadata reported `aStockQuoteLv1` and
  `has_futures_realtime=false`, so market-hours realtime behavior still needs a
  separate validation pass.
- M26 added `pipelines.tiger_realtime_validation`, a read-only QuoteClient-only
  market-hours validation harness. It checks Tiger trading windows before
  polling bars; if COMEX is closed it writes `status=pending_market_open`
  instead of marking realtime validation as passed. The real 2026-07-05 run at
  `2026-07-05T14:49:22+00:00` returned `pending_market_open`; the next trading
  window in the artifact is `2026-07-05T22:00:00+00:00` to
  `2026-07-06T21:00:00+00:00`. The harness does not write the market DB, open
  TradeClient, or submit orders.
- M27 wired the realtime-validation artifact into `TigerVenueStatus` and the
  dual-track dashboard. The venue API now exposes a redacted
  `realtime_validation` summary, and the Tiger venue card shows `行情验证`.
  `pending_market_open` remains informational, while market-hours `warn` or
  `fail` validation degrades the Tiger venue and surfaces the validation message.
- M28 added an explicit market-hours gate to
  `pipelines.tiger_realtime_validation`. Default validation still writes the
  artifact and exits 0 for compatibility. When
  `--require-market-hours-pass` is present, the artifact includes
  `market_hours_gate`; the CLI exits 0 only for `status=pass`, exits 75 for
  `pending_market_open`, and exits 2 for `warn`, `fail`, or unknown statuses.
  This is an operational retry gate only. It remains QuoteClient-only, does not
  write market bars, and does not authorize Tiger paper order submission.
- Real pre-open M28 run at `2026-07-05T15:21:57+00:00` returned
  `status=pending_market_open`, `market_hours_gate.exit_code=75`, and
  `operator_action=rerun_after_next_trading_window`. `TigerVenueStatus` still
  reported the paper venue as `ready`, because pending market-open validation is
  informational rather than a venue failure.
- M29 surfaced `market_hours_gate` through `TigerVenueStatus` and
  `dashboard-dualtrack-v5.html`. The Tiger venue card now shows `行情门禁` in
  addition to `行情验证`, making exit 75 visible as a retry state. This is
  display-only and does not change status degradation, open Tiger SDK clients,
  or add order-control endpoints.
- M30 added `TigerPriceFeedReadiness` and
  `pipelines.tiger_price_feed_readiness`, an artifact-only readiness gate for
  promoting Tiger as a price feed. It reads the Tiger futures feed receipt and
  realtime-validation artifact, requires the 500-bar feed window and a
  market-hours realtime pass, and writes `outputs/tiger_price_feed_readiness/`.
  It does not open Tiger SDK clients, write market bars, or authorize broker
  orders. The real `2026-07-05T15:44:29+00:00` M30 run was correctly
  `blocked`: feed import passed with 500 rows, but realtime market-hours gate
  was still pending with exit code 75.
- M31 surfaced the M30 artifact through `TigerVenueStatus` and
  `dashboard-dualtrack-v5.html`. The Tiger venue card now shows `价格源门` with
  status and blocker count. This remains display-only: blocked price-feed
  readiness does not change the paper venue/order status and does not add SDK
  clients or order-control endpoints.
- M32 changed Tiger connector-catalog semantics from credential-only
  readiness to evidence-backed price-feed readiness. With owner-only Tiger
  credentials present, Tiger `broker_order` can still report `ready`, but Tiger
  `price_feed` reports `blocked` until `outputs/tiger_price_feed_readiness/`
  is `ready_for_price_feed`. `ConnectorOnboardingDryRun` and
  `ConnectorActivationPlan` now pass `output_root` into the catalog, so preview
  decisions use the same local artifact evidence as the dashboard. The real
  M32 catalog run returned `ready_price_feed_count=3`, `ready_broker_count=1`,
  Tiger `price_feed=blocked`, and Tiger `broker_order=ready`.

## Promotion Gate

- Lab promotion can only mark a strategy as paper-eligible after both
  walk-forward and holdout objective results pass, and after holdout consumption
  is recorded.
- The promotion flag is lab-scoped. It does not mutate `configs/strategy.yaml`
  and does not enable paper, demo, or live execution.
- `strategy_leaderboard` may display a read-only `lab_expectation` block so
  forward-paper rows can be compared against lab evidence.

## Tiger/MGC Dualtrack Accounting Note - 2026-07-06

- Added optional dualtrack venue accounting through `services.dualtrack_costs`.
- Existing bp/notional mode remains the default.
- Tiger/MGC mode uses `execution_cost_model.venue=tiger_mgc`,
  whole-contract `contracts_per_rung`, `contract_multiplier=10`, and
  `configs/risk_rules.yaml` fixed-per-contract side cost.
- Machine-grid fills and manual human fills can now carry `contracts`,
  `quantity`, side `cost`, and venue cost model metadata.
- Manual Tiger/MGC fills require explicit contract count; notional-only
  manual fills are rejected in venue mode.
- This is accounting readiness only. COMEX session masking and Tiger fill sync
  into the human ledger remain separate milestones before a real comparable
  Tiger/MGC dualtrack run.

## Tiger/MGC Dualtrack Session Note - 2026-07-06

- Added optional COMEX futures session masking to `services.dualtrack_clock`.
- The mask uses `America/New_York` via `zoneinfo` and models the regular
  Sunday 18:00 ET to Friday 17:00 ET session with the daily 17:00-18:00 ET
  break.
- `DualTrackCycleRunner.auto()` and `intraday_tick()` skip with
  `reason=market_closed` when a config enables the mask and COMEX is closed.
- Cycle IDs and the default fixed 12h dualtrack behavior remain unchanged unless
  `market_session.enabled=true`.
- `_cycle_bars()` and `previous_cycle_range()` filter out closed-session bars
  when the mask is enabled, so a bad break-hour bar cannot widen the grid or
  trigger paper fills.
- This regular-hours mask does not model exchange holidays or special early
  closes; exact holidays still need Tiger/CME session artifacts.

## Tiger Fill Import Note - 2026-07-06

- Added `services.dualtrack_tiger_human_sync.DualTrackTigerHumanSync`.
- Added CLI `python3 -m pipelines.dualtrack_tiger_human_sync --date YYYY-MM-DD --json`.
- The importer reads `outputs/tiger_order_sync/current.json` and writes human
  ledger fills/accounts plus `outputs/dualtrack/tiger_human_sync/` receipts.
- It does not open Tiger SDK clients and does not submit, cancel, modify, or
  close orders.
- Imported fills keep `source=tiger_openapi_order_sync`, `source_fill_id`,
  `external_order_id`, `symbol`, `root_symbol`, `contracts`, `quantity`, side
  `cost`, and the Tiger/MGC cost model.
- Re-running the importer is idempotent because `source_fill_id` is checked
  before appending to `dualtrack/fills/<cycle_id>_human.json`.
- If Tiger reports `commission`, that broker-reported value becomes the fill
  cost and the estimate is retained under `cost_model.estimated_cost`.
- The importer currently accepts MGC filled orders only. Additional futures
  roots need explicit multipliers and venue cost models before import.

## Runner Human-Fill Sync Note - 2026-07-06

- `DualTrackCycleRunner.close_cycle()` can now run human-fill sync before close
  when `human_fill_sync.enabled=true`.
- Default config keeps this disabled, so existing GOLD/Binance dualtrack flows
  are unchanged.
- With `provider=tiger_openapi`, close first imports Tiger filled orders through
  `DualTrackTigerHumanSync`, then runs machine recomputation and scoring.
- If `require_success_before_close=true` and import is blocked, close returns
  `status=skipped` with `reason=human_fill_sync_blocked`; it does not write
  closed attribution.
- `refresh_order_sync_before_import=true` runs read-only `TigerOpenApiOrderSync`
  before import. This is explicit because it may open Tiger TradeClient for
  read-only order/fill evidence.

## Tiger/MGC Dualtrack Profile Preview Note - 2026-07-06

- `ConnectorActivationPlan` now returns `dualtrack_profile_preview` for
  `tiger_openapi` activation requests.
- The preview bundles the dualtrack runtime changes required for a coherent
  Tiger/MGC paper operation: `MGCmain/1m`, COMEX session mask, Tiger/MGC fixed
  per-contract accounting, two one-contract machine rungs, and close-time
  human-fill sync when broker order capability is requested.
- `DualTrackCycleRunner` now resolves its default market symbol/timeframe from
  `dualtrack_config()["market_data"]`; the checked-in default remains
  `GOLD/1m`.
- The activation result still writes no config and opens no SDK client. The
  profile is a review artifact for a later explicit config-write milestone.
- The profile explicitly marks strategy-edge approval and Tiger price-feed
  acceptance as required gates, and keeps broker orders disabled from this
  surface.

## Dashboard Tiger/MGC Profile Preview Note - 2026-07-06

- The connector panel now displays `activation.dualtrack_profile_preview`.
- Added compact rows for the Tiger/MGC profile status, market layer, fee and
  contract quantum, human-fill sync, and strategy/order gates.
- The frontend still calls only the existing activation preview endpoint and
  does not auto-run activation, validation commands, config writes, or broker
  actions.
- Static dashboard tests assert the new rows exist and that `submit_command`
  remains absent from the page.

## Connector Config Apply Note - 2026-07-06

- Added `services.connector_config_apply.ConnectorConfigApply` and
  `pipelines.connector_config_apply`.
- Config apply is separate from activation preview. Dry-run is the default and
  writes only `outputs/connector_config_apply/current.json`.
- `write=true` requires the activation-plan acknowledgement plus
  `accept_warnings=true` when the activation plan carries warnings.
- Before any config write, the service copies targeted config files into
  `outputs/connector_config_apply/backups/<apply_id>/`.
- Rollback restores the recorded backup after the rollback acknowledgement.
- Added dashboard server API builders/routes for `/api/connectors/config/apply`
  and `/api/connectors/config/rollback`; these are connector config endpoints,
  not dualtrack order endpoints.
- The service never opens Tiger SDK clients and cannot submit, preview, cancel,
  or close broker orders.

## Dashboard Connector Config Receipt Note - 2026-07-06

- Added `ConnectorConfigApply.status()` and dashboard server
  `GET /api/connectors/config/status` for compact receipt state.
- The dualtrack connector panel now loads config status and shows apply,
  backup, and rollback receipt summaries.
- The panel's `写入预检` action posts only the activation plan to
  `/api/connectors/config/apply`, leaving apply in default dry-run mode.
- The dashboard intentionally does not send `write=true`, acknowledgement, or
  warning-acceptance payloads, and it does not expose rollback execution.
- This is an operator receipt surface only; it does not open Tiger SDK clients,
  fetch prices, write configs, or touch broker orders.

## Dualtrack Live Tick Schedule Note - 2026-07-06

- `DualTrackCycleRunner.live_tick()` syncs the current and next Obsidian human
  plans, then runs intraday machine sampling for the active cycle.
- `pipelines.dualtrack_cycle_runner --event live-tick` is now a generated local
  schedule job: `com.wendy.trading-orchestrator.dualtrack-live-tick`.
- The job runs every 300 seconds with `RunAtLoad=true`, separate from the
  existing 60-second `dualtrack-cycle --event auto` boundary orchestrator.
- `ScheduleStatus` and `CompletionAudit` now require the live-tick job before
  schedule health can be considered fully active.
- Schedule generation still does not install or load launchd jobs. Installation
  remains the explicit `ScheduleInstaller`/operator step.

## Schedule Current-Version Audit Note - 2026-07-06

- `ScheduleStatus` now separates physical install/load counts from
  current-generated-plist matching:
  - `installed_count`
  - `loaded_count`
  - `matching_generated_count`
  - `active_current_count`
- Added `stale_installed` when launchd jobs exist but one or more installed
  plists differ from `outputs/schedules/launch_agents`.
- Added `mismatched_jobs`, `missing_installed_jobs`, and `unloaded_jobs` so OPS
  can show the exact follow-up target.
- `pipelines.schedule_status` and `pipelines.schedule_install` print the new
  current-version counts.
- OPS dashboard now renders `current / active` and `stale installs` rows.

## Schedule Install Dry-Run Plan Note - 2026-07-06

- `ScheduleInstaller.plan()` now writes a dry-run receipt before any launchd
  install/restart action.
- `python3 -m pipelines.schedule_install --dry-run --date YYYY-MM-DD --json`
  emits per-job actions and planned commands without executing them.
- Receipt artifacts:
  - `outputs/schedules/install_plan_current.json`
  - `outputs/schedules/install_plan_<date>.json`
- OPS dashboard now loads `schedule_install_plan` and renders an `install plan`
  row with replacement and blocker counts.
- Safety boundary:
  - Dry-run does not copy plists, bootstrap, bootout, kickstart, open broker
    clients, or submit orders.
  - It does call `launchctl print` through schedule status and writes receipt
    artifacts.
- Gotcha:
  - `--no-restart-loaded` can stage the current plist while launchd keeps
    running the already loaded old definition until restarted.

## Schedule Attended Install Gate Note - 2026-07-06

- Non-dry-run schedule install now requires the exact acknowledgement:
  - `I_UNDERSTAND_SCHEDULE_INSTALL_WILL_REPLACE_OR_RESTART_LOCAL_LAUNCHD_JOBS`
- `ScheduleInstaller.install()` runs the dry-run plan first and blocks the
  apply if the plan is blocked or the acknowledgement is missing.
- A blocked install writes `outputs/schedules/install_current.json` as a receipt
  but does not create LaunchAgents, copy plists, bootout, bootstrap, or
  kickstart.
- Successful attended installs back up existing plists under
  `outputs/schedules/launch_agent_backups/<install_id>/` before replacing them.
- Dashboard state now exposes `schedule_install`, and OPS renders an
  `install apply` row for the latest install receipt.
- This acknowledgement is local schedule authorization only. It does not
  authorize broker routing, Tiger SDK order clients, or real-money orders.

## Schedule Rollback Gate Note - 2026-07-06

- `ScheduleInstaller.rollback_plan()` reads an install receipt and verifies
  whether each job has an existing backup plist before any restore action.
- `python3 -m pipelines.schedule_install --rollback --dry-run --date YYYY-MM-DD --json`
  writes rollback plan receipts:
  - `outputs/schedules/rollback_plan_current.json`
  - `outputs/schedules/rollback_plan_<date>.json`
- Non-dry-run rollback requires the exact acknowledgement:
  - `I_UNDERSTAND_SCHEDULE_ROLLBACK_WILL_RESTORE_LOCAL_LAUNCHD_JOBS_FROM_BACKUP`
- A blocked rollback writes `outputs/schedules/rollback_current.json` but does
  not copy plists, bootout, bootstrap, or kickstart.
- Successful rollback backs up current targets under
  `outputs/schedules/rollback_target_backups/<rollback_id>/` before restoring
  backup plists.
- Dashboard state now exposes `schedule_rollback_plan` and `schedule_rollback`;
  OPS renders `rollback plan` and `rollback apply` rows.

## Schedule Post-Install Verification Note - 2026-07-06

- Added `services.schedule_post_install_verifier.SchedulePostInstallVerifier`.
- CLI:
  - `python3 -m pipelines.schedule_post_install_verify --date YYYY-MM-DD --json`
- Receipt artifacts:
  - `outputs/schedules/post_install_verify_current.json`
  - `outputs/schedules/post_install_verify_<date>.json`
- The verifier checks:
  - all generated LaunchAgents are installed, current, and loaded;
  - latest install receipt is successful/no-op;
  - rollback plan can restore backed-up plists, when backups are expected;
  - runner heartbeat is fresh.
- Dashboard state now exposes `schedule_post_install_verify`, OPS renders a
  `post-install verify` row, and ops-status contract includes the verifier
  under the runner diagnostics surface.
- Safety boundary:
  - It calls `launchctl print` through schedule status and writes receipt
    artifacts.
  - It does not copy plists, bootout, bootstrap, kickstart, open broker clients,
    or submit orders.

## Schedule Takeover Package Note - 2026-07-06

- Added `services.schedule_takeover_package.ScheduleTakeoverPackage`.
- CLI:
  - `python3 -m pipelines.schedule_takeover_package --date YYYY-MM-DD --json`
- Receipt artifacts:
  - `outputs/schedules/takeover_package_current.json`
  - `outputs/schedules/takeover_package_<date>.json`
- The package summarizes:
  - install dry-run readiness and replacement count;
  - post-install verifier status;
  - rollback plan status;
  - exact attended install, post-install verify, rollback dry-run, and attended
    rollback commands.
- Dashboard state now exposes `schedule_takeover_package`, OPS renders a
  `takeover package` row, and ops-status contract includes it under runner
  diagnostics.
- Safety boundary:
  - It writes receipt artifacts and calls `launchctl print` through schedule
    status checks.
  - It does not copy plists, bootout, bootstrap, kickstart, open broker clients,
    or submit orders.

## Schedule Takeover Package Binding Note - 2026-07-06

- `ScheduleTakeoverPackage` now emits a stable `package_id`, `valid_for_minutes`,
  and `expires_at`.
- The attended install command now includes `--package-id <package_id>`.
- `ScheduleInstaller.install()` requires the exact package id for non-noop
  attended installs after the acknowledgement is present.
- CLI `python3 -m pipelines.schedule_takeover_package --check-current --date YYYY-MM-DD --json`
  now writes a no-launchd-mutation package usability check:
  - `outputs/schedules/takeover_package_check_current.json`
  - `outputs/schedules/takeover_package_check_<date>.json`
- Blockers:
  - `missing_package_id`
  - `missing_takeover_package`
  - `stale_or_mismatched_package`
  - `takeover_package_not_ready`
  - `expired_package`
- OPS shows the takeover package id short code next to the replacement count.
- OPS shows takeover package expiry and marks the package row `expired` after
  `expires_at`, even if the package status remains `ready_for_attended_install`.
- OPS shows `package check` with the latest usability check and next operator
  action.
- OPS treats `install_current.json status=blocked` with `package_gate.ok=false`
  as an authorization-gate receipt and renders `gate <blocker>` instead of a
  generic failed install detail.
- Test coverage now exercises missing package id, missing package artifact,
  mismatched id, expired package, and not-ready package states.
- Safety boundary:
  - Package-id failure writes a blocked install receipt only.
  - It does not copy plists, bootout, bootstrap, kickstart, open broker clients,
    or submit orders.

## Tiger/MGC Price Feed Acceptance Result - 2026-07-06

- Ran market-hours Tiger price-feed acceptance for `MGCmain`.
- Result:
  - `tiger_price_feed_acceptance.status=accepted`
  - `steps.realtime_validation.status=pass`
  - `steps.price_feed_readiness.status=ready_for_price_feed`
  - `steps.data_source_preflight.status=pass`
- Refreshed local Tiger MGC bars with `python3 -m pipelines.tiger_futures_feed --date 2026-07-06 --contract MGCmain --limit 500`.
- Local MGC coverage now reaches `2026-07-06T05:31:00+00:00`.
- Safety boundary:
  - Acceptance opened read-only QuoteClient during the polling run.
  - Feed import wrote local market bars only.
  - Neither path opened TradeClient, opened order clients, submitted orders, or authorized broker routing.

## Tiger/MGC Connector Activation Preview Result - 2026-07-06

- Ran Tiger connector activation preview for `price_feed` + `broker_order`.
- Result:
  - `outputs/connector_activation_plan/current.json status=preview_ready`
  - blockers: 0
  - warning: `paper_network_order_not_armed`
  - pipeline config changes previewed: 15
  - dualtrack profile changes previewed: 16
- Ran connector config apply dry-run.
- Result:
  - `outputs/connector_config_apply/current.json status=dry_run_ready`
  - mode: `dry_run`
  - total planned config changes: 31
  - blockers: 0
  - warnings: `paper_network_order_not_armed`, `dry_run`
- Safety boundary:
  - No runtime config files were written by the dry-run.
  - No network clients were opened.
  - No broker orders were submitted, cancelled, or closed.
- Remaining attended step:
  - Actual config write requires explicit config apply acknowledgement and warning acceptance.

## Tiger/MGC Config Apply Package Id - 2026-07-06

- Added a stable `config_apply_package.package_id` to the Tiger connector activation package.
- Package id is now a full-content digest:
  - pipeline config patch digest includes `op`, `path`, `current`, `planned`, `reason`, `risk`, and `writes_config`.
  - dualtrack config patch digest includes the same fields plus `config_file`.
  - `ConnectorConfigApply` recomputes the package id from the submitted activation plan before any non-dry-run write.
- Current no-write package evidence:
  - `outputs/connector_activation_plan/current.json config_apply_package.package_id=631b8ed6cb3310de`
  - `outputs/connector_config_apply/current.json status=dry_run_ready`
  - `outputs/connector_config_apply/current.json package_id=631b8ed6cb3310de`
- Non-dry-run config writes now require both:
  - exact acknowledgement: `I_UNDERSTAND_CONNECTOR_CONFIG_WRITE_IS_SEPARATE_AND_REVERSIBLE`
  - matching `--package-id`
- The generated attended command in the activation package now includes `--package-id`.
- Safety boundary:
  - Dry-run still does not write runtime config or create backups.
  - Missing or mismatched package id blocks before writing config.
  - A plan whose contents no longer match its package id blocks with `config_apply_package_id_stale`.
  - Package id does not authorize broker orders, paper TradeClient submission, launchd takeover, or real-money execution.

## Tiger/MGC Config Package UI Visibility - 2026-07-06

- Updated the dualtrack connector card to show:
  - `审批包ID` from `configApplyPackage.package_id`
  - `写入边界` from `configApplyPackage.config_write_boundary`
  - `预检包ID` from `connector_config_apply.latest_apply.package_id`
- The UI remains display-only for config write:
  - no `write:true`
  - no `accept_warnings`
  - no acknowledgement string
  - no rollback control
  - no broker order control
- Safety coverage:
  - `tests/test_dashboard_dualtrack_static.py` checks package id visibility and continues to reject write-mode payload markers.
  - `tests/test_connector_config_apply.py` checks config status summarizes the package id after an applied write in temp files.

## Tiger/MGC Config Package Check Current - 2026-07-06

- Added a read-only `connector_config_apply check` command.
- Current no-write check evidence:
  - `outputs/connector_config_apply/check_current.json status=ready_for_attended_config_write`
  - `outputs/connector_config_apply/check_current.json package_id=631b8ed6cb3310de`
  - `usable_for_attended_config_write=true`
  - `operator_next_action.action=authorize_attended_config_write_with_warning_acceptance`
  - `operator_next_action.requires_accept_warnings=true`
  - `post_apply_validation_commands` contains 4 commands.
  - `rollback_boundary.required=true`
- The check validates:
  - activation plan schema and non-blocked status.
  - package id presence.
  - recomputed package id matches the activation plan contents.
  - latest dry-run receipt exists.
  - latest dry-run receipt is `dry_run_ready`.
  - latest dry-run package id matches the activation package.
  - latest dry-run safety still reports `writes_runtime_config=false`.
- The connector config status response now includes `latest_check`.
- `latest_check` summarizes `post_apply_validation_count` and `rollback_required`.
- The dualtrack connector card now renders:
  - `包检查`
  - `授权下一步`
  - `写后验收`
  - `回滚边界`
- Safety boundary:
  - Check is read-only for runtime config.
  - Check opens no network clients and submits no orders.
  - Check readiness is not config write, broker switch, order permission, launchd takeover, or strategy promotion.

## Tiger/MGC Operator Handoff Artifact - 2026-07-06

- Added a read-only `connector_config_apply handoff` command.
- Current handoff artifacts:
  - `outputs/connector_config_apply/handoff_current.json`
  - `outputs/connector_config_apply/handoff_current.md`
- Current handoff status:
  - `status=ready_for_operator_review`
  - `package_id=631b8ed6cb3310de`
  - `current_runtime.broker_provider=binance_usdm`
  - `current_runtime.dualtrack_symbol=GOLD`
  - `post_apply_validation_commands` count: 4
  - `rollback_boundary.required=true`
- Handoff includes:
  - exact attended config apply command.
  - current runtime state.
  - post-apply validation commands.
  - rollback boundary.
  - explicit not-authorized list.
- Safety boundary:
  - Handoff writes only local handoff artifacts.
  - Handoff does not write runtime config, open network clients, submit orders, switch broker routing, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## Tiger/MGC Handoff Evidence Chain - 2026-07-06

- Added evidence fingerprints to the read-only handoff artifact:
  - activation plan current artifact path/existence/SHA256 plus in-memory payload SHA256.
  - latest config-apply dry-run artifact path/existence/SHA256.
  - latest persisted config package check path/existence/SHA256.
  - computed package check payload SHA256.
  - current `configs/pipeline.yaml` and `configs/dualtrack.yaml` path/existence/SHA256.
- Added `latest_handoff` to connector config status:
  - handoff status/id/package.
  - current broker provider and dualtrack symbol seen by the handoff.
  - evidence count.
  - post-apply validation count.
  - safety flags for config write/network/orders.
- Updated the dualtrack connector card to show:
  - `交接单`
  - `运行现状`
- Safety boundary:
  - The dashboard still does not expose write-mode payloads, warning acceptance, acknowledgement strings, rollback controls, Tiger clients, or broker order controls.
  - Handoff evidence proves provenance only; it is not approval to write configs or trade.

## Tiger/MGC Handoff-Bound Config Write - 2026-07-06

- Non-dry-run connector config writes now require the latest handoff to be current.
- Write-mode validation now blocks when:
  - no handoff has been generated after dry-run/check.
  - the handoff is not `ready_for_operator_review`.
  - the handoff package id differs from the activation package id.
  - the handoff safety no longer proves no config write/network/order action.
  - the handoff activation payload SHA does not match the submitted plan.
  - activation/dry-run/check artifacts no longer match their handoff SHA256.
  - current `configs/pipeline.yaml` or `configs/dualtrack.yaml` no longer match their handoff SHA256.
- Tests added:
  - missing handoff blocks write-mode apply before config changes.
  - stale current config fingerprint blocks write-mode apply before config changes.
  - positive write/rollback tests now follow the intended sequence: dry-run -> check -> handoff -> attended write.
- Current refreshed local handoff:
  - `outputs/connector_config_apply/handoff_current.json`
  - `outputs/connector_config_apply/handoff_current.md`
  - `status=ready_for_operator_review`
  - `package_id=631b8ed6cb3310de`
  - runtime still `binance_usdm` / `GOLD`
- Safety boundary:
  - This guard protects a future attended write; it does not execute a write in the current run.
  - Current runtime config remains unswitched.

## Tiger/MGC Check Write Guard Visibility - 2026-07-06

- `connector_config_apply check` now distinguishes:
  - package/dry-run readiness.
  - handoff-bound write preflight readiness.
- `check` can now return:
  - `ready_for_handoff`: package/dry-run are consistent, but a current handoff is missing or stale.
  - `ready_for_attended_config_write`: package/dry-run and handoff-bound write preflight are both current.
  - `blocked`: package/dry-run validation failed.
- The check receipt now includes:
  - `write_preflight.requires_current_handoff`
  - `write_preflight.status`
  - `write_preflight.blockers`
  - `write_preflight.latest_handoff`
- `latest_check` status summaries now include:
  - `write_preflight_status`
  - `write_preflight_blocker_count`
- Updated the dualtrack connector card to render `写入守卫`.
- Hard write gates validate activation plan, latest dry-run, and current config fingerprints from the handoff.
- `persisted_config_check` remains in the handoff evidence chain but is not a hard write gate because read-only checks update that file.
- Current refreshed local evidence:
  - handoff `handoff_20260706T062840024487Z`
  - package `631b8ed6cb3310de`
  - check `status=ready_for_attended_config_write`
  - check `write_preflight.status=ready`
- Safety boundary:
  - These checks are read-only.
  - They do not write runtime config, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategies, or authorize real-money execution.

## Tiger/MGC Sandbox Config Rehearsal - 2026-07-06

- Added `connector_config_apply rehearse`.
- The rehearsal:
  - copies current `configs/pipeline.yaml` and `configs/dualtrack.yaml` into `outputs/connector_config_apply/rehearsals/<rehearsal_id>/configs/`.
  - seeds the activation plan into the rehearsal output root.
  - runs dry-run.
  - runs check and expects `ready_for_handoff`.
  - generates sandbox handoff.
  - runs check and expects `ready_for_attended_config_write`.
  - applies the config write against sandbox config copies.
  - rolls back sandbox config copies from the sandbox apply backup.
  - compares real runtime config SHA256 before/after and records `runtime_config.unchanged`.
- Current rehearsal:
  - `rehearsal_id=rehearsal_20260706T063827387627Z`
  - `package_id=631b8ed6cb3310de`
  - `status=passed`
  - `runtime_config.unchanged=true`
  - sandbox apply status `applied`
  - sandbox rollback status `rolled_back`
- Connector config status now includes `latest_rehearsal`.
- The dualtrack connector card now renders `切换演练`.
- Safety boundary:
  - Rehearsal writes only sandbox config copies and local rehearsal artifacts.
  - It does not write runtime config, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategies, or authorize real-money execution.

## Tiger/MGC Rehearsal-Bound Config Write - 2026-07-06

- Non-dry-run connector config writes now require both:
  - latest current handoff.
  - latest passed sandbox rehearsal.
- `connector_config_apply check` now has a three-step progression:
  - `ready_for_handoff`: dry-run/package are current, handoff is missing or stale.
  - `ready_for_rehearsal`: handoff is current, rehearsal is missing or stale.
  - `ready_for_attended_config_write`: handoff and rehearsal are both current.
- Write-mode validation now blocks when:
  - no rehearsal exists.
  - rehearsal did not pass.
  - rehearsal package id differs from the activation package.
  - rehearsal safety does not prove sandbox-only/no-network/no-order behavior.
  - rehearsal did not prove runtime config stayed unchanged.
  - current runtime config no longer matches the rehearsal evidence.
  - any rehearsal step failed to hit the expected status.
- Current refreshed local evidence:
  - handoff `handoff_20260706T064853774956Z`
  - rehearsal `rehearsal_20260706T064853829778Z`
  - package `631b8ed6cb3310de`
  - check `status=ready_for_attended_config_write`
  - check `write_preflight.status=ready`
- Safety boundary:
  - This guard still does not write runtime config by itself.
  - Rehearsal remains sandbox-only.
  - Tiger clients and order submission remain closed.

## Tiger/MGC Operator Authorization Package - 2026-07-06

- Added `connector_config_apply authorization`.
- The authorization package:
  - recomputes the current config write check.
  - requires a current handoff and passed current rehearsal.
  - records current runtime config summary.
  - records the attended apply command, post-apply validation commands, and rollback boundary in local artifacts.
  - writes `outputs/connector_config_apply/authorization_current.json`.
  - writes `outputs/connector_config_apply/authorization_current.md`.
  - appends `outputs/connector_config_apply/authorization_history.json`.
- Connector config status now includes `latest_authorization`.
- The dualtrack connector card renders only authorization status and blocker count.
- Current safety boundary:
  - The package is read-only.
  - It does not write runtime config, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## Tiger/MGC Final Readiness Audit - 2026-07-06

- Added the current operator audit artifact:
  - `outputs/connector_config_apply/final_readiness_audit_current.md`
- The audit classifies the current state as:
  - `pre-switch authorization-ready`
  - active runtime still not switched.
- Evidence summarized:
  - Tiger/MGC price-feed acceptance is `accepted`.
  - realtime validation is `pass`.
  - shared market DB contains `MGCmain/1m/tiger_openapi:COMEX` rows through `2026-07-06T05:31:00+00:00`.
  - config authorization package is `ready_for_operator_authorization`.
  - runtime config remains `binance_usdm` / `GOLD`.
  - strategy edge and broker-order submission remain unauthorized.
- Safety boundary:
  - This audit is documentation only.
  - It does not write runtime config, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## Tiger/MGC Repeatable Go/No-Go Readiness Audit - 2026-07-06

- Added `connector_config_apply readiness-audit`.
- The command reads:
  - Tiger price-feed acceptance.
  - Tiger price-feed readiness.
  - Tiger realtime validation.
  - Tiger futures feed receipt.
  - shared market DB coverage for `MGCmain/1m/tiger_openapi:COMEX`.
  - connector config authorization.
  - activation profile strategy/order gates.
  - current runtime config.
- Current generated result:
  - `status=go_for_attended_config_switch`.
  - `current_stage=pre_switch_authorization_ready`.
  - `can_switch_config_with_operator_authorization=true`.
  - `can_trade_machine_track=false`.
  - `can_submit_tiger_orders=false`.
  - `blocker_count=0`.
- Artifacts:
  - `outputs/connector_config_apply/final_readiness_audit_current.json`.
  - `outputs/connector_config_apply/final_readiness_audit_current.md`.
  - `outputs/connector_config_apply/final_readiness_audit_history.json`.
- Connector config status now includes `latest_readiness_audit`.
- The dualtrack connector card renders `最终审计`.
- Safety boundary:
  - The audit reads local artifacts and SQLite only.
  - It does not write runtime config, write market DB, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## Tiger/MGC Post-Switch Validation - 2026-07-06

- Added `connector_config_apply post-switch-validate`.
- The validator reads:
  - active runtime config.
  - latest applied config receipt.
  - latest authorization package.
  - latest final readiness audit.
  - Tiger price-feed acceptance.
  - Tiger realtime validation.
  - shared market DB coverage for `MGCmain/1m/tiger_openapi:COMEX`.
- A valid post-switch state requires:
  - latest config apply status `applied`.
  - matching package id.
  - rollback backup available.
  - `broker.provider=tiger_openapi`.
  - `broker.dry_run=true`.
  - `market_data.symbol=MGCmain`.
  - `market_data.provider=tiger_openapi:COMEX`.
  - COMEX session mask enabled.
  - Tiger/MGC fixed-dollar integer-contract cost model.
  - human fill sync enabled.
  - broker order submission still closed.
- Current generated result before attended config write:
  - `status=blocked`.
  - expected blockers include `config_apply_not_applied` and runtime mismatch checks because active config remains Binance/GOLD.
- Artifacts:
  - `outputs/connector_config_apply/post_switch_validation_current.json`.
  - `outputs/connector_config_apply/post_switch_validation_current.md`.
  - `outputs/connector_config_apply/post_switch_validation_history.json`.
- Connector config status now includes `latest_post_switch_validation`.
- The dualtrack connector card renders `切后验收`.
- Safety boundary:
  - The validator is read-only.
  - It does not write runtime config, write market DB, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## Tiger/MGC Rehearsal Includes Post-Switch Validation - 2026-07-06

- Extended `connector_config_apply rehearse`.
- Rehearsal steps now include:
  - `dry_run`
  - `pre_handoff_check`
  - `handoff`
  - `write_check`
  - `authorization`
  - `readiness_audit`
  - `sandbox_apply`
  - `post_switch_validation`
  - `sandbox_rollback`
- A passed rehearsal now requires:
  - `authorization.status=ready_for_operator_authorization`
  - `readiness_audit.status=go_for_attended_config_switch`
  - `post_switch_validation.status=validated_post_switch`
  - real runtime config unchanged before/after rehearsal.
- Rehearsal seeds sandbox support by copying local read-only price-feed artifacts and using SQLite backup to copy market DB coverage into the sandbox.
- Current generated result:
  - `status=passed`
  - `package_id=631b8ed6cb3310de`
  - `steps.post_switch_validation.status=validated_post_switch`
  - `runtime_config.unchanged=true`
- Safety boundary:
  - Rehearsal writes sandbox config copies and local rehearsal artifacts only.
  - It does not write runtime config, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, take over launchd, promote strategy, or authorize real-money execution.

## Tiger/MGC Dualtrack Same-Venue Scoring Guard - 2026-07-06

- Added a close-cycle integration guard for the future Tiger/MGC dualtrack profile.
- Test:
  - `tests/test_dualtrack_dt8_cycle_runner.py::test_mgc_dualtrack_close_scores_machine_and_human_with_same_tiger_contract_cost_model`.
- The test injects:
  - `market_data.symbol=MGCmain`
  - `market_data.provider=tiger_openapi:COMEX`
  - `market_session.venue=comex_futures`
  - `execution_cost_model.venue=tiger_mgc`
  - `execution_cost_model.quantity_mode=integer_contracts`
  - `execution_cost_model.contract_multiplier=10`
  - `human_fill_sync.provider=tiger_openapi`
- The close-cycle path now has regression coverage proving:
  - machine fills and imported human fills both carry `cost_model.venue=tiger_mgc`;
  - both tracks use `quantity_mode=integer_contracts`;
  - each MGC fill uses one whole contract in the guarded scenario;
  - side notional equals `price * 10`;
  - side cost is $2.70 per MGC contract;
  - attribution and daily ledger realized PnL match the same filled-order evidence.
- Safety boundary:
  - This is test-only coverage for the Tiger/MGC profile path.
  - It does not write active config, switch broker routing, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, promote strategy, or authorize real-money execution.

## Tiger/MGC Operator Stage Status - 2026-07-06

- Added a read-only connector status CLI:
  - `python3 -m pipelines.connector_config_apply status --json`
  - `--output-root` can point the status command at sandbox/test outputs.
- Status now includes:
  - `current_runtime`
  - `price_feed`
  - `operator_stage`
  - `latest_price_feed_refresh_runbook`
- Status separates price-feed pass state from evidence freshness:
  - `operator_stage.price_feed_status_ready`
  - `operator_stage.price_feed_evidence_fresh`
  - `operator_stage.price_feed_ready`
- Price-feed evidence used for the attended-switch stage expires after 900 seconds.
- Current real local stage after the original acceptance receipt aged out:
  - `operator_stage.stage=price_feed_evidence_stale_refresh_acceptance`
  - `operator_stage.price_feed_status_ready=true`
  - `operator_stage.price_feed_evidence_fresh=false`
  - `operator_stage.price_feed_ready=false`
  - `operator_stage.can_switch_config_with_operator_authorization=false`
  - `operator_stage.next_action=rerun_tiger_price_feed_acceptance_then_readiness_audit`
- Current generated refresh commands:
  - `python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-06 --contract MGCmain --poll-seconds 75 --plan-only --json`
  - `python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-06 --contract MGCmain --poll-seconds 75 --json`
  - `python3 -m pipelines.connector_config_apply readiness-audit --plan outputs/connector_activation_plan/current.json --json`
  - `python3 -m pipelines.connector_config_apply status --json`
- Refresh command safety:
  - the plan-only command writes `outputs/tiger_price_feed_acceptance_plan/` and opens no Tiger SDK client;
  - the acceptance command opens a read-only QuoteClient if run;
  - none of the generated commands opens TradeClient;
  - none submits/cancels/closes orders;
  - none writes runtime config.
- `pipelines.tiger_price_feed_acceptance --plan-only` returns `schema_version=tiger-price-feed-acceptance-plan-v1`, `status=plan_ready`, and a command sequence for acceptance -> final readiness audit -> status. It does not refresh realtime validation, readiness, connector catalog, data-source preflight, acceptance, or market DB artifacts.
- Refreshed real readiness audit:
  - `status=blocked`
  - blocker: `tiger_price_feed_evidence_stale`
  - `can_switch_config_with_operator_authorization=false`
  - latest local MGC market coverage remains 952 rows through `2026-07-06T05:31:00+00:00`
- Current active runtime remains unswitched:
  - `current_runtime.broker_provider=binance_usdm`
  - `current_runtime.dualtrack_symbol=GOLD`
  - `current_runtime.execution_cost_venue=""`
- The status command lets operators answer "what stage are we in?" from one read-only receipt instead of manually comparing activation, rehearsal, authorization, audit, post-switch, and price-feed artifacts.
- `readiness-audit` now blocks stale price-feed evidence with `tiger_price_feed_evidence_stale`, so a stale acceptance receipt cannot be refreshed into a Go/No-Go result without rerunning acceptance.
- Safety boundary:
  - It does not write runtime config, refresh market data, open Tiger clients, submit/cancel/close orders, arm Tiger paper orders, promote strategy, or authorize real-money execution.

## Tiger/MGC Operator Stage Frontend Rows - 2026-07-06

- The dualtrack connector card now renders `operator_stage` from `/api/connectors/config/status`.
- New display-only rows:
  - `接入阶段`: current synthesized stage plus whether runtime has switched.
  - `阶段下一步`: next required operator action.
  - `刷新预览`: whether the plan-only refresh preview opens Tiger clients and whether it writes a plan artifact.
  - `真实验收`: whether the actual acceptance command opens QuoteClient and whether it writes runtime config.
  - `交易权限`: whether machine trading and Tiger order submission remain closed.
- This keeps the browser aligned with the CLI status receipt: plan-only first, explicit price-feed acceptance second, readiness audit third, status fourth.
- Safety boundary:
  - The browser still does not run plan-only, run Tiger price-feed acceptance, open Tiger clients, write runtime config, accept warnings, expose acknowledgement strings, roll back config, take over schedules, submit/cancel/close orders, or authorize broker routing.

## Tiger/MGC Price-Feed Refresh Runbook - 2026-07-06

- Added a read-only operator package for the stale price-feed evidence state:
  - `python3 -m pipelines.connector_config_apply price-feed-refresh-runbook --json`
- Current local artifact:
  - `outputs/connector_config_apply/price_feed_refresh_runbook_current.json`
  - `outputs/connector_config_apply/price_feed_refresh_runbook_current.md`
  - `outputs/connector_config_apply/status_current.json`
- Current real local result:
  - `status=ready_for_operator_refresh`
  - `operator_stage.stage=price_feed_evidence_stale_refresh_acceptance`
  - `runtime_switched_to_tiger_mgc=false`
  - `can_switch_config_with_operator_authorization=false`
  - `can_trade_machine_track=false`
  - `can_submit_tiger_orders=false`
- Command sequence captured in the runbook:
  - plan-only Tiger price-feed acceptance preview;
  - real Tiger price-feed acceptance refresh;
  - final readiness audit;
  - connector switch status.
- The runbook now writes the exact `connector-config-status-v1` snapshot it was built from to `status_current.json`, so the referenced status evidence exists next to the runbook.
- `connector_config_apply status` now summarizes the latest refresh runbook without generating a new one:
  - `status=ready_for_operator_refresh`
  - `operator_stage=price_feed_evidence_stale_refresh_acceptance`
  - `command_count=4`
  - `status_snapshot_exists=true`
- The dualtrack connector card renders two display-only rows from that summary:
  - `刷新操作包`
  - `操作包证据`
- Safety boundary:
  - Runbook generation writes local runbook artifacts only.
  - It does not open QuoteClient, open TradeClient, run Tiger price-feed acceptance, write market DB bars, write runtime config, submit/cancel/close orders, take over schedules, promote strategy, or authorize broker routing.
  - Status/dashboard reads do not create a new runbook, run Tiger acceptance, open Tiger clients, write config, or submit orders.

## Tiger/MGC Refresh Runbook Read-Only API - 2026-07-06

- Added a display-only API for the existing market-open refresh package:
  - `GET /api/connectors/config/price-feed-refresh-runbook`
- Source artifact:
  - `outputs/connector_config_apply/price_feed_refresh_runbook_current.json`
- The response includes:
  - the latest runbook receipt when present;
  - `served_from` with the artifact path;
  - `endpoint_safety` proving the read path does not generate or execute anything;
  - `status=missing` and an empty command sequence when the artifact does not exist.
- The dualtrack connector card now prefers this endpoint over the summary embedded in `/api/connectors/config/status`, while keeping the display as status plus step count only.
- Safety boundary:
  - The endpoint does not call runbook generation.
  - It does not open QuoteClient, open TradeClient, run Tiger price-feed acceptance, refresh market bars, write runtime config, submit/cancel/close orders, expose credentials, take over schedules, promote strategy, or authorize broker routing.

## Tiger/MGC Refresh Runbook Step Display - 2026-07-06

- The dualtrack connector card now renders the existing runbook command sequence as display-only rows:
  - `刷新步骤 1`: preview the acceptance plan;
  - `刷新步骤 2`: run the read-only price-feed acceptance;
  - `刷新步骤 3`: refresh the final readiness audit;
  - `刷新步骤 4`: show the connector switch status.
- Each row includes safety markers for:
  - QuoteClient usage;
  - TradeClient usage;
  - order submission;
  - runtime config writes.
- The dashboard does not render the raw command as a button or executable control.
- The refresh-runbook API now overwrites `endpoint_safety` from server code so stale artifact metadata cannot describe the HTTP endpoint incorrectly.
- Safety boundary:
  - Step rows are labels, not controls.
  - They do not run plan-only preview, run Tiger acceptance, run readiness audit, open Tiger clients, write configs, submit/cancel/close orders, expose credentials, take over schedules, promote strategy, or authorize broker routing.

## Tiger/MGC Refresh Window Gate - 2026-07-06

- The price-feed refresh runbook now includes a local COMEX session gate:
  - `refresh_window_gate.status=wait_for_comex_open` during daily break/weekend close;
  - `refresh_window_gate.status=ready_to_run_acceptance_now` while COMEX is open;
  - `refresh_window_gate.can_preview_now=true` when a runbook exists;
  - `refresh_window_gate.can_run_quote_client_step=true` only when the runbook has an acceptance step and COMEX is locally open.
- CLI support:
  - `python3 -m pipelines.connector_config_apply price-feed-refresh-runbook --as-of <ISO8601> --json`
  - `--as-of` is for deterministic previews/tests; default uses current UTC time.
- Status/dashboard surface:
  - `latest_price_feed_refresh_runbook.refresh_window_status`
  - `latest_price_feed_refresh_runbook.refresh_window_next_open`
  - `latest_price_feed_refresh_runbook.refresh_window_can_run_quote_client_step`
  - dashboard row `验收窗口`
- Current regenerated local artifact:
  - `outputs/connector_config_apply/price_feed_refresh_runbook_current.json`
  - `runbook_id=price_feed_refresh_runbook_20260706T091234720737Z`
  - `refresh_window_gate.status=ready_to_run_acceptance_now`
  - `refresh_window_gate.can_run_quote_client_step=true`
  - `operator_stage.stage=price_feed_evidence_stale_refresh_acceptance`
  - `operator_stage.can_switch_config_with_operator_authorization=false`
- Safety boundary:
  - This gate uses the local COMEX session calendar only.
  - It does not open QuoteClient, open TradeClient, run Tiger acceptance, import bars, write configs, submit/cancel/close orders, promote strategy, or authorize broker routing.
  - `ready_to_run_acceptance_now` means "safe moment to run the explicit read-only acceptance command"; it is not an acceptance pass.

## Tiger/MGC Real Price-Feed Acceptance Refresh - 2026-07-06

- Ran the explicit read-only market-hours acceptance command:
  - `python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-06 --contract MGCmain --poll-seconds 75 --json`
- Result:
  - `status=accepted`
  - `ready_for_price_feed=true`
  - `exit_code=0`
  - realtime validation `status=pass`
  - `bar_advanced=true`
  - `fresh=true`
  - `opens_quote_client=true`
  - `opens_trade_client=false`
  - `submits_orders=false`
- Then refreshed local MGC bars for dashboard/data-source preflight:
  - `python3 -m pipelines.tiger_futures_feed --date 2026-07-06 --contract MGCmain --limit 500`
  - imported 500 bars
  - latest local `MGCmain/1m/tiger_openapi:COMEX` timestamp: `2026-07-06T09:16:00+00:00`
  - coverage rows: 1177
- Then refreshed scoped data-source preflight:
  - `python3 -m pipelines.data_source_preflight --date 2026-07-06 --symbol MGCmain --timeframe 1m`
  - `status=pass`
  - `ready_for_paper=true`
  - `ready_for_live=true`
  - `live_data_mode=execution_venue`
- Then refreshed acceptance aggregation without opening QuoteClient:
  - `python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-06 --contract MGCmain --skip-realtime-run --json`
  - `status=accepted`
  - data-source preflight step `status=pass`
  - safety `opens_quote_client=false`
- Final readiness audit:
  - `status=go_for_attended_config_switch`
  - `can_switch_config_with_operator_authorization=true`
  - `can_trade_machine_track=false`
  - `can_submit_tiger_orders=false`
- Current connector stage:
  - `operator_stage.stage=ready_for_attended_config_switch`
  - `price_feed_ready=true`
  - `readiness_audit_fresh=true`
  - `runtime_switched_to_tiger_mgc=false`
  - `can_trade_machine_track=false`
  - `can_submit_tiger_orders=false`
- Current refresh runbook:
  - `status=not_required`
  - `command_count=0`
  - `refresh_window_gate.status=not_required`
- Safety boundary:
  - The real acceptance command opened QuoteClient only.
  - No TradeClient/order client was opened.
  - No order was submitted, cancelled, or closed.
  - Runtime config remains unchanged and still active on `binance_usdm` / `GOLD`.

## Tiger/MGC Attended Switch Review Surface - 2026-07-06

- Added a read-only switch-review summary under:
  - `operator_stage.attended_switch_review`
- The summary exposes:
  - `status=ready_for_operator_review` when the current switch package is ready for human review;
  - `package_id`;
  - authorization and audit ids;
  - whether the switch requires an explicit operator command;
  - warning/package/rollback/post-apply validation requirements;
  - the fact that status/dashboard reads do not write runtime config, open clients, or submit orders;
  - machine track and Tiger order gates after switch remain closed.
- The dualtrack connector card now renders:
  - `人工切换`
  - `切换包ID`
  - `切后验收`
  - `切后交易`
- Added a redacted dashboard/API endpoint:
  - `GET /api/connectors/config/attended-switch-review`
  - `schema_version=connector-attended-switch-review-api-v1`
  - includes package id, authorization/audit ids, current runtime, after-switch gates, artifact paths, and endpoint safety;
  - omits the raw acknowledgement string and the attended apply command.
- The dashboard prefers this endpoint and falls back to `operator_stage.attended_switch_review`.
- The endpoint now also exposes top-level current-state facts:
  - `runtime_switched_to_tiger_mgc`
  - `current_broker_provider`
  - `current_dualtrack_symbol`
  - `can_trade_machine_track`
  - `can_submit_tiger_orders`
- The dashboard prefers those top-level facts from the review endpoint when rendering `接入阶段` and `交易权限`, so a ready switch package cannot be mistaken for an already switched or order-enabled runtime.
- Safety boundary:
  - This is a read model only.
  - It does not expose the raw acknowledgement string in the dashboard.
  - It does not run config apply, open Tiger clients, submit/cancel/close orders, promote strategy, or bypass post-apply validation.

## Tiger Paper Order Readiness Evidence Freshness - 2026-07-06

- Tightened `TigerOpenApiPaperOrderReadiness` so an attended paper-order canary can pass only when these evidence artifacts are current for the requested `run_date`:
  - dated contract status;
  - reconciliation flat check;
  - account sync;
  - order/fill sync;
  - kill-switch evidence;
  - local fake-client paper-order drill.
- This keeps `TigerVenueStatus` as a latest-known read model while making order readiness stricter: prior-day flat/order/account evidence can still be displayed, but it cannot authorize the next attended order step.
- The readiness receipt now enriches stale evidence with:
  - `required_run_date`;
  - `current_for_run_date=false`.
- `TigerVenueStatus` now separates:
  - `can_open_new_orders`: latest reconciliation says flat;
  - `can_enter_attended_paper_order`: the stricter attended paper-order readiness path is currently passable and still requires explicit operator authorization.
- The dashboard `新单门` row uses `can_enter_attended_paper_order` and shows reconciliation flatness only as context.
- `TigerVenueStatus.paper_order_readiness.operator_next_action` now summarizes:
  - whether the next action is `refresh_stale_evidence`, `resolve_blockers`, or `ready_for_operator_review`;
  - stale evidence count and blocker names;
  - refresh step labels;
  - whether any refresh step uses read-only TradeClient evidence;
  - that the refresh path submits no orders and writes no runtime config.
- The dashboard renders this as `下单证据` and `刷新路径` rows.
- Added an artifact-only refresh runbook:
  - CLI: `python3 -m pipelines.tiger_openapi_paper_order_readiness --date YYYY-MM-DD --refresh-runbook --json`
  - JSON: `outputs/tiger_paper_order_readiness/refresh_runbook_current.json`
  - Markdown: `outputs/tiger_paper_order_readiness/refresh_runbook_current.md`
  - The Tiger venue read model summarizes it under `paper_order_refresh_runbook`.
  - The dashboard renders it as `证据刷新包`.
- `paper_order_refresh_runbook.matches_current_readiness` compares the runbook's source readiness status/check time/blockers/stale count against the current readiness receipt.
  - The dashboard shows `current` when it matches.
  - The dashboard shows `stale package` when it does not match, even if the runbook itself says `ready_for_operator_refresh`.
- Added a redacted read-only dashboard API:
  - `GET /api/dualtrack/venue/tiger/paper-order-refresh-runbook`
  - It returns runbook status, current-readiness match state, blocker counts/names, step labels, and safety flags.
  - It does not return raw `command` text from the local runbook artifact.
  - The dashboard prefers this endpoint and falls back to the venue summary.
- Aligned the existing price-feed refresh-runbook API to the same redaction rule:
  - `GET /api/connectors/config/price-feed-refresh-runbook`
  - The local artifact still contains terminal commands.
  - The dashboard API strips raw `command` fields and returns labels/safety flags only.
  - Nested status snapshots are reduced to schema/status/stage summaries so embedded refresh commands are not leaked through JSON.
- Safety boundary:
  - This remains artifact-only.
  - It does not open QuoteClient, open TradeClient, refresh account/order state, submit/cancel/close orders, or write runtime config.
  - Stale evidence must be refreshed by the existing explicit read-only commands before any attended paper canary can proceed.
  - The dashboard refresh path rows are labels only; they do not execute the commands.
  - Runbook generation writes local JSON/Markdown only. The commands inside the runbook may be run later by an operator; generation itself does not run them.
