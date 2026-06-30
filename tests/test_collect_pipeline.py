from pipelines.collect import collect_once
from services.journal_store import load_json


def test_collect_once_writes_local_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4566.55")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-26T11:00:00+00:00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")

    records = collect_once("2026-05-26")

    assert records[0]["symbol"] == "GOLD"
    assert records[0]["timeframe"] == "5m"
    assert records[0]["close"] == 4566.55
    assert db_path.exists()
    stored = load_json(root / "collector_runs" / "2026-05-26.json")
    assert stored[0]["provider"] == "env_gold_price"
    raw_quotes = load_json(root / "raw_snapshots" / "2026-05-26" / "quote_snapshots.json")
    assert raw_quotes[0]["symbol"] == "GOLD"
    assert raw_quotes[0]["close"] == 4566.55
    assert raw_quotes[0]["record_type"] == "quote"
