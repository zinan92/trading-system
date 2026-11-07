---
status: accepted
---

# One Broker port direction, and Desk Venue Modules with declared capabilities

Adding a Venue Profile or a data source should mean adding one module, not
editing business code. Today two things stop that.

First, the repository has two Broker port families:
- `services/broker_port` is the ticket-based execution port of the legacy
  Paper/DCA path, used in 36 places.
- `packages/standard-broker` `ports` are the canonical Market Data, Instrument,
  Account, Order Execution, Protection Order and Fee ports, used by the
  Hyperliquid Testnet path.

Second, the trading desk branches on `asset["kind"]` (`hl_testnet`,
`xau_paper`, `watch`) in about fifteen places across reads, plans, preview,
execute, stop and alerts.

## Decision

**Ports.** The Standard Broker canonical ports are the one direction. A new
Venue Profile implements those ports. `services/broker_port` becomes a
compatibility layer for the existing ticket path: it stays, it is not extended
with new capabilities, and its adapters are wrapped onto the canonical ports
when they are next changed. Nothing is rewritten only to converge.

**Desk Venue Modules.** Each desk kind is one code module in a frozen,
code-defined registry. Each module holds the desk-side behaviour of that kind:
- labels;
- the plan template;
- how to read price, grid and bars;
- which desk flows apply;
- the capabilities it declares.

Business code asks the registry for the module and checks a capability. It
never tests the kind's name. Existing `kind` values stay as registry keys, so
stored asset rows need no data migration.

**Venue identity is not in the module.** `venue_profile_id` and the instrument
keep coming from the Dashboard catalog and selection (ADR 0006). The module
must not become a second catalog. Gold already shows why: the seeded
`binance.paper` id is stale, and the live venue comes from GridMind's catalog.
Assets remain user data in the desk database. A module may supply seed
defaults, but it never owns the user's asset rows.

**Capabilities are declared, never inferred.** Examples are `live_price`,
`bars`, `grid_plan`, `preview`, `execute`, `stop`, `rearm_hold` and
`price_alerts`. A module without a capability does not get that flow. Execute
is off unless declared.

**Conformance is the entry ticket.** Two gates, both real:
- At registry build, a declared capability the module cannot back is dropped
  and logged. For example, `execute` needs a preview, an execute, a started
  text and `preview` declared.
- The fail-closed conformance suite (`venues/conformance.py`) runs in CI,
  parametrized over every registered module. A module that fails it does not
  merge.

There is no runtime downgrade to read-only: the suite needs fakes and is not
run at import. Because the suite is parametrized rather than a hand-kept list,
a new module is covered automatically. (Corrected in #1381; the first version
said a failing module "is registered read-only".)

## Out of scope

- **Runtime install of third-party plugins.** It means executing arbitrary
  code, and the product does not need it.
- **Automatic failover between data sources.** Multiple sources are chosen
  explicitly and named on the page. A failed feed blocks new entries; it never
  borrows another venue's price.
- **GridMind-specific gold presentation** (`paperGoldIds`, `onGridMind`) stays
  as page presentation for now.
- **Run-record and event `kind` fields** (why, review timeline, watch, digest,
  provenance). These are not venues.

## Consequences

- Behaviour is locked before the swap by a byte-equal characterization snapshot
  per kind. The swap changes structure, not output.
- The store's `CHECK (kind in (...))` moves into code, validated against the
  registry, so a new module needs no schema edit.
- A fake test venue, added as one module in a test registry, must run the desk
  end to end with zero business-code change. That is the acceptance test.
- A capability is a desk-flow switch. It is never authority to trade: the
  Dashboard preview and runtime gates still decide every order.

## Considered options

- **Rejected: a static manifest that also names venue profiles and
  instruments.** It duplicates the Dashboard catalog and goes stale.
- **Rejected: deciding capabilities from which methods an adapter happens to
  implement.** Inherited methods would grant access, which ADR 0004's capability
  gates already forbid.
- **Rejected: making `services/broker_port` the canonical direction.** It has
  no protection, fee or instrument ports, and the Testnet path already runs on
  Standard Broker.
