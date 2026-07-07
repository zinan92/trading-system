from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.bias_ledger import BiasLedger
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.system_doctor import SystemDoctor
from schemas.market_data import Bar


def test_system_doctor_reports_warn_and_next_action_without_official_feed(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))
    run_date = "2026-05-26"
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    bars = [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), 4570, 4572, 4569, 4570 + index, 1, "gold-api.com", ["live_snapshot"])
        for index in range(220)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "raw_snapshots" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "raw_snapshots" / run_date / "quote_snapshots.json", [{"symbol": "GOLD", "close": bars[-1].close, "provider": "gold-api.com", "record_type": "quote"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m"}])
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true}}\n', encoding="utf-8")
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD", "direction": "watch", "strength": 52, "regime": "no_trade"}])
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD", "backtest_id": "local5m_test", "verdict": "no_trade", "sample_size": 0}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1", "ticket_id": "t1", "status": "filled", "fill_price": 4570, "requested_price": 4570, "quantity": 1}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "tr1", "order_id": "p1", "ticket_id": "t1", "symbol": "GOLD", "side": "long", "status": "open", "quantity": 1, "entry_price": 4570}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 1, "closed_all_count": 0, "realized_pnl_all": 0, "unrealized_pnl": 0, "net_pnl_marked": 0, "open_unrealized_r": 0, "expectancy_r": 0, "profit_factor": 0, "total_execution_costs": 0}, "open_trades": [{"trade_id": "t1"}], "closed_today": []}])
    write_json(root / "equity_curve" / f"{run_date}.json", [{"status": "pass", "current_equity": 100000, "current_drawdown_pct": 0, "max_drawdown_pct": 0, "point_count": 1, "points": [{"run_date": run_date, "equity": 100000}]}])
    write_json(root / "paper_trade_attribution" / f"{run_date}.json", [{"status": "pass", "summary": {"open_trades": 1, "closed_trades": 0, "attributed_open_trades": 1, "attributed_closed_trades": 0}}])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"symbol": "GOLD", "side": "long", "quantity": 1, "avg_price": 4570, "unrealized_pnl": 0}}\n', encoding="utf-8")
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"decision_status": "executed_paper", "paper_order": {"order_id": "p1"}}])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    write_json(root / "strategy_snapshots" / f"{run_date}.json", [{"config_hash": "abc123def456"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "collect_more_paper_trades"}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold_parameters"}])
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{run_date}.md").write_text("report\n", encoding="utf-8")
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text('{"state": "ok"}\n', encoding="utf-8")
    write_json(root / "oanda_feed" / "current.json", [{"status": "skipped", "ready": False, "imported_rows": 0, "missing_env": ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]}])
    BiasLedger(root, db_path).append_open_view(
        {
            "run_date": run_date,
            "generated_at": "2026-05-26T00:00:00+00:00",
            "direction_score": 65,
            "reference_price": 4570,
            "expiry": {"expires_at": "2099-05-26T12:00:00+00:00", "valid_for_hours": 12},
        }
    )

    result = SystemDoctor(root).run(run_date)

    assert result["status"] == "warn"
    assert result["summary"]["broker_feed_doctor"] == "warn"
    assert result["summary"]["oanda_feed"] == "skipped"
    assert result["summary"]["mock_runtime"] == "warn"
    assert result["summary"]["mock_ready"] is True
    assert result["summary"]["live_readiness"] == "fail"
    assert result["summary"]["live_cutover"] == "blocked"
    assert result["summary"]["live_cutover_blockers"] > 0
    assert result["summary"]["live_execution_ready"] is False
    assert result["summary"]["paper_ready"] is True
    assert result["summary"]["live_ready"] is False
    assert any("OANDA_API_TOKEN" in item for item in result["next_actions"])
    assert any("official broker" in item for item in result["next_actions"])
    assert load_json(root / "doctor" / "current.json")[0]["status"] == "warn"
