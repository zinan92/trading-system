from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.data_source_lineage import DataSourceLineage
from services.journal_store import load_json, write_json
from services.market_store import MarketStore


def test_data_source_lineage_marks_public_only_database_not_live_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    bar = Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 4530, 4532, 4529, 4531, 0, "gold-api.com", ["live_snapshot"])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    store = MarketStore(db_path)
    store.upsert_bars([bar])
    store.upsert_quote(Bar("GOLD", "5m", "2026-05-26T00:01:00+00:00", 4532, 4532, 4532, 4532, 0, "gold-api.com", ["live_quote"]))
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"status": "warn", "ready_for_paper": True, "ready_for_live": False}])

    result = DataSourceLineage(root, db_path).run(run_date)

    assert result["status"] == "warn"
    assert result["truth_level"] == "public_snapshot"
    assert result["ready_for_paper"] is True
    assert result["ready_for_live"] is False
    assert result["provider_groups"]["public"]["rows"] == 1
    assert result["provider_groups"]["official"]["rows"] == 0
    assert "no official broker rows" in result["lineage_message"]
    assert load_json(root / "data_source_lineage" / "current.json")[0]["latest_quote"]["close"] == 4532


def test_data_source_lineage_passes_when_latest_clean_bar_is_official(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    bar = Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 4530, 4532, 4529, 4531, 10, "mt5_csv", ["csv_import"])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    MarketStore(db_path).upsert_bars([bar])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"status": "pass", "ready_for_paper": True, "ready_for_live": True}])

    result = DataSourceLineage(root, db_path).run(run_date)

    assert result["status"] == "pass"
    assert result["truth_level"] == "official_broker"
    assert result["ready_for_live"] is True
    assert result["latest_official_bar"]["provider"] == "mt5_csv"
    assert result["provider_groups"]["official"]["providers"] == ["mt5_csv"]


def test_data_source_lineage_accepts_execution_venue_as_tradable_feed(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-06-02"
    config = {
        "market_data_sources": {
            "gold_5m": {
                "public_providers": ["gold-api.com", "binance_usdm"],
                "execution_venue_providers": ["binance_usdm"],
                "official_broker_providers": ["mt5_csv", "oanda"],
            }
        }
    }
    monkeypatch.setattr("services.data_source_lineage.load_pipeline_config", lambda: config)
    bar = Bar("GOLD", "5m", "2026-06-02T00:00:00+00:00", 4530, 4532, 4529, 4531, 10, "binance_usdm", ["execution_venue_feed"])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    MarketStore(db_path).upsert_bars([bar])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"status": "pass", "ready_for_paper": True, "ready_for_live": True, "execution_venue_rows": 1, "official_rows": 0}])

    result = DataSourceLineage(root, db_path).run(run_date)

    assert result["status"] == "pass"
    assert result["truth_level"] == "execution_venue"
    assert result["ready_for_live"] is True
    assert result["execution_venue_ready"] is True
    assert result["latest_execution_venue_bar"]["provider"] == "binance_usdm"
    assert result["provider_groups"]["execution_venue"]["providers"] == ["binance_usdm"]
    assert result["provider_groups"]["official"]["rows"] == 0
    assert "execution venue provider" in result["lineage_message"]


def test_market_store_load_latest_bar_can_filter_provider(tmp_path: Path):
    db_path = tmp_path / "market.db"
    store = MarketStore(db_path)
    store.upsert_bars([
        Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 1, 1, 1, 1, 0, "mt5_csv", []),
        Bar("GOLD", "5m", "2026-05-26T00:05:00+00:00", 2, 2, 2, 2, 0, "gold-api.com", []),
    ])

    assert store.load_latest_bar("GOLD", "5m")["close"] == 2
    assert store.load_latest_bar("GOLD", "5m", ["mt5_csv"])["close"] == 1
    assert store.load_latest_bar("GOLD", "5m", ["oanda"]) == {}
