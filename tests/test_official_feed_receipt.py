from pathlib import Path
from datetime import datetime, timedelta, timezone

from services.journal_store import load_json
from services.market_store import MarketStore
from services.official_feed_receipt import OfficialFeedReceipt
from services.journal_store import write_json
from schemas.market_data import Bar


def test_official_feed_receipt_passes_with_live_ready_official_bar(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"

    result = OfficialFeedReceipt(root).build(
        run_date,
        oanda_feed={"status": "skipped", "ready": False, "imported_rows": 0, "missing_env": []},
        feed_doctor={
            "status": "pass",
            "valid_file_count": 1,
            "file_count": 1,
            "row_count": 3,
            "latest_timestamp": "2026-05-26T00:10:00+00:00",
            "price_sanity": {"enabled": True, "min_price": 3000.0, "max_price": 6000.0},
        },
        feed_import={"new_files": 1, "imported_rows": 3, "errors": [], "input_dir": "/tmp/feed"},
        preflight={"status": "pass", "ready_for_paper": True, "ready_for_live": True, "official_rows": 3, "public_rows": 0, "latest_provider": "mt5_csv", "latest_price": 4531, "latest_timestamp": "2026-05-26T00:10:00+00:00"},
        lineage={
            "status": "pass",
            "truth_level": "official_broker",
            "ready_for_live": True,
            "provider_groups": {"official": {"rows": 3, "providers": ["mt5_csv"]}},
            "latest_official_bar": {"provider": "mt5_csv", "close": 4531, "timestamp": "2026-05-26T00:10:00+00:00"},
        },
    )

    assert result["status"] == "pass"
    assert result["ready_for_live"] is True
    assert result["official_rows"] == 3
    assert result["broker_csv"]["price_sanity"]["min_price"] == 3000.0
    assert result["latest_official_bar"]["provider"] == "mt5_csv"
    assert result["next_actions"] == ["Official GOLD/XAUUSD 5m feed is live-ready; keep paper/live gates controlled by live_readiness and live_activation."]
    assert load_json(root / "official_feed_receipts" / "current.json")[0]["status"] == "pass"
    assert (root / "official_feed_receipts" / f"{run_date}.md").exists()


def test_official_feed_receipt_warns_with_public_only_data(tmp_path: Path):
    root = tmp_path / "outputs"

    result = OfficialFeedReceipt(root).build(
        "2026-05-26",
        oanda_feed={"status": "skipped", "ready": False, "imported_rows": 0, "missing_env": ["OANDA_API_TOKEN"]},
        feed_doctor={"status": "warn", "valid_file_count": 0, "file_count": 0, "row_count": 0},
        feed_import={"new_files": 0, "imported_rows": 0, "errors": [], "input_dir": "/tmp/feed"},
        preflight={"status": "warn", "ready_for_paper": True, "ready_for_live": False, "official_rows": 0, "public_rows": 10, "latest_provider": "gold-api.com", "latest_price": 4531},
        lineage={"status": "warn", "truth_level": "public_snapshot", "ready_for_live": False, "provider_groups": {"official": {"rows": 0, "providers": []}}},
    )

    assert result["status"] == "warn"
    assert result["ready_for_live"] is False
    assert result["official_rows"] == 0
    assert any("OANDA_API_TOKEN" in item for item in result["next_actions"])


def test_official_feed_receipt_accepts_execution_venue_only_as_live_ready(tmp_path: Path):
    root = tmp_path / "outputs"

    result = OfficialFeedReceipt(root).build(
        "2026-06-22",
        oanda_feed={"status": "skipped", "ready": False, "imported_rows": 0, "missing_env": ["OANDA_API_TOKEN"]},
        feed_doctor={"status": "warn", "valid_file_count": 0, "file_count": 0, "row_count": 0},
        feed_import={"new_files": 0, "imported_rows": 0, "errors": [], "input_dir": "/tmp/feed"},
        preflight={
            "status": "pass",
            "ready_for_paper": True,
            "ready_for_live": True,
            "official_rows": 0,
            "execution_venue_rows": 3500,
            "public_rows": 0,
            "latest_provider": "binance_usdm",
            "latest_price": 4191.5,
            "latest_timestamp": "2026-06-22T14:30:00+00:00",
            "live_data_mode": "execution_venue",
        },
        lineage={
            "status": "pass",
            "truth_level": "execution_venue",
            "ready_for_live": True,
            "provider_groups": {"official": {"rows": 0, "providers": []}, "execution_venue": {"rows": 3500, "providers": ["binance_usdm"]}},
            "latest_official_bar": {},
            "latest_execution_venue_bar": {"provider": "binance_usdm", "close": 4191.5, "timestamp": "2026-06-22T14:30:00+00:00"},
        },
    )

    assert result["status"] == "pass"
    assert result["ready_for_live"] is True
    assert result["preflight_ready_for_live"] is True
    assert result["lineage_ready_for_live"] is True
    assert result["truth_level"] == "execution_venue"
    assert result["official_rows"] == 0
    assert result["execution_venue_rows"] == 3500
    assert result["latest_official_bar"] == {}
    assert result["latest_execution_venue_bar"]["provider"] == "binance_usdm"
    assert "execution venue feed is live-ready" in result["next_actions"][0]


def test_official_feed_receipt_refreshes_from_current_local_state(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    bars = [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), 4530, 4532, 4529, 4530 + index, 0, "gold-api.com", ["live_snapshot"])
        for index in range(3)
    ]
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "data_quality" / f"{run_date}.json", {"GOLD": {"allows_trading": True, "reasons": []}})
    MarketStore(db_path).upsert_bars(bars)
    MarketStore(db_path).upsert_quote(bars[-1])

    result = OfficialFeedReceipt(root, db_path).refresh(run_date)

    assert result["status"] == "warn"
    assert result["truth_level"] == "public_snapshot"
    assert result["official_rows"] == 0
    assert result["latest_provider"] == "gold-api.com"
    assert load_json(root / "official_feed_receipts" / f"{run_date}.json")[0]["latest_price"] == 4532
