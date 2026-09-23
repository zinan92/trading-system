# Reuse the Nautilus Hyperliquid adapter for low-level wire behavior

**Status:** accepted
**Date:** 2026-08-21

The first Hyperliquid adapter will use the existing NautilusTrader Hyperliquid implementation for low-level REST, WebSocket, signing, nonce, order lifecycle, and reconciliation behavior. `standard-broker` will wrap that implementation with its canonical contracts and capability policy instead of duplicating a second Hyperliquid SDK or adopting CCXT as a runtime layer.

## Considered options

- Reimplement Hyperliquid signing, transport, and lifecycle in `standard-broker` — rejected because it duplicates high-risk behavior already present in the Nautilus base used by the trading system.
- Use CCXT — rejected because it adds a lowest-common-denominator layer and hides Hyperliquid-specific protection, account, nonce, and reconciliation semantics behind parameters.
- Use the official Python SDK directly as the runtime — retained as protocol/signing reference and fixture source, but not the first runtime path because the system already uses Nautilus.

## Consequences

The integration must pin and audit the compatible Nautilus version, maintain conformance fixtures, and treat Nautilus adapter upgrades as compatibility changes. The `standard-broker` core must not import provider-native wire types.
