# Park legacy Paper clean-slate cutover

This runbook covers the one explicit migration from legacy accepted Paper
entries to the Park Strategy Track. It is not a strategy switch and it never
flattens positions.

## Accepted operator phrase

Park may send a bounded natural-language instruction such as:

> 取消这19个旧挂单，确认 clean slate，启用 Park Paper

The Telegram worker recognizes this phrase without depending on Codex. It
records a proposal containing the exact accepted order IDs, their cycle IDs,
the expected count, zero open-position evidence, and a digest. A digest-bound
confirmation is also accepted for a proposal that was created without the
bounded confirmation phrase.

## Execution contract

Before cancellation, the Paper control tick re-reads the authoritative account
under the production mutation lock and requires:

- the exact same accepted order ID set and count;
- zero open positions (no flatten/reverse path exists here);
- healthy reconciliation for every discovered Paper namespace;
- trusted/fresh market evidence and all source, boot, immutable-fill,
  Paper-only, owner, and fail-closed safety gates.

Only those order IDs are sent to the direct Nautilus Paper adapter. The
runtime flushes the resulting cancel commands, re-reads the account, and
records a completed receipt only when accepted orders and positions are both
zero. Any drift or uncertain state is durable `blocked` evidence and does not
widen the cancellation scope.

## Enablement and recovery

The checked-in config remains `feature_enabled: false`. A completed exact-set
receipt is the only durable fact that makes the effective runtime config
enabled. The next tick then waits for a new Park strategy; it does not create a
plan or place an order as a side effect of the cutover.

Telegram cursor recovery reprocesses only an already-ingested message that
matches the bounded legacy phrase and was previously misclassified by the
optional provider. Other historical messages are not replayed.

## Forbidden

Do not use this path to cancel arbitrary account orders, close positions,
reverse direction, submit a new plan, or bypass Park confirmation,
Paper-only/preflight, reconciliation, scheduler ownership, release-SHA,
boot, or immutable-fill gates.
