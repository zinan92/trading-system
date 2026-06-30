from pathlib import Path

from services.bot_checkpoint import BotCheckpoint
from services.journal_store import load_json, write_json


def test_bot_checkpoint_builds_resume_actions_for_open_risks(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    (root / "runner_status").mkdir(parents=True)
    (root / "runner_status" / "current.json").write_text(f'{{"run_date":"{run_date}","state":"ok","interval_seconds":300}}\n', encoding="utf-8")
    write_json(root / "bot_supervisor" / f"{run_date}.json", [{"status": "warn", "mock_bot_running": True}])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_paper": True, "latest_price": 4535, "latest_provider": "gold-api.com"}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "public_snapshot", "provider_groups": {"official": {"rows": 0}}}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "warn", "kill_switch_active": False}])
    write_json(root / "strategy_guardrails" / f"{run_date}.json", [{"allow_new_paper_order": True}])
    write_json(root / "strategy_improvement_plan" / f"{run_date}.json", [{"status": "action_required", "next_steps": [{"step_id": "official_feed"}]}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"net_pnl_marked": -10, "open_unrealized_r": -0.4}}])
    write_json(root / "paper_auto_approval_gate" / f"{run_date}.json", [{"allow_auto_approve": False}])
    write_json(root / "journal_pending" / f"{run_date}.json", [{"ticket_id": "t1"}])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1"}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "tr1"}])
    for folder, filename in [("journals", f"{run_date}.md"), ("review_notes", f"{run_date}.md"), ("reports", f"{run_date}.md")]:
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / filename).write_text("ok\n", encoding="utf-8")

    result = BotCheckpoint(root).build(run_date)

    assert result["status"] == "attention_required"
    assert result["mock_recoverable"] is True
    assert result["live_recoverable"] is False
    assert result["summary"]["official_rows"] == 0
    assert result["summary"]["pending_decisions"] == 1
    assert result["summary"]["open_trades"] == 1
    action_ids = {item["action_id"] for item in result["resume_actions"]}
    assert "import_official_feed" in action_ids
    assert "review_pending_tickets" in action_ids
    assert "review_open_trades" in action_ids
    assert "follow_improvement_plan" in action_ids
    assert "manual_paper_only" in action_ids
    assert load_json(root / "bot_checkpoints" / "current.json")[0]["status"] == "attention_required"
    assert (root / "bot_checkpoints" / f"{run_date}.md").exists()


def test_bot_checkpoint_ready_when_no_resume_blockers(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-31"
    (root / "runner_status").mkdir(parents=True)
    (root / "runner_status" / "current.json").write_text(f'{{"run_date":"{run_date}","state":"ok","interval_seconds":300}}\n', encoding="utf-8")
    write_json(root / "bot_supervisor" / f"{run_date}.json", [{"status": "pass", "mock_bot_running": True}])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_paper": True, "latest_price": 4535, "latest_provider": "mt5_csv"}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "official_broker", "provider_groups": {"official": {"rows": 500}}}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "pass", "kill_switch_active": False}])
    write_json(root / "strategy_guardrails" / f"{run_date}.json", [{"allow_new_paper_order": True}])
    write_json(root / "strategy_improvement_plan" / f"{run_date}.json", [{"status": "collecting_evidence", "next_steps": []}])
    write_json(root / "paper_auto_approval_gate" / f"{run_date}.json", [{"allow_auto_approve": True}])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [])
    write_json(root / "paper_trades" / "current.json", [])

    result = BotCheckpoint(root).build(run_date)

    assert result["status"] == "ready"
    assert result["resume_actions"][0]["action_id"] == "continue_mock_loop"
