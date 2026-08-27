# Issue #1069 — Jessie Hyperliquid Testnet market binding

Date: 2026-08-27

## Decision

Jessie keeps Binance Paper as the default market source. A message that
explicitly names `Testnet` selects the credential-free Hyperliquid Testnet
public reader and binds the facts to `hyperliquid.external_testnet` and
`BTC-USD-PERP`. There is no silent fallback to Binance/Gold, synthetic price,
or another instrument.

The reader requests only public `allMids` and `l2Book` data, validates positive
prices, non-crossed BBO, mid/BBO coherence, source identity, and fresh/trusted
status, then returns a source cursor digest. It has no signer, account, order,
or cancellation capability.

Testnet account/equity facts remain a separate required seam. A Testnet
finalize with no configured Testnet account reader returns a typed blocker and
never passes Paper account facts into a Testnet proposal or execution path.

## Live read-only evidence

```text
source=hyperliquid.external_testnet
environment=testnet
instrument_id=BTC-USD-PERP
mid=79685.5
bid=79694.0
ask=79715.0
trusted=true
fresh=true
```

The values above came from one direct public Testnet read during this Issue;
they are not trading authorization or a durable price claim.

## Verification

- The fake-opener reader suite covers mid/BBO parsing, source binding, timeout
  typing, and non-BTC rejection.
- Jessie conversation tests prove Testnet text never uses the Paper market
  reader, while Testnet finalize blocks before any Paper account fallback when
  the Testnet account seam is absent.
- Focused Testnet/Jessie/standard-broker validation passes `139` tests;
  ruff, compileall, and diff-check pass. No credential, Telegram token, order,
  position, scheduler, cloud, Mainnet, or Live mutation occurred.
