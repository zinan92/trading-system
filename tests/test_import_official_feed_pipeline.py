from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipelines.import_official_feed import run_import_official_feed
from services.journal_store import load_json


def test_import_official_feed_refreshes_readiness_with_mt5_csv(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    run_date = "2026-05-26"
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    rows = ["timestamp,open,high,low,close,volume"]
    for index in range(240):
        timestamp = (start + timedelta(minutes=5 * index)).isoformat()
        open_price = 4500 + index * 0.5
        rows.append(f"{timestamp},{open_price:.2f},{open_price + 2:.2f},{open_price - 2:.2f},{open_price + 0.75:.2f},10")
    (feed_dir / "XAUUSD_5m.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_BROKER_FEED_INPUT_DIR", str(feed_dir))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")

    result = run_import_official_feed(run_date)

    preflight = result["data_source_preflight"]
    assert result["feed_doctor"]["status"] == "pass"
    assert result["feed_import"]["imported_rows"] == 240
    assert preflight["ready_for_paper"] is True
    assert preflight["ready_for_live"] is True
    assert preflight["latest_provider"] == "mt5_csv"
    assert result["data_source_lineage"]["status"] == "pass"
    assert result["data_source_lineage"]["truth_level"] == "official_broker"
    assert result["data_source_lineage"]["provider_groups"]["official"]["rows"] == 240
    assert result["official_feed_receipt"]["status"] == "pass"
    assert result["official_feed_receipt"]["ready_for_live"] is True
    assert result["official_feed_receipt"]["official_rows"] == 240
    assert result["official_feed_receipt"]["latest_official_bar"]["provider"] == "mt5_csv"
    assert result["live_submission_safety"]["status"] == "pass"
    assert result["live_submission_safety"]["network_call_attempted"] is False
    assert result["doctor"]["summary"]["live_ready"] is True
    assert result["data_archive"]["status"] == "pass"
    quote_snapshots = load_json(root / "raw_snapshots" / run_date / "quote_snapshots.json")
    assert quote_snapshots[-1]["provider"] == "mt5_csv"
    assert quote_snapshots[-1]["record_type"] == "bar_as_quote"
    assert load_json(root / "data_source_preflight" / "current.json")[0]["ready_for_live"] is True
    assert load_json(root / "data_source_lineage" / "current.json")[0]["ready_for_live"] is True
    assert load_json(root / "official_feed_receipts" / "current.json")[0]["status"] == "pass"
