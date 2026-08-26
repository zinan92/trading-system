# Research: Hyperliquid Testnet multi-instrument market-data topology

Status: **provisional research artifact; #1018 remains blocked by #1016**.

This document resolves the market-data topology and quality-contract question
as far as the current primary sources allow. It does not resolve which
Hyperliquid instruments are eligible: that decision belongs to
[#1016](https://github.com/zinan92/trading-system/issues/1016). No order,
credential, signer, scheduler, or cloud state was used while producing this
artifact.

## Question and boundary

The destination is a multi-instrument Strategy Candidate Set for the existing
DCA/Grid semantics. The market-data path must provide synchronized,
instrument-identified facts for each candidate without mixing Hyperliquid
Testnet prices with another venue's execution.

The scope used here is the existing `standard-broker` Hyperliquid scope:
default validator-operated perpetuals. Spot, HIP-3, HIP-4, and other perp DEX
scopes are not silently included. The accepted broker specification explicitly
defers those product families because their collateral, margin, oracle, fee,
settlement, and lifecycle semantics differ
([standard-broker foundation spec](https://github.com/zinan92/standard-broker/blob/916b0eb241b50d5f46be08150eb3197996530552/docs/specs/standard-broker-hyperliquid-paper-foundation-v1.md#implementation-decisions)).

## Decision status

### Provisional decision

Use a **broker-native, venue-bound market-data spine** for execution-grade
facts, with a separate projection into the existing Data Feed and Standard K
Line display contracts:

```text
Hyperliquid Testnet REST + WebSocket
             │
             ▼
standard-broker MarketDataPort / InstrumentPort
             │  canonical identity + Decimal facts + freshness/provenance
             ▼
trading-system MultiInstrumentMarketSnapshot
       ┌───────────────┼─────────────────┐
       ▼               ▼                 ▼
Strategy Candidate   Portfolio Gate     standard-kline projection
Set input            input              (display/read-only)
```

The existing Data Feed may store or serve a Hyperliquid-normalized candle
projection later, but it must not become an independent execution authority.
For Testnet execution, the source identity must remain the same Hyperliquid
Testnet environment, default perp DEX, and canonical instrument as the Broker
binding. This follows the accepted transport-substitution ADR and the local
market-source identity contract (`services/market_source_binding.py`).

This is a topology recommendation, not an implementation authorization. The
current public Testnet profile in `standard-broker` exposes only `ticker` under
its external `market_data` capability; the full candle/book/context seam still
needs the decision and capability work tracked by
[#1017](https://github.com/zinan92/trading-system/issues/1017).

## Primary upstream facts

### Instrument metadata and asset contexts

Hyperliquid's [Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
supports `meta`, `metaAndAssetCtxs`, `allMids`, `l2Book`, and
`candleSnapshot`. The perpetual `metaAndAssetCtxs` response is a two-element
array: metadata containing `universe`, followed by a positional array of asset
contexts. The official [perpetuals API reference](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
documents context fields including funding, open interest, day volume, mark
price, oracle price, mid price, and impact prices.

The metadata fields are not a complete business eligibility verdict. In the
observed Testnet response below, every entry had `name`, `szDecimals`,
`maxLeverage`, and `marginTableId`, but there was no explicit `status` or
`tradable` boolean. Therefore “present in `universe`” is an upstream fact, not
the final `eligible_for_candidate_set` decision. #1016 must define the
refreshable eligibility rule.

The [Hyperliquid asset-ID reference](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)
states that perpetual asset IDs are integers derived from the coin's position
in the `meta` universe. The local `standard-broker` adapter preserves this as
`asset_index`, using an explicit upstream `index` when present and otherwise
the response position (`src/standard_broker/adapters/hyperliquid/instruments.py`).
The join must therefore bind the metadata response and context array to one
`universe_revision`; a context row must never be joined to a separately
refreshed or reordered universe.

Read-only observation against `https://api.hyperliquid-testnet.xyz/info` at
`2026-08-26T02:10:37Z`:

| Request | Observed shape | Interpretation |
| --- | --- | --- |
| `{"type":"metaAndAssetCtxs"}` | 210 perpetual metadata rows and 210 context rows | A bounded current snapshot, not a permanent universe size. |
| Perp metadata sample | `name`, `szDecimals`, `maxLeverage`, `marginTableId` | Precision and leverage facts are available; explicit active/tradable status is not present in this payload. |
| Context sample | `funding`, `openInterest`, `dayNtlVlm`, `oraclePx`, `markPx`, optional `midPx`, optional `impactPxs` | Context facts are separate from candles and must retain their own timestamps/quality. |
| `{"type":"spotMetaAndAssetCtxs"}` | 1,310 spot pairs and 1,649 tokens | Must not be merged into the default-perp universe. |

The count is included only to size the topology. It must be re-read at runtime
and carried as evidence; code must not hard-code 210.

### Candles

The official [candle snapshot contract](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#candle-snapshot)
supports intervals from `1m` through `1M` and exposes only the most recent
5,000 candles. Each row carries the venue symbol (`s`), interval (`i`), open
and close timestamps (`t` and `T`), OHLCV (`o`, `h`, `l`, `c`, `v`), and trade
count (`n`). The official [Python SDK schema](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/api/info/candle.yaml)
confirms the same fields.

The recommended path is:

1. REST `candleSnapshot` for initial bounded backfill and reconnect catch-up.
2. One WebSocket `candle` subscription per required `(coin, interval)` after
   the initial snapshot.
3. Only **closed** bars enter a strategy evaluation. The current/forming bar
   may be shown by Standard K Line but is not an authoritative Strategy input.
4. Missing or out-of-order rows remain a quality failure; no synthetic candle
   is created to repair a gap.

This reuses the local Data Feed envelope rules: `Bar` remains the only OHLCV
value object, while `MarketDataEnvelope` carries instrument, source,
freshness, cache/fallback, execution-venue, and rejection evidence
(`schemas/market_data.py`).

### Quote, book, mark, and oracle facts

The official [L2 book contract](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#l2-book-snapshot)
returns at most 20 levels per side. Each level carries price (`px`), size
(`sz`), and order count (`n`), with a venue timestamp and coin identity. The
official [WebSocket subscription contract](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)
provides distinct `l2Book`, `bbo`, `allMids`, `activeAssetCtx`, and `candle`
streams.

These values have different meanings and must not be collapsed:

| Fact | Use | Prohibited substitution |
| --- | --- | --- |
| Closed candle OHLCV | Existing DCA/Grid signal and strategy timing | Do not infer an OHLC candle from a mid or mark. |
| BBO bid/ask | Market-like order-price construction and execution slippage check | Do not use `allMids` as a BBO. |
| Mid | Scanner/ranking context and display | Do not treat it as executable size or fill price. |
| Mark price | Broker/position mark and protection semantics | Do not replace oracle or candle close. |
| Oracle price | Venue reference/dislocation check | Do not submit from oracle alone. |
| Impact prices | Slippage/liquidity assessment | Do not treat them as top-of-book quotes. |
| L2 levels | Depth, spread, and liquidity gate | Do not assume 20 levels prove sufficient fill capacity. |

The Info docs explicitly note that `allMids` falls back to the last trade when
the book is empty. It is therefore useful for broad observation but is not an
execution-grade gate. The local `standard-broker` adapter already keeps mid,
BBO, and L2 book as separate canonical types and verifies native coin identity
before mapping them (`src/standard_broker/adapters/hyperliquid/market_data.py`).

### WebSocket and recovery constraints

The official [WebSocket endpoint documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket)
defines the Testnet endpoint as `wss://api.hyperliquid-testnet.xyz/ws` and
requires automated clients to handle periodic server disconnects. It states
that missed data can be recovered from the subscription snapshot acknowledgement
or by querying the corresponding Info endpoint.

The official [subscription limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
include at most 10 WebSocket connections, 1,000 subscriptions, 2,000 messages
per minute, 100 in-flight WebSocket post messages, and 10 unique users across
user-specific subscriptions. These are topology constraints, not strategy
parameters.

The proposed collector must therefore maintain:

- `connection_epoch` and a monotonic local sequence for every received event;
- the venue timestamp from the message for ordering and `received_at` for
  freshness;
- per-channel cursors, because the public feed does not provide one universal
  cross-channel cursor for all instrument facts;
- a bounded queue per channel or shard; and
- an explicit `reconnecting`/`catching_up` quality state. A reconnect cannot
  reuse the last “fresh” quote until the required snapshot/catch-up is complete.

## Proposed topology in detail

### 1. Universe/catalog lane (REST, account-independent)

At startup and a versioned refresh cadence, call `metaAndAssetCtxs` for the
default perp DEX. Produce one immutable `UniverseSnapshot` containing:

- `broker_id=hyperliquid`, `environment=testnet`, and `execution_scope`;
- `universe_revision` (hash of the exact metadata payload);
- canonical `instrument_id`, venue `coin`, `asset_index`, quantity step, price
  rule, margin/leverage facts, and observed/received timestamps;
- the eligibility result and reason from #1016; and
- the source/provenance and freshness state.

The refresh replaces the catalog atomically. It never mutates an existing
instrument identity in place. A missing, malformed, stale, or reordered
metadata response blocks new candidate generation globally because no safe
identity join can be proven.

### 2. Broad observation lane

Use one `allMids` stream or periodic asset-context snapshot to rank/observe the
eligible universe cheaply. This lane may mark a pair as a candidate *for
further evaluation*, but it cannot authorize an entry. Empty-book fallback,
missing mid, stale context, and identity mismatch are explicit pair-level
quality flags.

### 3. Strategy-bar lane

Subscribe to the required strategy intervals, beginning with the old strategy's
declared interval rather than inventing a new one. If the current DCA/Grid
contract requires `1m`, subscribe to 1m per eligible instrument and derive
higher display intervals only through a separately versioned aggregation path.
The strategy receives closed `StandardKLine` values with a common
`evaluation_snapshot_id`; the latest forming bar stays display-only.

The current `standard-kline` package is intentionally provider-agnostic. Its
[README input contract](https://github.com/zinan92/standard-kline#input-contract)
accepts standardized OHLCV bars plus provider/source/trust metadata, and its
[Datafeed/Adapter boundary](https://github.com/zinan92/standard-kline#datafeed--adapter-boundary)
explicitly keeps exchange requests out of the package. Use it as a projection
and trust overlay, not as the market-data owner or candidate selector.

### 4. Context and liquidity lane

Keep `markPx`, `oraclePx`, funding, open interest, day volume, and impact prices
as a separate context stream/snapshot. Subscribe to BBO for every currently
eligible candidate when within the venue subscription budget. Fetch or stream
L2 depth for candidates entering the execution shortlist; the final order gate
must bind to a fresh BBO and explicit depth/slippage facts.

This avoids paying the cost of full-depth processing for every pair while still
allowing every supported pair to enter the candidate universe. It also handles
thin/dislocated pairs honestly: a pair can be a candidate in the universe yet
be excluded from the current Strategy Candidate Set because its execution
facts are not usable.

### 5. Account/order lane (shared-account, not one stream per strategy)

The account-scoped execution lifecycle should consume one set of user streams
for order updates, user events/fills/funding/ledger updates, and clearinghouse
state, then reconcile through the Broker's account/position ports. This lane
belongs to the per-asset Execution Slice and Portfolio Session work, not to
Standard K Line. It is included here only to define the join boundary: market
facts never infer account position or fill state.

### 6. Snapshot composition

The composition root should build a read-only snapshot for one candidate
evaluation:

```text
MultiInstrumentMarketSnapshot {
  schema_version
  snapshot_id
  universe_revision
  broker_id / environment / execution_scope
  mapping_revision
  observed_at / evaluation_at
  connection_epoch
  per_channel_cursors
  instruments: tuple[InstrumentMarketFacts, ...]
}
```

`InstrumentMarketFacts` contains the immutable instrument identity, closed
candles, BBO/mid, mark/oracle/context, liquidity summary, and fact-level
freshness/provenance. `snapshot_id` is a deterministic digest of the identity,
cursor, and facts; it is evidence for the Strategy Candidate Set and Portfolio
Gate, not a command to trade.

## Quality and freshness contract

### Identity join

Every fact must carry or be joined through the following exact tuple:

| Field | Required meaning |
| --- | --- |
| `broker_id` | `hyperliquid` |
| `environment` | `testnet`; Mainnet is a different binding, never a fallback |
| `execution_scope` | default validator-operated perp DEX, explicitly represented |
| `instrument_id` | canonical `standard-broker` instrument identity |
| `provider_symbol` | exact Hyperliquid `coin` from the same metadata revision |
| `asset_index` | metadata index/position used for venue actions |
| `universe_revision` | digest of the exact metadata snapshot used for the join |
| `mapping_revision` | reviewed standard-broker/Nautilus mapping release |
| `venue_timestamp` | timestamp supplied by Hyperliquid for the fact |
| `received_at` | local receipt time used for freshness |
| `connection_epoch` + `local_sequence` | deterministic ordering across reconnects and same-time events |

The local `MarketSourceIdentity` contract already requires source ID, Broker,
environment, instrument, and `execution_venue=true`. The multi-instrument
contract should extend that evidence with `execution_scope`, universe revision,
and mapping revision rather than inventing a second identity vocabulary.

### Fact-level required fields

| Fact | Required fields | Minimum quality to enter a candidate evaluation |
| --- | --- | --- |
| Closed candle | instrument identity, interval, `t/T`, OHLCV, trade count if supplied, venue timestamp, receipt time | Strict OHLCV geometry, ordered/unique timestamps, closed bar, fresh, exact source, no synthetic/fallback. |
| BBO | exact coin, venue time, bid/ask level or explicit incomplete state | Both sides present, positive and ordered, fresh, identity match. |
| Mid | exact coin or bound metadata revision, value, receipt time | Fresh and source-bound; never sufficient for execution by itself. |
| Context | oracle, mark, funding, OI, volume, optional mid/impact, receipt time | Required fields present for the policy; mark/oracle retained separately. |
| L2 | exact coin, venue time, up to 20 levels per side, level price/size/count | No crossed/invalid levels; spread/depth/impact calculations are explicit; insufficient depth is degraded, not silently zero. |
| Snapshot | all above identities, revision, cursors, observed/evaluation times | Bounded cross-fact skew; no reconnect/catch-up unknowns; deterministic digest. |

### Quality states and impact scope

Use a closed vocabulary at the market-data boundary:

`fresh`, `stale`, `unknown`, `degraded`, `identity_mismatch`,
`source_mismatch`, `incomplete`, `dislocated`, `unsupported`, and `blocked`.

The effect is intentionally two-level:

| Failure | Effect |
| --- | --- |
| One pair's missing candle, stale BBO, empty book, bad spread/depth, oracle/mark dislocation, or identity mismatch | Mark that instrument `blocked`/`degraded`; omit it from the current Candidate Set. Unrelated slices may continue if the Portfolio Snapshot and account state remain coherent. |
| Universe metadata missing/stale/reordered, mapping revision mismatch, or shared market snapshot cannot prove source/environment coherence | Portfolio-level hold on new entries; do not generate a partially joined Candidate Set. |
| Account-wide exposure/ownership/reconciliation unknown | Portfolio Risk Hold, as already established by `docs/adr/0005-multi-asset-portfolio-session-and-risk-gate.md`; market-data quality alone must not infer a position. |
| Display-only Standard K Line rejection | Block chart trust overlay; it must not change Broker/account state or create a strategy decision. |

### Freshness and cursor semantics

Use `received_at` for age and freshness, and `venue_timestamp` for ordering.
Do not derive freshness from venue time alone: clock skew and reconnects make
that unsafe. The policy must be versioned/configured per fact type and strategy
interval:

- A candle evaluation requires at least one fresh **closed** bar; the existing
  Data Feed precedent of up to three bar intervals is a starting hypothesis,
  not a final Testnet execution threshold.
- BBO/L2/context require a much shorter, independently configured age window;
  the local `standard-broker` `FreshnessPolicy` already classifies observations
  as `fresh`, `stale`, or `unknown` from receipt time and transport state.
- A reconnecting or catching-up channel is `unknown` regardless of the age of
  its last pre-disconnect event.
- An out-of-order, duplicate, or skipped candle sequence is a quality failure;
  recover from REST before re-admitting the pair.

No threshold should be hidden inside DCA/Grid or Standard K Line. The policy
revision and actual age must be included in the snapshot evidence.

### Precision and price binding

The official [tick/lot-size documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)
binds quantity precision to `szDecimals` and documents Hyperliquid's price
precision rules. The local `standard-broker` adapter maps `szDecimals` to a
quantity step and retains the price rule in `InstrumentSpec`; strategy code
must emit desired size, while the Broker/Instrument boundary validates and
rounds only according to the bound instrument. A market-data snapshot may carry
precision facts for evidence, but must not implement a second rounding rule.

## Backpressure, refresh, and degraded-pair behavior

The collector should shard subscriptions by deterministic instrument order and
respect the official 10-connection/1,000-subscription/2,000-message limits.
The exact shard count is derived from the current #1016 universe and requested
intervals, not hard-coded. A bounded queue overflow is a data-quality event:
the affected channel becomes `unknown`, the pair is blocked from new entries,
and the reconnect/catch-up path must restore a coherent cursor before admission.

REST refreshes are not a hidden fallback to another venue. They are the same
Hyperliquid Testnet source used to recover snapshots and fill gaps. If a fact
cannot be recovered, retain the blocker and omit only the affected pair unless
the shared identity/universe snapshot is no longer trustworthy.

Observed read-only behavior at `2026-08-26T02:12:07Z` illustrates why this
distinction matters: a Testnet `l2Book` request returned bounded level arrays
for both BTC and PAXG, while a 1m `candleSnapshot` returned recent rows for BTC
and no rows for PAXG in the requested five-minute window. This is not a trading
decision or a fixed liquidity verdict; it is evidence that “in universe” and
“currently data-ready for a strategy slice” are different states.

## Handoff to Strategy Candidate Set generation

The Strategy Candidate Set generator should consume only the composed snapshot,
not raw Hyperliquid payloads:

```text
MultiInstrumentMarketSnapshot
        │
        ├─ filter: exact identity/revision/source/environment
        ├─ filter: closed-bar + context freshness
        ├─ filter: instrument/precision eligibility from #1016
        ├─ filter: liquidity/dislocation quality
        ▼
Strategy Candidate Set
        │  Strategy owns signal, desired size, add/reduce/exit/protection intent
        ▼
Portfolio Risk Gate
        │  subtractive only; no market-data repair or strategy rewrite
        ▼
Execution Slice lifecycle
```

This preserves the accepted architecture: Strategy owns position management,
Portfolio applies subtractive constraints, Broker owns venue mapping, and
Standard K Line displays the standardized OHLCV/trust projection.

## What remains open

This artifact **must remain open/blocked** because #1016 is unresolved. The
following decisions cannot be made safely until that universe decision is
recorded:

1. The exact candidate universe scope, refresh cadence, eligibility predicate,
   and handling of metadata entries that lack explicit active/tradable status.
2. The exact subscription set and shard sizing, which depend on eligible
   instruments and strategy intervals.
3. The final `universe_revision`/instrument identity handoff to candidate
   generation.

The market-data path also depends on #1017 exposing the required public
standard-broker read capabilities (`candles`, `bbo/ticker`, `l2Book`, context,
and provenance/freshness) through the pinned Testnet profile. No implementation
should begin from this artifact alone; after #1016 and #1017 are resolved, the
remaining HITL tickets (#1019–#1022) must settle strategy fan-out, lifecycle,
scheduler authorization, and rollout evidence before `/to-spec`.

## Source register

### Primary upstream sources

- [Hyperliquid Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [Hyperliquid perpetual metadata and asset contexts](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
- [Hyperliquid WebSocket subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)
- [Hyperliquid WebSocket transport/reconnect guidance](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket)
- [Hyperliquid API rate and subscription limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
- [Hyperliquid asset IDs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)
- [Hyperliquid tick and lot size](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)
- [Official Hyperliquid Python SDK `metaAndAssetCtxs` schema](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/api/info/assetctxs.yaml)
- [Official Hyperliquid Python SDK candle schema](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/api/info/candle.yaml)
- [Official Hyperliquid Python SDK market-data types](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/utils/types.py)

### Local first-party contracts inspected

- `standard-broker` commit `916b0eb241b50d5f46be08150eb3197996530552`
  - `docs/specs/standard-broker-hyperliquid-paper-foundation-v1.md`
  - `src/standard_broker/market_data.py`
  - `src/standard_broker/adapters/hyperliquid/market_data.py`
  - `src/standard_broker/adapters/hyperliquid/instruments.py`
  - `src/standard_broker/adapters/hyperliquid/external.py`
- `datafeed` commit `55dfa2940bd1174ddcf588ecb426e9a672088a64`
  - `src/kline/models.py`
  - `src/kline/provenance.py`
  - `src/kline/quality.py`
- `standard-kline` commit `95dec8bc62835cd94a9406742e3a438fbe052a13`
  - `README.md` (input and Data Feed/Adapter boundary)
  - `standard-kline.js` (`adaptDatafeedResponse` and `TrustPolicy`)
  - `decision-log.md`
- `trading-system` contracts at the research branch base:
  - `schemas/market_data.py` (`MarketDataEnvelope`)
  - `services/market_source_binding.py` (`MarketSourceIdentity`)
  - `docs/datafeed-boundary.md`
  - `docs/adr/0004-broker-transport-substitution.md`
  - `docs/adr/0005-multi-asset-portfolio-session-and-risk-gate.md`

## Resolution recommendation

Attach this artifact to #1018, leave #1018 open with its existing blocking edge,
and revisit it immediately after #1016 closes. At that point the topology is
ready to become a buildable contract only if the accepted universe decision and
the #1017 capability decision are both carried into the snapshot schema. No
orders, scheduler, credentials, Testnet account state, Mainnet, or Live path
are authorized by this research.
