from pathlib import Path

from services.journal_store import load_json, write_json
from services.operation_runbook import OperationRunbook


def test_operation_runbook_blocks_live_and_auto_when_official_feed_and_risk_are_not_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"run_date": run_date, "status": "warn", "ready_for_paper": True, "ready_for_live": False, "latest_price": 4525, "latest_provider": "gold-api.com"}])
    write_json(root / "official_feed_receipts" / f"{run_date}.json", [{"run_date": run_date, "status": "warn", "ready_for_live": False, "official_rows": 0, "next_actions": ["import official feed"]}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"run_date": run_date, "status": "block", "kill_switch_active": True, "allow_paper_auto_approve": False, "summary": {"block_reasons": ["open exposure -1R"]}}])
    write_json(root / "live_readiness" / f"{run_date}.json", [{"run_date": run_date, "status": "fail", "live_ready": False, "summary": {"failed_checks": ["official_market_data"]}, "next_actions": ["run live readiness"]}])
    write_json(root / "live_activation" / f"{run_date}.json", [{"run_date": run_date, "status": "blocked", "real_money_ready": False}])
    write_json(root / "daily_review_runs" / f"{run_date}.json", [{"run_date": run_date, "status": "warn"}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"ticket_id": "t1"}])
    (root / "journals").mkdir(parents=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")

    result = OperationRunbook(root).run(run_date)

    assert result["status"] == "paper_manual_only"
    assert result["permissions"]["paper_manual_review"] is True
    assert result["permissions"]["paper_auto_approve"] is False
    assert result["permissions"]["live_trading"] is False
    assert any(item["name"] == "official_feed" for item in result["blocks"])
    assert any(item["name"] == "risk_kill_switch" for item in result["blocks"])
    assert "import official feed" in result["next_actions"]
    assert load_json(root / "operation_runbooks" / "current.json")[0]["run_date"] == run_date
    assert (root / "operation_runbooks" / f"{run_date}.md").exists()


def test_operation_runbook_prefers_dated_artifacts_over_stale_current(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"run_date": run_date, "status": "pass", "ready_for_paper": True, "ready_for_live": True, "latest_price": 4525, "latest_provider": "mt5_csv"}])
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": "2026-07-02", "status": "fail", "ready_for_paper": False}])
    write_json(root / "official_feed_receipts" / f"{run_date}.json", [{"run_date": run_date, "status": "pass", "ready_for_live": True, "official_rows": 240}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"run_date": run_date, "status": "pass", "kill_switch_active": False, "allow_paper_auto_approve": True, "summary": {}}])
    write_json(root / "live_readiness" / f"{run_date}.json", [{"run_date": run_date, "status": "pass", "live_ready": True, "summary": {}}])
    write_json(root / "live_activation" / f"{run_date}.json", [{"run_date": run_date, "status": "real_money_ready", "real_money_ready": True}])
    (root / "journals").mkdir(parents=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")

    result = OperationRunbook(root).run(run_date)

    assert result["status"] == "live_ready"
    assert result["permissions"]["paper_auto_approve"] is True
    assert result["permissions"]["live_trading"] is True
    assert result["summary"]["latest_provider"] == "mt5_csv"


def test_operation_runbook_uses_newer_current_artifact_for_same_run_date(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"run_date": run_date, "checked_at": "2026-05-26T08:00:00+00:00", "status": "warn", "ready_for_paper": True, "ready_for_live": False, "latest_price": 4532, "latest_provider": "gold-api.com"}])
    write_json(root / "data_source_preflight" / "current.json", [{"run_date": run_date, "checked_at": "2026-05-26T08:05:00+00:00", "status": "warn", "ready_for_paper": True, "ready_for_live": False, "latest_price": 4539, "latest_provider": "gold-api.com"}])
    write_json(root / "official_feed_receipts" / f"{run_date}.json", [{"run_date": run_date, "status": "warn", "ready_for_live": False, "official_rows": 0}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"run_date": run_date, "status": "pass", "kill_switch_active": False, "allow_paper_auto_approve": False, "summary": {}}])
    write_json(root / "live_readiness" / f"{run_date}.json", [{"run_date": run_date, "status": "fail", "live_ready": False, "summary": {"failed_checks": ["official_market_data"]}}])
    write_json(root / "live_activation" / f"{run_date}.json", [{"run_date": run_date, "status": "blocked", "real_money_ready": False}])
    (root / "journals").mkdir(parents=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")

    result = OperationRunbook(root).run(run_date)

    assert result["summary"]["latest_price"] == 4539


def test_operation_runbook_surfaces_paper_risk_action_commands(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"run_date": run_date, "status": "warn", "ready_for_paper": True, "ready_for_live": False, "latest_price": 4539, "latest_provider": "gold-api.com"}])
    write_json(root / "official_feed_receipts" / f"{run_date}.json", [{"run_date": run_date, "status": "warn", "ready_for_live": False, "official_rows": 0}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"run_date": run_date, "status": "pass", "kill_switch_active": False, "allow_paper_auto_approve": True, "summary": {}}])
    write_json(root / "paper_risk_action_plan" / f"{run_date}.json", [{
        "run_date": run_date,
        "status": "action_required",
        "summary": {"action_count": 1, "high_priority": 1},
        "actions": [{
            "summary": "Review and approve paper exit for trade_1.",
            "command": "python3 -m pipelines.paper_exit_decision --date 2026-05-26 --trade-id trade_1 --decision approve_exit",
        }],
    }])
    write_json(root / "live_readiness" / f"{run_date}.json", [{"run_date": run_date, "status": "fail", "live_ready": False, "summary": {"failed_checks": ["official_market_data"]}}])
    write_json(root / "live_activation" / f"{run_date}.json", [{"run_date": run_date, "status": "blocked", "real_money_ready": False}])
    (root / "journals").mkdir(parents=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")

    result = OperationRunbook(root).run(run_date)

    assert result["status"] == "paper_manual_only"
    assert result["permissions"]["paper_auto_approve"] is False
    assert result["summary"]["paper_risk_action_plan"] == "action_required"
    assert result["summary"]["paper_risk_high_priority"] == 1
    assert any(item["name"] == "paper_risk_actions" for item in result["blocks"])
    assert any("pipelines.paper_exit_decision" in item for item in result["next_actions"])
