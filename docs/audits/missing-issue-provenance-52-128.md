# Provenance index for GitHub-missing historical references #52–#128

## Boundary

GitHub no longer resolves the historical issue/PR numbers in this interval.
This index does **not** recreate Issues, claim their original text, or infer
their original labels/assignees. It records only repository-local evidence that
can still be independently verified by commit hash, tracked file history, and
the current decision log.

When this document says “unrecoverable”, it means the original GitHub Issue
body/comments cannot be verified from the repository. It does not mean the
related code did not exist.

## Evidence index

| Historical reference(s) | Verifiable repository evidence | What it establishes | What remains unrecoverable |
| --- | --- | --- | --- |
| #52–#66 | No individual merge/title mapping found in the retained Git history. | Only that the numbers are referenced as missing historical provenance. | Original Issue intent, acceptance criteria, discussions, and closure state. |
| #67, #69–#80 → #81 | `006bd2b` — “集成:落地 13 个 Ready PR(#67, #69-#80)——重挂/生命周期/复盘/Shadows/图表交互 (#81)”. | The listed Ready-PR work was integrated as a batch; lifecycle, review, Shadows, and chart interaction are the stated scope. | Individual PR discussion/review evidence and why #68 was not in that batch. |
| #68 | `ecfb703` — “Keep historical K-lines readable without weakening live trade gates (#68)”. | Historical-K-line work was separately retained with live entry gates unchanged. | The original Issue narrative and review. |
| #70–#76 | `fb4d6d3`, `2089426`, `78b4ccf`, `88aa46d`, `c1c2596`, `1d16b2c`, `b76ae60`. | Daily NAV, chart scaling, auditable counts, market/runtime header, lifecycle notifications, review/Shadows, and production summary each have a retained implementation commit. | The GitHub source items and exact acceptance wording. |
| #77–#80 | `c882d81`, `2f161ed`, `4a3c729`. | Range adjustment, preview safety, and atomic Paper-grid replacement have retained commits. | Original Issue comments and reviewer approval history. |
| #82 | `edb4b55`; decision log entry “#82”. | The Python 3.9 compatibility repair was a real implementation and has a later decision-log backfill. | Original PR/Issue conversation. |
| #83–#88 | `d2ed9d9`, `27dd2da`, `dac5137`, `5d10840`, and the #88 series ending `c620ec2`. | The 10 USD / 10x controls, Paper-grid validation, cancellation receipts, and Paper user acceptance were retained. | Individual GitHub tickets and review trail. |
| #89–#96 | `6963656`, `c620ec2`, `2020f81`, `62910a0`; decision log entry “#96”. | Paper control/E2E integration, risk acknowledgement, and authoritative trade toasts have repository evidence. | Original GitHub history, including whether #92/#93 were originally split. |
| #97–#100 | `d8482ef` is the retained #98 manual-parameter implementation. No individual retained mapping was found for #97, #99, or #100. | #98 exists as a concrete commit; the other numbers must not be treated as resolved merely from adjacency. | All original Issue content for #97/#99/#100. |
| #101–#110 | `deb7cca`, `346be6a`, `38a2466`, `707f4b5`, `ec460eb`. | History pagination, crosshair time placement, single-side semantics, and range-confirm CTA changes are retained. | Original tickets, review, and any missing #103/#104 narrative. |
| #112–#128 | `3189d92`, `2cf4703`, `0b81687`, `5e83d3f`, `814b895`, `ea6457e`; decision-log backfill includes #126. | Dashboard alignment, order/read-model/UI, human start blockers, and registry reconciliation are retained. | Original GitHub artifacts and missing-number context (#111/#113/#115/#117/#119/#121/#122/#125/#127). |

## How to verify

```bash
git log --all --oneline --grep='#81\|#82\|#88\|#96\|#98\|#101\|#102\|#110\|#128'
git show --stat 006bd2b ecfb703 edb4b55 c620ec2 62910a0 deb7cca ea6457e
rg -n '## .*#82|## .*#96|## .*#126' decision-log.md
```

## Maintenance rule

New implementation work must use currently resolvable GitHub Issues and PRs.
When a historical number is mentioned, link here and cite the commit or
decision-log section rather than treating a 404 as proof that nothing shipped.
