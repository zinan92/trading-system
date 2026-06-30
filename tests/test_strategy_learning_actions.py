from pathlib import Path

from services.journal_store import load_json, write_json
from services.strategy_learning_actions import StrategyLearningActions


def test_strategy_learning_actions_prioritizes_data_and_open_exposure(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"suggestions": ["Review open paper trades."]}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "collect_more_paper_trades", "closed_trade_count": 2}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold_parameters"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 2, "open_unrealized_r": -0.8}}])
    write_json(root / "paper_exit_monitor" / f"{run_date}.json", [{"summary": {"nearest_stop_distance_pct": 1.1}}])
    write_json(root / "paper_auto_approval_gate" / f"{run_date}.json", [{"allow_auto_approve": False, "reasons": ["manual review required"]}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "public_snapshot", "provider_groups": {"official": {"rows": 0}}}])

    result = StrategyLearningActions(root).build(run_date)

    assert result["status"] == "review_required"
    assert result["summary"]["high_priority"] >= 2
    action_ids = {item["action_id"] for item in result["actions"]}
    assert "official_feed" in action_ids
    assert "open_exposure_review" in action_ids
    assert "stop_distance_review" in action_ids
    assert "collect_trade_sample" in action_ids
    assert load_json(root / "strategy_learning_actions" / "current.json")[0]["status"] == "review_required"
    assert (root / "strategy_learning_actions" / f"{run_date}.md").exists()


def test_strategy_learning_actions_marks_experiment_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-31"
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"suggestions": []}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "stable_keep_parameters", "closed_trade_count": 24}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "eligible_for_small_experiment", "proposed_changes": [{"field": "entry_thresholds"}]}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 0, "open_unrealized_r": 0}}])
    write_json(root / "paper_exit_monitor" / f"{run_date}.json", [{"summary": {"nearest_stop_distance_pct": None}}])
    write_json(root / "paper_auto_approval_gate" / f"{run_date}.json", [{"allow_auto_approve": True, "reasons": []}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "official_broker", "provider_groups": {"official": {"rows": 250}}}])

    result = StrategyLearningActions(root).build(run_date)

    assert result["status"] == "experiment_ready"
    assert result["summary"]["experiment_allowed"] is True
    assert any(item["action_id"] == "shadow_experiment" for item in result["actions"])
