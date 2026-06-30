from pathlib import Path

from services.journal_store import load_json, write_json
from services.strategy_daily_review import StrategyDailyReview


def _reviewer(root: Path) -> StrategyDailyReview:
    reviewer = StrategyDailyReview(root)
    reviewer.strategy_config = {
        "alpha": {
            "timeframe": "1m",
            "classification": {
                "family": "mean_reversion",
                "style": "bollinger",
                "expected_trades_per_day_min": 2,
                "expected_trades_per_day_max": 8,
            },
        },
        "beta": {
            "timeframe": "5m",
            "classification": {
                "family": "chan",
                "style": "second_buy",
                "expected_trades_per_day_min": 1,
                "expected_trades_per_day_max": 3,
            },
        },
    }
    return reviewer


def test_strategy_daily_review_attributes_loss_and_stop_hit(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-25"
    ns = root / "strategies" / "alpha"
    write_json(ns / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "short"}])
    write_json(ns / "trade_tickets" / f"{run_date}.json", [{
        "ticket_id": "t1",
        "entry_zone": "4000-4010",
        "stop_loss": 4020,
        "targets": [3960],
        "trade_quality": {"target_equity_return_pct": 5.0, "reward_to_risk": 2.0},
    }])
    write_json(ns / "paper_orders" / f"{run_date}.json", [{"order_id": "o1", "ticket_id": "t1", "status": "filled"}])
    write_json(ns / "paper_trades" / "closed" / f"{run_date}.json", [{
        "trade_id": "tr1",
        "ticket_id": "t1",
        "exit_reason": "stop_loss",
        "realized_pnl": -12.5,
        "side": "short",
        "entry_price": 4005,
        "exit_price": 4020,
        "stop_loss": 4020,
        "target": 3960,
        "opened_at": "2026-06-25T03:14:00+00:00",
        "closed_at": "2026-06-25T04:15:00+00:00",
    }])
    write_json(ns / "paper_trades" / "current.json", [])
    write_json(ns / "decision_snapshots" / f"{run_date}.json", [{"signal_id": "s1", "final_decision": "go"}])
    leaderboard = {"strategies": [{"strategy_id": "alpha", "daily_execution": {"executed_trade_count": 2}, "closed_trades": 1, "return_pct": -0.1}]}

    result = _reviewer(root).build(run_date, leaderboard=leaderboard, frequency={"strategies": []})

    alpha = result["strategies"][0]
    assert alpha["strategy_id"] == "alpha"
    assert alpha["pnl"]["realized_today"] == -12.5
    assert alpha["tp_sl"]["stop_loss_hits"] == 1
    assert "loss_day" in alpha["attribution"]["reasons"]
    assert "stop_loss_hit" in alpha["attribution"]["reasons"]
    assert alpha["attribution"]["primary"] == "stop_loss_hit"
    assert alpha["pm_verdict"] == "loss_driver_review"
    assert alpha["pm_action"] == "review_loss_driver"
    assert alpha["review_priority"] == 95
    assert "SL" in alpha["review_question"]
    assert alpha["replay_context"]["strategy_id"] == "alpha"
    assert alpha["replay_context"]["review_trade"]["trade_id"] == "tr1"
    assert alpha["replay_context"]["review_trade"]["exit_reason"] == "stop_loss"
    assert alpha["replay_context"]["review_trade"]["future_outcome_hidden_until_exit"] is True
    assert alpha["replay_context"]["review_trade"]["opened_at"] == "2026-06-25T03:14:00+00:00"
    assert alpha["replay_context"]["review_trade"]["closed_at"] == "2026-06-25T04:15:00+00:00"
    assert alpha["replay_context"]["review_trade"]["take_profit"] == 3960
    assert (root / "strategy_daily_reviews" / f"{run_date}.md").exists()


