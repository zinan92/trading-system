from pathlib import Path

from services.journal_store import load_json, write_json
from services.strategy_frequency_governance import StrategyFrequencyGovernance


def test_strategy_frequency_governance_writes_actions_and_status(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    daily_samples = {
        "samples": [
            {"strategy_id": "volume", "candidate": True, "ticket_created": True, "execution_status": "paper_filled"},
            {"strategy_id": "volume", "candidate": True, "ticket_created": True, "execution_status": "paper_filled"},
            {"strategy_id": "blocked", "candidate": True, "ticket_created": True, "execution_status": "demo_blocked"},
            {"strategy_id": "chan", "candidate": False, "ticket_created": False, "execution_status": "no_signal"},
            {"strategy_id": "grid", "candidate": False, "ticket_created": False, "execution_status": "no_signal"},
        ]
    }
    leaderboard = {
        "strategies": [
            {
                "strategy_id": "volume",
                "engine": "bollinger_reversion",
                "timeframe": "1m",
                "classification": {"family": "mean_reversion", "role": "sample_volume_strategy", "frequency_bucket": "medium"},
                "daily_execution": {"executed_trade_count": 2, "signal_count": 2, "ticket_count": 2},
            },
            {
                "strategy_id": "blocked",
                "engine": "chan",
                "timeframe": "1m",
                "classification": {"family": "chan", "role": "active_position_gated_strategy", "frequency_bucket": "low"},
                "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 1},
            },
            {
                "strategy_id": "chan",
                "engine": "chan",
                "timeframe": "1m",
                "classification": {"family": "chan", "role": "shadow_variant", "frequency_bucket": "low"},
                "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 0},
            },
            {
                "strategy_id": "grid",
                "engine": "grid",
                "timeframe": "1m",
                "classification": {"family": "grid", "role": "sample_volume_strategy", "frequency_bucket": "high"},
                "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 0},
            },
        ]
    }
    rules = {
        "default": {
            "trade_quality": {
                "min_daily_executed_trades_per_strategy": 2,
                "daily_target_trade_samples_low": 5,
                "daily_target_trade_samples_high": 10,
            }
        }
    }

    result = StrategyFrequencyGovernance(root, rules).build(run_date, daily_samples=daily_samples, leaderboard=leaderboard)
    rows = {row["strategy_id"]: row for row in result["strategies"]}

    assert result["status"] == "below_portfolio_sample_target"
    assert rows["volume"]["stage"] == "effective"
    assert rows["volume"]["attribution"]["primary_reason"] == "executed"
    assert rows["blocked"]["stage"] == "execution_blocked"
    assert rows["blocked"]["attribution"]["primary_reason"] == "execution_blocker"
    assert rows["blocked"]["recommendation"]["action"] == "resolve_execution_blocker"
    assert rows["chan"]["recommendation"]["action"] == "keep_as_selective_observer"
    assert rows["chan"]["attribution"]["limiting_reason"] == "market_no_signal"
    assert rows["grid"]["recommendation"]["action"] == "review_market_regime_and_sampling_cadence"
    assert rows["grid"]["attribution"]["limiting_reason"] == "market_no_signal"
    assert result["next_actions"][0]["action"] == "resolve_execution_blockers"
    assert result["next_actions"][1]["action"] == "review_market_regime_and_sampling_cadence"
    assert result["next_actions"][1]["strategy_ids"] == ["grid"]
    assert load_json(root / "strategy_frequency" / "current.json")[0]["summary"]["portfolio_executed_count"] == 2


def test_strategy_frequency_governance_status_within_target(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    leaderboard = {
        "strategies": [
            {
                "strategy_id": "a",
                "classification": {"family": "mean_reversion", "role": "sample_volume_strategy"},
                "daily_execution": {"executed_trade_count": 3, "signal_count": 3, "ticket_count": 3},
            },
            {
                "strategy_id": "b",
                "classification": {"family": "breakout", "role": "sample_volume_strategy"},
                "daily_execution": {"executed_trade_count": 2, "signal_count": 2, "ticket_count": 2},
            },
        ]
    }
    rules = {
        "default": {
            "trade_quality": {
                "min_daily_executed_trades_per_strategy": 2,
                "daily_target_trade_samples_low": 5,
                "daily_target_trade_samples_high": 10,
            }
        }
    }

    result = StrategyFrequencyGovernance(root, rules).build(run_date, daily_samples={"samples": []}, leaderboard=leaderboard)

    assert result["status"] == "within_portfolio_sample_target"
    assert result["summary"]["effective_strategy_count"] == 2
    assert result["next_actions"] == []


def test_strategy_frequency_governance_uses_reconciliation_drift_as_execution_blocker(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{"reconciled": False, "drift_count": 1, "drifts": [{"reason": "exchange position has no local record"}]}],
    )
    leaderboard = {
        "strategies": [
            {
                "strategy_id": "gold_1m_chan",
                "classification": {"family": "chan", "role": "active_position_gated_strategy", "frequency_bucket": "low"},
                "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 0},
            }
        ]
    }

    result = StrategyFrequencyGovernance(root).build(run_date, daily_samples={"samples": []}, leaderboard=leaderboard)
    row = result["strategies"][0]

    assert row["stage"] == "execution_blocked"
    assert row["execution_blocker"]["status"] == "reconciliation_drift"
    assert row["recommendation"]["action"] == "resolve_execution_blocker"
    assert result["next_actions"][0]["strategy_ids"] == ["gold_1m_chan"]


def test_strategy_frequency_governance_uses_reconciliation_error_as_execution_blocker(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{"reconciled": False, "drift_count": 0, "error": "TimeoutError: The read operation timed out"}],
    )
    leaderboard = {
        "strategies": [
            {
                "strategy_id": "gold_1m_chan",
                "classification": {"family": "chan", "role": "active_position_gated_strategy", "frequency_bucket": "low"},
                "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 0},
            }
        ]
    }

    result = StrategyFrequencyGovernance(root).build(run_date, daily_samples={"samples": []}, leaderboard=leaderboard)
    row = result["strategies"][0]

    assert row["stage"] == "execution_blocked"
    assert row["execution_blocker"]["status"] == "reconciliation_error"
    assert "TimeoutError" in row["execution_blocker"]["reason"]
    assert row["recommendation"]["action"] == "resolve_execution_blocker"


