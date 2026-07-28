from pathlib import Path
from datetime import datetime, timedelta, timezone

from services.completion_audit import CompletionAudit
from services.bias_ledger import BiasLedger
from services.dualtrack_cycle_heartbeat import DualTrackCycleHeartbeat
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from schemas.market_data import Bar


def _write_current_dualtrack_liveness(root: Path) -> None:
    heartbeat = DualTrackCycleHeartbeat(root)
    schedule, error = heartbeat._cycle_schedule()
    assert schedule is not None and not error
    expected = heartbeat._latest_required_boundary(datetime.now(timezone.utc), schedule)
    assert expected is not None
    start = heartbeat._previous_start_before_boundary(expected, schedule)
    cycle_id = heartbeat._cycle_id_for_start(start, schedule)
    date_part = cycle_id.rsplit("_", 1)[0]
    write_json(root / "dualtrack" / "ledger" / "daily" / f"{date_part}.json", [{
        "date": date_part,
        "cycles": {cycle_id: {"machine": 0, "human": 0}},
    }])
    write_json(root / "dualtrack" / "attribution" / f"{cycle_id}.json", [{
        "cycle_id": cycle_id,
        "status": "closed",
    }])


def test_completion_audit_surfaces_paper_ready_with_live_broker_warning(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-25"
    store = MarketStore(db_path)
    start = datetime(2026, 5, 25, tzinfo=timezone.utc)
    store.upsert_bars(
        [
            Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), 4570, 4572, 4569, 4570 + index, 1, "broker_csv", [])
            for index in range(220)
        ]
    )
    write_json(root / "raw_snapshots" / run_date / "GOLD_5m.json", [{"close": 4571, "provider": "gold-api.com"}])
    write_json(root / "raw_snapshots" / run_date / "quote_snapshots.json", [{"symbol": "GOLD", "close": 4571, "provider": "gold-api.com", "record_type": "quote"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4571, "provider": "gold-api.com"}])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "clean_rows": 1}])
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true}}\n', encoding="utf-8")
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD", "direction": "watch", "strength": 52, "regime": "no_trade"}])
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD", "backtest_id": "local5m_test", "verdict": "no_trade", "sample_size": 0}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1"}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "t1"}])
    write_json(root / "performance" / f"{run_date}.json", [{"run_date": run_date, "summary": {"open_trade_count": 1, "closed_all_count": 0, "realized_pnl_all": 0, "unrealized_pnl": 0, "net_pnl_marked": 0, "open_unrealized_r": 0, "expectancy_r": 0, "profit_factor": 0, "total_execution_costs": 0}, "open_trades": [{"trade_id": "t1"}], "closed_today": []}])
    write_json(root / "equity_curve" / f"{run_date}.json", [{"status": "pass", "current_equity": 100000, "current_drawdown_pct": 0, "max_drawdown_pct": 0, "point_count": 1, "points": [{"run_date": run_date, "equity": 100000}]}])
    write_json(root / "paper_reconciliation" / f"{run_date}.json", [{"status": "pass", "summary": {"filled_orders": 1, "open_trades": 1, "failed": 0}, "computed_positions": {"GOLD": {"quantity": 1}}}])
    write_json(root / "paper_trade_attribution" / f"{run_date}.json", [{"status": "pass", "summary": {"open_trades": 1, "closed_trades": 0, "attributed_open_trades": 1, "attributed_closed_trades": 0}}])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"quantity": 1}}\n', encoding="utf-8")
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"decision_status": "executed_paper"}])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    write_json(root / "strategy_snapshots" / f"{run_date}.json", [{"config_hash": "abc123def456"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "collect_more_paper_trades"}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold_parameters"}])
    BiasLedger(root, db_path).append_open_view(
        {
            "run_date": run_date,
            "generated_at": "2026-05-25T00:00:00+00:00",
            "direction_score": 65,
            "reference_price": 4570,
            "expiry": {"expires_at": "2099-05-25T12:00:00+00:00", "valid_for_hours": 12},
        }
    )
    write_json(root / "strategy_guardrails" / f"{run_date}.json", [{"status": "warn", "allow_new_paper_order": True, "summary": {"warnings": 1}}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "warn", "kill_switch_active": False, "allow_paper_auto_approve": True}])
    write_json(root / "live_switch_plan" / f"{run_date}.json", [{"status": "blocked"}])
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{run_date}.md").write_text("report\n", encoding="utf-8")
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    write_json(root / "health" / "current.json", [{"checks": [{"name": "data_quality", "status": "ok"}]}])
    write_json(root / "broker_preflight" / "current.json", [{"mode": "paper", "ready": True, "block_reason": "paper mode"}])
    write_json(root / "oanda_feed" / "current.json", [{"status": "skipped", "ready": False, "missing_env": ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]}])
    write_json(root / "mt5_bridge_smoke" / "current.json", [{"status": "pass", "order": {"order_id": "o1"}}])
    write_json(root / "broker_receipts" / "current.json", [{"order_id": "o1", "status": "filled"}])
    write_json(
        root / "schedules" / "current.json",
        [
            {
                "status": "generated",
                "profile": "full",
                "jobs": [
                    {"label": "com.wendy.trading-orchestrator.runner"},
                    {"label": "com.wendy.trading-orchestrator.trading-plan"},
                    {"label": "com.wendy.trading-orchestrator.evening-review"},
                    {"label": "com.wendy.trading-orchestrator.daily-review"},
                    {"label": "com.wendy.trading-orchestrator.daily-24h-report"},
                    {"label": "com.wendy.trading-orchestrator.dashboard"},
                    {"label": "com.wendy.trading-orchestrator.strategies"},
                    {"label": "com.wendy.trading-orchestrator.dualtrack-cycle"},
                    {"label": "com.wendy.trading-orchestrator.dualtrack-live-tick"},
                    {"label": "com.wendy.trading-orchestrator.deadman-ping"},
                ],
            }
        ],
    )
    _write_current_dualtrack_liveness(root)

    result = CompletionAudit(root, db_path).run(run_date)

    assert result["status"] == "warn"
    statuses = {item["name"]: item["status"] for item in result["requirements"]}
    assert statuses["data_pipeline"] == "pass"
    data_evidence = next(item for item in result["requirements"] if item["name"] == "data_pipeline")["evidence"]
    assert data_evidence["quote_snapshot_rows"] == 1
    assert statuses["five_minute_gold_strategy"] == "pass"
    assert statuses["local_storage"] == "pass"
    assert statuses["paper_trading"] == "pass"
    assert statuses["mock_runtime"] == "warn"
    assert statuses["mock_uat"] == "warn"
    assert statuses["paper_performance"] == "pass"
    assert statuses["paper_equity_curve"] == "pass"
    assert statuses["paper_reconciliation"] == "pass"
    assert statuses["paper_trade_attribution"] == "pass"
    assert statuses["risk_monitor"] == "pass"
    assert statuses["journal_and_review"] == "pass"
    assert statuses["human_bias_ledger"] == "pass"
    assert statuses["strategy_guardrails"] == "pass"
    assert statuses["schedule_artifacts"] == "warn"
    assert statuses["dualtrack_cycle_liveness"] == "pass"
    journal_evidence = next(item for item in result["requirements"] if item["name"] == "journal_and_review")["evidence"]
    assert journal_evidence["learning_ledger_rows"] == 1
    assert journal_evidence["strategy_proposal_rows"] == 1
    assert statuses["broker_bridge_smoke"] == "pass"
    assert statuses["oanda_broker_boundary"] == "warn"
    assert statuses["live_submission_safety"] == "pass"
    assert statuses["live_broker_boundary"] == "warn"
    assert statuses["live_cutover_package"] == "pass"
    oanda_evidence = next(item for item in result["requirements"] if item["name"] == "oanda_broker_boundary")["evidence"]
    assert oanda_evidence["dry_run_preflight"]["provider"] == "oanda_rest"
    assert oanda_evidence["dry_run_preflight"]["ready"] is True
    assert "OANDA_API_TOKEN" in oanda_evidence["live_submission_requires"]
    assert load_json(root / "audits" / "current.json")[0]["run_date"] == run_date


