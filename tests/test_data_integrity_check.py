from pathlib import Path

from schemas.market_data import Bar
from services.data_archive_manifest import DataArchiveManifest
from services.data_integrity_check import DataIntegrityCheck
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from tests.test_data_archive_manifest import _write_required_outputs


def test_data_integrity_check_passes_with_restorable_snapshot(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    _write_required_outputs(root, run_date)
    rows = [
        {"symbol": "GOLD", "timeframe": "5m", "timestamp": "2026-05-26T00:00:00+00:00", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1, "provider": "gold-api.com", "quality_flags": []}
    ]
    write_json(root / "raw_snapshots" / run_date / "GOLD_5m.json", rows)
    write_json(root / "raw_snapshots" / run_date / "quote_snapshots.json", [{"symbol": "GOLD", "close": 1.5, "record_type": "quote"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", rows)
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "clean_rows": 1, "missing_bars": 0, "spike_flags": 0}])
    MarketStore(db_path).upsert_bars([Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 1, 2, 1, 1.5, 1, "gold-api.com", [])])
    DataArchiveManifest(root, db_path).run(run_date)

    result = DataIntegrityCheck(root, db_path).run(run_date)

    assert result["status"] == "pass"
    assert result["summary"]["failed"] == 0
    assert next(item for item in result["checks"] if item["name"] == "sqlite_market_db")["status"] == "pass"
    assert next(item for item in result["checks"] if item["name"] == "daily_snapshot")["status"] == "pass"
    assert load_json(root / "data_integrity" / "current.json")[0]["status"] == "pass"
    assert (root / "data_integrity" / f"{run_date}.md").exists()


def test_data_integrity_check_fails_without_market_db(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "raw_snapshots" / run_date / "GOLD_5m.json", [{"close": 1}])
    write_json(root / "raw_snapshots" / run_date / "quote_snapshots.json", [{"symbol": "GOLD", "close": 1, "record_type": "quote"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 1}])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "clean_rows": 1}])

    result = DataIntegrityCheck(root, tmp_path / "missing.db").run(run_date)

    assert result["status"] == "fail"
    assert next(item for item in result["checks"] if item["name"] == "sqlite_market_db")["status"] == "fail"
