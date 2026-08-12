from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.cloud_health import CloudPaperHealth, _hash_json, health_severity
from services.cycle_decision import CycleDecisionLedger
from services.deadman_ping import ExternalDeadmanPing
from services.journal_store import write_json

NOW = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
CYCLE_START = datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc)
CYCLE_ID = "2026-07-28_DAY"
SHA = "a" * 40


def _provider_timer_contract(
    *,
    timer_status: str = "pass",
    current_ok: bool = True,
) -> dict:
    return {
        "status": timer_status,
        "checks": [
            {
                "unit": "gridmind-ai-provider-readiness.timer",
                "status": timer_status,
                "fragment_path": (
                    "/etc/systemd/system/"
                    "gridmind-ai-provider-readiness.timer"
                ),
                "effective_unit_content_sha256": "f" * 64,
                "next_trigger": "Fri 2026-08-07 10:00:00 CST",
                "last_trigger": "Thu 2026-08-06 04:00:00 CST",
            }
        ],
        "provider_readiness": {
            "current": {
                "ok": current_ok,
                **(
                    {}
                    if current_ok
                    else {
                        "blocker": "cloud_ai_provider_readiness_not_passing",
                        "failure_code": (
                            "strategy_recommendation_provider_timeout"
                        ),
                    }
                ),
            },
            "latest_success": {
                "ok": True,
                "readiness_digest": "c" * 64,
                "source_sha": SHA,
            },
        },
    }


def _healthy(
    tmp_path: Path,
    *,
    now: datetime = NOW,
) -> CloudPaperHealth:
    output = tmp_path / "outputs"
    backup = tmp_path / "backups"
    fresh_at = (now - timedelta(minutes=1)).isoformat()
    write_json(
        output / "dualtrack" / "runner" / "2026-07-28_DAY.json",
        [{"event": "live_tick_heartbeat", "ts": fresh_at}],
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
                "created_at": (now - timedelta(minutes=30)).isoformat(),
                "manifest_hash": "manifest-hash",
            }
        ],
    )
    return CloudPaperHealth(
        output_root=output,
        backup_root=backup,
        owner_id="cloud-primary",
        deployed_sha=SHA,
        now=lambda: now,
        latest_market_provider=lambda: {
            "timestamp": fresh_at,
            "provider": "binance_usdm_futures",
        },
        timer_contract_provider=_provider_timer_contract,
    )


def _force_legacy_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.cloud_health.dualtrack_config",
        lambda: {"convergence": {"mode": "legacy_cycle_decision"}},
    )


def _set_supervisor_runtime(
    health: CloudPaperHealth,
    *,
    cycle_id: str = CYCLE_ID,
    running: bool = False,
    with_plan: bool = True,
) -> None:
    if with_plan:
        write_json(
            health.output_root
            / "dualtrack"
            / "strategy_control"
            / "plans"
            / f"{cycle_id}.json",
            [{
                "cycle_id": cycle_id,
                "strategy_plan_id": "active-plan",
                "strategy_plan_version": 2,
                "version": 2,
                "status": "active",
            }],
        )
    write_json(
        health.output_root
        / "dualtrack"
        / "strategy_control"
        / "runtime.json",
        [{
            "cycle_id": cycle_id,
            "strategy_plan_id": "active-plan" if with_plan else None,
            "strategy_plan_version": 2 if with_plan else None,
            "desired_state": "running" if running else "stopped",
            "actual_state": "running" if running else "stopped",
            "accepted_order_count": 38 if running else 0,
            "accepted_order_count_known": True,
        }],
    )


def _supervisor_observation(
    at: datetime,
    *,
    running_proven: bool,
    status: str = "observed",
) -> dict:
    return {
        "recorded_at": at.isoformat(),
        "payload": {
            "status": status,
            "running_evidence": {
                "cycle_id": CYCLE_ID,
                "evidence_at": at.isoformat(),
                "running_proven": running_proven,
                "plan_identity": {
                    "strategy_plan_id": "active-plan",
                    "strategy_plan_version": 2,
                },
                "runtime": {
                    "strategy_plan_id": "active-plan",
                    "strategy_plan_version": 2,
                },
            },
        },
    }


