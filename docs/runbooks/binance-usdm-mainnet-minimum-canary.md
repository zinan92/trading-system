# Binance USDM Mainnet Minimum-Capital Canary Runbook

Status: future manual runbook only. Do not execute from Codex.

This runbook is for the first human-gated mainnet canary after testnet evidence is green. It intentionally does not flip any gate by itself.

## Preconditions

- The current default repo config still has `execution_mode: paper`, `live_trading_enabled: false`, and `broker.dry_run: true`.
- Complete the testnet money-guardrails and kill-switch runbook first: `docs/runbooks/binance-usdm-testnet-money-guardrails-kill-switch.md`. Always-on heartbeat and mainnet auto-flatten remain future guardrail steps.
- Latest testnet canary is pass: `python3 -m pipelines.binance_usdm_testnet_canary --date <YYYY-MM-DD> --execute-testnet --quantity 0.002`.
- Final read-only reconciliation is flat: `confirmation_status=confirmed_flat`, `system_state=READY`, `drift_count=0`, no open orders.
- Binance USDM account is manually set to XAUUSDT, isolated margin, 1x leverage.
- Mainnet account has only the canary bankroll in the USDM futures wallet. Recommended first canary bankroll: 15 USDT. Do not keep unrelated funds in the futures wallet for the first canary.

## Manual Gates To Flip

Flip these in a reviewed local config change only after the preconditions are true:

- `execution_mode: live`
- `live_trading_enabled: true`
- `broker.environment: live`
- `broker.base_url: https://fapi.binance.com`
- `broker.dry_run: false`
- `broker.request_dir: live_order_requests`
- `broker.protective_order_endpoint: algoOrder`
- `broker.reconcile_account_history: true`

Keep these unchanged:

- `broker.provider: binance_usdm`
- `broker.instrument_map.GOLD: XAUUSDT`
- `market_data_sources.gold_5m.execution_venue_providers: ["binance_usdm"]`
- `market_data_sources.gold_5m.allow_execution_venue_for_live: true`

Human approval must be a dated artifact under `outputs/live_approvals/<YYYY-MM-DD>.approved.json`. Do not reuse a prior date.

## Canary Size

- Quantity: `0.002` XAUUSDT, unless Binance raises the minimum notional.
- Wallet bankroll: 15 USDT.
- Stop/TP: exchange-resident reduceOnly algo orders, same shape as testnet.
- Expected worst case for this canary is the funded test wallet balance, not the full production account.

## Execution Procedure

1. Run feed and preflight checks:
   - `python3 -m pipelines.binance_usdm_feed --date <YYYY-MM-DD>`
   - `python3 -m pipelines.broker_preflight`
   - `python3 -m pipelines.live_readiness --date <YYYY-MM-DD>`
   - `python3 -m pipelines.live_activation --date <YYYY-MM-DD>`
2. Confirm `live_activation.real_money_ready=true` and the approval artifact exists.
3. Submit exactly one canary order through the reviewed live path. Do not run strategy auto-approval for multiple tickets.
4. Immediately inspect:
   - `outputs/live_order_requests/<YYYY-MM-DD>.json`
   - `outputs/order_lifecycle/<YYYY-MM-DD>.json`
   - `outputs/live_reconciliation/current.json`
   - Binance UI: XAUUSDT position, open TP/SL algo orders, fills, fees.
5. Accept only this state as healthy:
   - entry filled quantity equals local position quantity;
   - two reduceOnly exchange-resident protective orders are visible;
   - reconciliation is `confirmed_open` while position is open;
   - after manual reduceOnly close, reconciliation becomes `confirmed_flat`;
   - lifecycle reaches `reconciled`.

## Emergency Stop

The automated kill-switch entry is testnet-proven only. For the first mainnet canary, keep the manual fallback ready in Binance UI and do not rely on an unreviewed mainnet kill command.

1. In Binance UI, cancel all XAUUSDT open orders and open algo orders.
2. Submit a reduceOnly market close for the exact XAUUSDT position size.
3. Run read-only reconciliation until it returns `confirmed_flat`.
4. Restore config to `execution_mode: paper`, `live_trading_enabled: false`, `broker.dry_run: true`.
5. Archive the failed artifacts and do not submit a second canary until the cause is reviewed.

## Seams For Next Guardrail Step

- Mainnet kill-switch command: adapt the testnet-only command only after a separate review and explicit mainnet approval.
- Auto-flatten: escalate stuck naked or protective-missing states from blocker to forced close under strict policy.
- Always-on heartbeat: external monitor must page when reconciliation becomes stale or `cannot_confirm`.
- Mainnet canary command: add a dedicated mainnet canary wrapper only after the above guardrails exist.
