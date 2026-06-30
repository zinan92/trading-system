from pathlib import Path

from services.dashboard_state import DashboardState
from services.journal_store import write_json
from services.market_store import MarketStore
from schemas.market_data import Bar


def test_gold_nav_bars_for_strategy_window_uses_historical_clean_bars(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(
        root / "clean_bars" / "2026-06-02" / "GOLD_5m.json",
        [
            {"timestamp": "2026-06-02T15:55:00+00:00", "close": 4100},
            {"timestamp": "2026-06-02T16:00:00+00:00", "close": 4110},
        ],
    )
    write_json(
        root / "clean_bars" / "2026-06-29" / "GOLD_5m.json",
        [
            {"timestamp": "2026-06-29T09:15:00+00:00", "close": 4055},
            {"timestamp": "2026-06-29T09:20:00+00:00", "close": 4050},
        ],
    )
    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db")

    rows = state._gold_nav_bars_for_strategy_window(
        run_date="2026-06-29",
        bars=[{"timestamp": "2026-06-29T09:20:00+00:00", "close": 4050}],
        strategy_rows=[
            {
                "strategy_id": "gold_1m_macd",
                "nav_points": [{"timestamp": "2026-06-02T15:56:02+00:00", "equity": 10_000}],
            }
        ],
    )

    assert [row["timestamp"] for row in rows] == [
        "2026-06-02T16:00:00+00:00",
        "2026-06-29T09:15:00+00:00",
        "2026-06-29T09:20:00+00:00",
    ]


def test_dashboard_state_aggregates_outputs_and_risk(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-13"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4572, "provider": "gold-api.com"}])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m"}])
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD", "direction": "long"}])
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD", "verdict": "supportive", "sample_size": 42}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [{"asset": "GOLD", "max_loss_pct": 0.5}])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1"}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "t1", "status": "open"}])
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [{"trade_id": "t0", "realized_pnl": 12.5}])
    write_json(root / "performance" / "current.json", [{"summary": {"net_pnl_marked": 12.5, "expectancy_r": 1.2}}])
    write_json(root / "equity_curve" / "current.json", [{"status": "pass", "current_equity": 100012.5, "current_drawdown_pct": 0, "max_drawdown_pct": 0, "point_count": 1}])
    write_json(root / "paper_reconciliation" / "current.json", [{"status": "pass", "summary": {"filled_orders": 1, "open_trades": 1, "failed": 0}}])
    write_json(root / "paper_trade_attribution" / "current.json", [{"status": "pass", "summary": {"open_trades": 1, "closed_trades": 1, "attributed_open_trades": 1, "attributed_closed_trades": 1}}])
    write_json(root / "paper_exit_monitor" / "current.json", [{"status": "pass", "summary": {"open_trades": 1, "stop_touched": 0, "target_touched": 0, "nearest_stop_distance_pct": 1.2}}])
    write_json(root / "daily_review_runs" / "current.json", [{"status": "pass", "summary": {"net_pnl_marked": 12.5}, "artifacts": {"journal": "j.md"}}])
    write_json(root / "operation_runbooks" / "current.json", [{"status": "paper_auto_ready", "permissions": {"paper_auto_approve": True}}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"decision_status": "executed_paper", "paper_order": {"order_id": "p1"}, "risk_snapshot": {"max_loss_pct": 0.5}}])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true, "missing_ratio": 0}}\n', encoding="utf-8")
    write_json(
        root / "market_views" / f"{run_date}.json",
        [
            {
                "run_date": run_date,
                "source": "Park口述",
                "generated_at": "2026-05-13T00:00:00+00:00",
                "direction_score": 10,
                "direction_bias": "strong_short",
                "stance": "只做空",
                "summary": "强空测试",
                "trade_plan": "只保留空头候选",
                "reference_price": 4580,
                "expiry": {
                    "valid_for_hours": 12,
                    "expires_at": "2026-05-13T12:00:00+00:00",
                    "expires_if_price_moves_pct": 1.0,
                    "reference_price": 4580,
                },
            }
        ],
    )
    write_json(root / "data_gaps" / "current.json", [{"status": "pass", "gap_count": 0}])
    write_json(root / "data_gap_repair_requests" / "current.json", [{"status": "pass"}])
    write_json(root / "data_archive" / "current.json", [{"status": "pass", "present_file_count": 14, "file_count": 14, "gold_5m_rows": 220}])
    write_json(root / "data_integrity" / "current.json", [{"status": "pass", "summary": {"passed": 4, "failed": 0}}])
    write_json(root / "broker_preflight" / "current.json", [{"mode": "paper", "provider": "manual_gateway", "ready": True}])
    write_json(root / "data_source_preflight" / "current.json", [{"status": "warn", "ready_for_paper": True, "ready_for_live": False, "latest_provider": "gold-api.com"}])
    write_json(root / "data_source_lineage" / "current.json", [{"status": "warn", "truth_level": "public_snapshot", "ready_for_live": False}])
    write_json(root / "data_trust" / "current.json", [{"status": "warn", "display_mode": "PAPER_PUBLIC", "summary": {"latest_is_mock": False}}])
    write_json(root / "official_feed_receipts" / "current.json", [{"status": "warn", "ready_for_live": False, "official_rows": 0}])
    write_json(root / "official_feed_onboarding" / "current.json", [{"status": "open", "current_official_rows": 0, "feed_dir": "feed"}])
    write_json(root / "broker_feed_doctor" / "current.json", [{"status": "pass", "file_count": 1, "valid_file_count": 1, "row_count": 2}])
    write_json(root / "broker_feed_imports" / "current.json", [{"provider": "mt5_csv", "new_files": 1, "imported_rows": 2}])
    write_json(root / "oanda_feed" / "current.json", [{"status": "skipped", "ready": False, "missing_env": ["OANDA_API_TOKEN"]}])
    write_json(root / "oanda_account" / "current.json", [{"status": "skipped", "ready": False, "missing_env": ["OANDA_API_TOKEN"], "instrument_name": "XAU_USD"}])
    write_json(root / "broker_receipts" / "current.json", [{"order_id": "o1", "status": "filled"}])
    write_json(root / "broker_receipts" / "summary_current.json", [{"new_receipts": 1, "total_receipts": 1}])
    write_json(root / "mt5_bridge_smoke" / "current.json", [{"status": "pass", "order": {"status": "bridge_dry_run"}}])
    write_json(root / "live_order_requests" / f"{run_date}.json", [{"order_id": "live1"}])
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "review", "metrics": {"open_trade_count": 1}, "suggestions": ["keep"]}])
    write_json(root / "strategy_snapshots" / "current.json", [{"strategy_id": "gold_5m_v1", "config_hash": "abc123def456"}])
    write_json(root / "learning_ledger" / "current.json", [{"review_days": 2, "learning_state": "collect_more_paper_trades"}])
    write_json(root / "strategy_change_proposals" / "current.json", [{"status": "hold_parameters", "reason": "not enough evidence"}])
    write_json(root / "strategy_learning_actions" / "current.json", [{"status": "review_required", "summary": {"action_count": 2, "high_priority": 1}}])
    write_json(root / "strategy_experiments" / "current.json", [{"status": "blocked", "paper_only": True, "auto_apply": False, "experiments": [{"variant_id": "baseline"}]}])
    write_json(root / "strategy_improvement_plan" / "current.json", [{"status": "action_required", "paper_only": True, "auto_apply": False, "next_steps": [{"step_id": "official_feed"}]}])
    write_json(root / "strategy_promotion_gate" / "current.json", [{"status": "blocked", "promotion_allowed": False, "auto_apply": False}])
    write_json(root / "strategy_guardrails" / "current.json", [{"status": "warn", "allow_new_paper_order": True, "summary": {"warnings": 1}}])
    write_json(
        root / "strategies" / "summary_current.json",
        [
            {
                "strategy_count": 2,
                "strategies": [
                    {
                        "strategy_id": "gold_1m_chan",
                        "execution_profile": {
                            "adapter": "binance_demo",
                            "mode": "binance_futures_demo",
                            "armed": True,
                            "endpoint": "https://demo-fapi.binance.com",
                        },
                    }
                ],
            }
        ],
    )
    write_json(root / "risk_monitor" / "current.json", [{"status": "warn", "kill_switch_active": False, "allow_paper_auto_approve": True}])
    write_json(root / "paper_auto_approval_gate" / "current.json", [{"status": "allow", "allow_auto_approve": True, "selected_ticket_id": "ticket_gold"}])
    write_json(root / "health" / "current.json", [{"status": "ok", "checks": []}])
    write_json(root / "audits" / "current.json", [{"status": "warn", "requirements": [{"name": "live_broker_boundary", "status": "warn"}]}])
    write_json(root / "mock_runtime" / "current.json", [{"status": "pass", "mock_ready": True, "mock_running": True}])
    write_json(root / "mock_uat" / "current.json", [{"status": "pass", "summary": {"passed": 6, "failed": 0}}])
    write_json(root / "bot_supervisor" / "current.json", [{"status": "pass", "mock_bot_running": True, "summary": {"runner_state": "ok", "latest_price": 4580}}])
    write_json(root / "bot_checkpoints" / "current.json", [{"status": "ready", "mock_recoverable": True, "resume_actions": [{"action_id": "continue_mock_loop"}]}])
    write_json(root / "live_readiness" / "current.json", [{"status": "fail", "live_ready": False, "summary": {"failed": 3}}])
    write_json(root / "live_activation" / "current.json", [{"status": "blocked", "dry_run_ready": False, "real_money_ready": False}])
    write_json(root / "live_approvals" / "current.json", [{"status": "requested", "approved": False}])
    write_json(root / "live_submission_safety" / "current.json", [{"status": "pass", "blocked_by_activation_gate": True, "network_call_attempted": False}])
    write_json(root / "live_broker_preflight" / "current.json", [{"status": "blocked", "real_submit_blocked": True}])
    write_json(root / "live_dry_run_drill" / "current.json", [{"status": "blocked", "safe_to_submit_live_order": False, "data_truth_level": "public_snapshot"}])
    write_json(root / "live_switch_plan" / "current.json", [{"status": "blocked", "steps": [{"name": "official_5m_data", "status": "blocked"}]}])
    write_json(root / "live_cutover" / "current.json", [{"status": "blocked", "blockers": [{"name": "official_market_data"}]}])
    write_json(root / "doctor" / "current.json", [{"status": "warn", "summary": {"paper_ready": True, "live_ready": False}, "next_actions": ["import official feed"]}])
    write_json(root / "schedules" / "current.json", [{"status": "generated", "jobs": [{"label": "com.wendy.trading-orchestrator.runner"}]}])
    write_json(root / "schedules" / "status_current.json", [{"status": "generated_only", "installed_count": 0, "loaded_count": 0, "required_count": 3}])
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text('{"state": "ok", "run_date": "2026-05-13"}\n', encoding="utf-8")
    MarketStore(tmp_path / "market.db").upsert_quote(Bar("GOLD", "5m", "2026-05-13T00:05:00+00:00", 4580, 4580, 4580, 4580, 0, "gold-api.com", ["live_quote"]))

    state = DashboardState(output_root=root, market_db=tmp_path / "market.db").snapshot(run_date)

    assert state["latest"]["close"] == 4572
    assert state["latest_quote"]["close"] == 4580
    assert state["backtests"][0]["verdict"] == "supportive"
    assert state["risk"]["used_loss_pct"] == 0.5
    assert state["risk"]["allows_next_paper_order"] is True
    assert state["data_quality"]["GOLD"]["allows_trading"] is True
    assert state["data_gaps"]["status"] == "pass"
    assert state["data_gap_repair"]["status"] == "pass"
    assert state["data_archive"]["status"] == "pass"
    assert state["data_integrity"]["status"] == "pass"
    assert state["performance"]["open_trades"] == 1
    assert state["performance"]["realized_pnl"] == 12.5
    assert state["paper_performance"]["summary"]["expectancy_r"] == 1.2
    assert state["equity_curve"]["current_equity"] == 100012.5
    assert state["nav_curve_intraday"]["point_count"] == 0
    assert state["paper_reconciliation"]["status"] == "pass"
    assert state["paper_trade_attribution"]["summary"]["attributed_open_trades"] == 1
    assert state["paper_exit_monitor"]["summary"]["nearest_stop_distance_pct"] == 1.2
    assert state["daily_review"]["status"] == "pass"
    assert state["operation_runbook"]["status"] == "paper_auto_ready"
    assert state["runner"]["state"] == "ok"
    assert state["market_db"]["exists"] is True
    assert state["data_provenance"]["mode"] == "PAPER_PUBLIC"
    assert state["data_provenance"]["allows_paper"] is True
    assert state["data_provenance"]["allows_live"] is False
    assert state["data_provenance"]["latest_provider"] == "gold-api.com"
    assert state["data_provenance"]["latest_truth_level"] == "public"
    assert state["data_provenance"]["latest_is_public"] is True
    assert state["ohlc_quality"]["provider"] == "gold-api.com"
    assert state["ohlc_quality"]["promotion_ready"] is False
    assert state["market_data_gate"]["mode"] == "replay_only"
    assert state["market_data_gate"]["trader_label"] == "仅可回放 / Replay only"
    assert "no_execution_grade_ohlc" in state["market_data_gate"]["blockers"]
    assert state["market_view_status"]["status"] == "active"
    assert state["market_view_status"]["direction_bias"] == "strong_short"
    assert state["market_view_status"]["filter_effect"] == "active_direction_filter_enabled"
    assert state["market_view_status"]["price_expiry_ready"] is True
    assert state["market_data_gate"]["oanda"]["missing_env"] == ["OANDA_API_TOKEN"]
    assert state["market_data_gate"]["broker_csv"]["valid_file_count"] == 1
    assert state["performance_board"]["ohlc_quality"]["trust_label"] == "Replay only / 仅用于回放"
    assert state["performance_board"]["market_data_gate"]["mode"] == "replay_only"
    assert state["strategy_detail"]["ohlc_quality"]["official_rows"] == 0
    assert state["strategy_config"]["gold_5m_v1"]["signal"]["ma_short_bars"] == 5
    assert state["risk_rules"]["default"]["max_loss_pct"] == 0.5
    assert state["broker_preflight"]["ready"] is True
    assert state["data_source_preflight"]["ready_for_paper"] is True
    assert state["data_source_lineage"]["truth_level"] == "public_snapshot"
    assert state["data_trust"]["display_mode"] == "PAPER_PUBLIC"
    assert state["official_feed_receipt"]["official_rows"] == 0
    assert state["official_feed_onboarding"]["status"] == "open"
    assert state["broker_feed_doctor"]["status"] == "pass"
    assert state["broker_feed"]["provider"] == "mt5_csv"
    assert state["oanda_feed"]["status"] == "skipped"
    assert state["oanda_account"]["instrument_name"] == "XAU_USD"
    assert state["journal"] == "journal\n"
    assert state["broker_receipts"][0]["status"] == "filled"
    assert state["broker_receipt_summary"]["total_receipts"] == 1
    assert state["mt5_bridge_smoke"]["status"] == "pass"
    assert state["live_order_requests"][0]["order_id"] == "live1"
    assert state["strategy_review"]["summary"] == "review"
    assert state["strategy_snapshot"]["config_hash"] == "abc123def456"
    assert state["learning_ledger"]["review_days"] == 2
    assert state["strategy_change_proposal"]["status"] == "hold_parameters"
    assert state["strategy_learning_actions"]["status"] == "review_required"
    assert state["strategy_experiments"]["paper_only"] is True
    assert state["strategy_improvement_plan"]["status"] == "action_required"
    assert state["strategy_promotion_gate"]["promotion_allowed"] is False
    assert state["strategy_guardrails"]["allow_new_paper_order"] is True
    assert state["strategy_summary"]["strategies"][0]["execution_profile"]["adapter"] == "binance_demo"
    assert state["strategy_summary"]["strategies"][0]["execution_profile"]["armed"] is True
    assert state["risk_monitor"]["allow_paper_auto_approve"] is True
    assert state["paper_auto_approval_gate"]["allow_auto_approve"] is True
    assert state["health"]["status"] == "ok"
    assert state["audit"]["status"] == "warn"
    assert state["mock_runtime"]["mock_running"] is True
    assert state["mock_uat"]["status"] == "pass"
    assert state["bot_supervisor"]["mock_bot_running"] is True
    assert state["bot_checkpoint"]["mock_recoverable"] is True
    assert state["live_readiness"]["live_ready"] is False
    assert state["live_activation"]["status"] == "blocked"
    assert state["live_approval"]["status"] == "requested"
    assert state["live_submission_safety"]["blocked_by_activation_gate"] is True
    assert state["live_broker_preflight"]["real_submit_blocked"] is True
    assert state["live_dry_run_drill"]["safe_to_submit_live_order"] is False
    assert state["live_switch_plan"]["steps"][0]["name"] == "official_5m_data"
    assert state["live_cutover"]["status"] == "blocked"
    assert state["doctor"]["status"] == "warn"
    assert state["schedule"]["status"] == "generated"
    assert state["schedule_status"]["status"] == "generated_only"
    assert "performance_board" in state
    assert "strategy_detail" in state
    assert "dashboard_health" in state
    assert state["contract"]["schema_version"] == "dashboard-v2.1"
    assert "performance_board" in state["contract"]["stable_sections"]
    assert state["contract"]["sample_count_contract"]["source_of_truth"] == "daily_execution.executed_trade_count"
    assert "watch" in state["contract"]["sample_count_contract"]["excludes"]
    assert "no-trade" in state["contract"]["sample_count_contract"]["excludes"]
    assert state["source_contracts"]["performance_board"]["required_sources"][0]["path"].endswith("strategy_leaderboard/current.json")
    schema_check = next(item for item in state["dashboard_health"]["checks"] if item["name"] == "schema_contract")
    assert schema_check["schema_version"] == "dashboard-v2.1"
    ohlc_check = next(item for item in state["dashboard_health"]["checks"] if item["name"] == "ohlc_quality")
    assert ohlc_check["status"] == "warn"
    market_data_check = next(item for item in state["dashboard_health"]["checks"] if item["name"] == "market_data_gate")
    assert market_data_check["mode"] == "replay_only"
    assert market_data_check["official_rows"] == 0