def _supervisor_model(
    at: datetime,
    *,
    mode: str = "ready",
    running_proven: bool = False,
    alert_required: bool = False,
    blocker: dict | None = None,
    observations: list[dict] | None = None,
    attempt_at: datetime | None = None,
    utilization: dict | None = None,
) -> dict:
    rows = observations or [
        _supervisor_observation(at, running_proven=running_proven)
    ]
    attempt_time = attempt_at or at
    latest_evidence = dict(rows[-1]["payload"]["running_evidence"])
    last_running = next(
        (
            dict(row["payload"]["running_evidence"])
            for row in reversed(rows)
            if row["payload"]["running_evidence"]["running_proven"] is True
        ),
        None,
    )
    return {
        "status": "available",
        "source_errors": [],
        "current_cycle": {
            "cycle_id": CYCLE_ID,
            "status": "available",
            "attempt_count": len(rows),
            "start_intent_count": 0,
            "last_observed_at": rows[-1]["recorded_at"],
            "last_attempt": {
                "observed_at": attempt_time.isoformat(),
                "result": "no_action",
            },
            "running_evidence": latest_evidence,
            "last_running_proof": (
                {
                    "cycle_id": last_running["cycle_id"],
                    "evidence_at": last_running["evidence_at"],
                    "plan_identity": last_running["plan_identity"],
                    "runtime": last_running["runtime"],
                }
                if last_running is not None
                else None
            ),
            "episode": {
                "mode": mode,
                "alert_required": alert_required,
                "blocker": blocker,
            },
            "history": {"observations": rows},
        },
        "utilization": utilization or {"windows": {}},
    }


def _install_supervisor_model(monkeypatch, model: dict) -> None:
    monkeypatch.setattr(
        "services.paper_supervisor_read_model.build_paper_supervisor_polling_summary",
        lambda *_args, **_kwargs: model,
    )


def test_cloud_health_separates_all_ready_layers(tmp_path: Path, monkeypatch) -> None:
    health = _healthy(tmp_path)
    _force_legacy_mode(monkeypatch)

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
        "provider_readiness",
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
    assert health_severity("provider_readiness_refresh_failed") == "warning"
    assert health_severity("new_unclassified_condition") == "critical"


def test_provider_readiness_refresh_failure_is_warning_until_it_blocks_convergence(
    tmp_path: Path,
) -> None:
    health = _healthy(tmp_path)
    health.timer_contract_provider = lambda: _provider_timer_contract(
        current_ok=False
    )

    result = health._provider_readiness_timer()

    assert result["status"] == "degraded"
    assert result["code"] == "provider_readiness_refresh_failed"
    assert result["severity"] == "warning"
    assert result["evidence"]["latest_success"]["ok"] is True


def test_provider_readiness_timer_inactive_is_immediately_critical(
    tmp_path: Path,
) -> None:
    health = _healthy(tmp_path)
    health.timer_contract_provider = lambda: _provider_timer_contract(
        timer_status="blocked"
    )

    result = health._provider_readiness_timer()

    assert result["status"] == "blocked"
    assert result["code"] == "provider_readiness_timer_unavailable"
    assert result["severity"] == "critical"


def test_missing_cycle_decision_is_blocked_and_detectable(
    tmp_path: Path, monkeypatch
) -> None:
    health = _healthy(tmp_path)
    _force_legacy_mode(monkeypatch)
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
    tmp_path: Path, monkeypatch
) -> None:
    health = _healthy(tmp_path)
    _force_legacy_mode(monkeypatch)
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


def test_stale_tick_is_blocked_with_stage_and_next_action(
    tmp_path: Path, monkeypatch
) -> None:
    health = _healthy(tmp_path)
    _force_legacy_mode(monkeypatch)
    write_json(
        health.output_root / "dualtrack" / "runner" / "2026-07-28_DAY.json",
        [{"event": "live_tick_heartbeat", "ts": "2026-07-28T01:40:00+00:00"}],
    )

    result = health.run()

    assert result["status"] == "blocked"
    assert result["checks"]["live_tick"]["code"] == "live_tick_stale"
    assert result["checks"]["live_tick"]["next_action"]


