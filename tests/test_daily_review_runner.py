from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.daily_review_runner import DailyReviewRunner
from services.bias_ledger import BiasLedger
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from schemas.market_data import Bar


def test_daily_review_runner_writes_receipt_and_review_artifacts(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    bars = [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), 4500 + index, 4501 + index, 4499 + index, 4500 + index, 1, "gold-api.com", ["live_snapshot"])
        for index in range(240)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "raw_snapshots" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "raw_snapshots" / run_date / "quote_snapshots.json", [{"symbol": "GOLD", "close": bars[-1].close, "provider": "gold-api.com", "record_type": "quote"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "clean_rows": 240, "missing_bars": 0, "spike_flags": 0}])
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true, "missing_ratio": 0, "synthetic_ratio": 0}}\n', encoding="utf-8")
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD", "signal_id": "s1", "direction": "watch", "strength": 52, "confidence": 60, "regime": "no_trade", "thesis": "wait", "invalid_if": "break"}])
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD", "signal_id": "s1", "backtest_id": "b1", "verdict": "no_trade", "sample_size": 0, "profit_factor": 0}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1", "ticket_id": "t1", "status": "filled", "requested_price": 4510, "fill_price": 4510, "quantity": 1}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "tr1", "order_id": "p1", "ticket_id": "t1", "symbol": "GOLD", "side": "long", "status": "open", "quantity": 1, "entry_price": 4510, "stop_loss": 4500, "target": 4530}])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"symbol": "GOLD", "side": "long", "quantity": 1, "avg_price": 4510, "unrealized_pnl": 0, "risk_used_pct": 0.5}}\n', encoding="utf-8")
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"ticket_id": "t1", "signal_id": "s1", "asset": "GOLD", "decision_status": "executed_paper", "paper_order": {"order_id": "p1"}, "risk_snapshot": {"max_loss_pct": 0.5}}])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    write_json(root / "broker_preflight" / "current.json", [{"mode": "paper", "provider": "manual_gateway", "ready": True, "dry_run": True}])
    write_json(root / "oanda_feed" / "current.json", [{"status": "skipped", "ready": False, "imported_rows": 0, "missing_env": ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]}])
    write_json(root / "broker_receipts" / "summary_current.json", [{"errors": [], "total_receipts": 0}])
    write_json(root / "mt5_bridge_smoke" / "current.json", [{"status": "pass", "order": {"order_id": "o1"}}])
    write_json(root / "broker_receipts" / "current.json", [{"order_id": "o1", "status": "filled"}])
    BiasLedger(root, db_path).append_open_view(
        {
            "run_date": run_date,
            "generated_at": "2026-05-26T00:00:00+00:00",
            "direction_score": 65,
            "reference_price": 4510,
            "expiry": {"expires_at": "2099-05-26T12:00:00+00:00", "valid_for_hours": 12},
        }
    )
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(f'{{"run_date": "{run_date}", "state": "ok", "interval_seconds": 300, "finished_at": "{datetime.now(timezone.utc).replace(microsecond=0).isoformat()}"}}\n', encoding="utf-8")

    result = DailyReviewRunner(root, db_path).run(run_date)

    assert result["status"] in {"pass", "warn"}
    assert result["summary"]["mock_ready"] is True
    assert result["summary"]["paper_reconciliation"] == "pass"
    assert result["summary"]["paper_trade_attribution"] == "pass"
    assert result["summary"]["paper_exit_monitor"] in {"pass", "empty"}
    assert result["summary"]["official_feed_receipt"] == "warn"
    assert result["summary"]["official_feed_onboarding"] == "open"
    assert result["summary"]["official_rows"] == 0
    assert result["summary"]["data_trust"] in {"pass", "warn"}
    assert result["summary"]["data_display_mode"] in {"PAPER_PUBLIC", "OFFICIAL_BROKER"}
    assert result["summary"]["mock_uat"] in {"pass", "warn"}
    assert result["summary"]["strategy_guardrails"] in {"pass", "warn", "block"}
    assert result["summary"]["strategy_experiments"] in {"blocked", "collecting_evidence", "experiment_ready"}
    assert result["summary"]["strategy_improvement_plan"] in {"action_required", "promotion_review", "collecting_evidence"}
    assert result["summary"]["strategy_promotion_gate"] in {"blocked", "requestable"}
    assert result["summary"]["operation_runbook"] in {"paper_auto_ready", "paper_manual_only", "data_blocked", "blocked"}
    assert isinstance(result["summary"]["allow_new_paper_order"], bool)
    assert isinstance(result["summary"]["risk_allow_paper_auto_approve"], bool)
    assert result["summary"]["paper_auto_approval_gate"] in {"allow", "block", "skipped"}
    assert result["summary"]["live_cutover"] in {"blocked", "dry_run_ready", "real_money_ready"}
    assert result["summary"]["live_submission_safety"] == "pass"
    assert result["summary"]["live_submission_blocked_by_gate"] is True
    assert result["summary"]["live_submission_network_call_attempted"] is False
    assert result["summary"]["live_dry_run_drill"] in {"blocked", "dry_run_ready", "real_money_ready"}
    assert result["summary"]["safe_to_submit_live_order"] is False
    assert result["summary"]["bot_supervisor"] in {"pass", "warn"}
    assert result["summary"]["bot_checkpoint"] in {"ready", "watch", "attention_required"}
    assert result["summary"]["data_integrity"] in {"pass", "warn"}
    assert isinstance(result["summary"]["mock_bot_running"], bool)
    assert result["artifacts"]["journal"].endswith(f"journals/{run_date}.md")
    assert result["artifacts"]["paper_reconciliation"].endswith(f"paper_reconciliation/{run_date}.json")
    assert result["artifacts"]["paper_trade_attribution"].endswith(f"paper_trade_attribution/{run_date}.json")
    assert result["artifacts"]["paper_exit_monitor"].endswith(f"paper_exit_monitor/{run_date}.json")
    assert result["artifacts"]["mock_uat"].endswith(f"mock_uat/{run_date}.json")
    assert result["artifacts"]["strategy_guardrails"].endswith(f"strategy_guardrails/{run_date}.json")
    assert result["artifacts"]["strategy_experiments"].endswith(f"strategy_experiments/{run_date}.json")
    assert result["artifacts"]["strategy_improvement_plan"].endswith(f"strategy_improvement_plan/{run_date}.json")
    assert result["artifacts"]["strategy_promotion_gate"].endswith(f"strategy_promotion_gate/{run_date}.json")
    assert result["artifacts"]["paper_auto_approval_gate"].endswith(f"paper_auto_approval_gate/{run_date}.json")
    assert result["artifacts"]["operation_runbook"].endswith(f"operation_runbooks/{run_date}.json")
    assert result["artifacts"]["official_feed_receipt"].endswith(f"official_feed_receipts/{run_date}.json")
    assert result["artifacts"]["data_trust"].endswith(f"data_trust/{run_date}.json")
    assert result["artifacts"]["official_feed_onboarding"].endswith(f"official_feed_onboarding/{run_date}.md")
    assert result["artifacts"]["live_cutover"].endswith(f"live_cutover/{run_date}.json")
    assert result["artifacts"]["live_submission_safety"].endswith(f"live_submission_safety/{run_date}.json")
    assert (root / "live_submission_safety" / f"{run_date}.json").exists()
    assert result["artifacts"]["live_dry_run_drill"].endswith(f"live_dry_run_drill/{run_date}.json")
    assert result["artifacts"]["bot_supervisor"].endswith(f"bot_supervisor/{run_date}.json")
    assert result["artifacts"]["bot_checkpoint"].endswith(f"bot_checkpoints/{run_date}.json")
    assert result["artifacts"]["data_integrity"].endswith(f"data_integrity/{run_date}.json")
    assert load_json(root / "daily_review_runs" / "current.json")[0]["finished_at"]
    assert (root / "reports" / f"{run_date}.md").exists()
    assert (root / "journals" / f"{run_date}.md").exists()
    assert (root / "performance" / f"{run_date}.json").exists()
    assert (root / "paper_exit_monitor" / f"{run_date}.json").exists()
    assert (root / "paper_auto_approval_gate" / f"{run_date}.json").exists()
    assert (root / "bot_supervisor" / f"{run_date}.json").exists()
    assert (root / "bot_checkpoints" / f"{run_date}.json").exists()
    assert (root / "data_integrity" / f"{run_date}.json").exists()
    assert (root / "data_trust" / f"{run_date}.json").exists()
    assert (root / "strategy_experiments" / f"{run_date}.json").exists()
    assert (root / "strategy_improvement_plan" / f"{run_date}.json").exists()
    assert (root / "strategy_promotion_gate" / f"{run_date}.json").exists()
    assert (root / "live_dry_run_drill" / f"{run_date}.json").exists()
    lifecycle_rows = load_json(root / "paper_trades" / "current.json") + load_json(root / "paper_trades" / "closed" / f"{run_date}.json")
    trade = lifecycle_rows[0]
    assert trade["signal_id"] == "s1"
    assert trade["attribution_status"] in {"signal", "journal_only"}
