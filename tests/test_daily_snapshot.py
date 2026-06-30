import tarfile
from pathlib import Path

from schemas.market_data import Bar
from services.daily_snapshot import DailySnapshot
from services.journal_store import load_json, write_json
from services.market_store import MarketStore


def test_daily_snapshot_builds_restore_package(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    target = root / "signals" / f"{run_date}.json"
    write_json(target, [{"asset": "GOLD"}])
    MarketStore(db_path).upsert_bars([Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 1, 2, 1, 1.5, 1, "gold-api.com", [])])
    files = [{"path": str(target), "exists": True}]

    result = DailySnapshot(root, db_path).build(run_date, files)

    assert result["status"] == "pass"
    assert result["snapshot_path"].endswith(f"{run_date}.tar.gz")
    assert len(result["snapshot_sha256"]) == 64
    assert result["included_file_count"] == 2
    with tarfile.open(result["snapshot_path"], "r:gz") as archive:
        names = archive.getnames()
    assert "data/market_data.db" in names
    assert f"outputs/signals/{run_date}.json" in names
    assert load_json(root / "snapshots" / "current.json")[0]["snapshot_sha256"] == result["snapshot_sha256"]
    assert (root / "snapshots" / f"{run_date}.md").exists()