def test_missing_review_and_backup_are_degraded_not_fabricated_healthy(
    tmp_path: Path, monkeypatch
) -> None:
    health = _healthy(tmp_path)
    _force_legacy_mode(monkeypatch)
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
        lambda: {"convergence": {"mode": "paper_supervisor"}},
    )

    result = health.run()

    assert "supervisor" in result["checks"]
    assert "cycle_decision" not in result["checks"]
    assert result["checks"]["supervisor"]["severity"] == "critical"
    assert result["checks"]["supervisor"]["code"] == (
        "supervisor_convergence_stalled"
    )


def test_supervisor_health_uses_current_cycle_when_runtime_is_stale(
    tmp_path: Path, monkeypatch
) -> None:
    health = _healthy(tmp_path)
    _force_legacy_mode(monkeypatch)
    monkeypatch.setattr(
        "services.cloud_health.dualtrack_config",
        lambda: {"convergence": {"mode": "paper_supervisor"}},
    )
    write_json(
        health.output_root
        / "dualtrack"
        / "strategy_control"
        / "plans"
        / "2026-07-28_DAY.json",
        [{"cycle_id": "2026-07-28_DAY", "status": "active"}],
    )
    write_json(
        health.output_root / "dualtrack" / "strategy_control" / "runtime.json",
        [{"cycle_id": "2026-07-27_NIGHT", "actual_state": "stopped"}],
    )

    result = health.run()

    supervisor = result["checks"]["supervisor"]
    assert supervisor["evidence"]["cycle_id"] == "2026-07-28_DAY"
    assert supervisor["code"] == "supervisor_convergence_stalled"


def test_running_runtime_does_not_hide_supervisor_structural_blocker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    health = _healthy(tmp_path)
    write_json(
        health.output_root
        / "dualtrack"
        / "strategy_control"
        / "plans"
        / "2026-07-28_DAY.json",
        [{
            "cycle_id": "2026-07-28_DAY",
            "strategy_plan_id": "active-plan",
            "status": "active",
        }],
    )
    write_json(
        health.output_root
        / "dualtrack"
        / "strategy_control"
        / "runtime.json",
        [{
            "cycle_id": "2026-07-28_DAY",
            "actual_state": "running",
            "desired_state": "running",
            "accepted_order_count": 38,
        }],
    )
    monkeypatch.setattr(
        "services.cloud_health.dualtrack_config",
        lambda: {"convergence": {"mode": "paper_supervisor"}},
    )
    monkeypatch.setattr(
        "services.paper_supervisor_read_model.build_paper_supervisor_polling_summary",
        lambda *_args, **_kwargs: {
            "status": "available",
            "source_errors": [],
            "current_cycle": {
                "status": "available",
                "attempt_count": 4,
                "start_intent_count": 1,
                "last_observed_at": "2026-07-28T01:59:00+00:00",
                "last_attempt": {
                    "observed_at": "2026-07-28T01:59:00+00:00",
                },
                "episode": {
                    "mode": "blocked_structural",
                    "alert_required": True,
                    "blocker": {
                        "machine_code": "ledger_reconciliation_drift",
                        "classification": "structural",
                    },
                },
            },
            "utilization": {"windows": {}},
        },
    )

    result = health.run()

    supervisor = result["checks"]["supervisor"]
    assert result["status"] == "blocked"
    assert result["severity"] == "critical"
    assert supervisor["code"] == "ledger_reconciliation_drift"
    assert supervisor["severity"] == "critical"
    assert supervisor["evidence"]["runtime_running"] is True
    assert (
        supervisor["evidence"]["blocker_machine_code"]
        == "ledger_reconciliation_drift"
    )