def test_dashboard_health_surfaces_alert_delivery_attention(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
    )
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": run_date, "ready_for_paper": True, "status": "pass"}])
    write_json(
        root / "health" / "current.json",
        [{
            "status": "warn",
            "checks": [
                {"name": "alert_delivery", "status": "warn", "message": "Telegram alert delivery is not configured; alerts are recorded locally only"},
                {"name": "pipeline_artifacts", "status": "ok", "message": "ok"},
            ],
        }],
    )

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    attention = state["dashboard_health"]["health_attention"]

    assert attention == [
        {
            "name": "alert_delivery",
            "status": "warn",
            "message": "Telegram alert delivery is not configured; alerts are recorded locally only",
        }
    ]
    check = next(item for item in state["dashboard_health"]["checks"] if item["name"] == "health_attention")
    assert check["status"] == "warn"


def test_dashboard_state_exposes_performance_board_trade_samples(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [
            {"timestamp": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
            {"timestamp": "2026-06-21T00:05:00+00:00", "open": 100, "high": 103, "low": 100, "close": 102, "volume": 1},
        ],
    )
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": run_date, "ready_for_paper": True, "status": "pass"}])
    write_json(root / "health" / "current.json", [{"status": "ok", "checks": []}])
    write_json(root / "strategy_frequency" / "current.json", [{"run_date": run_date, "status": "within_portfolio_sample_target"}])
    write_json(
        root / "daily_trade_samples" / "current.json",
        [{
            "run_date": run_date,
            "status": "below_minimum_executed_trades",
            "summary": {"observation_count": 9, "candidate_count": 2, "ticket_count": 1, "executed_count": 1},
        }],
    )
    write_json(
        root / "strategy_leaderboard" / "current.json",
        [{
            "run_date": run_date,
            "generated_at": "2026-06-21T00:10:00+00:00",
            "strategy_count": 2,
            "strategies": [
                {
                    "strategy_id": "alpha",
                    "rank": 1,
                    "engine": "chan",
                    "timeframe": "1m",
                    "classification": {"family": "chan", "family_label": "缠论"},
                    "daily_execution": {"executed_trade_count": 1, "signal_count": 1, "ticket_count": 1},
                    "starting_equity": 10000,
                    "current_equity": 10050,
                    "return_pct": 0.5,
                    "vs_gold_pct": 0.1,
                    "max_drawdown_pct": -0.1,
                    "win_rate": 0.5,
                    "net_pnl": 50,
                    "closed_trades": 0,
                    "open_trades": 1,
                    "position": {"summary": "flat"},
                    "equity_points": [
                        {"timestamp": "2026-06-20T00:00:00+00:00", "equity": 10000},
                        {"timestamp": "2026-06-21T00:00:00+00:00", "equity": 10050},
                    ],
                },
                {
                    "strategy_id": "beta",
                    "rank": 2,
                    "engine": "breakout",
                    "timeframe": "1m",
                    "classification": {"family": "breakout", "family_label": "突破"},
                    "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 0},
                    "starting_equity": 10000,
                    "current_equity": 10000,
                    "return_pct": 0,
                    "max_drawdown_pct": 0,
                    "win_rate": 0,
                    "net_pnl": 0,
                    "position": {"summary": "flat"},
                    "equity_points": [{"timestamp": "2026-06-21T00:00:00+00:00", "equity": 10000}],
                },
            ],
        }],
    )
    write_json(root / "strategies" / "alpha" / "paper_orders" / f"{run_date}.json", [{"ticket_id": "t1", "status": "filled"}])
    write_json(root / "strategies" / "alpha" / "paper_orders" / "2026-06-19.json", [{"ticket_id": "t0", "status": "filled"}])
    write_json(root / "strategies" / "beta" / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "watch", "status": "no_signal"}])

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    board = state["performance_board"]
    alpha = next(row for row in board["strategies"] if row["strategy_id"] == "alpha")
    beta = next(row for row in board["strategies"] if row["strategy_id"] == "beta")

    assert board["strategy_count"] == 2
    assert board["leaderboard"] == board["strategies"]
    assert board["today_executed_trade_count"] == 1
    assert state["daily_trade_samples"]["summary"]["observation_count"] == 9
    assert state["daily_trade_samples"]["summary"]["candidate_count"] == 2
    assert state["daily_trade_samples"]["summary"]["ticket_count"] == 1
    assert state["daily_trade_samples"]["summary"]["executed_count"] == 1
    assert alpha["today_trade_count"] == 1
    assert alpha["trade_count_7d"] == 2
    assert alpha["sample_status"] == "active"
    assert beta["today_trade_count"] == 0
    assert beta["is_low_sample"] is True
    assert "beta" in board["low_sample_strategy_ids"]
    assert board["gold_nav"]["point_count"] == 2
    assert board["nav_quality_summary"]["source"] == "paper_equity_curve_checkpoints"
    assert board["nav_quality_summary"]["with_nav_strategy_count"] == 2
    assert board["nav_quality_summary"]["comparable_strategy_count"] == 0
    assert board["nav_quality_summary"]["low_confidence_strategy_count"] == 2
    assert board["nav_quality_summary"]["trader_warning"] == "strategy_nav_points_are_equity_checkpoints_not_minute_curve"
    assert board["realized_evidence_summary"]["trader_warning"] == "separate_realized_pnl_from_open_marked_pnl"
    assert board["realized_evidence_summary"]["strategy_count"] == 2
    assert board["realized_evidence_summary"]["with_closed_trade_strategy_count"] == 0
    assert board["realized_evidence_summary"]["open_pnl_only_strategy_count"] == 1
    assert board["realized_evidence_summary"]["total_realized_pnl"] == 0
    assert board["realized_evidence_summary"]["total_open_marked_pnl"] == 50
    assert alpha["nav_quality"]["source"] == "paper_equity_curve_checkpoints"
    assert alpha["nav_quality"]["status"] == "not_comparable"
    assert alpha["nav_quality"]["overlap_point_count"] == 1
    assert alpha["nav_quality"]["recommended_view"] == "edge_checkpoints"
    assert beta["nav_quality"]["status"] == "not_comparable"
    assert beta["nav_quality"]["cadence"] == "single_checkpoint"
    assert alpha["realized_evidence"]["status"] == "open_pnl_only"
    assert alpha["realized_evidence"]["closed_trade_count"] == 0
    assert alpha["realized_evidence"]["open_trade_count"] == 1
    assert alpha["realized_evidence"]["realized_pnl"] == 0
    assert alpha["realized_evidence"]["open_marked_pnl"] == 50
    assert alpha["realized_evidence"]["trader_action"] == "wait_for_close"
    assert beta["realized_evidence"]["status"] == "waiting_for_closed"
    assert beta["realized_evidence"]["trader_action"] == "collect_samples"
    assert state["strategy_frequency"]["status"] == "within_portfolio_sample_target"
    assert alpha["closed_loop_audit"]["sample_source"] == "executed_trade_count"
    assert alpha["closed_loop_audit"]["counts_watch_no_trade_as_sample"] is False
    assert alpha["closed_loop_audit"]["status"] == "explainability_gap"
    assert "filled_order_without_trade" in alpha["closed_loop_audit"]["gap_types"]
    assert alpha["closed_loop_audit"]["next_action"] == "reconcile_order_trade_records"
    assert alpha["closed_loop_audit"]["root_cause_count"] == 1
    assert alpha["closed_loop_audit"]["dominant_gap_group"]["root_cause"] == "order_trade_reconciliation"
    assert alpha["performance_confidence"]["status"] == "open_pnl_only"
    assert alpha["performance_confidence"]["uses_unrealized_pnl"] is True
    assert beta["closed_loop_audit"]["status"] == "waiting_for_samples"
    assert beta["performance_confidence"]["status"] == "waiting_for_samples"
    assert "alpha" in board["explainability_gap_strategy_ids"]
    assert "alpha" in board["closed_loop_issue_strategy_ids"]
    assert board["explainability_gap_root_cause_count"] == 1
    assert board["explainability_gap_groups"][0]["root_cause"] == "order_trade_reconciliation"
    explainability_check = next(item for item in state["dashboard_health"]["checks"] if item["name"] == "strategy_explainability")
    closed_loop_check = next(item for item in state["dashboard_health"]["checks"] if item["name"] == "closed_loop_coverage")
    assert explainability_check["status"] == "warn"
    assert explainability_check["root_cause_count"] == 1
    assert closed_loop_check["status"] == "warn"


