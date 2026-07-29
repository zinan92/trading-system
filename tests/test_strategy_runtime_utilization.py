from __future__ import annotations

from pathlib import Path

from services.trading_system_read_model import (
    project_trading_system_read_model,
)
from services.control_audit import (
    append_control_event,
    build_control_event,
    build_runtime_utilization,
)


def _event(
    output_root: Path,
    *,
    ts: str,
    state: str,
    result: str = "accepted",
) -> None:
    append_control_event(
        output_root,
        build_control_event(
            cycle_id="2026-07-29_DAY",
            action="start" if state == "running" else "stop",
            actor=None,
            payload={},
            result=result,
            error=None,
            runtime={"actual_state": state, "desired_state": state},
            now=ts,
        ),
    )


def test_runtime_utilization_counts_only_proven_running_intervals(
    tmp_path: Path,
) -> None:
    _event(tmp_path, ts="2026-07-28T00:00:00Z", state="stopped")
    _event(tmp_path, ts="2026-07-29T02:00:00Z", state="running")
    _event(tmp_path, ts="2026-07-29T08:00:00Z", state="stopped")
    _event(
        tmp_path,
        ts="2026-07-29T09:00:00Z",
        state="running",
        result="rejected",
    )

    result = build_runtime_utilization(
        tmp_path,
        as_of="2026-07-29T12:00:00Z",
    )

    day = result["windows"]["24h"]
    assert day["evidence_status"] == "complete"
    assert day["running_seconds"] == 6 * 3600
    assert day["percentage"] == 25.0
    assert day["ending_state"] == "stopped"


def test_runtime_utilization_extends_current_running_state_to_as_of(
    tmp_path: Path,
) -> None:
    _event(tmp_path, ts="2026-07-20T00:00:00Z", state="stopped")
    _event(tmp_path, ts="2026-07-29T06:00:00Z", state="running")

    result = build_runtime_utilization(
        tmp_path,
        as_of="2026-07-29T12:00:00Z",
    )

    assert result["windows"]["24h"]["percentage"] == 25.0
    assert result["windows"]["7d"]["running_seconds"] == 6 * 3600


def test_runtime_utilization_does_not_invent_missing_boundary_state(
    tmp_path: Path,
) -> None:
    _event(tmp_path, ts="2026-07-29T06:00:00Z", state="running")

    result = build_runtime_utilization(
        tmp_path,
        as_of="2026-07-29T12:00:00Z",
    )

    assert result["windows"]["24h"]["evidence_status"] == "insufficient"
    assert result["windows"]["24h"]["percentage"] is None
    assert result["windows"]["7d"]["percentage"] is None


def test_runtime_utilization_reaches_read_model_and_dashboard() -> None:
    utilization = {
        "schema_version": "strategy-runtime-utilization-v1",
        "windows": {
            "24h": {"evidence_status": "complete", "percentage": 25.0},
            "7d": {"evidence_status": "complete", "percentage": 50.0},
        },
    }
    projected = project_trading_system_read_model(
        {"runtime": {}, "runtime_utilization": utilization}
    ).to_dict()
    assert projected["runtime"]["utilization"] == utilization

    html = (
        Path(__file__).resolve().parents[1] / "dashboard-gridmind.html"
    ).read_text(encoding="utf-8")
    assert '["策略运行占比",runtimeUtilizationText(runtime)]' in html
    assert '`24h ${value("24h")} / 7d ${value("7d")}`' in html
