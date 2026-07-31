from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.live_tick_latency_report import (
    build_live_tick_latency_report,
)
from services.live_tick_timing import REQUIRED_PHASES


def _row(
    index: int,
    *,
    source_sha: str = "a" * 40,
) -> dict:
    base = datetime(2026, 7, 30, 11, 58, tzinfo=timezone.utc)
    started = base + timedelta(seconds=index * 62)
    cycle_id = (
        "2026-07-30_DAY"
        if started < datetime(2026, 7, 30, 13, 0, tzinfo=timezone.utc)
        else "2026-07-30_NIGHT"
    )
    phases = [
        {
            "name": name,
            "status": "success",
            "duration_ms": 250.0,
            "error_type": None,
        }
        for name in REQUIRED_PHASES
    ]
    return {
        "schema_version": "paper-live-tick-timing-v1",
        "status": "success",
        "cycle_id": cycle_id,
        "observed_at": started.isoformat(),
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(seconds=2)).isoformat(),
        "hostname": "cloud-paper-1",
        "runtime_mode": "cloud",
        "scheduler_owner": {
            "status": "active",
            "active_owner_id": "cloud-primary",
            "epoch": 3,
        },
        "source": {
            "source_sha": source_sha,
            "source_tree_sha": "b" * 40,
            "tracked_tree_clean": True,
        },
        "phases": phases,
        "phase_sum_ms": 1750.0,
        "total_duration_ms": 2000.0,
        "unattributed_duration_ms": 250.0,
        "control_actions_executed": 0,
        "control_action_scope": "timing_instrumentation_only",
        "metadata_status": "pass",
        "metadata_errors": [],
        "error_type": None,
    }


def _rows(count: int = 120) -> list[dict]:
    return [_row(index) for index in range(count)]


def test_report_requires_and_summarizes_homogeneous_boundary_window() -> None:
    report = build_live_tick_latency_report(_rows())

    assert report["status"] == "pass"
    assert report["sample"]["count"] == 120
    assert report["sample"]["control_actions_executed"] == 0
    assert report["sample"]["boundaries"] == [
        {
            "from_cycle_id": "2026-07-30_DAY",
            "to_cycle_id": "2026-07-30_NIGHT",
            "from_kind": "DAY",
            "to_kind": "NIGHT",
            "last_tick_ended_at": _row(59)["ended_at"],
            "first_tick_started_at": _row(60)["started_at"],
        }
    ]
    assert report["latency_ms"]["total"] == {
        "p50": 2000.0,
        "p95": 2000.0,
        "p99": 2000.0,
        "max": 2000.0,
    }
    assert report["latency_ms"]["non_supervisor"] == {
        "p50": 1750.0,
        "p95": 1750.0,
        "p99": 1750.0,
        "max": 1750.0,
    }
    assert report["cadence"]["effect_detected"] is True
    assert report["cadence"]["inactive_gap_seconds"]["p50"] == 60.0
    assert (
        report["cadence"]["effective_cadence_seconds_at_total_p50"]
        == 62.0
    )
    assert report["budget_evidence"][
        "headroom_inside_10s_non_supervisor_allowance_seconds"
    ] == 8.25
    assert report["budget_evidence"]["non_supervisor_max_seconds"] == 1.75
    assert report["budget_evidence"][
        "max_headroom_inside_10s_non_supervisor_allowance_seconds"
    ] == 8.25
    assert report["budget_evidence"]["same_process_budget_supported"] is True
    assert report["budget_evidence"]["topology_evidence_result"] == (
        "same_live_tick_budget_supported"
    )
    assert report["budget_evidence"]["selected_budget_option"] == (
        "compressed_ai_sub_budget"
    )
    assert report["budget_evidence"]["selected_ai_sub_budget_seconds"] == 25.0
    assert report["budget_evidence"][
        "independent_timer_fresh_tick_prerequisite_required"
    ] is False


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda rows: rows.pop(), "sample_incomplete"),
        (
            lambda rows: [
                row.update(cycle_id="2026-07-30_NIGHT") for row in rows
            ],
            "cycle_time_identity_invalid",
        ),
        (
            lambda rows: rows[5]["source"].update(source_sha="c" * 40),
            "mixed_identity",
        ),
        (
            lambda rows: rows[3]["phases"].pop(),
            "phases_incomplete",
        ),
        (
            lambda rows: rows[2].update(control_actions_executed=1),
            "control_action_detected",
        ),
        (
            lambda rows: _alter_inactive_gaps(rows, seconds=10),
            "inactive_cadence_not_observed",
        ),
    ],
)
def test_report_fails_closed_on_unacceptable_sample(
    mutate,
    expected: str,
) -> None:
    rows = _rows()
    mutate(rows)

    with pytest.raises(ValueError, match=expected):
        build_live_tick_latency_report(rows)


def test_report_rejects_non_chronological_rows() -> None:
    rows = _rows()
    rows[1], rows[2] = rows[2], rows[1]

    with pytest.raises(ValueError, match="not_chronological"):
        build_live_tick_latency_report(rows)


def test_report_can_use_smaller_explicit_test_threshold() -> None:
    report = build_live_tick_latency_report(
        [
            _row(index)
            for index in (57, 58, 59, 60)
        ],
        minimum_samples=4,
    )

    assert report["sample"]["minimum_required"] == 4


def test_report_rejects_a_plausible_but_wrong_cycle_id() -> None:
    rows = _rows()
    rows[0]["cycle_id"] = "2026-07-30_NIGHT"

    with pytest.raises(ValueError, match="cycle_time_identity_invalid"):
        build_live_tick_latency_report(rows)


def test_report_requires_independent_timer_when_base_tick_p99_exceeds_10s() -> None:
    rows = _rows()
    for row in rows:
        row["total_duration_ms"] = 12250.0
        row["unattributed_duration_ms"] = 10500.0

    report = build_live_tick_latency_report(rows)

    assert report["budget_evidence"]["non_supervisor_p99_seconds"] == 12.0
    assert report["budget_evidence"]["same_process_budget_supported"] is False
    assert report["budget_evidence"]["topology_evidence_result"] == (
        "independent_timer_required"
    )
    assert report["budget_evidence"]["selected_budget_option"] == (
        "independent_supervisor_timer"
    )
    assert report["budget_evidence"]["selected_ai_sub_budget_seconds"] is None
    assert report["budget_evidence"][
        "independent_timer_fresh_tick_prerequisite_required"
    ] is True


def _alter_inactive_gaps(rows: list[dict], *, seconds: int) -> None:
    for row in rows:
        ended = datetime.fromisoformat(row["ended_at"])
        row["ended_at"] = (
            ended + timedelta(seconds=seconds)
        ).isoformat()