def test_dashboard_state_surfaces_active_demo_reconciliation_blocker(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [
            {"timestamp": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
            {"timestamp": "2026-06-21T00:05:00+00:00", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1},
        ],
    )
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": run_date, "ready_for_paper": True, "status": "pass"}])
    write_json(root / "health" / "current.json", [{"status": "ok", "checks": []}])
    write_json(
        root / "trading_plans" / "current.json",
        [{
            "run_date": run_date,
            "active_strategy_id": "gold_1m_chan",
            "allowed_to_trade": True,
            "decision": "ALLOW",
        }],
    )
    write_json(
        root / "strategy_leaderboard" / "current.json",
        [{
            "run_date": run_date,
            "generated_at": "2026-06-21T00:10:00+00:00",
            "strategies": [
                {
                    "strategy_id": "gold_1m_chan",
                    "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 0},
                    "equity_points": [{"timestamp": "2026-06-21T00:00:00+00:00", "equity": 10000}],
                }
            ],
        }],
    )
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{
            "run_date": run_date,
            "provider": "binance_usdm",
            "reconciled": False,
            "error": "",
            "drift_count": 1,
            "drifts": [{"exchange_symbol": "XAUUSDT", "reason": "exchange position has no local record"}],
        }],
    )

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    board = state["performance_board"]
    blocker_check = next(item for item in state["dashboard_health"]["checks"] if item["name"] == "active_demo_blocker")

    assert board["active_strategy_id"] == "gold_1m_chan"
    assert board["can_trade_today"] is False
    assert board["active_demo_blocker"]["blocked"] is True
    assert board["active_demo_blocker"]["status"] == "orphan_demo_position"
    assert "exchange position has no local record" in board["active_demo_blocker"]["reason"]
    assert blocker_check["status"] == "warn"


