from pathlib import Path

from services.journal_store import write_json
from services.trading_journal import TradingJournalBuilder


def test_trading_journal_builder_writes_daily_markdown(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "signals" / f"{run_date}.json", [{"signal_id": "sig_gold", "asset": "GOLD", "direction": "long", "regime": "trend_following", "strength": 61, "thesis": "wait", "invalid_if": "break"}])
    write_json(root / "backtests" / f"{run_date}.json", [{"signal_id": "sig_gold", "asset": "GOLD", "verdict": "mixed", "sample_size": 0, "profit_factor": 1.6}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"ticket_id": "ticket_gold", "decision_status": "executed_paper", "asset": "GOLD", "notes": "approved", "paper_order": {"order_id": "p1", "status": "filled", "fill_price": 4571}}])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1", "status": "filled", "quantity": 1, "fill_price": 4571}])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"side": "long", "quantity": 1, "avg_price": 4571, "unrealized_pnl": 0}}\n', encoding="utf-8")
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "t1"}])
    write_json(root / "risk_blocks" / f"{run_date}.json", [{"asset": "GOLD", "ticket_id": "ticket_gold_blocked", "signal_id": "sig_gold", "reason": "daily risk cap exceeded", "portfolio_risk": {"used_loss_pct": 1.0, "candidate_loss_pct": 0.5, "projected_loss_pct": 1.5, "daily_loss_stop_pct": 1.25}}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"net_pnl_marked": 0, "unrealized_pnl": 0, "open_risk_amount": 10, "open_unrealized_r": 0, "expectancy_r": 0, "win_rate": 0}}])
    write_json(root / "paper_exit_monitor" / f"{run_date}.json", [{"status": "pass", "summary": {"open_trades": 1, "stop_touched": 0, "target_touched": 0, "nearest_stop_distance_pct": 1.2}, "monitors": [{"trade_id": "t1", "side": "long", "latest_price": 4571, "distance_to_stop_pct": 1.2, "distance_to_target_pct": 3.4}]}])
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true, "missing_ratio": 0, "synthetic_ratio": 0}}\n', encoding="utf-8")
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"latest_price": 4571, "latest_provider": "gold-api.com", "price_sanity": {"passes": True}}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "public_snapshot"}])
    write_json(root / "data_integrity" / f"{run_date}.json", [{"status": "pass", "summary": {"passed": 4, "failed": 0}}])
    write_json(root / "official_feed_receipts" / f"{run_date}.json", [{"status": "warn", "official_rows": 0, "ready_for_live": False}])
    write_json(root / "operation_runbooks" / f"{run_date}.json", [{"status": "paper_manual_only", "permissions": {"paper_manual_review": True, "paper_auto_approve": False, "live_trading": False}}])
    write_json(root / "strategy_learning_actions" / f"{run_date}.json", [{"status": "review_required", "summary": {"action_count": 1, "high_priority": 1}, "actions": [{"priority": "high", "summary": "Review open exposure.", "command": "python3 -m pipelines.daily_review --date 2026-05-26"}]}])
    write_json(root / "paper_auto_approval_gate" / f"{run_date}.json", [{"status": "block", "allow_auto_approve": False, "auto_requested": True, "pending_count": 1, "selected_ticket_id": "ticket_gold_blocked", "reasons": ["manual review required"]}])
    write_json(root / "bot_supervisor" / f"{run_date}.json", [{"status": "pass", "mock_bot_running": True, "summary": {"runner_state": "ok", "latest_price": 4571, "latest_provider": "gold-api.com", "data_age_minutes": 1, "operation_status": "paper_manual_only"}}])
    write_json(root / "live_submission_safety" / f"{run_date}.json", [{"status": "pass", "blocked_by_activation_gate": True, "network_call_attempted": False, "provider": "oanda_rest", "dry_run": False, "live_trading_enabled": True, "error": "live activation gate is not real_money_ready; real broker submission is blocked"}])
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("# Strategy Review Notes\n- Review line\n", encoding="utf-8")

    path = TradingJournalBuilder(root).build(run_date)

    text = path.read_text(encoding="utf-8")
    assert "# Trading Journal - 2026-05-26" in text
    assert "ticket_gold" in text
    assert "executed_paper" in text
    assert "Realized PnL today" in text
    assert "Performance Review" in text
    assert "Exit Monitor" in text
    assert "Nearest stop distance: 1.2%" in text
    assert "Expectancy R" in text
    assert "Data truth: public_snapshot" in text
    assert "Data integrity: pass / pass=4 fail=0" in text
    assert "Official feed: status=warn official_rows=0 live_ready=False" in text
    assert "Trading gate: paper_manual_only paper_manual=True paper_auto=False live=False" in text
    assert "Paper auto gate: block / allow=False" in text
    assert "## Paper Auto Approval Gate" in text
    assert "Bot supervisor: pass / mock_running=True" in text
    assert "## Bot Supervisor" in text
    assert "Live submission safety: pass / blocked_by_activation_gate=True / network_call_attempted=False" in text
    assert "## Live Submission Safety" in text
    assert "Network call attempted: False" in text
    assert "## Strategy Learning Actions" in text
    assert "Review open exposure." in text
    assert "Blocked Candidates" in text
    assert "ticket_gold_blocked" in text
    assert "projected 1.5%" in text
    assert "Review line" in text
