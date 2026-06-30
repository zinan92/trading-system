from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.data_trust_report import DataTrustReport
from services.journal_store import load_json, write_json
from services.market_store import MarketStore


def test_data_trust_warns_for_public_price_without_official_rows(tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    run_date = "2026-05-26"
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    bars = [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * i)).isoformat(), 4500 + i, 4501 + i, 4499 + i, 4500 + i, 0, "gold-api.com", ["live_snapshot"])
        for i in range(3)
    ]
    MarketStore(db).upsert_bars(bars)
    MarketStore(db).upsert_quote(Bar("GOLD", "5m", bars[-1].timestamp, bars[-1].close, bars[-1].close, bars[-1].close, bars[-1].close, 0, "gold-api.com", ["live_snapshot"]))
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "public_snapshot", "provider_groups": {"official": {"rows": 0}, "public": {"rows": 3}}}])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_live": False, "official_rows": 0, "public_rows": 3}])

    result = DataTrustReport(root, db).run(run_date)

    assert result["status"] == "warn"
    assert result["display_mode"] == "PAPER_PUBLIC"
    assert result["summary"]["latest_price"] == 4502
    assert result["summary"]["latest_provider"] == "gold-api.com"
    assert result["summary"]["latest_is_public"] is True
    assert result["summary"]["latest_is_mock"] is False
    assert any(item["name"] == "official_rows" and item["status"] == "warn" for item in result["checks"])
    assert load_json(root / "data_trust" / "current.json")[0]["display_mode"] == "PAPER_PUBLIC"
    assert (root / "data_trust" / f"{run_date}.md").exists()


def test_data_trust_fails_when_latest_clean_bar_is_mock(tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    run_date = "2026-05-26"
    bar = Bar("GOLD", "5m", "mock-5m-001", 2350, 2351, 2349, 2350, 0, "local_synthetic_seed", ["synthetic_seed"])
    MarketStore(db).upsert_bars([bar])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "synthetic_seed", "provider_groups": {"official": {"rows": 0}}}])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_live": False, "official_rows": 0}])

    result = DataTrustReport(root, db).run(run_date)

    assert result["status"] == "fail"
    assert result["display_mode"] == "BLOCKED_SYNTHETIC"
    assert result["summary"]["latest_is_mock"] is True
    assert any(item["name"] == "latest_price" and item["status"] == "fail" for item in result["checks"])
    assert any(item["name"] == "mock_leak" and item["status"] == "fail" for item in result["checks"])


def test_data_trust_passes_for_official_broker_latest(tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    run_date = "2026-05-26"
    bar = Bar("GOLD", "5m", "2026-05-26T10:00:00+00:00", 4530, 4532, 4528, 4531, 1, "mt5_csv", ["official_broker_feed"])
    MarketStore(db).upsert_bars([bar])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "official_broker", "provider_groups": {"official": {"rows": 1}}}])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_live": True, "official_rows": 1, "public_rows": 0}])

    result = DataTrustReport(root, db).run(run_date)

    assert result["status"] == "pass"
    assert result["display_mode"] == "OFFICIAL_BROKER"
    assert result["summary"]["latest_is_official"] is True


def test_data_trust_accepts_execution_venue_as_tradable_feed(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    run_date = "2026-06-02"
    config = {
        "market_data_sources": {
            "gold_5m": {
                "public_providers": ["gold-api.com", "binance_usdm"],
                "execution_venue_providers": ["binance_usdm"],
                "official_broker_providers": ["mt5_csv", "oanda"],
                "price_sanity": {"min_price": 3000, "max_price": 6000},
            }
        }
    }
    monkeypatch.setattr("services.data_trust_report.load_pipeline_config", lambda: config)
    bar = Bar("GOLD", "5m", "2026-06-02T10:00:00+00:00", 4530, 4532, 4528, 4531, 1, "binance_usdm", ["execution_venue_feed"])
    MarketStore(db).upsert_bars([bar])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "execution_venue", "provider_groups": {"official": {"rows": 0}, "execution_venue": {"rows": 1}}}])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_live": True, "official_rows": 0, "execution_venue_rows": 1, "public_rows": 1}])

    result = DataTrustReport(root, db).run(run_date)

    assert result["status"] == "pass"
    assert result["display_mode"] == "EXECUTION_VENUE"
    assert result["summary"]["latest_is_execution_venue"] is True
    assert result["summary"]["execution_venue_rows"] == 1
    assert any(item["name"] == "execution_grade_rows" and item["status"] == "pass" for item in result["checks"])
