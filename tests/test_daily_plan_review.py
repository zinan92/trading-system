from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.daily_plan_review import DailyPlanReview
from services.journal_store import load_json, write_json
from services.trade_quality import DailyTradeSampler


ACTIVE_STRATEGY = "gold_1m_chan"


def _strategy_root(root: Path) -> Path:
    return root / "strategies" / ACTIVE_STRATEGY


def _write_position_bars(root: Path, run_date: str) -> None:
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    rows = []
    for index in range(80):
        close = 124.0 if index < 79 else 125.0
        rows.append(
            {
                "symbol": "GOLD",
                "timeframe": "4h",
                "timestamp": (start + timedelta(hours=4 * index)).isoformat(),
                "open": close,
                "high": 150.0,
                "low": 100.0,
                "close": close,
                "volume": 1,
                "provider": "test",
                "quality_flags": [],
            }
        )
    write_json(_strategy_root(root) / "clean_bars" / run_date / "GOLD_4h.json", rows)


def test_morning_plan_uses_active_chan_strategy_and_position_map(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    scoped = _strategy_root(root)
    _write_position_bars(root, run_date)
    write_json(
        scoped / "signals" / f"{run_date}.json",
        [{"asset": "GOLD", "direction": "long", "strength": 72, "thesis": "fresh 二买", "source_artifacts": None}],
    )
    write_json(
        scoped / "trade_tickets" / f"{run_date}.json",
        [{
            "ticket_id": "ticket_gold_1",
            "signal_id": "sig_gold_1",
            "asset": "GOLD",
            "action": "prepare_buy",
            "entry_zone": "124-126",
            "stop_loss": 121,
            "targets": [132],
            "max_loss_pct": 0.5,
            "trade_quality": {"passes": True, "target_equity_return_pct": 28.0, "reward_to_risk": 2.3},
        }],
    )
    write_json(scoped / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": False, "latest_provider": "binance_usdm", "latest_price": 125.0}])
    write_json(root / "broker_preflight" / "current.json", [{"provider": "binance_usdm", "dry_run": True, "ready": True, "block_reason": "dry_run"}])
    write_json(root / "risk_monitor" / "current.json", [{"allow_paper_auto_approve": False, "kill_switch_active": False}])
    write_json(root / "binance_usdm_feed" / "current.json", [{"status": "pass", "latest_price": 125.0}])
    write_json(
        root / "evening_reviews" / "2026-05-25.json",
        [{"run_date": "2026-05-25", "status": "review_ready", "attribution": {"primary": {"key": "missed_trade", "label": "该交易没交易"}}}],
    )

    result = DailyPlanReview(root).morning_plan(run_date)

    assert result["active_strategy_id"] == ACTIVE_STRATEGY
    assert result["strategy_runtime"]["active_strategy_id"] == ACTIVE_STRATEGY
    assert result["strategy_runtime"]["timeframe"] == "1m"
    assert result["strategy_profile"]["source_repo"] == "zinan92/chancode"
    assert result["decision"] == "TRADE_REVIEW"
    assert result["trade_plan"]["allowed_to_trade"] is True
    assert "broker dry_run is enabled" not in result["trade_plan"]["blocks"]
    assert result["position_map"]["status"] == "ready"
    assert result["position_map"]["trade_zone"]["near_key_level"] is True
    assert result["signal_gate"]["allow_candidate"] is True
    assert result["signals"][0]["position_context"]["location"] == "middle"
    assert result["tickets"][0]["position_context"]["nearest_level"]["name"] == "fib_0.5"
    assert result["previous_review"]["primary_attribution"]["key"] == "missed_trade"
    assert result["execution_playbook"]["quality_control"]["min_target_equity_return_pct"] == 1.0
    assert result["execution_playbook"]["decision_tree"][0]["name"] == "data_ready"
    assert result["trade_plan"]["setups"][0]["trade_quality"]["passes"] is True
    assert load_json(root / "trading_plans" / "current.json")[0]["run_date"] == run_date
    assert (root / "trading_plans" / f"{run_date}.md").exists()


def test_morning_plan_records_auditable_no_trade_reason(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    scoped = _strategy_root(root)
    _write_position_bars(root, run_date)
    write_json(scoped / "signals" / f"{run_date}.json", [{"asset": "GOLD", "direction": "watch", "strength": 0, "thesis": "no fresh 二买"}])
    write_json(scoped / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "trade_tickets" / f"{run_date}.json", [{"ticket_id": "legacy_global_ticket", "asset": "GOLD"}])
    write_json(scoped / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "latest_provider": "binance_usdm", "latest_price": 125.0}])

    result = DailyPlanReview(root).morning_plan(run_date)

    assert result["decision"] == "NO_TRADE"
    assert result["allowed_to_trade"] is False
    assert result["active_strategy_id"] == ACTIVE_STRATEGY
    assert result["blockers"] == result["trade_plan"]["blocks"]
    assert result["current_signal"]["direction"] == "watch"
    assert "no actionable signal" in result["trade_plan"]["blocks"]
    assert "no approved trade ticket" in result["trade_plan"]["blocks"]
    assert result["trade_plan"]["ticket_count"] == 0
    assert result["tickets"] == []
    assert result["signals"][0]["position_gate"]["effective_direction"] == "watch"


def test_evening_review_writes_attribution_hypotheses_and_learning_ledger(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    scoped = _strategy_root(root)
    write_json(scoped / "journal_decisions" / f"{run_date}.json", [{"decision_status": "executed_paper"}])
    write_json(scoped / "journal_pending" / f"{run_date}.json", [{"ticket_id": "pending"}])
    write_json(scoped / "paper_orders" / f"{run_date}.json", [])
    write_json(scoped / "demo_order_requests" / f"{run_date}.json", [])
    write_json(scoped / "signals" / f"{run_date}.json", [{"signal_id": "sig_1", "asset": "GOLD", "direction": "long", "status": "new", "regime": "chan_second_buy", "strength": 72, "confidence": 70}])
    write_json(
        scoped / "trade_tickets" / f"{run_date}.json",
        [{
            "ticket_id": "ticket_1",
            "signal_id": "sig_1",
            "asset": "GOLD",
            "action": "prepare_buy",
            "entry_zone": "100-100",
            "stop_loss": 99,
            "targets": [101],
            "trade_quality": {"passes": False, "reasons": ["target equity return below required"]},
        }],
    )
    write_json(scoped / "performance" / "current.json", [{"summary": {"net_pnl_marked": -12.5}}])
    write_json(
        root / "trading_plans" / f"{run_date}.json",
        [{
            "decision": "NO_TRADE",
            "trade_plan": {"allowed_to_trade": False},
            "signal_gate": {"raw_direction": "long", "allow_candidate": False, "reason": "not near key level"},
            "data_plan": {"ready_for_paper": True},
        }],
    )
    write_json(
        root / "position_maps" / f"{run_date}.json",
        [{"status": "ready", "location": "middle", "nearest_levels": [{"name": "fib_0.618", "price": 130.9}]}],
    )

    result = DailyPlanReview(root).evening_review(run_date, notes="follow the stop plan")

    keys = {item["key"] for item in result["attribution"]["categories"]}
    assert result["active_strategy_id"] == ACTIVE_STRATEGY
    assert result["strategy_runtime"]["active_strategy_id"] == ACTIVE_STRATEGY
    assert result["adherence"]["executed_count"] == 1
    assert result["adherence"]["pending_count"] == 1
    assert result["adherence"]["strict_execution"] is False
    assert {"position_filter_blocked", "missed_trade", "risk_review"}.issubset(keys)
    assert all(item["status"] == "shadow_only" and item["auto_apply"] is False for item in result["hypotheses"])
    assert len(result["improvement_queue"]) >= 3
    assert load_json(root / "strategy_hypotheses" / "current.json")[0]["strategy_id"] == ACTIVE_STRATEGY
    ledger = load_json(root / "learning_ledger" / "current.json")[0]
    assert ledger["strategy_id"] == ACTIVE_STRATEGY
    assert ledger["review_attribution"]["primary"]["key"] == "position_filter_blocked"
    assert ledger["latest_hypotheses"][0]["status"] == "shadow_only"
    assert result["daily_trade_samples"]["summary"]["total_samples"] >= 1
    assert result["trade_reviews"]["summary"]["reviewed_samples"] >= 1
    assert result["trade_reviews"]["reviews"][0]["trade_quality"]["passes"] is False
    assert "quality_gate_failed" in result["trade_reviews"]["reviews"][0]["review_tags"]
    assert load_json(root / "trade_reviews" / f"{run_date}.json")[0]["summary"]["reviewed_samples"] >= 1
    assert (root / "evening_reviews" / f"{run_date}.md").exists()


def test_daily_trade_samples_do_not_count_watch_rows_as_trade_target(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    rules = {
        "default": {
            "min_signal_strength": 60,
            "min_confidence": 55,
            "trade_quality": {
                "effective_leverage": 5,
                "min_target_equity_return_pct": 1.0,
                "min_reward_to_risk": 1.8,
                "daily_min_trade_samples": 3,
                "daily_target_trade_samples_low": 5,
                "daily_target_trade_samples_high": 10,
            },
        }
    }
    for strategy_id in ("gold_1m_chan", "gold_1m_macd", "gold_5m_v1"):
        write_json(
            root / "strategies" / strategy_id / "signals" / f"{run_date}.json",
            [{"signal_id": f"sig_{strategy_id}", "asset": "GOLD", "direction": "watch", "status": "no_signal"}],
        )

    result = DailyTradeSampler(root, rules).build(run_date, active_strategy_id=ACTIVE_STRATEGY)

    assert result["summary"]["total_samples"] == 3
    assert result["summary"]["observation_count"] == 3
    assert result["summary"]["executed_count"] == 0
    assert result["summary"]["below_minimum"] is True
    assert result["summary"]["target_range_met"] is False
    assert result["status"] == "below_minimum_executed_trades"
    assert result["summary"]["funnel"]["no_signal"] == 3


def test_daily_trade_samples_explain_candidate_without_ticket(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    rules = {
        "default": {
            "min_signal_strength": 60,
            "min_confidence": 55,
            "trade_quality": {
                "effective_leverage": 5,
                "min_target_equity_return_pct": 1.0,
                "min_reward_to_risk": 1.8,
                "daily_min_trade_samples": 1,
                "daily_target_trade_samples_low": 1,
                "daily_target_trade_samples_high": 2,
            },
        }
    }
    write_json(
        root / "strategies" / ACTIVE_STRATEGY / "signals" / f"{run_date}.json",
        [{"signal_id": "sig_weak", "asset": "GOLD", "direction": "long", "strength": 42, "confidence": 40, "status": "new"}],
    )

    result = DailyTradeSampler(root, rules).build(run_date, active_strategy_id=ACTIVE_STRATEGY)
    sample = result["samples"][0]

    assert result["summary"]["candidate_count"] == 1
    assert result["summary"]["ticket_count"] == 0
    assert result["summary"]["candidate_without_ticket_count"] == 1
    assert result["summary"]["executed_count"] == 0
    assert sample["execution_status"] == "candidate_without_ticket"
    assert "signal strength 42 below minimum 60" in sample["block_reasons"]
    assert "signal confidence 40 below minimum 55" in sample["block_reasons"]


def test_daily_trade_samples_explain_candidate_with_thin_backtest(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    rules = {
        "default": {
            "min_signal_strength": 60,
            "min_confidence": 55,
            "trade_quality": {
                "effective_leverage": 5,
                "min_target_equity_return_pct": 1.0,
                "min_reward_to_risk": 1.8,
                "daily_min_trade_samples": 1,
                "daily_target_trade_samples_low": 1,
                "daily_target_trade_samples_high": 2,
            },
        }
    }
    scoped = root / "strategies" / ACTIVE_STRATEGY
    write_json(
        scoped / "signals" / f"{run_date}.json",
        [
            {
                "signal_id": "sig_thin",
                "asset": "GOLD",
                "direction": "long",
                "strength": 70,
                "confidence": 60,
                "status": "new",
                "generated_at": "2026-05-26T08:01:00+00:00",
                "expires_at": "2026-05-26T08:06:00+00:00",
            }
        ],
    )
    write_json(
        scoped / "backtests" / f"{run_date}.json",
        [{"signal_id": "sig_thin", "verdict": "thin", "sample_size": 0, "setup_count": 0, "profit_factor": None}],
    )

    result = DailyTradeSampler(root, rules).build(run_date, active_strategy_id=ACTIVE_STRATEGY)
    sample = result["samples"][0]

    assert sample["execution_status"] == "candidate_without_ticket"
    assert sample["signal_generated_at"] == "2026-05-26T08:01:00+00:00"
    assert sample["decision_cursor"] == "2026-05-26T08:01:00+00:00"
    assert sample["backtest"]["verdict"] == "thin"
    assert sample["backtest"]["sample_size"] == 0
    assert sample["block_reasons"] == ["thin backtest verdict sample_size=0 setup_count=0"]


def test_daily_trade_samples_do_not_count_blocked_demo_order_as_execution(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    rules = {
        "default": {
            "trade_quality": {
                "daily_min_trade_samples": 1,
                "daily_target_trade_samples_low": 1,
                "daily_target_trade_samples_high": 2,
            }
        }
    }
    scoped = root / "strategies" / ACTIVE_STRATEGY
    write_json(scoped / "signals" / f"{run_date}.json", [{"signal_id": "sig_1", "asset": "GOLD", "direction": "long", "status": "new"}])
    write_json(scoped / "trade_tickets" / f"{run_date}.json", [{"ticket_id": "ticket_1", "signal_id": "sig_1"}])
    write_json(scoped / "demo_order_requests" / f"{run_date}.json", [{"ticket_id": "ticket_1", "status": "blocked"}])

    result = DailyTradeSampler(root, rules).build(run_date, active_strategy_id=ACTIVE_STRATEGY)
    sample = result["samples"][0]

    assert sample["execution_status"] == "demo_blocked"
    assert sample["execution_sample"] is False
    assert result["summary"]["executed_count"] == 0
    assert result["status"] == "below_minimum_executed_trades"


def test_daily_trade_samples_count_demo_receipt_fill_as_execution(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    rules = {
        "default": {
            "trade_quality": {
                "daily_min_trade_samples": 1,
                "daily_target_trade_samples_low": 1,
                "daily_target_trade_samples_high": 2,
            }
        }
    }
    scoped = root / "strategies" / ACTIVE_STRATEGY
    write_json(scoped / "signals" / f"{run_date}.json", [{"signal_id": "sig_1", "asset": "GOLD", "direction": "long", "status": "new"}])
    write_json(scoped / "trade_tickets" / f"{run_date}.json", [{"ticket_id": "ticket_1", "signal_id": "sig_1"}])
    write_json(scoped / "demo_order_requests" / f"{run_date}.json", [{"ticket": {"ticket_id": "ticket_1"}, "receipt": {"status": "filled"}}])

    result = DailyTradeSampler(root, rules).build(run_date, active_strategy_id=ACTIVE_STRATEGY)
    sample = result["samples"][0]

    assert sample["execution_status"] == "demo_filled"
    assert sample["execution_sample"] is True
    assert result["summary"]["executed_count"] == 1
    assert result["status"] == "target_met"


def test_daily_trade_samples_include_execution_without_current_signal(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    rules = {
        "default": {
            "trade_quality": {
                "daily_min_trade_samples": 1,
                "daily_target_trade_samples_low": 1,
                "daily_target_trade_samples_high": 2,
            }
        }
    }
    scoped = root / "strategies" / ACTIVE_STRATEGY
    write_json(scoped / "signals" / f"{run_date}.json", [{"signal_id": "sig_watch", "asset": "GOLD", "direction": "watch", "status": "no_signal"}])
    write_json(scoped / "paper_orders" / f"{run_date}.json", [{"ticket_id": "ticket_filled_earlier", "order_id": "paper_1", "status": "filled"}])

    result = DailyTradeSampler(root, rules).build(run_date, active_strategy_id=ACTIVE_STRATEGY)
    execution_rows = [sample for sample in result["samples"] if sample["execution_sample"]]

    assert result["summary"]["no_signal_count"] == 1
    assert result["summary"]["executed_count"] == 1
    assert execution_rows[0]["ticket_id"] == "ticket_filled_earlier"
    assert execution_rows[0]["execution_status"] == "paper_filled"
    assert result["status"] == "target_met"
