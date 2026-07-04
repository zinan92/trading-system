from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schemas.market_data import Bar
from services.lab_registry import LabRegistry
from services.lab_walkforward import HoldoutQuarantine, WalkForwardConfig, build_windows, run_walkforward


def _bars(days: int = 140) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(days * 24 * 60):
        ts = (start + timedelta(minutes=index)).isoformat()
        rows.append(Bar("GOLD", "1m", ts, 100, 101, 99, 100, 1, "test", []))
    return rows


def test_holdout_public_api_refuses_holdout_reads():
    quarantine = HoldoutQuarantine(_bars(), WalkForwardConfig(holdout_days=28))

    with pytest.raises(ValueError):
        quarantine.public_bars_between(quarantine.holdout_start - timedelta(minutes=1), quarantine.holdout_start + timedelta(minutes=1))


def test_walkforward_windows_exclude_holdout():
    bars = _bars()
    cfg = WalkForwardConfig(train_days=30, validate_days=7, step_days=7, holdout_days=28)
    quarantine = HoldoutQuarantine(bars, cfg)
    windows = build_windows(bars, cfg)

    assert windows
    assert all(datetime.fromisoformat(item["validate_end"]) < quarantine.holdout_start for item in windows)


def test_holdout_consumption_is_recorded(tmp_path: Path):
    registry = LabRegistry(tmp_path / "outputs")
    registry.start({"hypothesis": "h", "family": "f"}, exp_id="e1")
    quarantine = HoldoutQuarantine(_bars(), WalkForwardConfig(holdout_days=28))

    holdout = quarantine.consume_holdout(registry, "e1")

    assert holdout
    assert registry.load("e1")["holdout_consumed"] is True


def test_run_walkforward_calls_evaluator_for_validation_windows():
    bars = _bars()
    cfg = WalkForwardConfig(train_days=30, validate_days=7, step_days=14, holdout_days=28)

    result = run_walkforward(bars, lambda window_bars, _window: {"status": "valid", "bars": len(window_bars)}, cfg)

    assert result["status"] == "valid"
    assert result["window_count"] > 0
    assert all(item["result"]["bars"] > 0 for item in result["windows"])
