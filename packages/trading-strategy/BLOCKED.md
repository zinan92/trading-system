# Integration seam blocker

Extraction of the pure Strategy package is complete and independently tested.

No `trading-system-testnet` compatibility bridge was added in this task. The
existing call sites for DCA and Grid are composition-root or host-specific
paths: Paper lifecycle owns adapter calls and filesystem journals; Park paths
own authorization ledgers and Telegram outbox state; Grid control-plane paths
also compose risk, data, and execution lifecycle. Adding an import seam there
without a separate Issue/contract would change integration ownership and could
silently alter default behavior.

The safe next seam is an explicitly opt-in, source-owned adapter module that
maps the existing StrategyPlan/preview contracts to the composition root while
leaving default imports and behavior unchanged. It must be specified and
tested in a separate Issue before touching `trading-system-testnet`.

This file records an integration blocker only; it is not a claim that the
independent DCA/Grid extraction is incomplete.
