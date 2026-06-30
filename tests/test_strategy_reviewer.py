from pathlib import Path

from services.journal_store import load_json, write_json
from services.strategy_reviewer import StrategyReviewer


def test_strategy_reviewer_writes_learning_ledger(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-16"
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD", "signal_id": "s1", "regime": "no_trade", "direction": "watch", "strength": 52, "confidence": 60}])
    write_json(root / "backtests" / f"{run_date}.json", [{"signal_id": "s1", "verdict": "no_trade", "sample_size": 12, "evaluated_bars": 220, "setup_count": 12}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"decision_status": "executed_paper"}])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1"}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "t1", "status": "open"}])
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "missing_bars": 120, "spike_flags": 0}])

    review = StrategyReviewer(root).build(run_date)

    assert review["metrics"]["missing_5m_bars"] == 120
    assert review["metrics"]["open_trade_count"] == 1
    assert review["metrics"]["backtest_evaluated_bars"] == 220
    assert review["metrics"]["backtest_setup_count"] == 12
    assert review["strategy_snapshot"]["strategy_id"] == "gold_5m_v1"
    assert len(review["strategy_snapshot"]["config_hash"]) == 12
    assert any("importing broker" in item for item in review["suggestions"])
    assert load_json(root / "strategy_reviews" / f"{run_date}.json")[0]["run_date"] == run_date
    assert load_json(root / "strategy_snapshots" / f"{run_date}.json")[0]["config_hash"] == review["strategy_snapshot"]["config_hash"]
    ledger = load_json(root / "learning_ledger" / "current.json")[0]
    assert ledger["review_days"] == 1
    assert ledger["paper_order_count"] == 1
    assert ledger["learning_state"] == "collect_more_paper_trades"
    assert ledger["strategy_config_hash"] == review["strategy_snapshot"]["config_hash"]
    proposal = load_json(root / "strategy_change_proposals" / "current.json")[0]
    assert proposal["status"] == "hold_parameters"
    assert proposal["proposed_changes"] == []
    assert proposal["strategy_config_hash"] == review["strategy_snapshot"]["config_hash"]


def test_strategy_reviewer_cumulative_ledger_aggregates_review_days(tmp_path: Path):
    root = tmp_path / "outputs"
    reviewer = StrategyReviewer(root)
    for index, run_date in enumerate(["2026-05-16", "2026-05-17"]):
        write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD", "signal_id": f"s{index}", "regime": "no_trade", "direction": "watch", "strength": 50 + index, "confidence": 60}])
        write_json(root / "backtests" / f"{run_date}.json", [{"signal_id": f"s{index}", "verdict": "no_trade", "sample_size": 0}])
        write_json(root / "journal_decisions" / f"{run_date}.json", [{"decision_status": "executed_paper"}])
        write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": f"p{index}"}])
        write_json(root / "paper_trades" / "current.json", [])
        write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [{"realized_pnl": index - 0.5}])
        write_json(root / "risk_blocks" / f"{run_date}.json", [])
        write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "missing_bars": 0, "spike_flags": 0}])
        reviewer.build(run_date)

    ledger = load_json(root / "learning_ledger" / "current.json")[0]

    assert ledger["review_days"] == 2
    assert ledger["paper_order_count"] == 2
    assert ledger["closed_trade_count"] == 2
    assert ledger["closed_realized_pnl"] == 0.0


def test_strategy_change_proposal_allows_small_experiment_after_sufficient_positive_evidence(tmp_path: Path):
    root = tmp_path / "outputs"
    reviewer = StrategyReviewer(root)
    ledger = {
        "run_date": "2026-05-31",
        "review_days": 25,
        "closed_trade_count": 22,
        "closed_realized_pnl": 12.5,
        "risk_block_count": 1,
        "learning_state": "stable_keep_parameters",
    }

    proposal = reviewer.build_strategy_change_proposal("2026-05-31", ledger)

    assert proposal["status"] == "eligible_for_small_experiment"
    assert proposal["proposed_changes"]
    assert all(item["requires_manual_approval"] for item in proposal["proposed_changes"])
