# Research: Hyperliquid Testnet all-pair instrument universe and eligibility

Status: resolved decision for Wayfinder ticket [#1016](https://github.com/zinan92/trading-system/issues/1016).

This is a research artifact, not an execution authorization. It does not
submit orders, read credentials, enable a scheduler, or expand the accepted
venue boundary.

## Decision

The canonical universe for the current trading-system Testnet path is the
Hyperliquid **default validator-operated perpetual DEX**, queried with `dex`
omitted or set to the empty string. Its authoritative inventory source is the
`meta` info request; `metaAndAssetCtxs` is the paired metadata-plus-market
snapshot used to attach dynamic context. The public Hyperliquid docs define an
empty `dex` as the first/default perp DEX and expose separate metadata and
asset-context requests ([Perpetuals info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)).

The inventory is intentionally broader than the executable candidate set:

1. **Inventory**: every structurally valid row returned by default-DEX `meta`,
   retained with an explicit status and reason. No row is silently dropped.
2. **Strategy candidate universe**: inventory rows whose metadata is not
   marked `isDelisted=true` and whose canonical identity/precision facts are
   valid.
3. **Execution-eligible candidates**: active candidates that also have a
   coherent, fresh two-sided market observation, valid price/oracle quality,
   sufficient depth for the actual strategy order, and an order size that
   satisfies venue precision/minimum-notional rules.

This satisfies “all supported pairs are candidates” without pretending that a
delisted, empty-book, stale, or precision-invalid pair is safe to submit. Such
rows remain visible as inventory records with an eligibility reason code and
can be reconsidered after a refreshed snapshot.

HIP-3/builder-deployed markets are **not** part of this current candidate
universe. They use separate DEX scopes, may use `coin` names such as
`xyz:TSLA`, and have separate collateral/oracle/fee/margin semantics. The
official API exposes them through `perpDexs`, `meta`/`metaAndAssetCtxs` with a
DEX parameter, and `allPerpMetas`; the current standard-broker adapter
explicitly rejects deferred `:` products and builds the default validator
universe only. Adding HIP-3 requires a new explicit venue/profile decision,
not a string-prefix exception.

## Canonical source and snapshot identity

The source and identity contract is:

| Field | Contract |
|---|---|
| Environment | `testnet` only; use `https://api.hyperliquid-testnet.xyz/info` |
| Broker | `hyperliquid` |
| DEX scope | empty/default perp DEX, represented by `hypercore:default` at the host seam |
| Inventory request | `{"type":"meta"}` |
| Dynamic context request | `{"type":"metaAndAssetCtxs"}` |
| Pairing key | position in `meta.universe` paired with the same position in the context array; preserve any explicit metadata index if supplied |
| Broker symbol | metadata `universe[i].name`, exact case preserved |
| Canonical instrument | `<name>-USD-PERP` for default validator-operated perps |
| Snapshot identity | environment + broker + DEX scope + capture time + raw response digest + adapter/release revision |
| Source provenance | endpoint/request type, transport (`REST` or `WS`), received-at timestamp, venue timestamp when present, and completeness/status |

The exchange endpoint documentation confirms that perpetual order `asset` is
the index in the `meta.universe` response ([Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)).
The standard-broker mapping implements the same position fallback and maps
default names to `<name>-USD-PERP`; see
`/Users/wendy/work/standard-broker/src/standard_broker/adapters/hyperliquid/instruments.py`.

The snapshot must be immutable once published. A later response creates a new
snapshot/revision; it must not mutate facts already used by a Strategy
Candidate Set or execution lifecycle.

## Instrument facts that must be bound

Each inventory entry carries, at minimum:

- `broker_id=hyperliquid`, `environment=testnet`, `dex_scope=hypercore:default`;
- exact broker symbol and canonical instrument ID;
- asset index and metadata row position;
- `contract_type=linear_perpetual`, quote `USD`, default perp collateral
  `USDC`;
- `szDecimals` and `quantity_step = 10^(-szDecimals)`;
- the Hyperliquid price rule: at most five significant figures and, for
  perps, no more than `6 - szDecimals` decimal places;
- `maxLeverage`, `marginTableId` when present, and `marginMode`/`onlyIsolated`
  when present;
- supported order types and minimum-notional fact/derived minimum quantity;
- metadata response digest and adapter capability/revision.

The precision rule is owned by Hyperliquid’s tick/lot-size documentation
([Tick and lot size](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)).
`szDecimals` is the source for quantity rounding. A minimum quantity must not
be treated as a permanent scalar when the venue minimum-notional rule and
current price jointly determine the executable size. The current
standard-broker adapter uses a default-perp minimum notional of 10 quote units
and exposes the step/price rule through its canonical InstrumentSpec; the
actual execution contract must still validate the final order against the
fresh instrument revision.

## Eligibility filters

Eligibility is a deterministic classification, not a hidden drop. Each row
gets one of `inventory_only`, `candidate`, `eligible`, or `ineligible`, plus
stable reason codes.

### 1. Structural identity

Reject from the candidate set (but retain in inventory) if any of these fail:

- non-empty unique name and canonical instrument ID;
- unique asset index and position pairing;
- default-Dex name has no `:` product scope;
- required `szDecimals` and positive finite leverage facts are present;
- `szDecimals` is an integer in the standard-broker supported range `0..6`;
- metadata revision/digest is present.

The current public adapter is deliberately default-perps only and raises on
deferred `:` products; do not bypass that boundary by constructing a native
Hyperliquid payload in trading-system.

### 2. Venue status

`isDelisted=true` is never execution-eligible. It remains in inventory as
`ineligible_delisted`. A missing optional `isDelisted` field is not itself a
delisting signal because official active metadata omits the field; it still
requires all other checks. Any explicit future halt/status fact must create a
new ineligible reason until the host has a reviewed status mapping.

### 3. Dynamic market facts

The `metaAndAssetCtxs` row must align with the metadata row and carry finite,
positive `oraclePx` and `markPx`. `midPx` and `impactPxs` are useful quality
facts but may be null when a book is empty; the official info API documents
that `l2Book` returns at most 20 levels per side and that `allMids` can fall
back to the last trade when the book is empty ([Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)).
That fallback is not an execution quote: an eligible order requires a fresh,
two-sided `l2Book`/BBO observation with positive finite prices and quantities.

Minimum execution-quality checks:

- fresh observation within the configured market-data freshness budget;
- non-empty best bid and best ask, with bid <= ask;
- positive finite mid/spread and a configured maximum spread in basis points;
- positive finite oracle, mark, and (when supplied) impact prices;
- configured maximum deviation of executable quote/impact price from
  mark/oracle; a `price too far from oracle` rejection is a deterministic
  ineligible/requote outcome, not a retryable transport failure (see
  [Hyperliquid error responses](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses));
- available depth on both entry and close/protection sides covers the actual
  strategy request at its configured slippage ceiling;
- optional minimum 24-hour notional volume (`dayNtlVlm`) is a coarse floor only;
  volume alone never substitutes for fresh executable depth.

The threshold values (`max_spread_bps`, `max_oracle_deviation_bps`,
`max_slippage_bps`, `min_day_notional`, and required depth) belong to the
versioned Testnet execution policy. They must be evaluated against the actual
DCA/Grid order size after strategy sizing and Portfolio Gate scaling; a global
hard-coded “BTC is liquid” exception is not allowed.

Mark, oracle, and local-book prices remain separate facts. Hyperliquid’s
robust-price documentation describes mark/oracle construction and their risk
role ([Robust price indices](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices));
the adapter must not collapse them into last-trade price.

### 4. Precision and order feasibility

Before a candidate can produce an execution request, the requested price and
quantity must pass the bound instrument rules:

- quantity is rounded/validated at `szDecimals`;
- price satisfies five significant figures and `6 - szDecimals` decimal
  places for perps;
- notional is at least the venue/adapter minimum;
- direction, margin mode, leverage, order type, and reduce-only/protection
  intent are supported by the accepted standard-broker capability profile;
- the final request carries the same instrument revision as the facts used to
  size it.

If rounding makes the request zero, below minimum notional, or materially
changes the strategy’s requested size, classify it as `ineligible_precision`
or `ineligible_min_notional`; do not silently upsize. Portfolio Gate may only
subtract from strategy size, as decided in the existing Portfolio contracts.

## Refresh, freshness, and failure behavior

Recommended initial refresh policy for the next implementation contract:

- fetch `meta` and `metaAndAssetCtxs` at runner start and on a bounded periodic
  metadata refresh (initial proposal: five minutes, configurable and
  versioned);
- maintain a metadata response digest and publish a new universe revision on
  every change;
- stream `l2Book`/BBO and selected-candidate market data over the Testnet WebSocket
  endpoint; the official WebSocket docs require reconnect handling and a
  snapshot/missed-data repair before treating the stream as current
  ([WebSocket](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket));
- after disconnect, missing snapshot, timestamp regression, or source mismatch,
  mark affected rows stale/unknown and block new entries until rehydration and
  coherence checks pass;
- do not infer freshness from a cached `midPx`, last trade, or an unchanged
  metadata row;
- if metadata/context pairing, instrument revision, or market quality is
  unknown, stop new-entry generation and surface the reason to the lifecycle;
  existing lifecycle code may only follow its separately reviewed
  close/protection/reconciliation rules.

The refresh design must respect Hyperliquid’s published limits: REST requests
share a 1200-weight/minute IP budget; `l2Book` and `allMids` have weight 2,
other documented info requests generally have weight 20; WebSocket limits
include 10 connections, 1000 subscriptions, and 2000 messages/minute ([Rate
limits and user limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)).
The next market-data ticket must therefore choose a bounded subscription
topology rather than opening one untracked connection per asset.

## Observed Testnet snapshot (read-only, 2026-08-26 Asia/Shanghai)

These are observations, not fixed assumptions:

- `POST https://api.hyperliquid-testnet.xyz/info` with `{"type":"meta"}`
  returned 210 default-perp rows; 157 were not marked delisted and 53 had
  `isDelisted=true`.
- The response contained `collateralToken`, `marginTables`, and `universe`;
  all 210 rows had non-empty `name`, `szDecimals`, and `maxLeverage` in this
  snapshot; names contained no `:` because this was the default DEX scope.
- `metaAndAssetCtxs` returned 210 contexts aligned to 210 metadata rows. In
  the same observation, 156 contexts had `midPx` and two `impactPxs`; 54 had
  null `midPx`/`impactPxs`, demonstrating why a context-only presence check
  does not prove executable liquidity.
- Redacted provenance: `meta` response SHA-256
  `5d5ac211381a32407ba71406b83ec3be4fd08148d505b56863a2ab66b7c2ba51`;
  `metaAndAssetCtxs` response SHA-256
  `c365b6abff30237e2e2a19285a76989b148f866400071fe9c3e37c7170678123`;
  observed at `2026-08-26T02:10:12Z` (`2026-08-26T10:10:12+08:00`).

The count and dynamic context quality can change; code must not hard-code
210/157/53 or treat this snapshot as a trading allowlist.

## Handoff contract for the next ticket

The next implementation/research ticket should consume a broker-neutral
`InstrumentUniverseSnapshot` containing:

```text
snapshot_id
environment
broker_id
dex_scope
source_endpoint
captured_at / received_at
metadata_digest
adapter_revision
entries[]:
  instrument_id
  broker_symbol
  asset_index
  contract/quote/collateral
  precision and margin facts
  status (inventory/candidate/eligible/ineligible)
  reason_codes[]
  dynamic market-quality facts and their provenance/freshness
```

`StrategyCandidateSet` may include every `candidate` row, with execution
selection occurring only after the eligibility policy has been evaluated. The
execution bridge receives only the immutable eligible instrument facts plus
the strategy request and Portfolio Gate decision. It must not import
Hyperliquid-native types, call private runtime methods, create signed payloads,
or read credentials; those boundaries are explicit in the
standard-broker handoff at
`/Users/wendy/work/standard-broker/docs/handoffs/external-testnet-to-trading-system.md`.

## Open uncertainties (not blockers to this decision)

1. The exact spread, oracle-deviation, slippage, depth, and 24-hour-volume
   thresholds need calibration against the old DCA/Grid order sizes and the
   Testnet policy budget. This belongs to the market-data/execution policy
   ticket and must remain versioned.
2. The next lifecycle ticket must define how an already-open allocation behaves
   when a pair later becomes stale/delisted or loses liquidity; this research
   only establishes that new entries are blocked and the state is explicit.
3. HIP-3/default-Dex expansion is a separate venue-scope decision; it is not
   safe to merge its metadata into the default `name-USD-PERP` namespace.

