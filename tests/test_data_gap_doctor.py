from pathlib import Path

from services.data_gap_doctor import DataGapDoctor
from services.journal_store import load_json, write_json


def test_data_gap_doctor_reports_exact_gap(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [
            {"timestamp": "2026-05-26T01:00:00+00:00", "close": 4570, "provider": "mt5_csv", "quality_flags": []},
            {"timestamp": "2026-05-26T01:05:00+00:00", "close": 4571, "provider": "mt5_csv", "quality_flags": []},
            {"timestamp": "2026-05-26T01:30:00+00:00", "close": 4572, "provider": "gold-api.com", "quality_flags": ["missing_bar_before"]},
        ],
    )
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "missing_bars": 4}])

    result = DataGapDoctor(root).run(run_date)

    assert result["status"] == "fail"
    assert result["gap_count"] == 1
    assert result["estimated_missing_bars"] == 4
    assert result["latest_gap"]["from_timestamp"] == "2026-05-26T01:05:00+00:00"
    assert result["latest_gap"]["to_provider"] == "gold-api.com"
    assert load_json(root / "data_gaps" / "current.json")[0]["gap_count"] == 1


def test_data_gap_doctor_passes_continuous_bars(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [
            {"timestamp": "2026-05-26T01:00:00+00:00", "close": 4570, "provider": "mt5_csv", "quality_flags": []},
            {"timestamp": "2026-05-26T01:05:00+00:00", "close": 4571, "provider": "mt5_csv", "quality_flags": []},
        ],
    )
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "missing_bars": 0}])

    result = DataGapDoctor(root).run(run_date)

    assert result["status"] == "pass"
    assert result["gap_count"] == 0


def test_data_gap_doctor_warns_for_quote_derived_paper_snapshot_gap(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [
            {"timestamp": "2026-05-26T01:00:00+00:00", "close": 4570, "provider": "yahoo_chart:GC=F", "quality_flags": []},
            {
                "timestamp": "2026-05-26T03:00:00+00:00",
                "close": 4536,
                "provider": "gold-api.com",
                "quality_flags": ["live_snapshot", "quote_derived_bar", "quote_snapshot_gap_before"],
            },
        ],
    )
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "missing_bars": 0}])

    result = DataGapDoctor(root).run(run_date)

    assert result["status"] == "warn"
    assert result["gap_count"] == 0
    assert result["paper_snapshot_gap_count"] == 1
    assert result["estimated_missing_bars"] == 0
    assert result["latest_paper_snapshot_gap"]["gap_type"] == "paper_quote_snapshot"
