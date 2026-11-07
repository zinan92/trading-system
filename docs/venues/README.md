# Adding a venue to the trading desk

The desk is pluggable at one seam: a **Desk Venue Module** (ADR 0007, `CONTEXT.md`). Adding a venue
or a data source means adding one module and registering it. Business code (app, plans, executor,
notify, advice) does not change.

## Steps

1. **Copy the template.** Copy [`template_venue.py`](template_venue.py) to
   `apps/trading-desk/src/trading_desk/venues/<name>.py`. Set `kind`, the labels, the plan template,
   and the capabilities you implement.
2. **Register it.** Add it to `DEFAULT = build(...)` in `venues/__init__.py`. The registry is code and
   frozen. There is no runtime install.
3. **Add assets.** Use `Store.add_asset({...}, kinds=registry.kinds())`. The database no longer lists
   kinds; the registry does.
4. **Run the tests:**
   - `tests/test_venue_conformance.py` runs the fail-closed contract over every registered module.
     Your module is included automatically.
   - `tests/test_venue_characterization.py` must stay unchanged for the existing kinds.
   - `tests/test_fake_venue_acceptance.py` shows a whole flow with a pretend exchange.
5. **Write records.** Add a REGISTRY and decision-log entry. If you added `execute`, say what gate
   decides each order.

## Capabilities

| capability | the desk then | the module must provide |
|---|---|---|
| `live_price` | uses the venue price for plans (else the last 1-hour close) | `grid_and_price` |
| `bars` | draws K-lines | `bars` |
| `grid_plan` | drafts a grid from Park's direction (else the call is record-only) | `plan_template` |
| `preview` | previews an approved plan (no orders) | `preview` |
| `execute` | lets Park's 执行 button hand the plan on | `preview`, `execute`, `started_text`, and `preview` declared |
| `stop` | shows 停止网格 | Testnet coordinator semantics today |
| `rearm_hold` | shows 暂停补单 | Testnet lifecycle holds today |
| `price_alerts` | watches the grid against the price every 20 minutes | a grid record with price/upper/stop |
| `bars_health` | lists the K-line feed on the system page | `bars`, `bars_health_label` |
| `futures_overview` | refreshes the futures overview every 20 minutes | an asset `kline_key` |

A capability you declare without the thing it needs is dropped when the registry is built, and
`venue_capability_dropped` is logged. Capabilities are never inferred from methods.

## The fail-closed contract (`venues/conformance.py`)

With every upstream down, a module must:
- give no price;
- give a grid that is not `ok`;
- answer bars with `ok: false`, a reason and an empty list;
- never return a ready preview;
- never ask for a confirmation;
- never start a driver.

Texts the page reads must not raise.

## Do not

- **Name venue profiles or instruments in the module.** They come from the asset row and the
  Dashboard catalog (ADR 0006). The gold venue moved from Binance Paper to OKX Paper without a desk
  change because of this.
- **Fall back to another source when a feed fails.** Pick sources explicitly and show the source on
  the page.
- **Read credentials.** Execution with keys stays in the runtime behind the Dashboard gates.
- **Rename a `kind` once assets use it.**

## Broker ports

New broker adapters implement the Standard Broker canonical ports
(`packages/standard-broker/src/standard_broker/ports.py`). `services/broker_port` is the legacy
ticket path and is only wrapped, never extended (ADR 0007).
