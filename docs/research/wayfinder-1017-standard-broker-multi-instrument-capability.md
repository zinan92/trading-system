# Wayfinder research: standard-broker multi-instrument Testnet capability

**Ticket:** [WAYFINDER RESEARCH: determine standard-broker multi-instrument execution and protection capability](https://github.com/zinan92/trading-system/issues/1017)

**As of:** 2026-08-26 (Asia/Shanghai)

**Decision status:** resolved for the low-level execution/protection question; one
additional public multi-instrument reconciliation seam is required before the
portfolio automation goal can be claimed complete.

## Executive decision

The pinned `standard-broker` release already contains the low-level order and
position-following protection implementation required by the existing DCA and
Grid semantics. It is exposed through an **explicit opt-in** Hyperliquid
Testnet profile, not through the default profile:

```text
standard-broker release:        7a23054d3f8bcf4e3a17537dc3b8d3ebd361a70b
profile:                        hyperliquid-testnet-position-protection
capability revision:            hyperliquid-testnet-position-protection-runtime-v1
protection matrix:              hyperliquid-testnet-position-protection-v1
broker/environment:             hyperliquid / Testnet
execution scope:                hypercore:default
runtime adapter:                nautilus-hyperliquid 1.230.0
runtime commit:                 8160730c7c550480b0a439fb11086a4c4de15f0b
transport state:                external_testnet
signer kind:                    API_AGENT (opaque provider reference only)
```

The current `trading-system` external bridge does not consume that profile. It
pins `hyperliquid-testnet-default`, exposes only `PREFLIGHT`, binds one
instrument, reports `ready=false`, and rejects every order submission. This is
the immediate blocker, and it belongs in the trading-system composition layer.

For the complete multi-asset closed loop, the existing attended canary
reconciliation reader is still single-instrument and the external fact seam
does not expose funding. A Broker-owned, cursor-bound multi-instrument snapshot
seam (and an explicit decision on funding) is therefore required before
portfolio-level automation can be considered complete. That is an extension of
the public standard-broker fact/reconciliation contract, not a second venue
adapter or a strategy rewrite.

No external network call, credential resolution, Testnet order, Mainnet/Live
action, scheduler change, or cloud mutation was performed for this research.

## Exact identity and profile bindings

The `trading-system` consumer currently pins the public standard-broker release
and runtime identity in
`services/standard_broker_external_testnet.py` (trading-system `origin/main`
`b731035d2f9f70afc7b7cf3091a16df0c03b0782`):

| Field | Accepted value | Source |
|---|---|---|
| standard-broker release | `7a23054d3f8bcf4e3a17537dc3b8d3ebd361a70b` | `services/standard_broker_external_testnet.py:19` |
| external profile | `hyperliquid-testnet-default` | `services/standard_broker_external_testnet.py:20`; standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/profile.py:41-54` |
| Broker / environment | `hyperliquid` / `TESTNET` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/profile.py:42-45` |
| execution scope | `hypercore:default` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/profile.py:45` |
| runtime adapter | `nautilus-hyperliquid` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/profile.py:46` |
| Nautilus version / commit | `1.230.0` / `8160730c7c550480b0a439fb11086a4c4de15f0b` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/external.py:24-25` |
| default capability revision | `hyperliquid-testnet-runtime-v1` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/external.py:28-44` |
| default protection profile | `hyperliquid-testnet-protection-v1` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/protection.py:27-57` |

The required protection-enabled profile is a separate exact binding, not a
wildcard or a flag on the default profile:

| Field | Protection-enabled value | Source |
|---|---|---|
| profile | `hyperliquid-testnet-position-protection` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/profile.py:56-69` |
| capability revision | `hyperliquid-testnet-position-protection-runtime-v1` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/external.py:47-60` |
| protection matrix | `hyperliquid-testnet-position-protection-v1` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/protection.py:60-98` |
| public factory | `build_hyperliquid_testnet_protected_canary_binding_from_runtime` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/profile.py:239-313` |
| protection-only public binding | `build_hyperliquid_testnet_position_protection_binding_from_runtime` | standard-broker `7a23054`: `src/standard_broker/adapters/hyperliquid/profile.py:225-236` |

The standard-broker registry explicitly records that this opt-in profile is
the next consumer dependency and that the default profile remains
protection-disabled (`REGISTRY.md:35-40, 68-72` at the pinned release).

## Capability matrix

### Public standard-broker capability at the pinned release

| Seam | Public capability at `7a23054` | Multi-instrument meaning | DCA/Grid status |
|---|---|---|---|
| Instrument catalog | `instrument.read` | `load_instrument_definitions(include_perps=True, include_perps_hip3=False, include_outcomes=False)` loads the default validator-operated Hypercore perp universe; the typed mapper returns a deterministic tuple of canonical `InstrumentSpec` values | Available for all supported default perps; HIP-3, spot, and outcome products are out of scope |
| Market read | `market_data.ticker` | Ticker/BBO is requested per canonical instrument; the backend resolves instrument identity and returns provenance | Available per instrument; no batch/candle contract in this external profile |
| Submit | `order_execution.submit` | `OrderIntent.instrument_id` resolves through the shared catalog; canonical order identity and provenance are retained | Available in the public order facade; not exposed by current trading-system bridge |
| Cancel | `order_execution.cancel` | Uses canonical order identity plus Broker order identity | Available; ambiguous outcomes remain non-terminal until query/reconciliation |
| Replace | `order_execution.replace` | Uses explicit cancel-replace lineage and requires a confirmed terminal old order on fallback | Available; no blind retry |
| Query | `order_execution.query` | Resolves by canonical/client/Broker identity and rejects conflicting identity reports | Available |
| Open orders | `order_execution.open_orders` | Optional instrument scope; `None` can request account-wide open orders at the low-level backend | Available, but the current trading-system bridge does not expose it |
| Fills | `order_execution.fills` | Public facade accepts order or instrument scope; the external backend requires an instrument scope for a fill query, so a portfolio consumer must fan out across the catalog | Available per instrument/order; account-wide aggregation is not a single call |
| Actual fill fees | `fee.fill` | Fill mapping preserves maker/taker/rebate, fee currency, actual fee, and optional builder fee with instrument identity | Available for filled orders |
| Fee schedule | `fee.read` / `fee.schedule` | Canonical schedule identity and estimated schedule state | Available |
| Account | `account.read` | Canonical equity/balance/margin and account positions from the account state | Available; account snapshot carries all positions in the returned account fact |
| Positions | `account.positions` | Mapper validates one requested instrument at a time; account snapshot mapping can carry the full position list | Available, but a multi-asset snapshot must be assembled through a Broker-owned contract |
| Protection submit/cancel/replace/query | `protection_order.{submit,cancel,replace,query}` in the opt-in profile | Typed `ExternalProtectionBinding`, digest-bound observations, and remote coverage confirmation | Available for the supported position-following group shape |
| Protection semantics | `reduce_only`, mark trigger, grouped TP/SL, sibling cancellation, position coverage, partial-fill repair, position-following, position-level TP/SL, TP-limit, SL-market/limit, cancel-replace | Explicit capability matrix; no silent emulation | Satisfies the existing DCA and Grid shapes below |
| Ambiguous/unknown outcome | Canonical `UNKNOWN`/frozen/non-coherent outcomes | Caller must query/reconcile; blind retry is forbidden | Required and available as a fail-closed primitive |
| Reconciliation envelope | `ExternalReconciliationSnapshot` | Cursor/watermark, identity, freshness/skew, unknown, drift, incomplete and digest checks | Primitive available; current attended reader is single-instrument |

Sources for this table:

- `standard-broker@7a23054`: `src/standard_broker/adapters/hyperliquid/external.py:28-60, 169-219, 221-315, 835-855, 570-680`.
- `standard-broker@7a23054`: `src/standard_broker/adapters/hyperliquid/orders.py:1174-1283` (public external order facade).
- `standard-broker@7a23054`: `src/standard_broker/adapters/hyperliquid/read_facts.py:52-123, 125-250, 333-389`.
- `standard-broker@7a23054`: `src/standard_broker/external_reconciliation.py:151-305, 307-444`.

### Protection matrix details

The default profile intentionally declares all protection behavior unavailable
except `reduce_only_close=true`. The opt-in matrix declares the following
support:

```text
submit=true                 cancel=true                 replace=true
query=true                  reduce_only=true            reduce_only_close=true
mark_price_trigger=true    grouped_tp_sl=true          sibling_cancellation=true
position_coverage=true      partial_fill_repair=true   position_following=true
position_level_tpsl=true    take_profit_limit=true     stop_loss_market=true
stop_loss_limit=true        cancel_replace=true
```

It deliberately keeps these unsupported:

```text
retry=false                 fixed_size=false            bracket=false
parent_child=false         take_profit_market=false
```

The `take_profit_market=false` value is intentional: the pinned Nautilus
1.230.0 public transformer cannot convert the market-if-touched TP shape.
The backend rejects that shape rather than silently changing it
(`src/standard_broker/adapters/hyperliquid/external.py:249-307` and
`src/standard_broker/adapters/hyperliquid/protection.py:86-93`).

This does **not** block the locked legacy DCA/Grid semantics in this repository:

- DCA creates a position-following group with take-profit **limit** and
  stop-loss **market** (`services/dca_testnet_lifecycle.py:942-971`).
- Grid's hard stop creates a position-following stop-loss **market** group
  (`services/grid_testnet_lifecycle.py:613-628`).
- The existing external DCA preflight intentionally requires TP-limit and
  SL-market while requiring TP-market to remain false
  (`pipelines/standard_broker_external_dca.py:342-355`).

## Current trading-system bridge versus required behavior

The current external bridge is a deliberate preflight-only adapter, not an
execution adapter:

| Current bridge fact | Evidence | Consequence |
|---|---|---|
| It pins the default profile, not the protection-enabled profile | `services/standard_broker_external_testnet.py:19-24` | The profile cannot authorize `protection_order` operations |
| Local capability declaration is only `{PREFLIGHT}` | `services/standard_broker_external_testnet.py:62-64, 226-228` | Trading-system cannot call submit/cancel/replace/query/fills through this adapter |
| One `InstrumentBinding` and one `MarketSourceIdentity` are required | `services/standard_broker_external_testnet.py:107-118, 147-175, 203-224` | The current composition is single-instrument, not a portfolio universe |
| `preflight()` returns `ready=false`, `protection_ready=false`, `account_read_ready=false`, `order_execution_ready=false` | `services/standard_broker_external_testnet.py:268-314` | It is correctly blocked before execution |
| `submit_order()` always raises | `services/standard_broker_external_testnet.py:316-321` | No order can be submitted through the current bridge |

The public standard-broker handoff assigns the boundaries correctly: Broker
owns runtime identity, canonical facts, order lifecycle, protection gaps, and
reconciliation evidence; trading-system owns composition, strategy, risk,
authorization, Recording Track, Supervisor, and the decision to block on a
non-pass reconciliation (`standard-broker@7a23054`:
`docs/handoffs/external-testnet-to-trading-system.md:5-27`).

## What is already available versus what is missing

### Already available in standard-broker

No new venue serializer or second Broker adapter is needed for the following:

1. Exact Hyperliquid Testnet identity and release binding.
2. Default Hypercore perp catalog discovery and canonical precision rules.
3. Canonical order submit/cancel/replace/query/open-orders/fill operations.
4. Idempotent order and fill identity, replacement lineage, and unknown-stop
   behavior.
5. Opt-in position-following TP-limit/SL-market protection required by the
   current DCA and Grid strategy contracts.
6. Canonical account, position, fill, fee, provenance, and reconciliation value
   types.

### Required in trading-system

The trading-system bridge must:

1. Bind the exact opt-in profile and capability revision, with a new attended
   Testnet approval/release/lifecycle identity; it must not toggle the default
   profile in place.
2. Replace the single `InstrumentBinding` with a catalog/universe binding and
   preserve instrument and market provenance for every candidate.
3. Adapt the existing DCA/Grid lifecycle to the public `OrderIntent`,
   `ExternalOrderLifecyclePort`, and `ExternalProtectionPort` seams, preserving
   the old strategy semantics.
4. Keep Portfolio Gate subtractive: it may reduce/reject a candidate but may
   not add exposure or rewrite DCA/Grid order semantics.
5. Own the strategy-session/asset-slice journal, expiry transitions,
   next-entry authorization, kill switch, scheduler lease, and explicit
   unknown-stop. No blind retry is permitted.

### Required public extension before full multi-asset automation is complete

The low-level order/protection primitives are present, but the current public
attended reconciliation reader is not a portfolio reader:

- `HyperliquidExternalSnapshotReader.read_reconciliation()` accepts one
  `instrument_id`, reads positions for that instrument, and reads open orders
  for that instrument (`standard-broker@7a23054`:
  `src/standard_broker/external_canary.py:84-161`).
- The external fill facade requires an instrument or order scope, so a
  portfolio snapshot must fan out across instruments
  (`src/standard_broker/adapters/hyperliquid/orders.py:976-1021`).
- The cursor-bound reconciliation contract requires one coherent cursor across
  account, positions, open orders, fills, and fees; stale, drift, incomplete,
  and unknown results are non-pass (`src/standard_broker/external_reconciliation.py:307-444`).

Therefore the smallest safe public addition is one Broker-owned
multi-instrument snapshot/facts seam, for example:

```text
read_portfolio_snapshot(
    instrument_ids: tuple[str, ...] | None,  # None = full supported catalog
    now: datetime,
) -> ExternalReconciliationSnapshot
```

The implementation may internally fan out, but the returned snapshot must
carry one account identity, one cursor/watermark, one provenance/release
binding, all positions/open orders/fills/fees included in scope, and explicit
`UNKNOWN`/`INCOMPLETE`/`DRIFT` outcomes. The consumer must not manufacture a
cursor by combining unrelated reads.

Funding is a separate explicit gap: the external capability profile has no
`fee.funding` operation, `HyperliquidExternalFactAdapter.map_funding()` raises
`funding_transport_gap`, and the current canary snapshot sets
`funding_applicable=false` (`read_facts.py:383-389`;
`external_canary.py:241-255`). If portfolio equity/risk/PnL for perpetuals is
required to include funding, the same public snapshot extension must add
funding facts and set `funding_applicable=true`; otherwise the testnet
automation scope must explicitly record funding as excluded rather than imply
complete account accounting.

## Recommended route

1. Treat `standard-broker@7a23054` as the current low-level baseline; do not
   create a new Broker adapter or reimplement Hyperliquid transport in
   trading-system.
2. Add/resolve the standard-broker multi-instrument snapshot/funding decision
   above as a public contract (the implementation may remain inside
   standard-broker).
3. In trading-system, build a new exact-profile multi-asset external host
   bridge over the existing public order/protection/fact facades.
4. Wire the old DCA and Grid lifecycles through Portfolio Gate and per-asset
   execution slices, with one account-wide reconciliation/ownership journal.
5. Add attended BTC canary, then bounded multi-asset Testnet soak. Only after
   those evidence gates may the scheduler be considered for unattended
   Testnet operation. Mainnet/Live remains outside this map.

## Primary source index

- Pinned public source: `standard-broker@7a23054d3f8bcf4e3a17537dc3b8d3ebd361a70b`.
- Exact default and opt-in profiles: `src/standard_broker/adapters/hyperliquid/profile.py`.
- Runtime capabilities and multi-instrument catalog loading: `src/standard_broker/adapters/hyperliquid/external.py`.
- Protection capability matrix and position-following binding: `src/standard_broker/adapters/hyperliquid/protection.py`, `src/standard_broker/external_protection.py`.
- Public order lifecycle: `src/standard_broker/adapters/hyperliquid/orders.py`.
- Public typed facts: `src/standard_broker/adapters/hyperliquid/read_facts.py`.
- Cursor-bound reconciliation: `src/standard_broker/external_reconciliation.py`.
- Boundary handoff: `docs/handoffs/external-testnet-to-trading-system.md`.
- Trading-system consumer bridge: `services/standard_broker_external_testnet.py`.
- Legacy strategy protection shapes: `services/dca_testnet_lifecycle.py`, `services/grid_testnet_lifecycle.py`.
