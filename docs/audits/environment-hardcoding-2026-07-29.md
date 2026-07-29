# Environment hardcoding audit — 2026-07-29

Issue: #422  
Baseline: `main@db66da1f4fdee93e1f8b2e647a2412fe1d269dc6`

## Scope and method

The repository was scanned for `/opt/homebrew`, `/Users/wendy`,
`/usr/bin/python3`, `Library/LaunchAgents`, `launchctl`, `osascript`, `plutil`,
`xattr`, `open -a`, and explicit Darwin platform branches. Generated runtime
data, Git internals and lockfiles were excluded. The initial scan returned 267
matches. Every matching file is classified below; repeated test fixtures and
historical evidence are grouped by file because their individual lines share
one disposition.

The executable Cloud closure is independently declared in
`services/cloud_linux_portability.py`. It is checked directly by
`python -m pipelines.cloud_linux_portability --json` and is also a mandatory
`linux_runtime_portability` check in the normal Cloud Paper preflight.

## Configurable active runtime paths

These were active or operator-facing defaults and were changed:

| File | Finding | Disposition |
|---|---|---|
| `configs/dualtrack.yaml` | Personal Nautilus runtime and newsletter paths | Runtime path is empty and must arrive through the existing environment contract; newsletter fallback is repository-relative. |
| `services/dualtrack_config.py` | Personal newsletter path | Replaced by `inputs/newsletters`. |
| `services/dualtrack_machine_plan.py` | Personal newsletter path | Replaced by a repository-relative default; explicit absolute configuration remains supported. |
| `services/strategy_recommendation.py` | Prepended `/opt/homebrew/bin` | Removed. The configured provider command resolves only through the service's actual `PATH`. |
| `services/codex_newsletter_strategy_proposal.py` | Prepended `/opt/homebrew/bin` | Removed for the same provider-boundary rule. |
| `pipelines/dashboard_server.py` | Personal Cloudflared log path | Replaced by `outputs/cloudflared.log`; `GOLDBOT_CLOUDFLARED_LOG` remains the explicit override. |
| `tests/browser_strategy_console_acceptance.mjs` | Personal evidence directory default | Replaced by `docs/evidence/browser`; environment override remains. |
| `tests/browser_a6_read_model_acceptance.mjs` | Personal evidence directory default | Replaced by `docs/evidence/browser`; environment override remains. |
| `scratch_repro_p1_daily_loss_window.py` | Personal `sys.path` injection | Deleted as stale, unreferenced reproduction scratch. |

## Intentionally retained macOS adapters

These paths and commands are valid only inside the explicit local launchd
adapter. They are excluded from the Linux Cloud runtime closure and retained
because they are still required for local failback/status operations:

- `services/schedule_installer.py`
- `services/schedule_manager.py`
- `services/schedule_status.py`
- `services/paper_service_rebootstrap.py`
- `pipelines/schedule_install.py`
- `pipelines/dualtrack_nautilus_cutover_apply.py`

`services/python_runtime_compatibility.py` retains `/usr/bin/python3` solely as
an explicit compatibility probe fallback. Managed jobs are still discovered
from their actual plist interpreter/PATH; the fallback is not a Cloud runtime
selection. `services/code_reload.py` contains `launchctl` only in explanatory
text.

## Retained test fixtures

The following files intentionally exercise macOS adapter generation, legacy
path rejection, or portability failures. Their literals are test data and are
not runtime defaults:

- `tests/test_schedule_manager.py`
- `tests/test_paper_service_rebootstrap.py`
- `tests/test_paper_predeploy_gate.py`
- `tests/test_paper_release_rollback_runbook.py`
- `tests/test_python_runtime_compatibility.py`
- `tests/test_dualtrack_nautilus_cutover_integration.py`
- `tests/test_dualtrack_nautilus_runtime_integration.py`
- `tests/test_market_data_access.py`
- `tests/test_safe_repair_queue.py`
- `tests/test_strategy_recommendation.py`
- `tests/test_cloud_linux_portability.py`

The new portability fixture deliberately embeds forbidden markers and proves
that the executable guard reports the exact file and line.

## Retained historical and platform documentation

These files preserve migration history, old failure evidence, or document the
Mac adapter itself. Rewriting them would erase provenance rather than improve
runtime portability:

- `decision-log.md`
- `REGISTRY.md`
- `implementation-notes.md`
- `codex-task-02-schedule-drift.md`
- `codex-task-04-focus-mode-schedule.md`
- `codex-task-04b-focus-mode-feed-gap.md`
- `docs/datafeed-boundary.md`
- `docs/dualtrack-critical-blind-protocol-corruption.md`
- `docs/dualtrack-nautilus-migration-plan.md`
- `docs/plans/2026-07-18-a1-market-data-envelope-cutover.md`
- `docs/plans/2026-07-18-a6-trading-system-read-model.md`
- `docs/plans/cloud-always-on-paper-2026-07-27.md`
- `docs/runbooks/launchd-python-compatibility.md`
- `docs/runbooks/paper-release-rollback-v1.md`
- `docs/tiger-openapi-integration.md`
- `docs/trading-system-ports-and-adapters-audit-2026-07-18.md`

## Result and operating rule

The declared Linux Cloud runtime closure has no macOS or personal absolute-path
dependency. A future match for Homebrew, a personal macOS home, macOS system
Python, LaunchAgents, `launchctl`, or `osascript` blocks Cloud Paper preflight
before service installation or boot evidence can pass.

This does not claim that every historical document is portable, and it does
not remove the local Mac failback adapter. The enforceable boundary is that
Linux Cloud production code cannot depend on either one.
