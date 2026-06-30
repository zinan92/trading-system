from pathlib import Path

from services.journal_store import load_json, write_json
from services.official_feed_onboarding import OfficialFeedOnboarding


def test_official_feed_onboarding_writes_local_runbook(tmp_path: Path):
    root = tmp_path / "outputs"
    feed = tmp_path / "feed"
    run_date = "2026-05-26"
    write_json(root / "official_feed_receipts" / f"{run_date}.json", [{"run_date": run_date, "status": "warn", "ready_for_live": False, "truth_level": "public_snapshot", "official_rows": 0}])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"run_date": run_date, "latest_price": 4536.2, "latest_provider": "gold-api.com"}])
    write_json(root / "broker_feed_doctor" / f"{run_date}.json", [{"run_date": run_date, "status": "warn"}])
    write_json(root / "data_gaps" / f"{run_date}.json", [{"run_date": run_date, "latest_gap": {"from_timestamp": "2026-05-26T01:00:00Z", "to_timestamp": "2026-05-26T02:00:00Z"}}])
    write_json(root / "data_gap_repair_requests" / f"{run_date}.json", [{"run_date": run_date, "csv_template": str(feed / "NEEDS.csv"), "request_markdown": str(root / "gap.md")}])
    write_json(root / "oanda_feed" / f"{run_date}.json", [{"run_date": run_date, "missing_env": ["OANDA_API_TOKEN"]}])

    result = OfficialFeedOnboarding(root).build(run_date)

    assert result["status"] == "open"
    assert result["current_official_rows"] == 0
    assert result["target_timeframe_seconds"] == 300
    assert result["required_columns"] == ["timestamp", "open", "high", "low", "close", "volume"]
    assert result["price_sanity_range"]["min_price"] == 3000
    assert result["price_sanity_range"]["max_price"] == 6000
    assert result["handoff"]["broker_symbol"] == "XAUUSD"
    assert result["handoff"]["target_timeframe"] == "5m"
    assert result["handoff"]["sample_filename"] == f"XAUUSD_5m_{run_date}.csv"
    assert "official_rows" in {item["name"] for item in result["current_blockers"]}
    assert "truth_level" in {item["name"] for item in result["current_blockers"]}
    assert "pipelines.import_official_feed" in result["commands"][2]
    assert "broker_feed_doctor.status == pass" in result["acceptance"]
    assert "NEEDS.csv" in result["next_action"]
    assert Path(result["template"]).exists()
    markdown = (root / "official_feed_onboarding" / f"{run_date}.md").read_text()
    assert "Broker Feed Handoff" in markdown
    assert "Current Blockers" in markdown
    assert load_json(root / "official_feed_onboarding" / "current.json")[0]["status"] == "open"


def test_official_feed_onboarding_marks_ready_without_blockers(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "official_feed_receipts" / f"{run_date}.json",
        [{"run_date": run_date, "status": "pass", "ready_for_live": True, "truth_level": "official_broker", "official_rows": 240}],
    )
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"run_date": run_date, "ready_for_live": True, "latest_price": 4536.2, "latest_provider": "mt5_csv"}])
    write_json(root / "broker_feed_doctor" / f"{run_date}.json", [{"run_date": run_date, "status": "pass"}])
    write_json(root / "oanda_feed" / f"{run_date}.json", [{"run_date": run_date, "missing_env": []}])

    result = OfficialFeedOnboarding(root).build(run_date)

    assert result["status"] == "ready"
    assert result["current_blockers"] == []
    assert result["next_action"] == "Official XAUUSD 5m data is live-ready; continue through live_readiness and live_activation gates."


def test_official_feed_onboarding_keeps_execution_venue_only_receipt_open(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-22"
    write_json(
        root / "official_feed_receipts" / f"{run_date}.json",
        [
            {
                "run_date": run_date,
                "status": "warn",
                "ready_for_live": True,
                "truth_level": "execution_venue",
                "official_rows": 0,
            }
        ],
    )
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"run_date": run_date, "ready_for_live": True, "latest_price": 4191.5, "latest_provider": "binance_usdm"}])
    write_json(root / "broker_feed_doctor" / f"{run_date}.json", [{"run_date": run_date, "status": "warn", "message": "No broker CSV found"}])
    write_json(root / "oanda_feed" / f"{run_date}.json", [{"run_date": run_date, "missing_env": ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]}])

    result = OfficialFeedOnboarding(root).build(run_date)

    assert result["status"] == "open"
    assert result["current_official_rows"] == 0
    assert result["current_truth_level"] == "execution_venue"
    assert "official_rows" in {item["name"] for item in result["current_blockers"]}
    assert "truth_level" in {item["name"] for item in result["current_blockers"]}
    assert "live-ready" not in result["next_action"]
