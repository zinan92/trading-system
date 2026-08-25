---
status: accepted
---

# Use one Portfolio Session with asset execution slices and a subtractive risk gate

The future multi-asset model keeps Strategy independent of asset identity while
allowing one Strategy Session to produce a ranked Strategy Candidate Set across
many assets. The Strategy owns opportunity selection, desired position size,
position management, exits, and protection intent.

One account-scoped Portfolio Session owns the resulting allocation and
continuation decisions. Each asset is represented as an Asset Allocation Slice
with a Broker-bound Execution Slice for orders, positions, protection, fills,
and reconciliation. These slices are not independent top-level strategies.

The Portfolio Risk Gate is a fixed constraint boundary. For each Strategy
Position Plan it may accept unchanged, scale down, or reject. It may never
increase exposure, change direction, or rewrite Strategy position-management
semantics. A Portfolio Selection is immutable evidence of accepted assets,
effective sizes, rejected candidates, and reasons.

## Portfolio constraints

- Single-asset concentration is capped at 30% of total AUM.
- The Portfolio holds no more than 10 active asset allocations.
- Global exposure, margin, loss, and cash-buffer limits are evaluated before
  accepting new exposure.
- A new candidate may use unused capacity, but replacing an existing slice
  requires a separate Portfolio Rebalance Decision.
- A slice with unknown non-zero exposure creates a Portfolio Risk Hold when
  account-level risk or ownership cannot be proven independent.

## Considered options

- **Accepted:** reuse Nautilus for canonical account, position, portfolio,
  exposure, PnL, and equity facts; keep Portfolio ownership, risk policy,
  authorization, and allocation provenance in trading-system.
- **Rejected:** create one independent Strategy lifecycle per asset, because it
  duplicates portfolio risk and makes cross-asset limits and ownership unclear.
- **Rejected:** introduce another execution/portfolio repository now, because it
  would duplicate the Broker identity, order, fill, and reconciliation seams.
- **Rejected:** remove the account-level gate and ignore existing exposure,
  because cross-margin and unknown order state would no longer be attributable.

## Consequences

- Nautilus remains the multi-asset runtime/account/portfolio fact engine, not the
  owner of Park authorization or Strategy semantics.
- Trading-system must add a Portfolio Coordinator/Selection contract before
  concurrent multi-asset execution is enabled.
- The current Path A single-instrument Testnet proof remains unchanged; BTC is
  a future asset candidate and needs a fresh instrument/quantity plan.
- Dashboard should project one Portfolio summary with expandable asset slices,
  not pretend every asset is an independent strategy.
