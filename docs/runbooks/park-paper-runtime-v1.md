# Park Paper Runtime v1

This is a Paper-only, Telegram-only control and execution path. It is separate
from the legacy DualTrack cycle runner. A recording window ending at 09:00 or
21:00 only closes the facts package and review; it never changes the active
strategy or flattens exposure.

## Required target-environment configuration

Inject these values through the Paper host secret/config mechanism. Do not put
the bot token in Git, issue text, receipts, logs, or chat messages.

```text
TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN
TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID
TRADING_ORCHESTRATOR_TELEGRAM_PARK_USER_ID
TRADING_ORCHESTRATOR_NAUTILUS_PYTHON
TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED=1
TRADING_ORCHESTRATOR_OUTPUT_ROOT
```

The deployed Park config must be an approved copy of
`configs/park_strategy_track.json` with `feature_enabled=true`, while retaining
`runtime_mode=paper_only`, `execution_track_count=1`, `control_plane=telegram`,
`autonomous=false`, `shadow_mutation=false`, and `feishu_control=false`.

The approved copy must also include an explicit `paper_execution` contract:

```json
{
  "instrument": {
    "instrument_id": "XAUUSDT.BINANCE",
    "symbol": "XAUUSDT",
    "venue": "BINANCE",
    "provider": "park_paper_config"
  },
  "paper_fee_model": {
    "mode": "paper_contract",
    "maker_fee_rate": "0",
    "taker_fee_rate": "0.000400",
    "funding_rate": "0",
    "source": "park_paper_config",
    "environment": "paper",
    "real_money_eligible": false
  }
}
```

The runtime writes a short-lived, source-bound
`park_strategy/paper_preflight_current.json` from this contract. It does not
read exchange credentials or require the legacy seven-cycle Shadow cutover
receipt. Missing, stale, dirty-source, digest-mismatched, or real-money
eligible preflight evidence blocks before any adapter mutation.

Before starting the service, write source-bound release/boot and all ten
cutover safety-gate receipts to the target output root. Missing or stale
evidence blocks without attempting an order.

## One bounded pass

```bash
PYTHONPATH=. python3 -m pipelines.park_control \
  --output-root "$TRADING_ORCHESTRATOR_OUTPUT_ROOT"
```

Run this command from the exact committed Paper release SHA under a supervised
launchd/systemd timer. It performs one Telegram `getUpdates` pass, handles
proposal/confirmation, executes only the exact confirmed Park revision through
the direct authoritative Paper adapter, records receipts, and drains Telegram
outbound messages. A non-zero exit is a durable blocked state, not permission
to retry with a different strategy.

## Runtime semantics

- A strategy message creates a deterministic proposal and sends its digest,
  not an order.
- Only `confirm sha256:<digest>` from the configured Park user/chat authorizes
  the later Paper tick.
- The strategy session/revision owns every projected order and receipt. A
  duplicate pass is idempotent.
- Touching/crossing the configured boundary is the explicit Park-confirmed
  invalidation trigger: it freezes and cancels only owned unfilled entries,
  closes only positions carrying the exact Park ownership identity, reconciles,
  pauses, and asks Park. Unrelated positions are never touched.
- A new strategy cannot be admitted until the previous session is terminal and
  the clean-slate account checks pass.
- 09:00/21:00 packages and reviews are recording-only and cannot call an
  execution mutation.

## Explicit non-goals

Do not use `pipelines.dualtrack_cycle_runner`, `--paper-auto-approve`, the old
cycle stop/flatten path, Shadow mutation, Feishu, exchange keys, or any live
path for Park.