def test_strategy_frequency_governance_uses_cannot_confirm_as_execution_blocker(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{
            "reconciled": False,
            "confirmation_status": "cannot_confirm",
            "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
            "reason_code": "venue_state_unknown",
            "drift_count": 0,
            "error": "TimeoutError: The read operation timed out",
        }],
    )
    leaderboard = {
        "strategies": [
            {
                "strategy_id": "gold_1m_chan",
                "classification": {"family": "chan", "role": "active_position_gated_strategy", "frequency_bucket": "low"},
                "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 0},
            }
        ]
    }

    result = StrategyFrequencyGovernance(root).build(run_date, daily_samples={"samples": []}, leaderboard=leaderboard)
    row = result["strategies"][0]

    assert row["stage"] == "execution_blocked"
    assert row["execution_blocker"]["status"] == "reconciliation_unknown"
    assert "cannot confirm" in row["execution_blocker"]["reason"]


def test_strategy_frequency_governance_attributes_risk_budget_not_no_signal(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    samples = [
        {"strategy_id": "bollinger", "candidate": True, "ticket_created": True, "execution_status": "paper_filled", "execution_sample": True},
        {"strategy_id": "bollinger", "candidate": True, "ticket_created": True, "execution_status": "paper_filled", "execution_sample": True},
    ]
    for idx in range(12):
        samples.append(
            {
                "strategy_id": "bollinger",
                "candidate": True,
                "ticket_created": False,
                "execution_status": "blocked",
                "block": {"reason": "daily risk budget cap exceeded"},
                "block_reasons": ["daily risk budget cap exceeded"],
                "sample_id": f"blocked-{idx}",
            }
        )
    leaderboard = {
        "strategies": [
            {
                "strategy_id": "bollinger",
                "classification": {"family": "mean_reversion", "role": "sample_volume_strategy", "frequency_bucket": "high"},
                "daily_execution": {"executed_trade_count": 2, "signal_count": 14, "ticket_count": 2},
            }
        ]
    }
    rules = {"default": {"trade_quality": {"min_daily_executed_trades_per_strategy": 2, "daily_target_trade_samples_low": 5}}}

    result = StrategyFrequencyGovernance(root, rules).build(run_date, daily_samples={"samples": samples}, leaderboard=leaderboard)
    row = result["strategies"][0]

    assert row["stage"] == "effective"
    assert row["attribution"]["reason_counts"]["executed"] == 2
    assert row["attribution"]["reason_counts"]["risk_budget_used"] == 12
    assert row["attribution"]["limiting_reason"] == "risk_budget_used"
    assert row["recommendation"]["action"] == "keep_running_risk_budget_capped"
    assert result["summary"]["portfolio_limiting_reason"] == "risk_budget_used"


def test_strategy_frequency_governance_attributes_quality_gate_failure(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    daily_samples = {
        "samples": [
            {
                "strategy_id": "fib",
                "candidate": True,
                "ticket_created": True,
                "execution_status": "ticket_created",
                "trade_quality": {"passes": False, "reasons": ["target equity return 0.62% below required 1.00%"]},
            }
        ]
    }
    leaderboard = {
        "strategies": [
            {
                "strategy_id": "fib",
                "classification": {"family": "fib", "role": "sample_volume_strategy", "frequency_bucket": "medium"},
                "daily_execution": {"executed_trade_count": 0, "signal_count": 1, "ticket_count": 1},
            }
        ]
    }

    result = StrategyFrequencyGovernance(root).build(run_date, daily_samples=daily_samples, leaderboard=leaderboard)
    row = result["strategies"][0]

    assert row["stage"] == "ticket_no_execution"
    assert row["attribution"]["primary_reason"] == "quality_gate_failed"
    assert row["attribution"]["limiting_reason"] == "quality_gate_failed"
    assert row["recommendation"]["action"] == "inspect_trade_quality_gate"


def test_strategy_frequency_governance_treats_thin_backtest_as_context_not_blocker(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    daily_samples = {
        "samples": [
            *[
                {
                    "strategy_id": "grid",
                    "candidate": False,
                    "ticket_created": False,
                    "execution_status": "no_signal",
                    "sample_id": f"grid-watch-{idx}",
                }
                for idx in range(10)
            ],
            {
                "strategy_id": "grid",
                "candidate": True,
                "ticket_created": False,
                "execution_status": "candidate_without_ticket",
                "block_reasons": ["thin backtest verdict sample_size=0 setup_count=0"],
                "blocker_diagnostics": [
                    {
                        "code": "backtest_thin_context",
                        "label": "Thin backtest context / 回测样本薄",
                        "summary": "thin backtest verdict sample_size=0 setup_count=0",
                        "blocking": False,
                    }
                ],
                "sample_id": "grid-thin-1",
                "signal_generated_at": "2026-06-21T08:01:00+00:00",
                "decision_cursor": "2026-06-21T08:01:00+00:00",
            }
        ]
    }
    leaderboard = {
        "strategies": [
            {
                "strategy_id": "grid",
                "classification": {"family": "grid", "role": "sample_volume_strategy", "frequency_bucket": "high"},
                "daily_execution": {"executed_trade_count": 0, "signal_count": 11, "ticket_count": 0},
            }
        ]
    }

    result = StrategyFrequencyGovernance(root).build(run_date, daily_samples=daily_samples, leaderboard=leaderboard)
    row = result["strategies"][0]

    assert row["stage"] == "candidate_without_ticket"
    assert row["attribution"]["primary_reason"] == "candidate_without_ticket"
    assert row["attribution"]["primary_reason_label"] == "有方向信号但未出票"
    assert row["attribution"]["limiting_reason"] == "candidate_without_ticket"
    thin_evidence = [item for item in row["attribution"]["evidence"] if item["reason"] == "backtest_thin_context"]
    assert thin_evidence[0]["decision_cursor"] == "2026-06-21T08:01:00+00:00"
    assert row["recommendation"]["action"] == "inspect_funnel_blocker"
    assert result["summary"]["portfolio_limiting_reason"] == "candidate_without_ticket"
    assert result["summary"]["portfolio_limiting_reason_label"] == "有方向信号但未出票"


def test_strategy_frequency_governance_treats_quiet_market_as_diagnostic_not_failure(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    daily_samples = {
        "samples": [
            {"strategy_id": "chan", "candidate": False, "ticket_created": False, "execution_status": "no_signal"},
            {"strategy_id": "chan", "candidate": False, "ticket_created": False, "execution_status": "no_signal"},
        ]
    }
    leaderboard = {
        "strategies": [
            {
                "strategy_id": "chan",
                "classification": {"family": "chan", "role": "shadow_variant", "frequency_bucket": "low"},
                "daily_execution": {"executed_trade_count": 0, "signal_count": 2, "ticket_count": 0},
            }
        ]
    }

    result = StrategyFrequencyGovernance(root).build(run_date, daily_samples=daily_samples, leaderboard=leaderboard)
    row = result["strategies"][0]

    assert row["stage"] == "no_signal"
    assert row["attribution"]["limiting_reason"] == "market_no_signal"
    assert row["attribution"]["frequency_is_diagnostic_not_sla"] is True
    assert row["recommendation"]["action"] == "keep_as_selective_observer"
