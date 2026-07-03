# Attended Mainnet Canary Preflight Checklist

Status: manual runbook only. Do not run mainnet from Codex.

This checklist is for the first attended Binance USDM mainnet canary after the repeatable TESTNET drill is green. The repo defaults must remain paper/dry-run until a human explicitly flips the gates.

## Required TESTNET Evidence

Run the rehearsal first:

```bash
python3 -m pipelines.testnet_drill --execute-testnet
```

Required artifact:

- `outputs/testnet_drill/<run_id>.json`
- `status=pass`
- `base_url=https://demo-fapi.binance.com`
- all scenarios pass: `connectivity`, `green_loop`, `crash_recovery`, `kill_switch`, `always_on_deadman`, `money_guardrails`, `naked_window`

Evidence mapping:

- Green loop: `scenarios[].name=green_loop`
- Crash/restart recovery: `scenarios[].name=crash_recovery`
- Kill switch/HALT: `scenarios[].name=kill_switch`
- Always-on/deadman fail branch: `scenarios[].name=always_on_deadman`
- Hard money guardrails: `scenarios[].name=money_guardrails`
- Naked-window recovery: `scenarios[].name=naked_window`

Known rehearsal limitation: the daily-loss subcase uses an injected fresh same-day reconciliation snapshot because Binance TESTNET cannot deterministically create an exact daily loss threshold on demand. It still proves the production guardrail code blocks before any order POST.

## Environment To Prepare

- Binance USDM mainnet API key/secret are present in the reviewed live env file. Do not paste secrets into notes.
- If a key/secret was ever pasted into chat, revoke it first and generate a new restricted Futures key before funding.
- `TZ=UTC` is installed in launchd schedules.
- `TRADING_ORCHESTRATOR_DEADMAN_URL` is installed and externally alerting.
- Optional but recommended: `TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL` points to a louder channel.
- Binance account is manually set to XAUUSDT, one-way mode, isolated margin, and 1x leverage.
- Operator stays at the machine for the whole canary. If the operator must leave, close the position and confirm flat first.

## Canary Capital And Limit Calibration

These are hard pre-gate steps, not a review note.

1. Transfer the exact canary bankroll into the Binance USDM futures wallet before any live gate is flipped.
2. Confirm the futures wallet contains only the canary bankroll plus expected dust. Do not keep unrelated production funds in the first canary wallet.
3. Confirm daily loss is computed from real exchange balance, not config `reference_equity`. The canary bankroll must be present and accurate before the daily-loss percentage is meaningful.
4. Reset `configs/risk_rules.yaml` live-money limits from the real canary bankroll. For the first `$100` canary, the reviewed starting point is:
   - `daily_loss_limit_pct: 1.0`
   - `single_order_max_notional: 10.0`
   - `total_account_max_notional: 10.0`
   - `max_daily_entry_orders: 1`
5. If the bankroll changes from `$100`, recalibrate:
   - `daily_loss_limit_pct`
   - `single_order_max_notional`
   - `total_account_max_notional`
   - `max_daily_entry_orders`
   Do not leave the TESTNET placeholder notional limit `25.0` in place for mainnet.
6. Confirm no stale `reference_equity` value can dilute any live/testnet money-guardrail calculation.
7. On the funding day, expect an account-level `income` row such as `TRANSFER` with an empty symbol. That row is intentionally skipped by reconciliation and covered by the TESTNET drill, but still watch `live_money_guardrails` and deadman after funding to confirm it does not become `BLOCKED_MONEY_GUARDRAIL_UNKNOWN`.

## Deadman Live Test

Do this before the first live gate flip. If any item fails, do not flip live gates.

1. Send one real deadman ping:
   - `python3 -m pipelines.deadman_ping --date <YYYY-MM-DD>`
2. Confirm the external healthcheck page received the live ping.
3. Intentionally miss one expected ping window or unload/stop only the deadman schedule long enough to trigger the external service.
4. Confirm the external service actually alerts on absence, not only on explicit `/fail`.
5. If `TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL` is configured, confirm that position-aware critical pings route to the louder channel.

## Kill Path Approval

Mainnet kill switch is the primary kill path only after explicit approval.

1. Record the exact reviewed command, config profile, symbol, and confirmation flags in the dated approval artifact.
2. Dry-run/verify the command path before the first live order:
   - `python3 -m pipelines.binance_usdm_mainnet_kill_switch --date <YYYY-MM-DD>`
   - Expected: `status=dry_run`, `base_url=https://fapi.binance.com`, `symbol=XAUUSDT`, and no HALT activation.
3. The real emergency command requires both mainnet flags:
   - `python3 -m pipelines.binance_usdm_mainnet_kill_switch --date <YYYY-MM-DD> --confirm-mainnet-kill --i-understand-this-is-mainnet`
   - This persists HALT first, cancels open orders and open algo orders, submits reduce-only close orders, then requires reconciliation `confirmed_flat`.
4. HALT clear after manual verification:
   - `python3 -m pipelines.binance_usdm_mainnet_kill_switch --date <YYYY-MM-DD> --clear-halt --confirm-clear`