def test_dashboard_state_surfaces_active_demo_reconciliation_error_blocker(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
    )
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": run_date, "ready_for_paper": True, "status": "pass"}])
    write_json(root / "health" / "current.json", [{"status": "ok", "checks": []}])
    write_json(
        root / "trading_plans" / "current.json",
        [{"run_date": run_date, "active_strategy_id": "gold_1m_chan", "allowed_to_trade": True}],
    )
    write_json(
        root / "strategy_leaderboard" / "current.json",
        [{
            "run_date": run_date,
            "generated_at": "2026-06-21T00:10:00+00:00",
            "strategies": [{"strategy_id": "gold_1m_chan", "daily_execution": {"executed_trade_count": 0}}],
        }],
    )
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{
            "run_date": run_date,
            "provider": "binance_usdm",
            "reconciled": False,
            "error": "TimeoutError: The read operation timed out",
            "drift_count": 0,
            "drifts": [],
        }],
    )

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    board = state["performance_board"]
    blocker_check = next(item for item in state["dashboard_health"]["checks"] if item["name"] == "active_demo_blocker")

    assert board["can_trade_today"] is False
    assert board["active_demo_blocker"]["blocked"] is True
    assert board["active_demo_blocker"]["status"] == "reconciliation_error"
    assert "TimeoutError" in board["active_demo_blocker"]["reason"]
    assert blocker_check["status"] == "warn"


