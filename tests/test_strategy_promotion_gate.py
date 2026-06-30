from pathlib import Path

from services.journal_store import load_json, write_json
from services.strategy_promotion_gate import StrategyPromotionGate


def test_strategy_promotion_gate_blocks_current_public_data_state(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "strategy_experiments" / f"{run_date}.json", [{
        "status": "blocked",
        "baseline": {"variant_id": "baseline", "score": 0.1},
        "best_candidate": {"variant_id": "shorter_hold", "score": 0.6, "verdict": "mixed"},
        "blockers": [{"name": "execution_grade_data", "summary": "missing execution-grade rows"}],
        "closed_trade_count": 2,
    }])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"closed_trade_count": 2}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "public_snapshot", "provider_groups": {"official": {"rows": 0}}}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "warn", "kill_switch_active": False}])
    write_json(root / "strategy_guardrails" / f"{run_date}.json", [{"status": "warn", "allow_new_paper_order": True}])

    result = StrategyPromotionGate(root).run(run_date)

    assert result["status"] == "blocked"
    assert result["promotion_allowed"] is False
    assert result["auto_apply"] is False
    assert result["paper_only"] is True
    assert any(item["name"] == "experiment_execution_grade_data" for item in result["blockers"])
    assert load_json(root / "strategy_promotion_gate" / "current.json")[0]["status"] == "blocked"
    assert (root / "strategy_promotion_gate" / f"{run_date}.md").exists()


def test_strategy_promotion_gate_allows_request_only_when_evidence_passes(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "strategy_experiments" / f"{run_date}.json", [{
        "status": "experiment_ready",
        "strategy_id": "gold_5m_v1",
        "baseline": {"variant_id": "baseline", "score": 0.2},
        "best_candidate": {"variant_id": "shorter_hold", "score": 0.8, "verdict": "supportive"},
        "blockers": [],
        "closed_trade_count": 25,
    }])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"closed_trade_count": 25}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "official_broker", "provider_groups": {"official": {"rows": 300}}}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "pass", "kill_switch_active": False}])
    write_json(root / "strategy_guardrails" / f"{run_date}.json", [{"status": "pass", "allow_new_paper_order": True}])

    result = StrategyPromotionGate(root).run(run_date)

    assert result["status"] == "requestable"
    assert result["promotion_allowed"] is True
    assert result["auto_apply"] is False
    assert result["live_config_change_allowed"] is False
    assert result["score_delta"] == 0.6
    assert result["blockers"] == []


def test_strategy_promotion_gate_accepts_execution_venue_rows(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-29"
    write_json(root / "strategy_experiments" / f"{run_date}.json", [{
        "status": "experiment_ready",
        "strategy_id": "gold_1m_chan",
        "baseline": {"variant_id": "baseline", "score": 0.2},
        "best_candidate": {"variant_id": "shorter_hold", "score": 0.8, "verdict": "supportive"},
        "blockers": [],
        "closed_trade_count": 25,
    }])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"closed_trade_count": 25}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "execution_venue", "provider_groups": {"official": {"rows": 0}, "execution_venue": {"rows": 300}}}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "pass", "kill_switch_active": False}])
    write_json(root / "strategy_guardrails" / f"{run_date}.json", [{"status": "pass", "allow_new_paper_order": True}])

    result = StrategyPromotionGate(root).run(run_date)

    assert result["status"] == "requestable"
    assert result["promotion_allowed"] is True
    assert result["execution_grade_rows"] == 300
    assert result["blockers"] == []