5. Keep Binance UI as the backup kill path. Before live gate flip, rehearse the UI sequence without a live position: open XAUUSDT futures, locate open orders, locate open algo orders, locate reduce-only market close, and confirm where exact size is entered.
6. During the canary, if automated kill output and Binance UI disagree, trust exchange UI state first and use read-only reconciliation to verify flat after action.

## Gates To Flip, In Order

1. Confirm current defaults still safe:
   - `execution_mode: paper`
   - `live_trading_enabled: false`
   - `broker.dry_run: true`
2. Run TESTNET drill and confirm the artifact above is green.
3. Complete canary capital and limit calibration.
4. Complete the deadman live test.
5. Explicitly approve the mainnet kill switch path, then dry-run/verify the command and parameters without closing a real position.
6. Walk the Binance UI backup kill path once: cancel open orders, cancel open algo orders, reduce-only market close exact size, then read-only reconcile.
7. Run read-only mainnet readiness/preflight only.
8. Create a dated human approval artifact under `outputs/live_approvals/<YYYY-MM-DD>.approved.json`.
9. In a reviewed local config change only, flip:
   - `execution_mode: live`
   - `live_trading_enabled: true`
   - `broker.environment: live`
   - `broker.base_url: https://fapi.binance.com`
   - `broker.dry_run: false`
   - `broker.request_dir: live_order_requests`
   - `broker.protective_order_endpoint: algoOrder`
   - `broker.reconcile_account_history: true`
10. Re-run the activation gate AFTER the flips and confirm it minted `real_money_ready=true`:
    - `python3 -m pipelines.live_activation --date <YYYY-MM-DD>`
    - A pre-flip run records `real_money_ready=false` (execution mode / dry-run checks) and order submission
      would refuse at the adapter's activation gate. The artifact must be dated the canary day.
11. Run the read-only canary gate check and confirm `GO`:
    - `python3 -m pipelines.mainnet_canary --date <YYYY-MM-DD>`
    - It verifies: live gates flipped, activation `real_money_ready`, TESTNET drill evidence `pass`,
      the dated human approval artifact, and that the placeholder TESTNET money limits (`25.0` notional /
      `5000` reference_equity) have been recalibrated. Any blocker means stop.
12. Submit exactly one minimum-size canary through the canary command (never via unattended auto-approval):
    - `python3 -m pipelines.mainnet_canary --date <YYYY-MM-DD> --submit-mainnet-canary --confirm-mainnet-canary --side buy --quantity 0.002 --entry-price <p> --stop-loss <macd-cross-low> --take-profit <entry + 1.5R>`
    - Both flags are required; the command refuses on any NO-GO blocker, refuses a second order the same
      day, and records the check + ticket + broker receipt under `outputs/mainnet_canary/<YYYY-MM-DD>.json`.
    - Reconciliation freshness is handled inline: the live adapter refreshes
      `outputs/live_reconciliation/current.json` from the exchange immediately before the order and blocks
      on drift / naked position / cannot-confirm, so the money guardrails always see a same-day snapshot.

## Watch During The Canary

Keep these open while the order is live:

- Binance UI: XAUUSDT position, open orders, open algo orders, fills, fees.
- `outputs/live_order_requests/<YYYY-MM-DD>.json`
- `outputs/order_lifecycle/<YYYY-MM-DD>.json`
- `outputs/live_reconciliation/current.json`
- `outputs/live_money_guardrails/current.json`
- dashboard `trade_permission`
- external deadman status page

Healthy state while open:

- entry filled quantity equals exchange position quantity;
- exchange has resting reduceOnly `STOP_MARKET` coverage for the full position;
- TP algo order is also visible;
- `live_reconciliation.confirmation_status=confirmed_open`;
- `suspected_naked_position=false`;
- lifecycle reaches `protective_attached`.

Healthy state after close:

- reduce-only close fills exact position quantity;
- all open/algo orders are cancelled or gone;
- `live_reconciliation.confirmation_status=confirmed_flat`;
- `system_state=READY`;
- lifecycle reaches `reconciled`.

## Kill Immediately If

- Position exists and no resting reduceOnly stop is visible.
- `BLOCKED_NAKED_POSITION_SUSPECTED` appears.
- `cannot_confirm` appears while a position may be open.
- Protective order POST returns non-resting / terminal status.
- Position quantity differs from local lifecycle or paper mirror.
- Daily loss, notional, or daily order guardrail reports `UNKNOWN` or `BLOCKED_*` unexpectedly.
- Always-on/deadman sends `/fail` or misses expected pings.
- Any order stays in `submitting`, `accepted`, or `partially_filled` beyond the watchdog window.

Kill procedure:

1. Trigger the reviewed kill switch entry for the active environment only if it has been explicitly approved for mainnet.
2. Otherwise use Binance UI: cancel all XAUUSDT open/algo orders, then reduce-only market close the exact position size.
3. Run read-only reconciliation until `confirmed_flat`.
4. Restore config to paper/dry-run gates.
5. Keep the failed artifacts and do not submit a second canary before review.

## Stop And Reset

After a successful canary:

- Confirm `confirmed_flat`, no open orders, no open algo orders.
- Confirm the external deadman is green.
- Restore unattended gates to paper/dry-run unless the next attended canary is explicitly approved.
- Archive the drill artifact and mainnet canary artifacts together.
