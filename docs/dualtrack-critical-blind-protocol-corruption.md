# CRITICAL: automation writes AI-derived plans into the HUMAN slot

Found 2026-07-06 during a live-system audit (not caught by any test suite). This
is more severe than any prior DT finding — it corrupts the exact thing the whole
product exists to measure: an honest human-vs-AI track record.

## What's wrong

`DualTrackPlanStore.ensure_human_plan_from_market_view`
(`services/dualtrack_store.py:89`) derives a plan from the Obsidian market view
and writes it into the **human** plan slot with `author: "human"`, `source:
"obsidian"`. It is called from two places on the live/automated path:
- `DualTrackCycleRunner.pre_cycle` (`pipelines/dualtrack_cycle_runner.py:60`) —
  runs on every `auto` invocation (the scheduled cycle-boundary job).
- `DualTrackCycleRunner.sync_obsidian_human_plan` /
  `sync_obsidian_human_plans` (`pipelines/dualtrack_cycle_runner.py:184-199`) —
  runs every 5 minutes via `live_tick`.

Confirmed live 2026-07-06: `outputs/dualtrack/plans/2026-07-06_NIGHT_human.json`
was `status: "locked"`, `author: "human"`, `source: "obsidian"`,
`locked_at: 2026-07-06T03:03:05Z` — **hours before the owner would sit down to
do his blind questionnaire at 21:00 Beijing (13:00 UTC).** Because
`save_human_plan` correctly rejects mutation of an already-locked plan
(`dualtrack_store.py:35-40`, invariant 2), this means: **when the owner submits
his real blind plan tonight, the API rejects it with "human plan is locked and
immutable,"** and the machine track/scoreboard silently runs on an AI-derived
plan mislabeled as the owner's own judgment.

This is not a leak of AI content to the owner (invariant 1 territory) — it's
the inverse and worse: **AI content impersonating the owner's judgment in the
system of record.** Every closed cycle scored this way corrupts the human
scoreboard (`services/dualtrack_scoring.py`) with a false "human" data point.

## Why it happened (read of intent)

This looks like a well-meant "fallback pre-fill" — perhaps to give the console
something to show before the owner answers, or a convenience default. But the
existing, correct mechanism for exactly that purpose already exists:
`DualTrackPlanStore.ensure_ai_plan` writes to the **ai** slot
(`author: "ai"`), and `effective_plan()` (`dualtrack_store.py:103-113`) already
implements the fallback precedence correctly (human if locked-before-deadline,
else AI, else stand down — invariant 3, previously verified). Confirmed both
mechanisms coexist right now: `outputs/dualtrack/plans/2026-07-06_NIGHT_ai.json`
is correctly `author: ai, status: fallback_active, source: obsidian`. The new
human-slot writer is redundant with a mechanism that already works safely, and
duplicates it in a way that breaks the core invariant.

## Fix (required, not optional)

**Only `save_human_plan` — the path driven by a real console/API submission
from the owner — may ever write `author: "human"` to the plan store.** No
scheduled/automated code path may write to the human slot under any
circumstances, regardless of "draft" vs "locked" status intent.

Concretely:
- Delete `ensure_human_plan_from_market_view` (`dualtrack_store.py:89-129`) and
  every call site: `pre_cycle`'s `human_plan = self.store.ensure_human_plan_from_market_view(...)`
  call, `sync_obsidian_human_plan`, `sync_obsidian_human_plans`, and their
  invocation inside `live_tick`. The existing `ensure_ai_plan` +
  `effective_plan()` fallback chain already covers "no human answer → AI plan
  drives the machine, fail-closed if neither exists." Nothing is lost by
  removing this — it was solving a problem that didn't exist.
- If a "show something before the owner answers" UI affordance is wanted,
  render it from the **AI plan slot** (already correctly populated,
  already gated by `reveal_allowed` per invariant 1) — never write it into the
  human slot.

## Tests (must add)

- No scheduled/automated entrypoint (`pre_cycle`, `intraday_tick`,
  `close_cycle`, `auto`, `live_tick`, and any `sync_obsidian_*` method) may ever
  write `outputs/dualtrack/plans/{cycle_id}_human.json`. Assert file mtime is
  unchanged (or the file is absent) after calling each of these with no prior
  real submission.
- The owner's real submission via `save_human_plan` always succeeds when no
  prior real human submission exists for that cycle, regardless of how much
  automation has run beforehand.
- `effective_plan()` still correctly falls back to the AI plan when no locked
  human plan exists (regression check — this part was already correct, keep it
  exactly as is).

## What I did as an immediate mitigation (2026-07-06, by Claude, not Codex)

1. `launchctl bootout` both `com.wendy.trading-orchestrator.dualtrack-live-tick`
   and `...dualtrack-cycle` — **paused, not deleted**, the plists are untouched
   on disk. This stops the corrupting writes from recurring before tonight.
2. Deleted the corrupted `outputs/dualtrack/plans/2026-07-06_NIGHT_human.json`
   (mislabeled AI content, not the owner's real data — safe to remove).
3. Verified the owner's real submission path now succeeds (tested, then removed
   the test artifact).

**Automation is currently OFF.** Do not re-load the launchd jobs until this fix
lands and is verified, or the owner's blind plan will be at risk of being
overwritten/blocked again on the next scheduled tick.

## Not affected

Invariant 1 (blind-before-lock reveal to the owner), invariant 4 (intraday
machine PnL-only), DT6 replay closed-only gate, grid mechanics, sizing model —
all previously verified and unrelated to this finding.