def test_running_runtime_is_ready_only_after_fresh_healthy_supervisor_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    health = _healthy(tmp_path)
    _set_supervisor_runtime(health, running=True)
    monkeypatch.setattr(
        "services.cloud_health.dualtrack_config",
        lambda: {"convergence": {"mode": "paper_supervisor"}},
    )
    read_model_calls: list[dict] = []

    def supervisor_read_model(*_args, **kwargs) -> dict:
        read_model_calls.append(dict(kwargs))
        model = _supervisor_model(
            NOW - timedelta(minutes=1),
            running_proven=True,
            utilization={
                "windows": {
                    "24h": {
                        "evidence_status": "complete",
                        "conservative_percentage": 90,
                    }
                }
            },
        )
        model["current_cycle"]["attempt_count"] = 4
        model["current_cycle"]["start_intent_count"] = 1
        return model

    monkeypatch.setattr(
        "services.paper_supervisor_read_model.build_paper_supervisor_polling_summary",
        supervisor_read_model,
    )

    result = health.run()

    supervisor = result["checks"]["supervisor"]
    assert supervisor["status"] == "ready"
    assert supervisor["code"] == "supervisor_running"
    assert supervisor["severity"] == "none"
    assert supervisor["evidence"]["current_cycle_summary"][
        "attempt_count"
    ] == 4
    assert supervisor["evidence"]["runtime_utilization"]["windows"][
        "24h"
    ]["conservative_percentage"] == 90
    assert read_model_calls == [
        {
            "cycle_id": "2026-07-28_DAY",
            "as_of": NOW,
            "utilization": None,
            "count_summary": None,
        }
    ]


@pytest.mark.parametrize(
    ("age_seconds", "expected_code", "expected_severity"),
    [
        (299, "supervisor_converging", "none"),
        (300, "supervisor_converging", "none"),
        (301, "supervisor_convergence_stalled", "critical"),
    ],
)
def test_no_plan_stopped_uses_bounded_current_cycle_convergence_clock(
    tmp_path: Path,
    monkeypatch,
    age_seconds: int,
    expected_code: str,
    expected_severity: str,
) -> None:
    at = CYCLE_START + timedelta(seconds=age_seconds)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=False, with_plan=False)
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(at, running_proven=False),
    )

    supervisor = health._supervisor(at)

    assert supervisor["code"] == expected_code
    assert supervisor["severity"] == expected_severity
    assert supervisor["evidence"]["active_plan"] is False
    assert supervisor["evidence"][
        "continuous_nonconvergence_age_seconds"
    ] == age_seconds


@pytest.mark.parametrize(
    ("age_seconds", "expected_code"),
    [
        (300, "supervisor_converging"),
        (301, "supervisor_convergence_stalled"),
    ],
)
def test_active_plan_stopped_uses_same_300_second_boundary(
    tmp_path: Path,
    monkeypatch,
    age_seconds: int,
    expected_code: str,
) -> None:
    at = CYCLE_START + timedelta(seconds=age_seconds)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=False)
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(at, running_proven=False),
    )

    supervisor = health._supervisor(at)

    assert supervisor["code"] == expected_code
    assert supervisor["evidence"]["active_plan"] is True


def test_previous_cycle_running_runtime_does_not_satisfy_current_cycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    at = CYCLE_START + timedelta(seconds=301)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(
        health,
        cycle_id="2026-07-27_NIGHT",
        running=True,
        with_plan=False,
    )
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(at, running_proven=False),
    )

    supervisor = health._supervisor(at)

    assert supervisor["code"] == "supervisor_convergence_stalled"
    assert supervisor["evidence"]["runtime_running"] is False


def test_fresh_attempts_structural_clearance_and_restart_do_not_reset_clock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    at = CYCLE_START + timedelta(seconds=301)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=False)
    observations = [
        _supervisor_observation(
            CYCLE_START + timedelta(seconds=offset),
            running_proven=False,
            status="structural_cleared" if offset == 301 else "observed",
        )
        for offset in (60, 180, 301)
    ]
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(
            at,
            running_proven=False,
            observations=observations,
            attempt_at=at,
        ),
    )

    first = health._supervisor(at)
    restarted = CloudPaperHealth(
        output_root=health.output_root,
        backup_root=health.backup_root,
        owner_id="cloud-primary",
        deployed_sha=SHA,
        now=lambda: at,
        latest_market_provider=health.latest_market_provider,
    )._supervisor(at)

    assert first["code"] == "supervisor_convergence_stalled"
    assert restarted["code"] == "supervisor_convergence_stalled"
    assert first["evidence"]["attempt_age_seconds"] == 0
    assert first["evidence"][
        "continuous_nonconvergence_age_seconds"
    ] == 301
    assert restarted["evidence"][
        "continuous_nonconvergence_started_at"
    ] == CYCLE_START.isoformat()


