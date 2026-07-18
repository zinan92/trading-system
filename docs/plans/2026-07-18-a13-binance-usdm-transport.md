# A13 Binance USD-M Transport Extraction Plan

**Status:** In progress.

**Goal:** Move Binance USD-M endpoint selection, request signing, public
exchange metadata, and HTTP I/O out of the legacy `LiveBrokerAdapter` while
preserving every current execution, activation, risk, reconciliation,
lifecycle, and protective-order behavior.

**Architecture:** Add one venue-owned transport behind the existing broker
adapter seam. The transport owns Binance wire concerns only. The legacy class
keeps thin compatibility methods so demo, testnet, kill-switch, canary, and
direct-test callers continue to work while the larger execution adapter is
strangled incrementally.

## User outcome

Park can replace or test Binance networking without editing the provider-free
Broker Port or the cross-venue compatibility class. Current grid orders retain
the same money gates, request payloads, idempotency, protection, and recovery
semantics.

## Observable success criteria

1. A dedicated Binance USD-M transport owns base-URL resolution, instrument
   mapping, signed GET/POST/DELETE requests, ExchangeInfo retrieval, timeout,
   credentials, and safe failure normalization.
2. `LiveBrokerAdapter` contains no Binance endpoint literal, signing code,
   HMAC, timestamp/receive-window construction, or ExchangeInfo parser.
3. Existing private compatibility methods keep their exact names and route
   through the transport, so demo/testnet subclasses and operational kill
   switches require no behavior rewrite.
4. HTTP method, URL, query/body ordering, headers, signature input, timeout,
   endpoint choice, response decoding, and ExchangeInfo fallback remain
   byte-for-byte or structurally identical to A12 behavior.
5. Missing credentials still fail before network I/O; public metadata failure
   remains `UNKNOWN`; no secret, signature, timestamp, or receive window enters
   descriptors or durable safe receipts.
6. Mainnet/demo/testnet activation, same-cycle reconciliation, canonical risk,
   ambiguous submission, idempotent recovery, cancellation, protective orders,
   and emergency close remain untouched and pass their existing suites.
7. Architecture fitness, focused/full regressions, Ruff, Opus review, and the
   Evidence Contract audit pass without changing config, credentials, orders,
   positions, accounts, or production state.

## Milestone 1 — Freeze the wire contract

**User outcome:** The extraction has a precise no-behavior-change boundary.

**Success criteria:**

- Capture signed write, signed read, public ExchangeInfo, endpoint, and failure
  behavior in focused tests.
- Confirm the current demo/testnet/mainnet compatibility call sites.
- Freeze the existing 100-test broker/Binance regression baseline.
- Record which concerns remain deliberately in the execution adapter.

**In scope:** wire behavior and compatibility inventory.
**Out of scope:** changing Binance API versions or payload semantics.

## Milestone 2 — Venue-owned transport and compatibility facade

**User outcome:** Binance networking becomes physically isolated and
replaceable.

**Success criteria:**

- Add a small immutable transport configuration boundary.
- Move signing, request construction, response decoding, symbol mapping, and
  ExchangeInfo parsing into the Binance module.
- Keep adapter methods as thin delegating wrappers.
- Construct transport lazily from the adapter's effective config and opener.
- Preserve subclass overrides and instance monkeypatch seams.

**In scope:** Binance wire code only.
**Out of scope:** order lifecycle, local accounting, risk, reconciliation,
protective policy, or broker-registry authority.

## Milestone 3 — Isolation proof and closure

**User outcome:** Modularity is proven without weakening trading safety.

**Success criteria:**

- Add transport contract tests and a static guard against Binance wire code
  returning to `broker_adapter.py`.
- Re-run all mainnet/demo/testnet and Broker Port tests.
- Run the whole repository and Ruff.
- Obtain verified Opus adversarial review and address P0-P2 findings.
- Update the architecture audit, decision log, and Evidence Contract result.

**In scope:** proof and documentation. **Out of scope:** UI changes.

## Mature-pattern decision

- Continue the repository's A5 Broker Port strangler and the official
  Nautilus-style split between normalized execution behavior and venue-owned
  networking. No new plug-in framework is needed for one transport.
- Use dependency injection for the opener and clock so signed requests are
  deterministic under test and no global HTTP client is introduced.
- Keep the compatibility facade until all Binance operational callers depend
  on a public venue adapter; deleting private names in this step would add risk
  without adding user value.

## Gotchas

- Binance signature validity depends on exact parameter insertion order. A
  seemingly harmless sort or serialization cleanup can change the signed wire
  request.
- Signed writes place the query in the request body, while signed GET places it
  in the URL. They must not be generalized into a different shape.
- `BinanceDemoBrokerAdapter` intentionally overrides signed GET to return an
  `{ok,status,body|error}` envelope. The base extraction must not bypass that
  override during recovery and demo position checks.
- The same opener is shared with live reconciliation. Transport construction
  cannot wrap or replace it in a way that changes test or operational routing.
- Demo and testnet endpoint defaults are not interchangeable with mainnet even
  when callers override `base_url` explicitly.
- This milestone isolates transport only. Binance lifecycle/protection logic
  remains in the compatibility adapter and is named follow-on debt, not hidden
  as completed venue isolation.
- This milestone has no visible surface. Tests, code, commits, docs, and review
  receipts are trace material, not visual Evidence.

## Baseline

- A12 full repository regression: `1813 passed, 7 skipped`.
- A13 broker/Binance transport regression: `100 passed`.

## Completion boundary

A13 is complete only when no Binance wire implementation remains in
`LiveBrokerAdapter`, all old call seams preserve their behavior through the new
transport, and mainnet/demo/testnet safety suites plus the full repository pass.
Moving helper names without removing signing/endpoints/HTTP from the legacy
module does not count.
