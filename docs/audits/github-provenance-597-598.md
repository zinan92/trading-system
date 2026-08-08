# Commit-SHA provenance for GitHub-missing PR objects #597 and #598

## Boundary

GitHub no longer resolves PR objects #597 and #598, but their GitHub-authored
merge commits and implementation parents remain reachable from `main`. This is
the same repository-level provenance limitation already documented for
[#52–#128](missing-issue-provenance-52-128.md) and
[#222–#329](github-provenance-222-329.md), not a one-off incident whose cause
can be inferred from the missing pages.

A PR number, merge-subject line, or remembered URL is not an audit credential.
For delivered code, the durable authority is the exact reachable commit SHA,
its tree, its parents, and proof that the merge commit is an ancestor of the
current protected branch. The reachable tracking record is
[#602](https://github.com/zinan92/trading-system/issues/602).

## Evidence index

| Missing PR object | Story | Implementation commit | Merge commit on `main` | Committed tree |
| --- | --- | --- | --- | --- |
| #597 | #594 canonical Paper StartFacts | [`67560110e845604b4cf114f5976ffe59037551a0`](https://github.com/zinan92/trading-system/commit/67560110e845604b4cf114f5976ffe59037551a0) | [`913afe978f378f0d3b5c8551b8ca92262a19e563`](https://github.com/zinan92/trading-system/commit/913afe978f378f0d3b5c8551b8ca92262a19e563) | `eadb8398528bad9714c316a30974e9851df474bc` |
| #598 | #595 verified next-cycle precompute | [`bdf81c41c986eda4e516e9a6c762871a2598aa8b`](https://github.com/zinan92/trading-system/commit/bdf81c41c986eda4e516e9a6c762871a2598aa8b) | [`c2e32b7fac9387e64f19c3a027d5fc452ba01d8c`](https://github.com/zinan92/trading-system/commit/c2e32b7fac9387e64f19c3a027d5fc452ba01d8c) | `536e89fc712b1c0417e598ad9ef40e98f88e9a36` |

For each row, the implementation and merge commits resolve to the same tree.
That proves the named implementation content is what the merge commit retained;
it does not recreate the missing review discussion, checks UI, labels, or
approval metadata.

## Reproduction

Positive repository evidence:

```bash
git show --no-patch --format='%H%n%P%n%T%n%cn <%ce>%n%s' \
  913afe978f378f0d3b5c8551b8ca92262a19e563 \
  c2e32b7fac9387e64f19c3a027d5fc452ba01d8c
git merge-base --is-ancestor \
  913afe978f378f0d3b5c8551b8ca92262a19e563 origin/main
git merge-base --is-ancestor \
  c2e32b7fac9387e64f19c3a027d5fc452ba01d8c origin/main
git rev-parse \
  67560110e845604b4cf114f5976ffe59037551a0^{tree} \
  913afe978f378f0d3b5c8551b8ca92262a19e563^{tree} \
  bdf81c41c986eda4e516e9a6c762871a2598aa8b^{tree} \
  c2e32b7fac9387e64f19c3a027d5fc452ba01d8c^{tree}
```

Negative PR-object readback:

```bash
gh api repos/zinan92/trading-system/pulls/597
gh api repos/zinan92/trading-system/pulls/598
```

The positive commands must succeed and the paired tree SHAs must match. The
negative commands currently return HTTP 404. If GitHub later restores either
object, record that new evidence without rewriting this historical finding.

## Reporting rule

- Cite exact commit URLs and #602; do not present `/pull/597` or `/pull/598` as
  a delivered link while readback fails.
- Do not infer direct-push, review status, or the cause of metadata loss from a
  missing PR object.
- Apply the same readback-before-reporting rule to every future Issue or PR.
  A number that cannot be read back is a hint, never the audit authority.
