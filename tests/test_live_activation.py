from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.journal_store import load_json, write_json
from services.live_activation import LiveActivationGate
from services.market_store import MarketStore


def test_live_activation_blocks_without_official_data_and_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "absent.env"))
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_live": False, "ready_for_paper": True}])
    write_json(root / "schedules" / "status_current.json", [{"status": "active"}])
    write_json(root / "mock_runtime" / "current.json", [{"mock_ready": True, "mock_running": True}])

    result = LiveActivationGate(root).run(run_date)

    assert result["status"] == "blocked"
    assert result["dry_run_ready"] is False
    assert result["approval"]["status"] == "missing"
    checks = {item["name"]: item["status"] for item in result["checks"]}
    assert checks["official_5m_data"] == "fail"
    assert checks["live_env"] == "fail"
    assert load_json(root / "live_activation" / "current.json")[0]["status"] == "blocked"


def test_live_activation_accepts_execution_venue_market_data_but_still_blocks_missing_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "absent.env"))
    root = tmp_path / "outputs"
    run_date = "2026-06-22"
    write_json(
        root / "data_source_preflight" / f"{run_date}.json",
        [
            {
                "ready_for_live": True,
                "ready_for_paper": True,
                "live_data_mode": "execution_venue",
                "latest_provider": "binance_usdm",
                "official_rows": 0,
                "execution_venue_rows": 500,
            }
        ],
    )
    write_json(root / "schedules" / "status_current.json", [{"status": "active"}])
    write_json(root / "mock_runtime" / "current.json", [{"mock_ready": True, "mock_running": True}])

    result = LiveActivationGate(root).run(run_date)

    checks = {item["name"]: item for item in result["checks"]}
    assert result["status"] == "blocked"
    assert checks["official_5m_data"]["status"] == "pass"
    assert "execution venue 5m OHLC is ready" in checks["official_5m_data"]["summary"]
    assert checks["live_env"]["status"] == "fail"


def test_live_activation_can_reach_dry_run_ready_with_official_data(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=token\nOANDA_ACCOUNT_ID=account\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    MarketStore(tmp_path / "market.db").upsert_bars(
        [
            Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), 4570, 4571, 4569, 4570.5, 1, "mt5_csv", ["csv_import"])
            for index in range(240)
        ]
    )
    write_json(
        root / "data_source_preflight" / f"{run_date}.json",
        [
            {
                "ready_for_live": True,
                "latest_provider": "mt5_csv",
                "live_data_mode": "official_broker",
                "official_rows": 240,
                "official_broker_providers": ["mt5_csv"],
            }
        ],
    )
    write_json(root / "schedules" / "status_current.json", [{"status": "active"}])
    write_json(root / "mock_runtime" / "current.json", [{"mock_ready": True, "mock_running": True}])
    for folder, suffix, body in [
        ("journals", ".md", "journal\n"),
        ("review_notes", ".md", "review\n"),
        ("reports", ".md", "report\n"),
    ]:
        path = root / folder / f"{run_date}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"state": "collect"}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold"}])

    result = LiveActivationGate(root).run(run_date)

    assert result["status"] == "blocked"
    assert result["dry_run_ready"] is False
    assert any("active broker keys" in action for action in result["next_actions"])