def test_dashboard_state_surfaces_reconciliation_unknown_and_naked_position(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": run_date, "ready_for_paper": True, "status": "pass"}])
    write_json(root / "health" / "current.json", [{"status": "ok", "checks": []}])
    write_json(root / "trading_plans" / "current.json", [{"run_date": run_date, "active_strategy_id": "gold_1m_chan", "allowed_to_trade": True}])
    write_json(
        root / "strategy_leaderboard" / "current.json",
        [{"run_date": run_date, "strategies": [{"strategy_id": "gold_1m_chan", "daily_execution": {"executed_trade_count": 0}}]}],
    )
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{
            "run_date": run_date,
            "reconciled": False,
            "confirmation_status": "cannot_confirm",
            "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
            "reason_code": "naked_position_suspected",
            "suspected_naked_position": True,
            "escalation_action": "halt_new_orders_and_verify_or_flatten_suspected_naked_position",
            "error": "TimeoutError: venue read timed out",
            "drift_count": 0,
            "drifts": [],
        }],
    )

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    blocker = state["performance_board"]["active_demo_blocker"]

    assert state["performance_board"]["can_trade_today"] is False
    assert blocker["blocked"] is True
    assert blocker["status"] == "suspected_naked_position"
    assert blocker["system_state"] == "BLOCKED_RECONCILIATION_UNKNOWN"
    assert blocker["reason_code"] == "naked_position_suspected"
    assert "naked position" in blocker["reason"]


def test_dashboard_state_prefers_active_demo_reconciliation_over_stale_global(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-25"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-25T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
    )
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": run_date, "ready_for_paper": True, "status": "pass"}])
    write_json(root / "health" / "current.json", [{"status": "ok", "checks": []}])
    write_json(root / "trading_plans" / "current.json", [{"run_date": run_date, "active_strategy_id": "gold_1m_chan", "allowed_to_trade": True}])
    write_json(
        root / "strategy_leaderboard" / "current.json",
        [{
            "run_date": run_date,
            "strategies": [
                {
                    "strategy_id": "gold_1m_chan",
                    "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 0},
                    "equity_points": [{"timestamp": "2026-06-25T00:00:00+00:00", "equity": 10000}],
                }
            ],
        }],
    )
    write_json(
        root / "live_reconciliation" / "current.json",
        [{
            "run_date": "2026-06-21",
            "provider": "binance_usdm",
            "reconciled": True,
            "drift_count": 0,
            "checked_at": "2026-06-21T00:00:00+00:00",
        }],
    )
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{
            "run_date": run_date,
            "provider": "binance_usdm",
            "reconciled": True,
            "drift_count": 0,
            "checked_at": "2026-06-25T06:45:04+00:00",
        }],
    )

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)

    assert state["live_reconciliation"]["run_date"] == run_date
    assert state["live_reconciliation"]["truth_scope"] == "active_demo_strategy"
    assert state["live_reconciliation"]["strategy_id"] == "gold_1m_chan"
    assert state["legacy_live_reconciliation"]["run_date"] == "2026-06-21"
    assert state["legacy_live_reconciliation"]["truth_scope"] == "legacy_global"


def test_dashboard_state_treats_protective_failure_as_clear_after_emergency_close(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
    )
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": run_date, "ready_for_paper": True, "status": "pass"}])
    write_json(root / "health" / "current.json", [{"status": "ok", "checks": []}])
    write_json(root / "trading_plans" / "current.json", [{"run_date": run_date, "active_strategy_id": "gold_1m_chan", "allowed_to_trade": True, "decision": "ALLOW"}])
    write_json(
        root / "strategy_leaderboard" / "current.json",
        [{
            "run_date": run_date,
            "strategies": [
                {
                    "strategy_id": "gold_1m_chan",
                    "daily_execution": {"executed_trade_count": 1, "signal_count": 1, "ticket_count": 1},
                    "equity_points": [{"timestamp": "2026-06-21T00:00:00+00:00", "equity": 10000}],
                }
            ],
        }],
    )
    write_json(
        root / "strategies" / "gold_1m_chan" / "demo_order_requests" / f"{run_date}.json",
        [{
            "order_id": "o1",
            "ticket_id": "t1",
            "receipt": {"status": "protective_order_missing_closed"},
            "broker_response": {
                "protective_status": "failed",
                "emergency_close": {"status": "closed", "local_mirror": {"closed": True}},
            },
        }],
    )

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    board = state["performance_board"]

    assert board["active_demo_blocker"]["blocked"] is False
    assert board["active_demo_blocker"]["status"] == "protective_failure_emergency_closed"
    assert board["can_trade_today"] is True


