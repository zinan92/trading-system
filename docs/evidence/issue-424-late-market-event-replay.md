# Issue #424 — late market event replay root cause

## Runtime evidence

On 2026-07-29 the Cloud Paper tick repeatedly failed closed with:

```text
immutable fill history regressed;
missing=[];
changed=['nautilus-command-f791ef735781b5b66b70']
```

The persisted fill and a fresh replay had exactly one differing economic field:

```text
ts: 2026-07-29T02:41:00+00:00
 -> 2026-07-29T02:31:00+00:00
```

The entry command was a sell limit at 4039.64. The processed-event ledger
showed that the 02:30 candle (canonical execution timestamp 02:31) had not been
present when the fill was committed at 02:41. It appeared later as the sole
pending event, behind an execution watermark already advanced through 02:43.
Full-history replay sorted the delayed candle by market timestamp and therefore
retroactively moved the fill ten minutes earlier. The immutable-fill guard
correctly rejected that revised history.

## Resolution

Raw events remain append-only audit facts. Before replay, pending events at or
behind the latest accepted execution timestamp are classified
`late_ignored` and excluded from the execution replay. Their ID, timestamp,
watermark and reason are appended to `processed_events`; no event, command,
fill, trade or cycle package is rewritten.

Previously accepted events remain replay inputs. New events strictly after the
watermark retain normal behavior. Immutable-fill comparison remains unchanged
and fail-closed.

## Regression contract

A focused test first commits a fill from a 01:41 event, then delivers a 01:31
event. It proves that:

- both raw market events remain stored;
- replay receives only the accepted 01:41 event;
- the persisted fill is byte-for-byte unchanged; and
- the delayed event receives an explicit `late_ignored` audit disposition.
