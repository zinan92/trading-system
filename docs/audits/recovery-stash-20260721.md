# Recovery stash audit — 2026-07-21

## Scope and method

This is a read-only, three-way audit of recovery commit
`4da758e` (`codex/recovery-stash-20260721`), its parent
`f892d22`, and `main@5ec5198` (the audit baseline). The recovery branch stays
intact as the immutable source artifact; no code is copied from it in this
change.

The handoff called out exactly four production files. Other files in the
snapshot are outside this audit and remain recoverable on the branch.

| Recovery file | Snapshot delta | Main comparison and evidence | Disposition |
| --- | --- | --- | --- |
| `pipelines/bot.py` | Catches `DatafeedUnavailable` around the legacy Binance feed and writes a synthetic `fail` row so the rest of the legacy bot cycle continues. | Paper execution now reads its market boundary through `services/dualtrack_market_feed.py`; an unavailable upstream returns an explicit `status=blocked`, `fresh=false` payload with the upstream errors. The active Paper tick has separate heartbeat and lifecycle gates. | **Retired — intentionally not migrated.** Continuing a legacy bot cycle after a market failure is the wrong control point and could disguise a Paper execution blocker. |
| `services/data_health.py` | Converts errors from the former datafeed-backed row loader into a health error document. | Main’s data-health auditor reads the independent market-data repository, reports no-bars/staleness as health failures, and its maintenance calls are explicitly read-only when the datafeed owns the store. It is dashboard/diagnostic evidence, not the Paper entry gate. | **Retired — superseded by the authoritative market-feed and health projections.** Do not restore a broad catch here without a separate contract for health artifact semantics. |
| `services/datafeed_market_client.py` | Moves `urlopen` resolution from a default argument to construction time so monkeypatch-based outage drills can intercept it. | Main retains explicit `opener` injection on `DatafeedMarketClient`; all production callers can supply an adapter seam and the runtime returns `DatafeedUnavailable` on HTTP/network/format errors. The snapshot’s import-time monkeypatch convenience is not needed by that contract. | **Retired — test-seam-only delta.** A future change to default-opener binding needs its own testability Issue, not a recovery copy. |
| `services/trade_record_acceptance.py` | Adds `latest_price_evidence` instead of a bare latest price and turns datafeed lookup errors into an unknown-price card state. | Main trade-record acceptance is a historical/read-only sampled-card audit; Paper control and Dashboard positions/orders/fills use canonical accounting/read model rather than this report. The snapshot would change its evidence schema without a current consumer or migration. | **Retired — incompatible reporting-schema change.** Preserve it on the recovery branch; reopen only with a consumer and a versioned report contract. |

## Evidence commands

```bash
git show --name-status 4da758e
git diff 4da758e^ 4da758e -- pipelines/bot.py services/data_health.py \
  services/datafeed_market_client.py services/trade_record_acceptance.py
git show origin/main:services/dualtrack_market_feed.py
```

## Safety conclusion

All four recovered deltas are either on a legacy/read-only path or would alter
a current schema without a consumer. None should be deployed by copying from a
stash. The recovery branch remains retained for provenance; current Paper
execution remains governed by the mainline `DualTrackMarketFeed`, tick-health,
and canonical-accounting contracts.
