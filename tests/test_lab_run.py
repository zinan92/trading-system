from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.lab_registry import LabRegistry
from pipelines.lab_run import (
    XAUUSDT_ONBOARD_DATE,
    _apply_direction_filter,
    _break_even_bp,
    _data_coverage,
    _report_markdown,
    _run_e1,
    _run_e2,
    _run_e3,
    _run_e4,
    _status_with_coverage,
    main,
)


def test_break_even_interpolates_cost_curve():
    result = _break_even_bp({
        "0bp": {"status": "valid", "metrics": {"expectancy_per_trade": 1.0}},
        "1bp": {"status": "valid", "metrics": {"expectancy_per_trade": 0.5}},
        "2bp": {"status": "valid", "metrics": {"expectancy_per_trade": 0.0}},
    })

    assert result == 2.0


def test_direction_filter_removes_counter_bias_signals():
    signals = [
        {"timestamp": "2026-06-25T00:00:00+00:00", "direction": "long"},
        {"timestamp": "2026-06-25T00:01:00+00:00", "direction": "short"},
    ]
    views = {"2026-06-25": {"direction_score": 10}}

    filtered = _apply_direction_filter(signals, views)

    assert filtered == [signals[1]]


def test_report_markdown_shows_trial_count(tmp_path):
    registry = LabRegistry(tmp_path / "outputs")
    registry.start({"hypothesis": "h", "family": "f"}, exp_id="e1")

    body = _report_markdown("Title", registry, "f", {"answer": 1})

    assert "Trial count for family `f`: 1" in body
    assert '"answer": 1' in body


def test_run_experiments_register_short_data_paths(tmp_path):
    root = tmp_path / "outputs"
    registry = LabRegistry(root)
    bars = _bars(180)

    _run_e1(root, registry, bars)
    _run_e2(root, registry, bars)
    _run_e3(root, registry, bars)
    _run_e4(root, registry, bars)

    entries = {item["exp_id"]: item for item in registry.entries()}
    assert entries["E1_macd_baseline_cost_curve"]["status"] == "invalid"
    assert entries["E2_direction_filter_ab"]["status"] == "invalid"
    assert entries["E3_gold_regime_share"]["status"] == "invalid"
    assert entries["E4_maker_vs_taker"]["status"] == "invalid"
    assert (root / "lab" / "reports" / "E1_macd_baseline_cost_curve.md").exists()
    assert (root / "lab" / "reports" / "E3_gold_regime_share.md").exists()


def test_full_available_history_satisfies_data_requirement():
    bars = _bars(180, start=XAUUSDT_ONBOARD_DATE)

    coverage = _data_coverage(bars)

    assert coverage["meets_12_month_requirement"] is False
    assert coverage["meets_full_available_history_requirement"] is True
    assert coverage["meets_data_requirement"] is True
    assert coverage["acceptance_blocker"] is False
    assert _status_with_coverage("valid", bars) == "valid"


def test_main_list_show_compare(tmp_path, monkeypatch, capsys):
    root = tmp_path / "outputs"
    registry = LabRegistry(root)
    registry.start({"hypothesis": "left", "family": "f"}, exp_id="left")
    registry.start({"hypothesis": "right", "family": "f"}, exp_id="right")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))

    monkeypatch.setattr(sys, "argv", ["lab_run", "list"])
    main()
    assert "left" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["lab_run", "show", "left"])
    main()
    assert '"exp_id": "left"' in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["lab_run", "compare", "left", "right"])
    main()
    out = capsys.readouterr().out
    assert '"left"' in out
    assert '"right"' in out


def _bars(count: int, start: str | None = None) -> list[Bar]:
    parsed_start = datetime.fromisoformat(start) if start else datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Bar("GOLD", "1m", (parsed_start + timedelta(minutes=index)).isoformat(), 100, 101, 99, 100, 1, "test", [])
        for index in range(count)
    ]
