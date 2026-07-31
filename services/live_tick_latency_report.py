"""Fail-closed latency report for natural Cloud Paper live ticks."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from services.live_tick_timing import (
    REQUIRED_PHASES,
    validate_homogeneous_sample,
)
from services.dualtrack_clock import cycle_window


REPORT_SCHEMA_VERSION = "paper-live-tick-latency-report-v1"
DEFAULT_MINIMUM_SAMPLES = 120
DEFAULT_INACTIVE_SECONDS = 60.0
DEFAULT_CADENCE_TOLERANCE_SECONDS = 5.0


def build_live_tick_latency_report(
    rows: list[dict[str, Any]],
    *,
    minimum_samples: int = DEFAULT_MINIMUM_SAMPLES,
    expected_inactive_seconds: float = DEFAULT_INACTIVE_SECONDS,
    cadence_tolerance_seconds: float = DEFAULT_CADENCE_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    """Summarize one homogeneous Cloud window or reject it completely."""

    if minimum_samples < 2:
        raise ValueError("live_tick_latency_minimum_samples_invalid")
    if expected_inactive_seconds <= 0 or cadence_tolerance_seconds < 0:
        raise ValueError("live_tick_latency_cadence_contract_invalid")

    validated = validate_homogeneous_sample(rows)
    if len(validated) < minimum_samples:
        raise ValueError("live_tick_latency_sample_incomplete")

    ordered = _require_chronological(validated)
    _require_cycle_identity_matches_time(ordered)
    boundaries = _cycle_boundaries(ordered)
    if not boundaries:
        raise ValueError("live_tick_latency_real_cycle_boundary_missing")

    inactive_gaps = [
        (
            _timestamp(current, "started_at")
            - _timestamp(previous, "ended_at")
        ).total_seconds()
        for previous, current in zip(ordered, ordered[1:])
    ]
    if any(value < 0 for value in inactive_gaps):
        raise ValueError("live_tick_latency_overlapping_ticks")

    start_intervals = [
        (
            _timestamp(current, "started_at")
            - _timestamp(previous, "started_at")
        ).total_seconds()
        for previous, current in zip(ordered, ordered[1:])
    ]
    gap_stats = _stats(inactive_gaps)
    interval_stats = _stats(start_intervals)
    cadence_detected = (
        abs(gap_stats["p50"] - expected_inactive_seconds)
        <= cadence_tolerance_seconds
        and gap_stats["p95"]
        <= expected_inactive_seconds + cadence_tolerance_seconds
    )
    if not cadence_detected:
        raise ValueError("live_tick_latency_inactive_cadence_not_observed")

    total_stats = _stats(
        [float(row["total_duration_ms"]) for row in ordered]
    )
    non_supervisor_stats = _stats(
        [
            float(row["total_duration_ms"])
            - _phase_duration(row, "cycle_decision")
            for row in ordered
        ]
    )
    phase_stats = {
        phase: _stats(
            [
                float(
                    next(
                        item["duration_ms"]
                        for item in row["phases"]
                        if item["name"] == phase
                    )
                )
                for row in ordered
            ]
        )
        for phase in REQUIRED_PHASES
    }
    first = ordered[0]
    last = ordered[-1]
    non_supervisor_p99_seconds = round(
        non_supervisor_stats["p99"] / 1000.0,
        3,
    )
    non_supervisor_max_seconds = round(
        non_supervisor_stats["max"] / 1000.0,
        3,
    )
    same_process_supported = non_supervisor_max_seconds <= 10.0
    sample_seconds = (
        _timestamp(last, "started_at")
        - _timestamp(first, "started_at")
    ).total_seconds()
    effective_cadence_seconds = (
        expected_inactive_seconds + total_stats["p50"] / 1000.0
    )
    expected_tick_count = (
        math.floor(sample_seconds / effective_cadence_seconds) + 1
    )
    coverage = min(1.0, len(ordered) / max(1, expected_tick_count))
    identity = {
        "hostname": first["hostname"],
        "source_sha": first["source"]["source_sha"],
        "source_tree_sha": first["source"]["source_tree_sha"],
        "scheduler_owner_id": first["scheduler_owner"]["active_owner_id"],
        "scheduler_owner_epoch": first["scheduler_owner"]["epoch"],
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "pass",
        "sample": {
            "count": len(ordered),
            "minimum_required": minimum_samples,
            "window_started_at": first["started_at"],
            "window_ended_at": last["ended_at"],
            "window_seconds": round(sample_seconds, 3),
            "cycle_ids": list(dict.fromkeys(row["cycle_id"] for row in ordered)),
            "boundaries": boundaries,
            "identity": identity,
            "runtime_mode": "cloud",
            "control_actions_executed": sum(
                int(row["control_actions_executed"]) for row in ordered
            ),
        },
        "latency_ms": {
            "total": total_stats,
            "non_supervisor": non_supervisor_stats,
            "phases": phase_stats,
        },
        "cadence": {
            "timer_contract": "OnUnitInactiveSec",
            "configured_inactive_seconds": expected_inactive_seconds,
            "tolerance_seconds": cadence_tolerance_seconds,
            "effect_detected": True,
            "inactive_gap_seconds": gap_stats,
            "start_interval_seconds": interval_stats,
            "effective_cadence_seconds_at_total_p50": round(
                effective_cadence_seconds,
                3,
            ),
            "expected_tick_count": expected_tick_count,
            "observed_successful_tick_count": len(ordered),
            "successful_tick_coverage": round(coverage, 6),
        },
        "budget_evidence": {
            "current_live_tick_timeout_seconds": 55.0,
            "proposed_supervisor_budget_seconds": 45.0,
            "selected_ai_sub_budget_seconds": (
                25.0 if same_process_supported else None
            ),
            "required_non_supervisor_allowance_seconds": 10.0,
            "non_supervisor_p99_seconds": non_supervisor_p99_seconds,
            "non_supervisor_max_seconds": non_supervisor_max_seconds,
            "headroom_before_55s_timeout_seconds": round(
                55.0 - non_supervisor_stats["p99"] / 1000.0,
                3,
            ),
            "headroom_inside_10s_non_supervisor_allowance_seconds": round(
                10.0 - non_supervisor_stats["p99"] / 1000.0,
                3,
            ),
            "max_headroom_inside_10s_non_supervisor_allowance_seconds": round(
                10.0 - non_supervisor_stats["max"] / 1000.0,
                3,
            ),
            "same_process_budget_supported": same_process_supported,
            "topology_evidence_result": (
                "same_live_tick_budget_supported"
                if same_process_supported
                else "independent_timer_required"
            ),
            "selected_budget_option": (
                "compressed_ai_sub_budget"
                if same_process_supported
                else "independent_supervisor_timer"
            ),
            "independent_timer_fresh_tick_prerequisite_required": (
                not same_process_supported
            ),
        },
    }


def _require_chronological(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    starts = [_timestamp(row, "started_at") for row in rows]
    if any(current <= previous for previous, current in zip(starts, starts[1:])):
        raise ValueError("live_tick_latency_sample_not_chronological")
    return rows


def _cycle_boundaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    boundaries: list[dict[str, Any]] = []
    for previous, current in zip(rows, rows[1:]):
        previous_kind = _cycle_kind(str(previous["cycle_id"]))
        current_kind = _cycle_kind(str(current["cycle_id"]))
        if previous["cycle_id"] == current["cycle_id"]:
            continue
        if {previous_kind, current_kind} != {"DAY", "NIGHT"}:
            continue
        boundaries.append(
            {
                "from_cycle_id": previous["cycle_id"],
                "to_cycle_id": current["cycle_id"],
                "from_kind": previous_kind,
                "to_kind": current_kind,
                "last_tick_ended_at": previous["ended_at"],
                "first_tick_started_at": current["started_at"],
            }
        )
    return boundaries


def _require_cycle_identity_matches_time(
    rows: list[dict[str, Any]],
) -> None:
    for row in rows:
        started = _timestamp(row, "started_at")
        if cycle_window(started).cycle_id != str(row["cycle_id"]):
            raise ValueError(
                "live_tick_latency_cycle_time_identity_invalid"
            )


def _cycle_kind(cycle_id: str) -> str:
    value = cycle_id.rsplit("_", 1)[-1]
    return value if value in {"DAY", "NIGHT"} else ""


def _phase_duration(row: dict[str, Any], name: str) -> float:
    try:
        value = next(
            item["duration_ms"]
            for item in row["phases"]
            if item["name"] == name
        )
    except (KeyError, StopIteration, TypeError) as exc:
        raise ValueError("live_tick_latency_phases_incomplete") from exc
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("live_tick_latency_stat_value_invalid")
    return duration


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("live_tick_latency_stat_sample_empty")
    numbers = [float(value) for value in values]
    if any(not math.isfinite(value) or value < 0 for value in numbers):
        raise ValueError("live_tick_latency_stat_value_invalid")
    numbers.sort()
    return {
        "p50": round(_percentile(numbers, 0.50), 3),
        "p95": round(_percentile(numbers, 0.95), 3),
        "p99": round(_percentile(numbers, 0.99), 3),
        "max": round(numbers[-1], 3),
    }


def _percentile(numbers: list[float], quantile: float) -> float:
    position = (len(numbers) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return numbers[lower]
    weight = position - lower
    return numbers[lower] * (1.0 - weight) + numbers[upper] * weight


def _timestamp(row: dict[str, Any], key: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(row[key]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"live_tick_latency_{key}_invalid") from exc
    if value.tzinfo is None:
        raise ValueError(f"live_tick_latency_{key}_invalid")
    return value
