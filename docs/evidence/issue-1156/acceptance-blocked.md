# Issue #1156 acceptance evidence

## Local compact contract check

Command:

```text
node compact Confirm body check
```

Result: PASS. The Confirm handler contains the top-level `preview_digest`,
`acknowledged`, `operator_id`, and `statement` fields, and does not contain the
old `preview` request field. The success and blocker UI tokens are present.

## #1155 browser acceptance

The fetched `origin/codex/issue-1149-dashboard-e2e` script was run unchanged:

```text
git show origin/codex/issue-1149-dashboard-e2e:tests/e2e/dashboard_confirm_flow.py \
  | PYTHONPATH=src python3 - --base http://127.0.0.1:8766 \
  --evidence-dir docs/evidence/issue-1156
```

Result: BLOCKED before Preview/Confirm:

```text
dashboard-confirm-flow: FAIL: Testnet account admission was not READY: Account BLOCKED · testnet_account_unavailable
```

The isolated server used a temporary output root and local-only process. No
launchd service, live path, credential, or order lifecycle was accessed.
