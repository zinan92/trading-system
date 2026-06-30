from pathlib import Path

from services.journal_store import load_json, write_json
from services.paper_risk_action_plan import PaperRiskActionPlan


def test_paper_risk_action_plan_turns_exit_queue_into_commands(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "block", "kill_switch_active": True, "summary": {"block_reasons": ["open R breach"]}}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 1, "open_unrealized_r": -1.1}}])
    write_json(
        root / "paper_exit_decisions" / f"{run_date}.json",
        [
            {
                "status": "alert",
                "queue": [
                    {
                        "trade_id": "tr1",
                        "priority": "high",
                        "exit_type": "portfolio_risk_reduction",
                        "required_user_action": "approve_exit",
                        "suggested_exit_price": 4518.4,
                        "unrealized_r": -0.56,
                        "portfolio_open_unrealized_r": -1.1,
                    }
                ],
            }
        ],
    )

    result = PaperRiskActionPlan(root).build(run_date)

    assert result["status"] == "action_required"
    assert result["summary"]["high_priority"] == 1
    assert result["summary"]["exit_actions"] == 1
    assert result["actions"][0]["type"] == "paper_exit"
    assert "--trade-id tr1 --decision approve_exit" in result["actions"][0]["command"]
    assert load_json(root / "paper_risk_action_plan" / "current.json")[0]["status"] == "action_required"
    assert (root / "paper_risk_action_plan" / f"{run_date}.md").exists()


def test_paper_risk_action_plan_falls_back_to_risk_review(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "block", "kill_switch_active": True, "summary": {"block_reasons": ["risk kill switch"]}}])
    write_json(root / "paper_exit_decisions" / f"{run_date}.json", [{"status": "empty", "queue": []}])

    result = PaperRiskActionPlan(root).build(run_date)

    assert result["status"] == "action_required"
    assert result["actions"][0]["action_id"] == "risk_review"
    assert "pipelines.risk_monitor" in result["actions"][0]["command"]
