from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.journal_store import load_json, write_json
from services.live_readiness import LiveReadiness
from services.market_store import MarketStore
from schemas.market_data import Bar


def test_live_readiness_fails_when_system_is_only_paper_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    bars = [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), 4570, 4572, 4569, 4570 + index, 1, "gold-api.com", ["live_snapshot"])
        for index in range(220)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m"}])
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true}}\n', encoding="utf-8")
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text('{"state": "ok", "interval_seconds": 300}\n', encoding="utf-8")
    write_json(root / "oanda_feed" / "current.json", [{"status": "skipped", "ready": False, "missing_env": ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]}])
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{run_date}.md").write_text("report\n", encoding="utf-8")
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"state": "collect"}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold"}])

    result = LiveReadiness(root, db_path).run(run_date)

    assert result["status"] == "fail"
    assert result["live_ready"] is False
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["official_market_data"]["status"] == "fail"
    assert checks["execution_mode"]["status"] == "fail"
    assert checks["broker_provider"]["status"] == "fail"
    assert checks["risk_rules"]["status"] == "pass"
    assert checks["runner"]["status"] == "pass"
    assert checks["journal_review"]["status"] == "pass"
    assert load_json(root / "live_readiness" / "current.json")[0]["run_date"] == run_date


def test_live_readiness_accepts_execution_venue_market_data(tmp_path: Path):
    readiness = LiveReadiness(tmp_path / "outputs", tmp_path / "market_data.db")

    check = readiness._official_market_data(
        {
            "ready_for_live": True,
            "ready_for_paper": True,
            "live_data_mode": "execution_venue",
            "latest_provider": "binance_usdm",
            "official_rows": 0,
            "execution_venue_rows": 500,
        }
    )

    assert check["status"] == "pass"
    assert "execution venue 5m OHLC is ready" in check["summary"]