def test_dashboard_state_exposes_strategy_frequency_diagnostics(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
    )
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": run_date, "ready_for_paper": True, "status": "pass"}])
    write_json(root / "health" / "current.json", [{"status": "ok", "checks": []}])
    no_signal = root / "strategies" / "no_signal"
    candidate = root / "strategies" / "candidate"
    quality = root / "strategies" / "quality"
    risked = root / "strategies" / "risked"
    blocked = root / "strategies" / "blocked"
    effective = root / "strategies" / "effective"
    write_json(no_signal / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "watch"}])
    write_json(candidate / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "long"}])
    write_json(quality / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "long"}])
    write_json(
        quality / "trade_tickets" / f"{run_date}.json",
        [{"ticket_id": "t1", "trade_quality": {"passes": False, "reasons": ["target return below 1%"]}}],
    )
    write_json(risked / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "long"}])
    write_json(risked / "risk_blocks" / f"{run_date}.json", [{"reason": "daily risk cap exceeded"}])
    write_json(blocked / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "long"}])
    write_json(
        blocked / "live_reconciliation" / "current.json",
        [{"reconciled": False, "drift_count": 1, "drifts": [{"reason": "exchange position has no local record"}]}],
    )
    write_json(effective / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "long"}])
    write_json(effective / "paper_orders" / f"{run_date}.json", [{"ticket_id": "t1", "status": "filled"}])

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    rows = {row["strategy_id"]: row["frequency_diagnostics"] for row in state["performance_board"]["strategies"]}

    assert rows["no_signal"]["stage"] == "no_signal"
    assert rows["no_signal"]["attribution"]["limiting_reason"] == "market_no_signal"
    assert rows["candidate"]["stage"] == "candidate_without_ticket"
    assert rows["candidate"]["attribution"]["limiting_reason"] == "candidate_without_ticket"
    assert rows["quality"]["stage"] == "quality_failed"
    assert rows["quality"]["attribution"]["limiting_reason"] == "quality_gate_failed"
    assert "target return below 1%" in rows["quality"]["reason"]
    assert rows["risked"]["stage"] == "risk_blocked"
    assert rows["risked"]["attribution"]["limiting_reason"] == "risk_budget_used"
    assert rows["blocked"]["stage"] == "execution_blocker"
    assert rows["blocked"]["attribution"]["limiting_reason"] == "execution_blocker"
    assert rows["effective"]["stage"] == "effective"
    assert rows["effective"]["attribution"]["primary_reason"] == "executed"
    assert state["performance_board"]["frequency_board"]["effective_strategy_count"] == 1
    assert state["performance_board"]["frequency_board"]["needs_attention_count"] == 5


def test_dashboard_state_strategy_detail_uses_scoped_timeframe_and_reasons(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "alpha"
    run_date = "2026-06-21"
    write_json(
        root / "clean_bars" / run_date / "GOLD_1m.json",
        [
            {"timestamp": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
            {"timestamp": "2026-06-21T00:01:00+00:00", "open": 100, "high": 102, "low": 100, "close": 101, "volume": 1, "provider": "binance_usdm", "quality_flags": ["public_proxy_feed"]},
        ],
    )
    write_json(root / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "long", "thesis": "signal thesis", "generated_at": "2026-06-21T00:00:00+00:00"}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [{"ticket_id": "t1", "signal_id": "s1", "asset": "GOLD", "rationale": "ticket rationale"}])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "o1", "ticket_id": "t1", "status": "filled", "fill_price": 100, "quantity": 1}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"ticket_id": "t1", "signal_id": "s1", "decision_status": "executed_paper", "decided_at": "2026-06-21T00:01:00+00:00", "notes": "executed", "risk_snapshot": {"max_loss_pct": 0.5, "daily_loss_stop_pct": 1.25}}])
    write_json(
        root / "decision_snapshots" / f"{run_date}.json",
        [
            {"strategy_id": "alpha", "bar_timestamp": "2026-06-21T00:00:00+00:00", "final_decision": "no_go", "signal": {"direction": "watch"}},
            {
                "strategy_id": "alpha",
                "bar_timestamp": "2026-06-21T00:01:00+00:00",
                "final_decision": "go",
                "signal": {"direction": "long", "confidence": 62},
                "execution_plan": {"entry_zone": "99-101", "take_profit": 104, "stop_loss": 98},
            },
        ],
    )
    write_json(
        root / "paper_trades" / "current.json",
        [{
            "trade_id": "tr1",
            "ticket_id": "t1",
            "signal_id": "s1",
            "symbol": "GOLD",
            "side": "long",
            "status": "open",
            "quantity": 1,
            "entry_price": 100,
            "stop_loss": 98,
            "target": 104,
            "opened_at": "2026-06-21T00:01:00+00:00",
        }],
    )
    write_json(root / "performance" / "current.json", [{"summary": {"open_trade_count": 1, "unrealized_pnl": 1, "net_pnl_marked": 1}}])
    write_json(root / "equity_curve" / "current.json", [{"starting_equity": 10000, "current_equity": 10001, "max_drawdown_pct": 0, "points": [{"timestamp": "2026-06-21T00:01:00+00:00", "equity": 10001}]}])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    detail = state["strategy_detail"]

    assert state["strategy_id"] == "alpha"
    assert state["bar_timeframe"] == "1m"
    assert detail["timeframe"] == "1m"
    assert len(detail["bars"]) == 2
    assert detail["ohlc_quality"]["proxy_feed"] is False
    assert detail["ohlc_quality"]["truth_level"] == "execution_venue"
    assert detail["ohlc_quality"]["wide_bar_count"] == 2
    assert detail["ohlc_quality"]["promotion_ready"] is True
    assert state["market_data_gate"]["mode"] == "promotion_ready"
    assert "wide_ohlc_bars" not in state["market_data_gate"]["blockers"]
    assert detail["orders"][0]["order_id"] == "o1"
    assert detail["open_trades"][0]["entry_reason"] == "ticket rationale"
    assert detail["review_events"][0]["counts_as_trade_sample"] is False
    assert any(event["counts_as_trade_sample"] is True for event in detail["review_events"])
    assert detail["source_contract"]["section"] == "strategy_detail"
    assert detail["source_contract"]["strategy_id"] == "alpha"
    assert detail["decision_snapshot_summary"]["count"] == 2
    assert detail["decision_snapshot_summary"]["go_count"] == 1
    assert detail["latest_decision_snapshot"]["bar_timestamp"] == "2026-06-21T00:01:00+00:00"
    assert detail["latest_go_decision_snapshot"]["execution_plan"]["take_profit"] == 104
    assert detail["explainability_status"] == "ok"
    assert detail["explainability_gaps"] == []
    lifecycle = detail["trade_lifecycle"][0]["stages"]
    assert [item["stage"] for item in lifecycle] == ["signal", "ticket", "gate", "order", "position", "exit", "review"]
    assert next(item for item in lifecycle if item["stage"] == "signal")["present"] is True
    assert next(item for item in lifecycle if item["stage"] == "ticket")["summary"] == "ticket rationale"
    assert next(item for item in lifecycle if item["stage"] == "gate")["status"] == "passed"
    assert next(item for item in lifecycle if item["stage"] == "order")["summary"] == "fill_price=100 quantity=1"


