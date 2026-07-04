from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.journal_store import load_json
from services.lab_regimes import build_coverage_report, label_regimes, write_regime_artifact


def _bars(count: int = 24 * 60 * 5) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        close = 100 + (index % 200) * 0.01
        ts = (start + timedelta(minutes=index)).isoformat()
        rows.append(Bar("GOLD", "1m", ts, close, close + 0.1, close - 0.1, close, 1, "test", []))
    return rows


def test_coverage_report_counts_gaps_by_day_and_hour():
    bars = _bars(120)
    broken = bars[:60] + bars[70:]

    report = build_coverage_report(broken)

    assert report["status"] == "warn"
    assert report["gap_count"] == 1
    assert report["missing_minutes"] == 10
    assert report["missing_by_day"]
    assert report["missing_by_hour"]


def test_regime_labeler_reports_tradeable_share():
    result = label_regimes(_bars())

    assert result["status"] == "valid"
    assert result["summary"]["day_share"]["total"] > 0
    assert result["summary"]["four_hour_share"]["total"] > 0
    assert "tradeable_directional_pct" in result["summary"]["four_hour_share"]


def test_write_regime_artifact_uses_json_store(tmp_path: Path):
    payload = write_regime_artifact(tmp_path / "outputs", "sample", _bars())

    saved = load_json(tmp_path / "outputs" / "lab" / "regimes" / "sample.json")[0]
    assert saved == payload
