# Start with local Paper default perpetuals

**Status:** accepted
**Date:** 2026-08-21

Hyperliquid v1 will cover default validator-operated perpetuals through local fake/fixture transport only. Mainnet, testnet, native spot, HIP-3 builder-deployed perpetuals, HIP-4 outcome markets, MCP write tools, agent runtimes, and automatic broker switching are deferred and cannot be enabled by configuration alone.

## Considered options

- Connect testnet immediately — rejected because testnet is an external network with separate credentials, approvals, asset IDs, and account state; it is not equivalent to local Paper.
- Support all Hyperliquid products in v1 — rejected because Spot, HIP-3, and HIP-4 have materially different collateral, margin, settlement, oracle, fee, and lifecycle semantics.
- Start with default perpetuals in local Paper — accepted because it provides a narrow execution-grade contract while preserving the existing Paper-only safety boundary.

## Consequences

The first implementation proves domain mapping, precision, order/protection semantics, fees, ambiguous outcomes, and reconciliation with fixtures. A later testnet or live milestone must introduce separate credential, environment, approval, and runtime gates.
