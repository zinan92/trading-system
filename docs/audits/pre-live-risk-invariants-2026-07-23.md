# Pre-live risk-invariant revalidation — 2026-07-23

## Scope

This is local, credential-free verification. It does not submit, cancel, or
close an exchange order. A pass proves only the stated code/test contract; it
is not permission to enable real-money trading.

| Historical risk | Current disposition | Verifiable evidence | Remaining gate |
| --- | --- | --- | --- |
| Daily loss can fail open on NaN, timezone mismatch, or stale artifact | **Pass — fail closed.** | `LiveMoneyGuardrails` requires finite account/PnL, fresh reconciliation, matching run date, and exact UTC-day window. Tests reject NaN, stale/missing/fetch-error artifacts, future/mismatched day data, and missing account history. | Mainnet remains blocked by separate activation/approval. |
| Entry may leave a naked position if protective orders fail | **Pass for testnet contract; mainnet evidence remains blocked.** | The Binance adapter marks protective failure, records a blocker, and has reduce-only emergency close. Testnet kill-switch tests exercise protective recovery/naked-window classification without mainnet. | An attended mainnet canary must prove exchange-resident TP/SL and recovery. |
| `position_size_pct` has a leverage/percent mismatch | **Pass for percentage unit.** | `RiskEngine` preserves the percentage; generic and Binance sizing compute equity × `position_size_pct / 100`, then quantity = notional / price. Focused tests preserve the configured value. | Leverage/margin limits remain separate broker/money gates. |
| Daily gate implicitly requires the local Obsidian vault | **Pass — optional integration.** | `pipelines.daily._sync_market_view_from_obsidian` returns `skipped` if root or note is absent; it is not an entry authority. | Human-plan intake remains audited but optional. |

## Focused verification

```bash
python3 -m pytest -q tests/test_live_money_guardrails.py tests/test_risk_contract.py tests/test_daily_pipeline.py tests/test_gold_signal_and_risk.py tests/test_binance_usdm_testnet_kill_switch.py
```

Result: **30 passed** (local fixtures only).

## Explicit non-conclusion

Nothing here changes execution mode, broker routing, exchange keys, or
activation. The system remains Paper-first; real-money operation requires its
separate approval, activation, and attended-canary evidence.