def test_completion_audit_fails_without_local_market_db(tmp_path: Path):
    result = CompletionAudit(tmp_path / "outputs", tmp_path / "missing.db").run("2026-05-26")

    assert result["status"] == "fail"
    assert any(item["name"] == "local_storage" and item["status"] == "fail" for item in result["requirements"])


def test_completion_audit_warns_when_bridge_smoke_missing(tmp_path: Path):
    result = CompletionAudit(tmp_path / "outputs", tmp_path / "missing.db").run("2026-05-26")
    statuses = {item["name"]: item["status"] for item in result["requirements"]}

    assert statuses["broker_bridge_smoke"] == "warn"


def test_completion_audit_focus_profile_parks_daily_review_without_passing(tmp_path: Path):
    result = CompletionAudit(tmp_path / "outputs", tmp_path / "missing.db")._daily_review_run("2026-05-26")

    assert result["name"] == "daily_review_run"
    assert result["status"] == "warn"
    assert "parked_by_focus_mode" in result["summary"]
    assert result["evidence"]["restore_path"] == "schedule.profile: full"


def test_completion_audit_focus_profile_requires_only_focus_schedule_jobs(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(
        root / "schedules" / "current.json",
        [
            {
                "status": "generated",
                "profile": "dualtrack_focus",
                "jobs": [
                    {"label": "com.wendy.trading-orchestrator.dashboard"},
                    {"label": "com.wendy.trading-orchestrator.dualtrack-cycle"},
                    {"label": "com.wendy.trading-orchestrator.gold-1m-feed"},
                    {"label": "com.wendy.trading-orchestrator.dualtrack-live-tick"},
                    {"label": "com.wendy.trading-orchestrator.deadman-ping"},
                    {"label": "com.wendy.trading-orchestrator.daily-24h-report"},
                ],
            }
        ],
    )

    result = CompletionAudit(root, tmp_path / "missing.db")._schedule_artifacts("2026-05-26")

    assert result["name"] == "schedule_artifacts"
    assert result["status"] == "warn"
    assert result["evidence"]["profile"] == "dualtrack_focus"
    assert result["evidence"]["required_labels"] == [
        "com.wendy.trading-orchestrator.gold-1m-feed",
        "com.wendy.trading-orchestrator.dualtrack-live-tick",
        "com.wendy.trading-orchestrator.dashboard",
        "com.wendy.trading-orchestrator.deadman-ping",
        "com.wendy.trading-orchestrator.daily-24h-report",
    ]


def test_completion_audit_fails_when_learning_artifacts_missing(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-27"
    write_json(root / "journal_decisions" / f"{run_date}.json", [])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{run_date}.md").write_text("report\n", encoding="utf-8")
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")

    result = CompletionAudit(root, tmp_path / "missing.db").run(run_date)

    journal = next(item for item in result["requirements"] if item["name"] == "journal_and_review")
    assert journal["status"] == "fail"
    assert journal["evidence"]["learning_ledger_rows"] == 0
    assert journal["evidence"]["strategy_proposal_rows"] == 0
