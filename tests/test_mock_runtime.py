from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.mock_runtime import MockTradingRuntime
from schemas.market_data import Bar


def _write_daily_artifacts(root: Path, run_date: str) -> None:
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD", "direction": "watch", "strength": 52}])
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD", "verdict": "no_trade", "sample_size": 0}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1", "status": "filled"}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"decision_status": "executed_paper", "paper_order": {"order_id": "p1"}}])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "t1", "status": "open"}])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"quantity": 1}}\n', encoding="utf-8")
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{run_date}.md").write_text("report\n", encoding="utf-8")
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "collect_more_paper_trades"}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold_parameters"}])
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true}}\n', encoding="utf-8")


def test_mock_runtime_reports_ready_and_fresh_runner(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    bars = [
        Bar("GOLD", "5m", (now - timedelta(minutes=5 * (220 - index))).isoformat(), 4500, 4501, 4499, 4500 + index, 1, "gold-api.com", ["live_snapshot"])
        for index in range(220)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m"}])
    _write_daily_artifacts(root, run_date)
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(
        f'{{"run_date": "{run_date}", "state": "ok", "interval_seconds": 300, "finished_at": "{now.isoformat()}"}}\n',
        encoding="utf-8",
    )

    result = MockTradingRuntime(root, db_path).run(run_date)

    assert result["status"] == "pass"
    assert result["mock_ready"] is True
    assert result["mock_running"] is True
    assert load_json(root / "mock_runtime" / "current.json")[0]["status"] == "pass"


def test_mock_runtime_warns_when_runner_is_stale(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    bars = [
        Bar("GOLD", "5m", (now - timedelta(minutes=5 * (220 - index))).isoformat(), 4500, 4501, 4499, 4500 + index, 1, "gold-api.com", ["live_snapshot"])
        for index in range(220)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m"}])
    _write_daily_artifacts(root, run_date)
    stale = now - timedelta(minutes=30)
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(
        f'{{"run_date": "{run_date}", "state": "ok", "interval_seconds": 300, "finished_at": "{stale.isoformat()}"}}\n',
        encoding="utf-8",
    )

    result = MockTradingRuntime(root, db_path).run(run_date)

    assert result["status"] == "warn"
    assert result["mock_ready"] is True
    assert result["mock_running"] is False
    assert result["summary"]["warned_checks"] == ["runner_freshness"]


def test_mock_runtime_accepts_fresh_running_runner(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    bars = [
        Bar("GOLD", "5m", (now - timedelta(minutes=5 * (220 - index))).isoformat(), 4500, 4501, 4499, 4500 + index, 1, "gold-api.com", ["live_snapshot"])
        for index in range(220)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m"}])
    _write_daily_artifacts(root, run_date)
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(
        f'{{"run_date": "{run_date}", "state": "running", "interval_seconds": 300, "started_at": "{now.isoformat()}"}}\n',
        encoding="utf-8",
    )

    result = MockTradingRuntime(root, db_path).run(run_date)

    assert result["status"] == "pass"
    assert result["mock_ready"] is True
    assert result["mock_running"] is True
