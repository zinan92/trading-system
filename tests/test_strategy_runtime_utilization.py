from __future__ import annotations

from pathlib import Path

from services.control_audit import (
    append_control_event,
    build_control_event,
    build_runtime_utilization,
)
from services.trading_system_read_model import (
    project_trading_system_read_model,
)


def test_control_audit_cannot_invent_runtime_utilization(
    tmp_path: Path,
) -> None:
    append_control_event(
        tmp_path,
        build_control_event(
            cycle_id="2026-07-29_DAY",
            action="start",
            actor=None,
            payload={},
            result="accepted",
            error=None,
            runtime={
                "actual_state": "running",
                "desired_state": "running",
            },
            now="2026-07-29T02:00:00Z",
        ),
    )

    result = build_runtime_utilization(
        tmp_path,
        as_of="2026-07-29T12:00:00Z",
    )

    assert result["source"] == "paper_supervisor_running_evidence"
    assert result["windows"]["24h"]["evidence_status"] == "insufficient"
    assert result["windows"]["24h"]["percentage"] is None
    assert result["windows"]["7d"]["percentage"] is None
    assert (
        result["interval_policy"]["control_event_extrapolation"]
        is False
    )


def test_runtime_utilization_and_supervisor_reach_read_model() -> None:
    utilization = {
        "schema_version": "strategy-runtime-utilization-v2",
        "source": "paper_supervisor_running_evidence",
        "windows": {
            "24h": {
                "evidence_status": "complete",
                "percentage": 90.0,
            },
            "7d": {
                "evidence_status": "insufficient",
                "percentage": None,
            },
        },
    }
    supervisor = {
        "schema_version": "paper-supervisor-read-model-v1",
        "status": "available",
        "current_cycle": {
            "attempt_count": 2,
            "history": {"observations": [{"sequence": 1}]},
        },
        "utilization": utilization,
    }
    projected = project_trading_system_read_model(
        {
            "runtime": {},
            "runtime_utilization": utilization,
            "paper_supervisor": supervisor,
        }
    ).to_dict()

    assert projected["runtime"]["utilization"] == utilization
    assert projected["runtime"]["supervisor"] == supervisor


def test_dashboard_shows_supervisor_utilization_and_history() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "dashboard-gridmind.html"
    ).read_text(encoding="utf-8")

    assert '["策略运行占比",runtimeUtilizationText(runtime)]' in html
    assert '`24h ${value("24h")} / 7d ${value("7d")}`' in html
    assert 'data-tab="supervisor"' in html
    assert 'data-panel="supervisor"' in html
    assert "renderSupervisor(data)" in html
