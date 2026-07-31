from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.cloud_health import CloudPaperHealth, _hash_json
from services.cloud_health import health_severity
from services.cycle_decision import CycleDecisionLedger
from services.journal_store import write_json


NOW = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
SHA = "a" * 40


def _healthy(tmp_path: Path) -> CloudPaperHealth:
    output = tmp_path / "outputs"
    backup = tmp_path / "backups"
    write_json(
        output / "dualtrack" / "runner" / "2026-07-28_DAY.json",
        [{"event": "live_tick_heartbeat", "ts": "2026-07-28T01:59:00+00:00"}],
    )
    write_json(
        output / "dualtrack" / "strategy_control" / "runtime.json",
        [
            {
                "cycle_id": "2026-07-28_DAY",
                "actual_state": "stopped",
                "accepted_order_count": 0,
                "accepted_order_count_known": True,
                "previous_runtime_unresolved": False,
            }
        ],
    )
    CycleDecisionLedger(output).record(
        {
            "cycle_id": "2026-07-28_DAY",
            "recorded_at": "2026-07-28T01:59:00+00:00",
            "source": "auto_ai",
            "outcome": "executed",
            "strategy_plan_id": "plan-1",
        }
    )
    write_json(
        output
        / "dualtrack"
        / "nautilus_authoritative"
        / "snapshots"
        / "2026-07-28_DAY.json",
        [{"reconciliation": {"status": "ok"}}],
    )
    review = {
        "report_date": "2026-07-27",
        "status": "complete",
    }
    review["review_hash"] = _hash_json(review)
    write_json(
        output / "dualtrack" / "daily_self_reviews" / "current.json",
        [review],
    )
    write_json(
        output / "cloud" / "scheduler_ownership" / "current.json",
        [
            {
                "status": "active",
                "active_owner_id": "cloud-primary",
                "epoch": 3,
            }
        ],
    )
    write_json(
        backup / "current.json",
        [
            {
                "status": "pass",
                "backup_id": "backup-20260728T013000Z-safe",
                "created_at": "2026-07-28T01:30:00+00:00",
                "manifest_hash": "manifest-hash",
            }
        ],
    )
    return CloudPaperHealth(
        output_root=output,
        backup_root=backup,
        owner_id="cloud-primary",
        deployed_sha=SHA,
        now=lambda: NOW,
        latest_market_provider=lambda: {
            "timestamp": "2026-07-28T01:59:00+00:00",
            "provider": "binance_usdm_futures",
        },
    )


def test_cloud_health_separates_all_ready_layers(tmp_path: Path) -> None:
    health = _healthy(tmp_path)

    result = health.run()

    assert result["status"] == "healthy"
    assert set(result["checks"]) == {
        "datafeed",
        "live_tick",
        "execution",
        "cycle_decision",
        "reconciliation",
        "daily_self_review",
        "backup",
        "scheduler_ownership",
        "source",
    }
    assert all(row["status"] == "ready" for row in result["checks"].values())
    assert result["dashboard_reachable_is_not_system_health"] is True
    assert result["control_actions_executed"] == 0
    assert result["secrets_included"] is False
    assert result["severity"] == "none"
    assert all(row["severity"] == "none" for row in result["checks"].values())


def test_health_severity_is_explicit_and_unknown_fails_closed() -> None:
    assert health_severity("daily_self_review_missing_or_incomplete") == "warning"
    assert health_severity("backup_missing_or_stale") == "warning"
    assert health_severity("runtime_utilization_insufficient") == "insufficient"
    assert health_severity("new_unclassified_condition") == "critical"


def test_missing_cycle_decision_is_blocked_and_detectable(tmp_path: Path) -> None:
    health = _healthy(tmp_path)
    health.output_root.joinpath(
        "dualtrack",
        "strategy_control",
        "cycle_decisions",
        "2026-07-28_DAY.json",
    ).unlink()

    result = health.run()

    assert result["status"] == "blocked"
    assert result["checks"]["cycle_decision"]["code"] == "cycle_decision_missing"


