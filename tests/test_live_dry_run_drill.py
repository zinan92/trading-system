from pathlib import Path

from services.journal_store import load_json, write_json
from services.live_dry_run_drill import LiveDryRunDrill


def test_live_dry_run_drill_blocks_public_data_and_keeps_live_submit_false(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": False, "latest_price": 4524.2, "latest_provider": "gold-api.com", "official_rows": 0}])
    write_json(root / "data_source_lineage" / "current.json", [{"truth_level": "public_snapshot"}])
    write_json(root / "broker_preflight" / "current.json", [{"provider": "manual_gateway", "ready": True, "dry_run": True}])
    write_json(root / "risk_monitor" / "current.json", [{"status": "block", "kill_switch_active": False}])
    write_json(root / "operation_runbooks" / "current.json", [{"status": "paper_manual_only"}])
    write_json(root / "live_readiness" / "current.json", [{"status": "fail", "live_ready": False, "checks": [
        {"name": "official_market_data", "status": "fail", "summary": "public feed only"},
        {"name": "live_env", "status": "fail", "summary": "missing keys"},
        {"name": "broker_provider", "status": "fail", "summary": "manual gateway"},
        {"name": "broker_feedback", "status": "fail", "summary": "no receipts"},
        {"name": "runner", "status": "pass", "summary": "5m runner ok"},
        {"name": "risk_rules", "status": "pass", "summary": "rules ok"},
        {"name": "journal_review", "status": "pass", "summary": "journal exists"},
    ]}])
    write_json(root / "live_activation" / "current.json", [{"status": "blocked", "dry_run_ready": False, "real_money_ready": False, "checks": [], "real_money_checks": []}])
    write_json(root / "live_approvals" / "current.json", [{"status": "requested", "approved": False}])
    write_json(root / "live_submission_safety" / "current.json", [{"run_date": run_date, "status": "pass", "blocked_by_activation_gate": True, "network_call_attempted": False}])
    write_json(root / "live_cutover" / "current.json", [{"status": "blocked", "required_external_inputs": [{"name": "official_gold_5m_feed"}]}])
    write_json(root / "live_switch_plan" / "current.json", [{"status": "blocked", "steps": []}])

    result = LiveDryRunDrill(root, tmp_path / "missing.db").run(run_date, refresh_dependencies=False)

    assert result["status"] == "blocked"
    assert result["safe_to_submit_live_order"] is False
    assert result["network_call_attempted"] is False
    assert result["data_truth_level"] == "public_snapshot"
    assert result["latest_price"] == 4524.2
    assert any(item["name"] == "official_data" for item in result["blockers"])
    assert (root / "live_dry_run_drill" / f"{run_date}.md").exists()
    assert load_json(root / "live_dry_run_drill" / "current.json")[0]["safe_to_submit_live_order"] is False


def test_live_dry_run_drill_reports_real_money_ready_when_all_gates_pass(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    pass_checks = [
        {"name": "official_market_data", "status": "pass", "summary": "official feed ok"},
        {"name": "live_env", "status": "pass", "summary": "env ok"},
        {"name": "broker_provider", "status": "pass", "summary": "broker ok"},
        {"name": "broker_feedback", "status": "pass", "summary": "feedback ok"},
        {"name": "runner", "status": "pass", "summary": "runner ok"},
        {"name": "risk_rules", "status": "pass", "summary": "rules ok"},
        {"name": "journal_review", "status": "pass", "summary": "journal ok"},
    ]
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_live": True, "latest_price": 4524.2, "latest_provider": "oanda", "official_rows": 100}])
    write_json(root / "data_source_lineage" / "current.json", [{"truth_level": "official_broker"}])
    write_json(root / "broker_preflight" / "current.json", [{"provider": "oanda_rest", "ready": True, "dry_run": False}])
    write_json(root / "risk_monitor" / "current.json", [{"status": "pass", "kill_switch_active": False}])
    write_json(root / "operation_runbooks" / "current.json", [{"status": "live_ready"}])
    write_json(root / "live_readiness" / "current.json", [{"status": "pass", "live_ready": True, "checks": pass_checks}])
    write_json(root / "live_activation" / "current.json", [{"status": "real_money_ready", "dry_run_ready": True, "real_money_ready": True, "checks": [], "real_money_checks": []}])
    write_json(root / "live_approvals" / "current.json", [{"status": "approved", "approved": True}])
    write_json(root / "live_submission_safety" / "current.json", [{"run_date": run_date, "status": "pass", "blocked_by_activation_gate": True, "network_call_attempted": False}])
    write_json(root / "live_cutover" / "current.json", [{"status": "real_money_ready", "required_external_inputs": []}])
    write_json(root / "live_switch_plan" / "current.json", [{"status": "ready", "steps": []}])

    result = LiveDryRunDrill(root, tmp_path / "missing.db").run(run_date, refresh_dependencies=False)

    assert result["status"] == "real_money_ready"
    assert result["safe_to_submit_live_order"] is True
    assert result["blockers"] == []
