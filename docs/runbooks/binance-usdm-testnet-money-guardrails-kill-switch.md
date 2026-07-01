# Binance USDM Testnet Money Guardrails And Kill Switch Runbook

Status: testnet proof only. Do not use this runbook to submit mainnet orders.

This runbook proves Row 4 hard money guardrails and Row 5 kill switch on Binance USDM testnet. The repo defaults remain unchanged: `execution_mode: paper`, `live_trading_enabled: false`, and mainnet `broker.dry_run: true`.

## Config Source Of Truth

All hard money limits come from `configs/risk_rules.yaml`:

- Daily loss limit: `default.daily_loss_stop_pct` = `1.25` percent.
- Single entry order max notional: `default.live_money_guardrails.single_order_max_notional` = `25.0` USDT.
- Total account notional cap: `default.live_money_guardrails.total_account_max_notional` = `25.0` USDT.
- Max live/testnet entry orders per day: `default.live_money_guardrails.max_daily_entry_orders` = `2`.
- Max reconciliation artifact age: `default.live_money_guardrails.max_reconciliation_age_seconds` = `600`.
- Fallback reference equity for guardrail math: `default.live_money_guardrails.reference_equity` = `5000.0` USDT.

The canonical artifact is `outputs/live_money_guardrails/current.json`. Dashboard and cycle audit consume the resulting `trade_permission`; they must not recalculate these limits.

## Row 4 Guardrail Semantics

Before any non-dry-run Binance USDM live/testnet entry POST, the adapter evaluates:

- Trading day = UTC date. CLI defaults and generated launchd plists use UTC; do not rely on the host local timezone.
- Daily loss = realized PnL plus unrealized PnL, with fees and funding already included through `live_reconciliation.exchange_accounting.net_realized_pnl_estimate`.
- Daily loss is known only when `live_reconciliation` is fresh, has no fetch error, has the same `run_date`, has `account_observation.account_observed=true`, has `exchange_balance.balance_present=true`, and its `exchange_accounting.utc_trading_day` exactly matches that UTC run date. Missing/stale/error/mismatched/unobserved artifacts block as `BLOCKED_MONEY_GUARDRAIL_UNKNOWN`.
- Daily loss equity must come from the real exchange balance row. The configured fallback reference equity is retained for reporting/seams, but it is not accepted as proof for the live/testnet daily-loss hard gate.
- Candidate single-order notional = requested price times rounded exchange quantity.
- Projected total notional = current exchange position notional plus candidate notional.
- Daily entry count = existing live/testnet entry request artifacts for the run date.
- Persistent HALT from `outputs/live_halt/current.json`.

Any breach returns a canonical `BLOCKED_*` status and writes `outputs/live_money_guardrails/<YYYY-MM-DD>.json`. The adapter records a blocked request artifact and raises before submitting entry or protective orders.

## Row 5 Testnet Kill Switch

Dry run:

```bash
python3 -m pipelines.binance_usdm_testnet_kill_switch --date <YYYY-MM-DD> --json
```

Execute against Binance USDM testnet only:

```bash
python3 -m pipelines.binance_usdm_testnet_kill_switch --date <YYYY-MM-DD> --confirm-testnet-kill --json
```

Expected behavior:

- Persist HALT first in `outputs/live_halt/current.json`.
- Cancel XAUUSDT open orders.
- Cancel XAUUSDT algo orders.
- Submit reduceOnly market close orders for non-zero XAUUSDT testnet positions.
- Cancel open/algo orders again.
- Re-run exchange reconciliation.
- Report success only when reconciliation is `confirmed_flat` and `exchange_open_orders` is empty.

The report is written to `outputs/testnet_kill_switch/current.json` and `outputs/testnet_kill_switch/<YYYY-MM-DD>.json`.

## HALT Clear

HALT survives process restart. Do not clear it until exchange UI and `outputs/live_reconciliation/current.json` both confirm flat and no open orders.

Dry-run clear:

```bash
python3 -m pipelines.binance_usdm_testnet_kill_switch --date <YYYY-MM-DD> --clear-halt --json
```

Confirmed clear:

```bash
python3 -m pipelines.binance_usdm_testnet_kill_switch --date <YYYY-MM-DD> --clear-halt --confirm-clear --json
```

After clearing, run a validation-only canary before any new testnet execution:

```bash
python3 -m pipelines.binance_usdm_testnet_canary --date <YYYY-MM-DD> --json
```

## Required Testnet Evidence

- Daily loss breach blocks before entry POST and writes `BLOCKED_DAILY_LOSS_LIMIT`.
- Single-order notional breach writes `BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT`.
- Total account notional breach writes `BLOCKED_TOTAL_NOTIONAL_LIMIT`.
- Daily entry N+1 breach writes `BLOCKED_DAILY_TRADE_LIMIT`.
- Kill switch report shows reduceOnly close submitted, open orders cancelled, algo orders cancelled, reconciliation `confirmed_flat`, and HALT active.
- A restarted adapter remains blocked by `BLOCKED_OPERATOR_HALT` until confirmed manual clear.

## Mainnet Boundary

This code path is testnet-proven only. Mainnet activation still requires a separate human-reviewed config change, dated approval artifact, and the mainnet minimum canary runbook.
