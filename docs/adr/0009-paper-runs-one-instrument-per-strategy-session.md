---
status: accepted
---

# Paper runs one instrument at a time, chosen per Strategy Session

#1393 puts silver (`XAG-USDT-SWAP`) and crude (`CL-USDT-SWAP`) on Paper, using
OKX public data and no account.

Paper is built around one gold instrument:

- `paper_gold_venue.active` is one config key, read when config loads. Bars, the
  instrument definition and the execution instrument all derive from it.
- `execution_shadow.nautilus.instrument_endpoint` and `source_symbol` name gold
  and are not derived.
- The market route is keyed `GOLD`.
- The desk writes 「纸面黄金」 into the Paper drafter's message.
- The desk refuses a second grid: 「一次只跑一个」.

ADR 0005 says concurrent multi-asset execution needs a Portfolio Coordinator
first. That contract exists for the Testnet path, not for Paper.

## Decision

**One Paper instrument at a time.** A Strategy Session runs one instrument. A
second instrument waits until Paper is idle. This does not change the 「一次只跑一个」
rule, and it does not start ADR 0005's concurrent model.

**The instrument belongs to the session, not to the process.** Starting a session
records its instrument, the way a session already records `paper_gold_venue`.
Market reads, the contract, fees and fill receipts come from that record. No
restart or deploy is needed to change instrument. A running session is never
changed.

**Bars, definition and execution stay from one source.** The `require_instrument_matches`
check applies to every instrument. There is no fallback: if OKX public data is
missing or stale, that instrument cannot open new orders.

**Contract specs come from the datafeed.** Gold's OKX profile hardcodes its
increments. Silver and crude read theirs from the instrument definition, and an
unreadable definition blocks the instrument. Quantity is in the base unit;
`contract_multiplier` is `1` for all three on 2026-11-07, and a different value
blocks the instrument until it is handled.

**Fees are an assumption.** Silver and crude reuse the OKX Paper rates, with
`verified: false`, shown as 「未核实」.

## Order of work

1. This ADR (#1404).
2. The instrument table; gold's behaviour is unchanged (#1405).
3. The runtime reads the instrument from the session (#1406).
4. The catalog and the desk offer silver and crude (#1407).

## Consequences

- Park can run one Paper grid at a time, across all instruments.
- `gm-desk.js` must stop treating every `.paper` venue as gold: `selectionAsset()`
  and `paperGoldIds()` key on the instrument id (#1407).
- Each instrument needs its own plan template. Gold's 10 rungs of 0.2 oz would be
  about $10 of silver per rung.
- A preview's maximum loss is tested against a hand calculation for one silver and
  one crude plan.
- Moving silver and crude off `okx_public` is a desk migration, so calls recorded
  earlier keep scoring on their recorded feed.
- Verification uses mocks and dev. No silver or crude grid is started in production
  as part of this work.

## Considered options

- **Rejected: concurrent instruments now.** It needs the Portfolio Coordinator,
  with shared risk limits, before any order. That is much larger than #1393.
- **Rejected: a config switch and a restart.** Changing instrument would need a
  deploy, so the desk could not offer it.
- **Rejected: a separate simulator for silver and crude.** It would be a second
  order path with its own fills.