def test_active_plan_stopped_past_deadline_is_detected_for_clock_cycle(
    tmp_path: Path,
) -> None:
    health = _healthy(tmp_path)
    health.output_root.joinpath(
        "dualtrack",
        "strategy_control",
        "cycle_decisions",
        "2026-07-28_DAY.json",
    ).unlink()
    write_json(
        health.output_root
        / "dualtrack"
        / "strategy_control"
        / "plans"
        / "2026-07-28_DAY.json",
        [
            {
                "strategy_plan_id": "stalled-plan",
                "cycle_id": "2026-07-28_DAY",
                "status": "active",
                "locked_at": "2026-07-28T01:50:00+00:00",
            }
        ],
    )
    write_json(
        health.output_root
        / "dualtrack"
        / "strategy_control"
        / "runtime.json",
        [
            {
                "cycle_id": "2026-07-27_NIGHT",
                "desired_state": "stopped",
                "actual_state": "stopped",
                "accepted_order_count": 0,
            }
        ],
    )

    result = health.run()

    decision = result["checks"]["cycle_decision"]
    assert decision["status"] == "blocked"
    assert decision["code"] == "cycle_decision_stalled_after_plan_activation"
    assert decision["evidence"]["strategy_plan_id"] == "stalled-plan"
    assert decision["evidence"]["plan_age_seconds"] == 600


def test_stale_tick_is_blocked_with_stage_and_next_action(tmp_path: Path) -> None:
    health = _healthy(tmp_path)
    write_json(
        health.output_root / "dualtrack" / "runner" / "2026-07-28_DAY.json",
        [{"event": "live_tick_heartbeat", "ts": "2026-07-28T01:40:00+00:00"}],
    )

    result = health.run()

    assert result["status"] == "blocked"
    assert result["checks"]["live_tick"]["code"] == "live_tick_stale"
    assert result["checks"]["live_tick"]["next_action"]


def test_missing_review_and_backup_are_degraded_not_fabricated_healthy(
    tmp_path: Path,
) -> None:
    health = _healthy(tmp_path)
    (
        health.output_root / "dualtrack" / "daily_self_reviews" / "current.json"
    ).unlink()
    (health.backup_root / "current.json").unlink()

    result = health.run()

    assert result["status"] == "degraded"
    assert result["checks"]["daily_self_review"]["status"] == "degraded"
    assert result["checks"]["daily_self_review"]["evidence"]["review_status"] == "missing"
    assert result["checks"]["backup"]["status"] == "degraded"
    assert result["checks"]["backup"]["evidence"]["created_at"] is None
    assert result["checks"]["daily_self_review"]["severity"] == "warning"
    assert result["checks"]["backup"]["severity"] == "warning"
    assert result["severity"] == "warning"


def test_supervisor_mode_replaces_legacy_cycle_decision_check(
    tmp_path: Path, monkeypatch
) -> None:
    health = _healthy(tmp_path)
    write_json(
        health.output_root
        / "dualtrack"
        / "strategy_control"
        / "plans"
        / "2026-07-28_DAY.json",
        [
            {
                "strategy_plan_id": "active-plan",
                "cycle_id": "2026-07-28_DAY",
                "status": "active",
            }
        ],
    )
    write_json(
        health.output_root / "dualtrack" / "strategy_control" / "runtime.json",
        [
            {
                "cycle_id": "2026-07-28_DAY",
                "actual_state": "stopped",
                "desired_state": "running",
                "accepted_order_count": 0,
                "accepted_order_count_known": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.cloud_health.dualtrack_config",
        lambda: {"convergence": {"mode": "supervisor"}},
    )

    result = health.run()

    assert "supervisor" in result["checks"]
    assert "cycle_decision" not in result["checks"]
    assert result["checks"]["supervisor"]["severity"] == "critical"
    assert result["checks"]["supervisor"]["code"] == "supervisor_observation_missing"


def test_scheduler_owner_mismatch_blocks_cloud_health(tmp_path: Path) -> None:
    health = _healthy(tmp_path)
    write_json(
        health.output_root / "cloud" / "scheduler_ownership" / "current.json",
        [{"status": "active", "active_owner_id": "local-mac", "epoch": 3}],
    )

    result = health.run()

    assert result["status"] == "blocked"
    owner = result["checks"]["scheduler_ownership"]
    assert owner["code"] == "scheduler_owner_mismatch"
    assert owner["evidence"]["active_owner_id"] == "local-mac"


def test_datafeed_probe_failure_is_bounded_and_secret_free(tmp_path: Path) -> None:
    health = _healthy(tmp_path)
    health.latest_market_provider = lambda: (_ for _ in ()).throw(
        RuntimeError("https://secret.invalid/token")
    )

    result = health.run()

    assert result["checks"]["datafeed"]["summary"].endswith("RuntimeError")
    assert "secret.invalid" not in str(result)
