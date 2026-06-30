from pathlib import Path

from services.journal_store import load_json, write_json
from services.strategy_improvement_plan import StrategyImprovementPlan


def test_strategy_improvement_plan_merges_actions_experiments_and_blocks(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "strategy_learning_actions" / f"{run_date}.json", [{
        "status": "review_required",
        "actions": [{"action_id": "official_feed", "priority": "high", "type": "data", "summary": "Import official feed.", "command": "import", "evidence": {"official_rows": 0}}],
    }])
    write_json(root / "strategy_experiments" / f"{run_date}.json", [{
        "run_date": run_date,
        "strategy_id": "gold_5m_v1",
        "baseline": {"profit_factor": 1.1},
        "best_candidate": {"variant_id": "tighter_stop", "score": 2.1, "profit_factor": 1.4, "avg_r": 0.2},
        "blockers": [{"name": "official_data", "summary": "Official data required.", "evidence": {"official_rows": 0}}],
    }])
    write_json(root / "strategy_promotion_gate" / f"{run_date}.json", [{"run_date": run_date, "promotion_allowed": False, "blockers": [{"name": "official_data"}]}])
    write_json(root / "strategy_guardrails" / f"{run_date}.json", [{"run_date": run_date, "allow_new_paper_order": True, "summary": {}}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_unrealized_r": -0.2}}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "public_snapshot", "provider_groups": {"official": {"rows": 0}}}])
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "collect evidence"}])

    result = StrategyImprovementPlan(root).build(run_date)

    assert result["status"] == "action_required"
    assert result["paper_only"] is True
    assert result["auto_apply"] is False
    assert result["summary"]["official_rows"] == 0
    step_ids = {item["step_id"] for item in result["next_steps"]}
    assert "official_feed" in step_ids
    assert "shadow_best_candidate" in step_ids
    assert "experiment_blocker_official_data" in step_ids
    assert "hold_parameters" in step_ids
    assert load_json(root / "strategy_improvement_plan" / "current.json")[0]["status"] == "action_required"
    assert (root / "strategy_improvement_plan" / f"{run_date}.md").exists()


def test_strategy_improvement_plan_marks_promotion_review(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-31"
    write_json(root / "strategy_learning_actions" / f"{run_date}.json", [{"actions": []}])
    write_json(root / "strategy_experiments" / f"{run_date}.json", [{"strategy_id": "gold_5m_v1", "best_candidate": {"variant_id": "wider_target"}}])
    write_json(root / "strategy_promotion_gate" / f"{run_date}.json", [{"run_date": run_date, "promotion_allowed": True, "candidate": {"variant_id": "wider_target"}}])
    write_json(root / "strategy_guardrails" / f"{run_date}.json", [{"run_date": run_date, "allow_new_paper_order": True}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "official_broker", "provider_groups": {"official": {"rows": 500}}}])

    result = StrategyImprovementPlan(root).build(run_date)

    assert result["status"] == "promotion_review"
    assert result["summary"]["promotion_allowed"] is True
    assert any(item["step_id"] == "promotion_request" for item in result["next_steps"])