def test_strategy_daily_review_flags_low_frequency(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-25"
    ns = root / "strategies" / "beta"
    write_json(ns / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "watch"}])
    write_json(ns / "trade_tickets" / f"{run_date}.json", [])
    write_json(ns / "risk_blocks" / f"{run_date}.json", [])
    leaderboard = {"strategies": [{"strategy_id": "beta", "daily_execution": {"executed_trade_count": 0}, "closed_trades": 0}]}

    result = _reviewer(root).build(run_date, leaderboard=leaderboard, frequency={"strategies": []})

    beta = next(row for row in result["strategies"] if row["strategy_id"] == "beta")
    assert beta["pm_verdict"] == "low_frequency_review"
    assert beta["frequency"]["executed_today"] == 0
    assert "frequency_below_expected" in beta["attribution"]["reasons"]
    assert beta["attribution"]["primary"] == "market_no_signal"
    assert beta["pm_action"] == "validate_no_signal"
    assert "No-Go" in beta["pm_summary"]
    saved = load_json(root / "strategy_daily_reviews" / f"{run_date}.json")[0]
    assert saved["status_counts"]["low_frequency_review"] >= 1


def test_strategy_daily_review_loss_takes_priority_over_low_frequency(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-25"
    ns = root / "strategies" / "alpha"
    write_json(ns / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "long"}])
    write_json(ns / "trade_tickets" / f"{run_date}.json", [{"ticket_id": "t1", "stop_loss": 3900, "targets": [4100]}])
    write_json(ns / "paper_orders" / f"{run_date}.json", [{"order_id": "o1", "ticket_id": "t1", "status": "filled"}])
    write_json(ns / "paper_trades" / "closed" / f"{run_date}.json", [{
        "trade_id": "tr1",
        "ticket_id": "t1",
        "exit_reason": "stop_loss",
        "realized_pnl": -9.0,
        "opened_at": "2026-06-25T03:14:00+00:00",
    }])
    leaderboard = {"strategies": [{"strategy_id": "alpha", "daily_execution": {"executed_trade_count": 1}, "closed_trades": 1}]}

    result = _reviewer(root).build(run_date, leaderboard=leaderboard, frequency={"strategies": []})

    alpha = next(row for row in result["strategies"] if row["strategy_id"] == "alpha")
    assert alpha["frequency"]["executed_today"] == 1
    assert "frequency_below_expected" in alpha["attribution"]["reasons"]
    assert alpha["pm_verdict"] == "loss_driver_review"
    assert alpha["pm_action"] == "review_loss_driver"
    assert alpha["next_review_cursor"] == "2026-06-25T03:14:00+00:00"
    assert alpha["replay_context"]["cursor"] == "2026-06-25T03:14:00+00:00"
    assert alpha["replay_context"]["artifact_path"].endswith("/decision_snapshots/2026-06-25.json")
    assert alpha["replay_context"]["review_trade"]["trade_id"] == "tr1"
    assert alpha["replay_context"]["review_trade"]["replay_anchor"] == "entry"


def test_strategy_daily_review_explains_candidate_without_ticket(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-25"
    ns = root / "strategies" / "beta"
    write_json(ns / "signals" / f"{run_date}.json", [{"signal_id": "s1", "direction": "short"}])
    write_json(ns / "trade_tickets" / f"{run_date}.json", [])
    write_json(ns / "risk_blocks" / f"{run_date}.json", [])
    write_json(ns / "decision_snapshots" / f"{run_date}.json", [{
        "signal_id": "s1",
        "bar_timestamp": "2026-06-25T08:01:00+00:00",
        "signal": {"direction": "short"},
        "final_decision": "candidate",
    }])
    leaderboard = {"strategies": [{"strategy_id": "beta", "daily_execution": {"executed_trade_count": 0}, "closed_trades": 0}]}

    result = _reviewer(root).build(run_date, leaderboard=leaderboard, frequency={"strategies": []})

    beta = next(row for row in result["strategies"] if row["strategy_id"] == "beta")
    assert beta["pm_verdict"] == "low_frequency_review"
    assert beta["attribution"]["primary"] == "candidate_rejected_before_ticket"
    assert beta["pm_action"] == "inspect_signal_to_ticket"
    assert beta["review_priority"] == 75
    assert beta["next_review_cursor"] == "2026-06-25T08:01:00+00:00"
    assert "质量门" in beta["pm_summary"]