def test_only_matching_running_proof_restarts_nonconvergence_interval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    at = CYCLE_START + timedelta(minutes=30)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=False)
    proven_at = at - timedelta(seconds=200)
    observations = [
        _supervisor_observation(
            CYCLE_START + timedelta(minutes=1),
            running_proven=False,
        ),
        _supervisor_observation(proven_at, running_proven=True),
        _supervisor_observation(at, running_proven=False),
    ]
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(at, observations=observations),
    )

    matched = health._supervisor(at)

    assert matched["code"] == "supervisor_converging"
    assert matched["evidence"][
        "continuous_nonconvergence_started_at"
    ] == proven_at.isoformat()
    assert matched["evidence"][
        "continuous_nonconvergence_age_seconds"
    ] == 200

    observations[1]["payload"]["running_evidence"]["plan_identity"][
        "strategy_plan_id"
    ] = "old-plan"
    observations[1]["payload"]["running_evidence"]["runtime"][
        "strategy_plan_id"
    ] = "old-plan"
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(at, observations=observations),
    )
    mismatched = health._supervisor(at)
    assert mismatched["code"] == "supervisor_convergence_stalled"
    assert mismatched["evidence"][
        "continuous_nonconvergence_started_at"
    ] == CYCLE_START.isoformat()


@pytest.mark.parametrize("mode", ["backing_off", "probing"])
def test_legal_transient_wait_is_noncritical_inside_convergence_window(
    tmp_path: Path,
    monkeypatch,
    mode: str,
) -> None:
    at = CYCLE_START + timedelta(seconds=299)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=False)
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(
            at,
            mode=mode,
            running_proven=False,
            attempt_at=CYCLE_START + timedelta(seconds=60),
        ),
    )

    supervisor = health._supervisor(at)

    assert supervisor["status"] == "ready"
    assert supervisor["severity"] == "none"
    assert supervisor["code"] == f"supervisor_{mode}"


@pytest.mark.parametrize("mode", ["backing_off", "probing"])
def test_transient_wait_becomes_critical_after_convergence_window(
    tmp_path: Path,
    monkeypatch,
    mode: str,
) -> None:
    at = CYCLE_START + timedelta(seconds=301)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=False)
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(
            at,
            mode=mode,
            running_proven=False,
            attempt_at=CYCLE_START + timedelta(seconds=60),
        ),
    )

    supervisor = health._supervisor(at)

    assert supervisor["status"] == "blocked"
    assert supervisor["severity"] == "critical"
    assert supervisor["code"] == "supervisor_convergence_stalled"


def test_probe_attention_is_immediately_critical_with_underlying_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    at = CYCLE_START + timedelta(minutes=30)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=False)
    blocker = {
        "machine_code": "episode_short_budget_exhausted",
        "classification": "transient",
    }
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(
            at,
            mode="probing",
            running_proven=False,
            alert_required=True,
            blocker=blocker,
        ),
    )

    supervisor = health._supervisor(at)

    assert supervisor["status"] == "blocked"
    assert supervisor["severity"] == "critical"
    assert supervisor["code"] == "episode_short_budget_exhausted"


def test_unknown_probe_blocker_code_fails_closed_without_alert_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    at = CYCLE_START + timedelta(minutes=10)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=False)
    blocker = {
        "machine_code": "new_unclassified_probe_condition",
        "classification": "transient",
    }
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(
            at,
            mode="probing",
            running_proven=False,
            blocker=blocker,
        ),
    )

    supervisor = health._supervisor(at)

    assert supervisor["status"] == "blocked"
    assert supervisor["severity"] == "critical"
    assert supervisor["code"] == "new_unclassified_probe_condition"


def test_utilization_ramp_is_considered_only_after_running_is_proven(
    tmp_path: Path,
    monkeypatch,
) -> None:
    at = NOW
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=True)
    utilization = {
        "windows": {
            "24h": {
                "evidence_status": "insufficient",
                "conservative_percentage": 0,
            }
        }
    }
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(
            at - timedelta(minutes=1),
            running_proven=True,
            utilization=utilization,
        ),
    )

    supervisor = health._supervisor(at)

    assert supervisor["code"] == "runtime_utilization_insufficient"
    assert supervisor["severity"] == "insufficient"

    _set_supervisor_runtime(health, running=False)
    _install_supervisor_model(
        monkeypatch,
        _supervisor_model(
            at,
            running_proven=False,
            utilization=utilization,
        ),
    )
    stopped = health._supervisor(at)
    assert stopped["code"] == "supervisor_convergence_stalled"
    assert stopped["severity"] == "critical"