def test_dashboard_state_strategy_detail_extends_replay_ohlc_from_market_db(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "alpha"
    run_date = "2026-06-21"
    write_json(
        root / "clean_bars" / run_date / "GOLD_1m.json",
        [
            {"timestamp": "2026-06-21T12:00:00+00:00", "open": 120, "high": 121, "low": 119, "close": 120, "volume": 1, "provider": "artifact"},
            {"timestamp": "2026-06-21T12:01:00+00:00", "open": 120, "high": 121, "low": 119, "close": 121, "volume": 1, "provider": "artifact"},
        ],
    )
    write_json(
        root / "paper_trades" / "closed" / "2026-06-21.json",
        [{
            "trade_id": "historic_trade",
            "ticket_id": "ticket_1",
            "signal_id": "signal_1",
            "symbol": "GOLD",
            "side": "long",
            "status": "closed",
            "quantity": 1,
            "entry_price": 100,
            "exit_price": 130,
            "stop_loss": 95,
            "target": 130,
            "opened_at": "2026-06-02T00:10:00+00:00",
            "closed_at": "2026-06-21T00:20:00+00:00",
            "realized_pnl": 30,
        }],
    )
    write_json(root / "performance" / "current.json", [{"summary": {"closed_all_count": 1, "realized_pnl_all": 30}}])
    write_json(root / "equity_curve" / "current.json", [{"points": [{"timestamp": "2026-06-21T12:01:00+00:00", "equity": 10030}]}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "journal_decisions" / f"{run_date}.json", [])

    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars([
        Bar("GOLD", "5m", "2026-06-02T00:05:00+00:00", 99, 101, 98, 100, 10, "binance_usdm", ["public_proxy_feed"]),
        Bar("GOLD", "5m", "2026-06-02T00:10:00+00:00", 100, 102, 99, 101, 11, "binance_usdm", ["public_proxy_feed"]),
        Bar("GOLD", "5m", "2026-06-21T00:20:00+00:00", 129, 131, 128, 130, 12, "binance_usdm", ["public_proxy_feed"]),
        Bar("GOLD", "5m", "2026-06-21T12:00:00+00:00", 120, 122, 119, 121, 13, "binance_usdm", ["public_proxy_feed"]),
    ])

    state = DashboardState(output_root=root, market_db=db_path).snapshot(run_date)
    detail = state["strategy_detail"]

    assert detail["replay_ohlc"]["source"] == "market_data_db"
    assert detail["replay_ohlc"]["requested_timeframe"] == "1m"
    assert detail["replay_ohlc"]["timeframe"] == "5m"
    assert detail["replay_ohlc"]["is_complete_replay_window"] is True
    assert detail["bars"][0]["timestamp"] == "2026-06-02T00:05:00+00:00"
    assert detail["bars"][-1]["timestamp"] == "2026-06-21T00:20:00+00:00"
    assert {bar["provider"] for bar in detail["bars"]} == {"binance_usdm"}


def test_dashboard_state_strategy_detail_loads_cross_day_trade_trace_artifacts(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "alpha"
    run_date = "2026-06-23"
    trace_date = "2026-06-22"
    write_json(
        root / "clean_bars" / run_date / "GOLD_1m.json",
        [
            {"timestamp": "2026-06-23T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
            {"timestamp": "2026-06-23T00:01:00+00:00", "open": 100, "high": 101, "low": 100, "close": 100.5, "volume": 1},
        ],
    )
    write_json(root / "signals" / f"{run_date}.json", [])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [])
    write_json(root / "journal_decisions" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(
        root / "signals" / f"{trace_date}.json",
        [{"signal_id": "sig_alpha_20260622_1", "direction": "long", "thesis": "prior day signal", "generated_at": "2026-06-22T23:58:00+00:00"}],
    )
    write_json(
        root / "trade_tickets" / f"{trace_date}.json",
        [{"ticket_id": "ticket_alpha_20260622_1", "signal_id": "sig_alpha_20260622_1", "asset": "GOLD", "rationale": "prior day ticket"}],
    )
    write_json(
        root / "paper_orders" / f"{trace_date}.json",
        [{"order_id": "order_alpha_20260622_1", "ticket_id": "ticket_alpha_20260622_1", "status": "filled", "fill_price": 100, "quantity": 1}],
    )
    write_json(
        root / "journal_decisions" / f"{trace_date}.json",
        [{
            "ticket_id": "ticket_alpha_20260622_1",
            "signal_id": "sig_alpha_20260622_1",
            "decision_status": "executed_paper",
            "decided_at": "2026-06-22T23:59:00+00:00",
            "notes": "executed prior day",
            "risk_snapshot": {"max_loss_pct": 0.5, "daily_loss_stop_pct": 1.25},
        }],
    )
    write_json(
        root / "paper_trades" / "current.json",
        [{
            "trade_id": "trade_alpha_1",
            "order_id": "order_alpha_20260622_1",
            "ticket_id": "ticket_alpha_20260622_1",
            "signal_id": "sig_alpha_20260622_1",
            "symbol": "GOLD",
            "side": "long",
            "status": "open",
            "quantity": 1,
            "entry_price": 100,
            "stop_loss": 98,
            "target": 104,
            "opened_at": "2026-06-22T23:59:30+00:00",
        }],
    )
    write_json(root / "performance" / "current.json", [{"summary": {"open_trade_count": 1, "unrealized_pnl": 0.5, "net_pnl_marked": 0.5}}])
    write_json(root / "equity_curve" / "current.json", [{"starting_equity": 10000, "current_equity": 10000.5, "max_drawdown_pct": 0, "points": [{"timestamp": "2026-06-23T00:01:00+00:00", "equity": 10000.5}]}])

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    detail = state["strategy_detail"]
    lifecycle = detail["trade_lifecycle"][0]["stages"]

    assert detail["explainability_status"] == "ok"
    assert detail["explainability_gaps"] == []
    assert detail["trades"][0]["entry_reason"] == "prior day ticket"
    assert detail["trades"][0]["strategy_signal"]["thesis"] == "prior day signal"
    assert detail["trades"][0]["order"]["order_id"] == "order_alpha_20260622_1"
    assert detail["orders"][0]["order_id"] == "order_alpha_20260622_1"
    assert next(item for item in lifecycle if item["stage"] == "signal")["present"] is True
    assert next(item for item in lifecycle if item["stage"] == "ticket")["present"] is True
    assert next(item for item in lifecycle if item["stage"] == "order")["present"] is True
    audit = DashboardState(output_root=tmp_path / "outputs", market_db=tmp_path / "missing.db")._strategy_closed_loop_audit(root, run_date, 1)
    assert audit["status"] == "closed_loop_ok"
    assert audit["warn_count"] == 0


def test_dashboard_state_flags_strategy_detail_explainability_gaps(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "alpha"
    run_date = "2026-06-21"
    write_json(
        root / "clean_bars" / run_date / "GOLD_1m.json",
        [{"timestamp": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
    )
    write_json(root / "signals" / f"{run_date}.json", [{"signal_id": "s-orphan", "direction": "long", "thesis": "directional but no ticket"}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "o1", "ticket_id": "missing-ticket", "status": "filled", "fill_price": 100, "quantity": 1}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"ticket_id": "missing-ticket", "signal_id": "missing-signal", "decision_status": "executed_paper", "decided_at": "2026-06-21T00:01:00+00:00", "notes": "executed without full artifacts"}])
    write_json(
        root / "paper_trades" / "current.json",
        [{
            "trade_id": "tr1",
            "order_id": "o1",
            "ticket_id": "missing-ticket",
            "signal_id": "missing-signal",
            "symbol": "GOLD",
            "side": "long",
            "status": "open",
            "quantity": 1,
            "entry_price": 100,
            "stop_loss": 90,
            "target": 110,
            "opened_at": "2026-06-21T00:01:00+00:00",
        }],
    )
    write_json(root / "performance" / "current.json", [{"summary": {"open_trade_count": 1}}])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    detail = state["strategy_detail"]
    gap_types = {item["type"] for item in detail["explainability_gaps"]}
    gap_groups = {item["type"]: item for item in detail["explainability_gap_groups"]}
    root_groups = {item["root_cause"]: item for item in detail["explainability_root_cause_groups"]}
    lifecycle = detail["trade_lifecycle"][0]["stages"]

    assert detail["explainability_status"] == "warn"
    assert "missing_signal_artifact" in gap_types
    assert "missing_ticket_artifact" in gap_types
    assert "missing_gate_context" in gap_types
    assert "directional_signal_without_ticket" in gap_types
    assert gap_groups["missing_signal_artifact"]["count"] == 1
    assert root_groups["artifact_provenance_missing"]["count"] == 2
    assert root_groups["risk_gate_snapshot_missing"]["next_action"] == "attach_risk_gate_snapshot"
    assert detail["performance_confidence"]["status"] == "open_pnl_only"
    assert next(item for item in lifecycle if item["stage"] == "signal")["present"] is False
    assert next(item for item in lifecycle if item["stage"] == "order")["present"] is True


def test_dashboard_state_builds_intraday_nav_curve_from_open_trades(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-14"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [
            {"timestamp": "2026-05-14T00:00:00+00:00", "close": 100, "provider": "broker_csv"},
            {"timestamp": "2026-05-14T00:05:00+00:00", "close": 110, "provider": "broker_csv"},
        ],
    )
    write_json(
        root / "performance" / "current.json",
        [
            {
                "summary": {"realized_pnl_all": 0, "total_execution_costs": 0},
                "open_trades": [
                    {
                        "trade_id": "tr1",
                        "symbol": "GOLD",
                        "side": "long",
                        "status": "open",
                        "quantity": 2,
                        "entry_price": 100,
                        "opened_at": "2026-05-14T00:00:00+00:00",
                    }
                ],
            }
        ],
    )
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "journal_decisions" / f"{run_date}.json", [])

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)

    curve = state["nav_curve_intraday"]
    assert curve["source"] == "5m_mark_to_market"
    assert curve["point_count"] == 2
    assert curve["points"][0]["equity"] == 10000
    assert curve["points"][1]["equity"] == 10020
    assert state["strategy_detail"]["nav_curve_intraday"]["source"] == "5m_mark_to_market"
    assert state["strategy_detail"]["nav_curve_intraday"]["points"][1]["equity"] == 10020


def test_dashboard_state_nav_curve_marks_closed_trades_as_a_path(tmp_path: Path):
    """A closed position must render as a curve: flat before open, marked to
    market while held, frozen at recorded realized pnl after close — and the
    close boundary must compare correctly despite the Z vs +00:00 formats."""
    root = tmp_path / "outputs"
    run_date = "2026-05-25"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [
            {"timestamp": "2026-05-25T18:00:00+00:00", "close": 100, "provider": "binance_usdm"},  # before open
            {"timestamp": "2026-05-25T18:20:00+00:00", "close": 110, "provider": "binance_usdm"},  # held, +20
            {"timestamp": "2026-05-25T20:00:00+00:00", "close": 80,  "provider": "binance_usdm"},  # exactly at close (==closed_at)
            {"timestamp": "2026-05-25T21:00:00+00:00", "close": 70,  "provider": "binance_usdm"},  # after close
        ],
    )
    # closed_at uses the Z form while the bars use +00:00 — a string compare
    # would mis-bucket the 20:00 bar. realized_pnl is recorded, not exit-entry.
    write_json(
        root / "paper_trades" / "closed" / f"{run_date}.json",
        [{
            "trade_id": "t_closed_1", "symbol": "GOLD", "side": "long", "quantity": 2,
            "entry_price": 100, "exit_price": 90, "opened_at": "2026-05-25T18:17:05+00:00",
            "closed_at": "2026-05-25T20:00:00Z", "realized_pnl": -20,
        }],
    )
    write_json(root / "performance" / "current.json", [{"summary": {}, "open_trades": []}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "journal_decisions" / f"{run_date}.json", [])

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    eq = [p["equity"] for p in state["nav_curve_intraday"]["points"]]

    assert eq[0] == 10000          # flat before the position opened
    assert eq[1] == 10020          # marked to market while held (110-100)*2
    assert eq[2] == 9980           # AT close: recorded realized -20 (not (80-100)*2=-40)
    assert eq[3] == 9980           # stays at realized after close
    assert state["nav_curve_intraday"]["current_drawdown_pct"] < 0


def test_dashboard_state_surfaces_risk_block(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-14"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4572, "provider": "gold-api.com"}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "journal_decisions" / f"{run_date}.json", [])
    write_json(
        root / "risk_blocks" / f"{run_date}.json",
        [{"asset": "GOLD", "ticket_id": "ticket_gold", "signal_id": "sig_gold", "reason": "daily risk cap exceeded", "portfolio_risk": {"daily_loss_stop_pct": 1.25, "used_loss_pct": 1.0, "candidate_loss_pct": 0.5, "projected_loss_pct": 1.5, "open_trades": 2, "allows_candidate": False, "block_reason": "daily risk cap exceeded"}}],
    )

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)

    assert state["risk"]["block_reason"] == "daily risk cap exceeded"
    assert state["risk"]["allows_next_paper_order"] is False
    assert state["risk"]["candidate_loss_pct"] == 0.5
    assert state["risk"]["projected_loss_pct"] == 1.5
    assert state["risk"]["blocked_ticket_id"] == "ticket_gold"
    assert state["risk"]["blocked_signal_id"] == "sig_gold"


def test_dashboard_state_summarizes_market_db_coverage(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-15"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4572, "provider": "gold-api.com"}])
    store = MarketStore(db_path)
    store.upsert_bars(
        [
            Bar("GOLD", "5m", "2026-05-15T00:00:00+00:00", 4570, 4571, 4569, 4570.5, 0, "local_synthetic_seed", ["seed"]),
            Bar("GOLD", "5m", "2026-05-15T00:05:00+00:00", 4571, 4573, 4570, 4572, 100, "broker_csv", ["csv_import"]),
            Bar("DXY", "1d", "2026-05-15T00:00:00+00:00", 100, 101, 99, 100.5, 0, "fred:DTWEXBGS", ["daily_factor"]),
        ]
    )

    state = DashboardState(output_root=root, market_db=db_path).snapshot(run_date)

    assert state["market_db"]["exists"] is True
    assert state["market_db"]["gold_5m_total_rows"] == 2
    assert state["market_db"]["gold_5m_imported_rows"] == 1
    assert state["market_db"]["gold_5m_official_rows"] == 1
    assert state["market_db"]["gold_5m_synthetic_rows"] == 1
    assert any(item["provider"] == "broker_csv" for item in state["market_db"]["bars"])
