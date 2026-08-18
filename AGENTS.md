# trading-system working rules

## Safety boundary

- This repository is Paper-first. Do not enable, submit, cancel, or close a
  real-money order, and never read or print exchange credentials.
- A market/datafeed failure blocks new entries. Last trusted chart data is
  view-only evidence, not authority to trade.
- Treat a Paper lifecycle receipt as evidence only for the recorded action; it
  is not proof of a live venue action.

## Delivery contract

- Start every implementation from a GitHub Issue. Use one issue, branch, and
  PR; include focused validation and `Closes #N` in the PR body.
- Before reporting a new Issue or PR link, switch the active `gh` account to
  `zinan92` and read that exact object back through the GitHub API. If the API
  returns 404, report the reachable commit and tracking Issue instead of a
  non-existent link.
- Small UI or adapter changes use focused tests. Run the complete suite only
  for an explicitly broad integration change.
- After non-live code merges, deploy only the exact main commit to Goldbot V5
  Paper and verify the changed read/control flow. Do not restart or alter an
  order lifecycle merely to deploy presentation code.

## Evidence and documentation

- Update `REGISTRY.md` and `decision-log.md` for every merged Issue. Record
  decision, gotchas, and verification; never report an unverified runtime
  state as healthy.
- Preserve user/runtime artifacts and unrelated working-tree changes. Stage
  explicit paths only; do not use `git add -A`.

## Agent skills

### Issue tracker

Issues and specs are tracked in GitHub Issues for `zinan92/trading-system`.
See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five canonical triage labels defined in
`docs/agents/triage-labels.md`.

### Domain docs

This repository uses a single-context domain model. Read `CONTEXT.md` and
relevant records under `docs/adr/` before planning or implementation. See
`docs/agents/domain.md`.
