# OctoBot Grid / DCA reference for trading-system lifecycle #1020

- Research date: 2026-08-26 (Asia/Shanghai)
- Evidence status: `verified` against the official OctoBot repository only
- OctoBot source snapshot: [`dc0efc8ec36c138bd619272b2b59042778668408`](https://github.com/Drakkar-Software/OctoBot/tree/dc0efc8ec36c138bd619272b2b59042778668408)
- Scope: strategy architecture, asset/session cardinality, order lifecycle, protection, partial fills, restart, and recovery patterns
- Boundary: read-only repository research; no credentials, exchange connection, order submission, or trading-system code change

## Conclusion first

OctoBot is a useful reference for the *mechanics* of single-instrument DCA and Grid execution, but it is not a drop-in replacement for the lifecycle contract already being defined in trading-system.

The most important architectural fact is:

> OctoBot supports several symbols under one ExchangeManager/portfolio by creating a separate symbol-scoped trading-mode instance for each symbol. It does not turn DCA or Grid into one cross-asset portfolio-selection strategy.

That matches Park's clarified first-version boundary: DCA and Grid are generally single-asset execution strategies. A future MACD/SRSI or rotation strategy would be a different strategy family that selects assets first and then delegates execution to an asset-level execution slice.

For #1020, adopt OctoBot's deterministic order mechanics and restart reconstruction patterns; retain our stricter rules for immutable non-expiring Plans, scoped manual interrupt, explicit protection/reconciliation gates, and fail-closed `unknown` handling.

## 1. Strategy and asset/session cardinality

OctoBot separates an evaluator/strategy signal from a trading mode. The official trading-mode guide says trading modes define what order to create, sizing/price, TP/SL, and when to cancel; evaluators and strategies notify the mode when an order should be created. [Trading modes guide](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/docs/content/guides/octobot-trading-modes/trading-modes.mdx#L13-L44)

Both DCA and Grid declare themselves symbol-dependent (`get_is_symbol_wildcard() == False`) and expose their configured symbols as dependencies:

- DCA returns its deduplicated `trading_pairs` and creates symbol dependencies, with optional time-frame dependencies. [`dca_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/dca_trading.py#L1150-L1199)
- Grid/Staggered Orders return each configured `pair` from `pair_settings` and create one symbol dependency per pair. [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L282-L305)
- The trading-mode factory sets `trading_mode.symbol` while creating one mode for each non-wildcard symbol. [`modes_factory.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/modes/modes_factory.py#L29-L84), [`AbstractTradingMode`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/modes/abstract_trading_mode.py#L95-L105)

Therefore the cardinality is effectively:

```text
one ExchangeManager / account portfolio
  ├── DCA mode instance for BTC/USDC
  ├── DCA mode instance for ETH/USDC
  └── ...

or

one ExchangeManager / account portfolio
  ├── Grid mode instance for BTC/USDC
  ├── Grid mode instance for ETH/USDC
  └── ...
```

The repository has multi-symbol tests, but they verify independent producers and shared account funds, not a single DCA/Grid strategy ranking a universe. [`test_staggered_orders_trading_mode.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/tests/test_staggered_orders_trading_mode.py#L234-L275)

**Implication for #1020:** keep one active single-asset `ExecutionSlice` for the selected DCA/Grid Plan. Do not model the configured candidate universe as “one DCA across all assets.” Asset selection belongs above this execution mode in a future strategy family; the current DCA/Grid goal can select one asset, then run one slice.

## 2. OctoBot DCA mechanics

The official DCA guide describes two entry triggers:

- time-based periodic entries;
- evaluator-signal entries that require a new maximum `-1` or `1` signal.

It supports market or limit entries, secondary entries at additional prices, TP/SL after an entry fills, split exit orders, and a per-asset holding cap. [DCA guide](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/docs/content/guides/octobot-trading-modes/dca-trading-mode.md#L20-L55)

### Entry replacement and sizing

On each entry trigger the consumer fetches a current price, computes the initial entry price, optionally cancels eligible same-side open entry orders, computes quantity, and creates the initial plus configured secondary entries. It checks exchange precision/minimums and available funds. [`dca_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/dca_trading.py#L79-L229)

When `cancel_open_orders_at_each_entry` is enabled, the cancellation filter intentionally excludes orders that are already partially filled. [`dca_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/dca_trading.py#L245-L290) The DCA tests verify this edge case and preserve the partially filled order while cancelling replaceable orders. [`test_dca_trading_mode.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/tests/test_dca_trading_mode.py#L1008-L1065)

The DCA mode also checks a spot max asset-holding ratio, including holdings in open orders, before creating an entry. [`dca_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/dca_trading.py#L342-L352), [`dca_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/dca_trading.py#L509-L522)

### Protection topology

For every entry, DCA can register a stop-loss and/or take-profit as chained orders. The entry is submitted first; the chained orders are created/activated after the entry fills. The generic Order state performs that trigger step from `on_fill`/`on_filled`. TP and SL are put in an OCO group when both are enabled. For futures, the chained orders are reduce-only and active; for spot, the implementation can hold an inactive TP alongside an active stop. [`dca_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/dca_trading.py#L399-L507), [`order.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/personal_data/orders/order.py#L611-L728)

The tests verify that chained exits preserve the entry quantity, keep the triggering relationship, use OCO for TP/SL, split quantities when configured, and use reduce-only protection on futures. [`test_dca_trading_mode.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/tests/test_dca_trading_mode.py#L505-L691)

**What this supports for our model:** an entry-fill transition should create protection against the *confirmed filled quantity*, and next-entry eligibility should wait for protection and account facts. OctoBot gives us the chained-order shape, but its code is not a substitute for our explicit `ENTRY_FILLED_PENDING_FACTS`/coverage gate.

### DCA scheduler and plan lifetime

Time-based DCA runs an async loop, triggers the configured symbol, and sleeps for the configured period. It is a recurring trigger, not a plan-expiry mechanism. [`dca_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/dca_trading.py#L731-L789)

The docs describe a DCA configuration as recurring and do not define a Plan expiry. The source inspected here also contains no immutable Plan revision or expiry transition. This is compatible with Park's decision: a DCA Plan remains active until TP, SL, or manual interrupt; scheduler cadence is not Plan expiry.

### DCA health check

When enabled and TP/SL protection is configured, DCA can detect traded assets that are not covered by sell orders and market-sell them into a common quote if they exceed a threshold. The guide explicitly warns that this can sell any uncovered asset associated with a traded pair, even if it was not bought by this mode. [`dca_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/dca_trading.py#L1231-L1270), [`dca_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/dca_trading_mode/dca_trading.py#L1272-L1332), [DCA guide](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/docs/content/guides/octobot-trading-modes/dca-trading-mode.md#L49-L55)

**Adopt only the invariant, reject the action:** our system should detect uncovered owned quantity, but it must not auto-sell unowned/unattributed assets. Ownership and protection coverage must be explicit; recovery/flatten remains a separate authorized action.

## 3. OctoBot Grid mechanics

Grid is a simplified Staggered Orders mode. It uses a fixed spread, fixed increment, buy/sell order counts, per-pair funds/volume, optional reinvestment, mirror delay, and optional trailing up/down. [`grid_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/grid_trading_mode/grid_trading.py#L35-L46), [`grid_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/grid_trading_mode/grid_trading.py#L48-L228)

The Grid resource describes the core behavior plainly: create fixed buy/sell orders; when one is filled, create the opposite mirror order. The default configuration is a finite price ladder around the current price. [`GridTradingMode.md`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/grid_trading_mode/resources/GridTradingMode.md#L1-L39)

### Fill-to-mirror transition

Grid listens to order notifications and only sends a fill callback for a bot-owned limit order whose status is `FILLED`. The callback computes the opposite side, uses the origin price where available, adjusts volume for the target price and optional fees, then creates the mirror order immediately or after the configured delay. [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L261-L280), [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L839-L980)

The mirror quantity can either be fixed or derived from the fill price/volume. With `reinvest_profits` disabled, the sell mirror is reduced to the cost-equivalent amount so profit is extracted to the quote asset; with fees enabled, the mirror quantity is reduced for fees. [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L866-L965)

**Important limitation:** the Grid callback is keyed to a fully `FILLED` notification. The generic Order model does track `filled_quantity`, remaining quantity, and `is_partially_filled()`, but the Grid mirror path is not itself a partial-fill protection/reconciliation contract. [`order.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/personal_data/orders/order.py#L87-L94), [`order.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/personal_data/orders/order.py#L920-L935)

### Grid refresh, restart, and missed fills

Staggered Orders has a startup/reschedule loop. It loads current price/market/portfolio data, examines current open orders and recent trades, and fills missing orders; the Grid subclass uses a longer recent-trade window and explicitly enables recent-trade-based order restoration. [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L559-L584), [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L717-L804), [`grid_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/grid_trading_mode/grid_trading.py#L305-L320)

The order-analysis code infers missing ladder orders from price spacing and recent closed trades, avoiding immediate restoration when a nearby order was just closed. The Grid tests cover canceled orders being restored, offline fills being reconciled on restart, and health-check/startup behavior. [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L1910-L1980), [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L2270-L2497), [`test_staggered_orders_trading_mode.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/tests/test_staggered_orders_trading_mode.py#L687-L879)

Grid also uses an exchange-wide lock and an account-scoped available-funds ledger to avoid double-spending shared currencies across concurrent symbol producers. [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L575-L584), [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L1060-L1085), [multi-symbol funds tests](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/tests/test_staggered_orders_trading_mode.py#L278-L350)

### Grid trailing and interruption

When trailing is enabled and price leaves the configured range, OctoBot can cancel/recreate the grid and, if necessary, convert funds to rebalance the base/quote split. Full-grid trailing explicitly cancels open orders before any conversion/recreation. [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L1524-L1553), [`staggered_orders_trading.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/tentacles/Trading/Mode/staggered_orders_trading_mode/staggered_orders_trading.py#L1823-L1908)

This is a useful distinction for #1020: a Grid's normal mirror cycle is not a TP/SL Plan termination. A trailing/recenter operation is a strategy-specific rebalance, not an expiry. It should remain out of the first DCA/Grid lifecycle unless explicitly included in the Plan contract.

## 4. Persistence and generic order facts

OctoBot's core Order model stores local/exchange IDs, status, original and filled quantities, filled price, fee, total cost, execution/cancel timestamps, reduce-only, chained orders, and related order-group state. [`order.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/personal_data/orders/order.py#L40-L146)

The OrdersManager can upsert exchange orders and initialize from exchange data. When storage-origin details are present, it restores local tags and order groups before binding them to current exchange orders. [`orders_manager.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/personal_data/orders/orders_manager.py#L159-L213), [`orders_manager.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/personal_data/orders/orders_manager.py#L273-L292), [`test_orders_manager.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/tests/personal_data/orders/test_orders_manager.py#L418-L508)

The generic exchange status parser has an `UNKNOWN` status when the raw status is missing, and the core order state includes `PARTIALLY_FILLED`, `PENDING_CANCEL`, `EXPIRED`, `REJECTED`, and `UNKNOWN`. [`order_util.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/personal_data/orders/order_util.py#L478-L488), [`enums.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/enums.py#L73-L84)

However, the inspected DCA/Grid modes do not define the stronger trading-system semantics we need: an immutable Plan revision, causal external-action receipt, scoped `unknown` recovery state, or an explicit “protection coverage confirmed before next entry” gate. Those remain trading-system responsibilities.

## 5. Unknown/cancel behavior: reference, not policy

OctoBot's generic cancellation path retries a failed exchange cancel once, then may force synchronization of the order state to distinguish already-canceled, filling, filled, closed, or still-open conditions. [`trader.py`](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/packages/trading/octobot_trading/exchanges/traders/trader.py#L599-L730)

This is valuable evidence that cancel/query ambiguity is a normal venue condition, but it is **not** our adopted policy. For the Hyperliquid external bridge, a submit/cancel/query result that cannot be causally proven remains `unknown`: freeze only the affected slice, allow one identity-bound reconciliation query, and prohibit blind resubmit/replace/next-entry. If account-wide facts are unknown, escalate to Portfolio Risk Hold.

## 6. Manual stop versus #1020 manual interrupt

OctoBot's public stop-investing guide says stopping a running bot cancels all open buy/sell orders, prevents new orders, and can optionally sell each coin bought by the strategy; restart resumes the strategy with its historical portfolio/profitability. [Stop investing guide](https://github.com/Drakkar-Software/OctoBot/blob/dc0efc8ec36c138bd619272b2b59042778668408/docs/content/investing/stop-a-strategy.md#L9-L24)

That is broader than Park's chosen minimum-disruption interrupt:

```text
MANUAL_INTERRUPT
  -> cancel pending Entry orders for this Plan
  -> stop new Entry / DCA next-entry / Grid re-arm
  -> retain the existing position and confirmed protection
  -> do not auto-flatten
```

Therefore use OctoBot's control separation as inspiration, but do not copy its optional flatten behavior or global bot stop into the Plan lifecycle.

## 7. Direct comparison with #1020 decisions

| #1020 concern | OctoBot evidence | Decision for trading-system | Adopt / reject |
|---|---|---|---|
| Plan expiry | DCA is periodic; Grid is maintained until stopped/reconfigured. No inspected Plan-expiry contract. | No expiry. Plan ends only by TP, SL, or manual interrupt. | Adopt our rule |
| DCA entry | Time/evaluator trigger; initial + optional secondary entries; optional replacement of same-side open entries. | Preserve old DCA semantics; scheduler may auto-trigger only after gates pass. | Adopt mechanics |
| DCA protection | Chained TP/SL after entry; OCO when both enabled; quantity split supported. | Protect confirmed filled quantity; protection facts gate next-entry. | Adopt shape, strengthen gate |
| Grid cycle | Fixed ladder; fully filled order creates opposite mirror; optional delay/reinvestment. | A filled rung closes one Grid cycle and re-arms same Plan after reconciliation; not Plan terminal. | Adopt |
| Grid trailing | Optional cancel/recreate/rebalance when price leaves bounds. | Keep optional trailing out of the minimal #1020 terminal contract unless explicitly specified. | Defer |
| Partial fills | Generic Order tracks them; DCA replacement skips partially-filled entries; Grid fill callback is keyed to `FILLED`. | Explicit partial-fill state and protection coverage for actual filled quantity; no next-entry until reconciled. | Strengthen, do not copy blindly |
| Fees | DCA adapts entry/exit quantities for fees; Grid can reduce mirror size for fees. | Execution receipt must capture actual fee; missing fee is incomplete fact, not zero. | Adopt |
| Restart | OrdersManager restores open orders/storage groups; Grid reconstructs missing ladder orders from open orders/recent trades; DCA health check can find uncovered assets. | Read-only startup reconcile first; no new submit until orders/fills/fees/position/protection facts are fresh and coherent. | Adopt pattern, strengthen safety |
| Unknown | Core parser has `UNKNOWN`; cancellation path retries once and syncs. | Scoped freeze + one bounded identity-bound reconcile; no blind retry/resubmit/replace. | Reject automatic retry |
| Manual stop | Bot stop cancels all orders and may flatten strategy holdings. | Cancel only pending Entries; retain position/protection; flatten is separate explicit action. | Reject broad behavior |
| Portfolio | Shared account/funds lock exists; DCA max holding is per asset/mode. | Portfolio Gate remains the subtractive 30%/10-assets policy; not delegated to OctoBot mode logic. | Keep ours |

## 8. Recommended #1020 state transitions

### DCA

```text
READY
  -> ENTRY_AUTHORIZED
  -> ENTRY_WORKING
  -> ENTRY_FILLED_PENDING_FACTS
  -> PROTECTED_AND_RECONCILED
  -> NEXT_ENTRY_ELIGIBLE

TP or SL
  -> TERMINAL_RECONCILE

MANUAL_INTERRUPT
  -> cancel pending Entry orders
  -> PAUSED_INTERRUPTED (retain position/protection; no auto-flatten)

Any submit/cancel/query/protection/reconcile ambiguity
  -> SCOPED_RECOVERY_REQUIRED
```

### Grid

```text
READY
  -> GRID_WORKING
  -> RUNG_FILLED_PENDING_FACTS
  -> MIRROR_PROTECTED_AND_RECONCILED
  -> GRID_WORKING

MANUAL_INTERRUPT
  -> cancel pending Entry-side orders
  -> PAUSED_INTERRUPTED (retain position/protection)

Any ambiguity
  -> SCOPED_RECOVERY_REQUIRED
```

This preserves the user's rule that a Plan has no expiry, while still giving each external action a bounded lifecycle and preventing a late/unknown order from silently becoming a new entry.

## 9. What to reuse and what not to reuse

### Reuse/adapt

1. **One symbol-scoped execution slice per DCA/Grid Plan** under one account-scoped portfolio.
2. **DCA chained-exit topology** as the model for entry-to-protection linkage.
3. **Grid mirror-order topology** as the model for filled-rung continuation.
4. **Fee-aware quantity adaptation** and exchange min/precision checks.
5. **Open-order + recent-trade startup reconstruction** as a read-only reconciliation input.
6. **Shared account lock and funds reservation** as implementation patterns, with Portfolio Gate applied before authorization.

### Do not reuse as-is

1. OctoBot's automatic cancel retry when the result is ambiguous.
2. OctoBot's broad bot-stop/optional-flatten semantics for manual interrupt.
3. Health-check auto-selling of uncovered but unowned assets.
4. Grid's assumption that a fully-filled callback is sufficient for all fill handling.
5. Per-mode max holding as a replacement for the trading-system Portfolio Risk Gate.
6. The assumption that restoring an order ladder is equivalent to proving a causal execution receipt.

## 10. Research answer for #1020

OctoBot confirms that the old DCA/Grid foundation should remain asset-scoped and can run repeatedly without a Plan expiry. It is a good source of execution mechanics, but the Hyperliquid bridge still needs our own durable Plan/ExecutionSlice records, account-wide fact cursor, protection-coverage proof, manual-interrupt command, scoped recovery blocker, and scheduler idempotency. The research does not authorize Testnet orders and does not close the remaining HITL decisions in #1020.