def test_missing_corrupt_and_unknown_supervisor_evidence_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    health = _healthy(tmp_path)
    _set_supervisor_runtime(health, running=False)

    corrupt = {
        "status": "unavailable",
        "source_errors": [{"machine_code": "attempt_store_corrupt"}],
        "current_cycle": {"status": "unavailable"},
        "utilization": {"windows": {}},
    }
    _install_supervisor_model(monkeypatch, corrupt)
    assert health._supervisor(NOW)["code"] == "attempt_store_corrupt"

    missing = _supervisor_model(NOW)
    missing["current_cycle"]["running_evidence"] = None
    missing["current_cycle"]["last_running_proof"] = None
    _install_supervisor_model(monkeypatch, missing)
    assert health._supervisor(NOW)["code"] == (
        "supervisor_running_evidence_missing"
    )

    unknown = _supervisor_model(NOW, mode="new_unknown_mode")
    _install_supervisor_model(monkeypatch, unknown)
    assert health._supervisor(NOW)["code"] == "supervisor_episode_invalid"


@pytest.mark.parametrize(
    ("age_seconds", "expected_target", "expected_sources"),
    [
        (299, "success", []),
        (301, "fail", ["cloud_health"]),
    ],
)
def test_real_cloud_health_to_deadman_routes_convergence_and_stall(
    tmp_path: Path,
    monkeypatch,
    age_seconds: int,
    expected_target: str,
    expected_sources: list[str],
) -> None:
    at = CYCLE_START + timedelta(seconds=age_seconds)
    health = _healthy(tmp_path, now=at)
    _set_supervisor_runtime(health, running=False, with_plan=False)
    monkeypatch.setattr(
        "services.cloud_health.dualtrack_config",
        lambda: {"convergence": {"mode": "paper_supervisor"}},
    )
    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    calls: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def opener(request, timeout):
        calls.append(request.full_url)
        return Response()

    service = ExternalDeadmanPing(
        health.output_root,
        tmp_path / "unused-market.db",
        url="https://hc-ping.example/deadman",
        opener=opener,
        cloud_health_provider=lambda: health.run(persist=False),
    )
    service._vitals = lambda *_args, **_kwargs: {
        "always_on": {"status": "ok"}
    }
    service._schedule_runtime = lambda *_args, **_kwargs: {
        "status": "active",
        "runtime_failed_jobs": [],
    }
    service._exposure_snapshot = lambda *_args, **_kwargs: {
        "has_open_position": False,
        "position_unknown": False,
    }

    result = service.run("2026-07-28")

    assert result["ping"]["target_kind"] == expected_target
    assert result["failure_signal_sources"] == expected_sources
    assert calls
    assert ("/fail?" in calls[0]) is (expected_target == "fail")
    assert result["cloud_health"]["control_actions_executed"] == 0


def test_scheduler_owner_mismatch_blocks_cloud_health(
    tmp_path: Path, monkeypatch
) -> None:
    health = _healthy(tmp_path)
    _force_legacy_mode(monkeypatch)
    write_json(
        health.output_root / "cloud" / "scheduler_ownership" / "current.json",
        [{"status": "active", "active_owner_id": "local-mac", "epoch": 3}],
    )

    result = health.run()

    assert result["status"] == "blocked"
    owner = result["checks"]["scheduler_ownership"]
    assert owner["code"] == "scheduler_owner_mismatch"
    assert owner["evidence"]["active_owner_id"] == "local-mac"


def test_datafeed_probe_failure_is_bounded_and_secret_free(
    tmp_path: Path, monkeypatch
) -> None:
    health = _healthy(tmp_path)
    _force_legacy_mode(monkeypatch)
    health.latest_market_provider = lambda: (_ for _ in ()).throw(
        RuntimeError("https://secret.invalid/token")
    )

    result = health.run()

    assert result["checks"]["datafeed"]["summary"].endswith("RuntimeError")
    assert "secret.invalid" not in str(result)
