from pathlib import Path

from services.journal_store import load_json, write_json
from services.live_cutover_package import LiveCutoverPackage


def test_live_cutover_package_blocks_without_official_inputs(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": False, "message": "public feed only"}])
    write_json(root / "live_env" / "current.json", [{"status": "warn", "missing_keys": ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]}])
    write_json(root / "oanda_account" / "current.json", [{"status": "skipped", "account_ready": False, "instrument_ready": False}])
    write_json(root / "broker_preflight" / "current.json", [{"provider": "manual_gateway", "ready": True, "dry_run": True}])
    write_json(root / "live_readiness" / "current.json", [{"status": "fail", "live_ready": False, "checks": [{"name": "official_market_data", "status": "fail", "summary": "public feed only"}]}])
    write_json(root / "live_activation" / "current.json", [{"status": "blocked", "dry_run_ready": False, "real_money_ready": False, "real_money_checks": [{"name": "official_5m_data", "status": "fail", "summary": "official feed missing"}]}])
    write_json(root / "live_switch_plan" / "current.json", [{"status": "blocked", "steps": [{"name": "official_5m_data", "status": "blocked", "action": "import feed"}]}])

    result = LiveCutoverPackage(root, tmp_path / "missing.db").run(run_date)

    assert result["status"] == "blocked"
    assert result["real_money_ready"] is False
    assert any(item["name"] == "official_market_data" for item in result["blockers"])
    assert any(item["name"] == "OANDA_API_TOKEN" and item["secret"] is True for item in result["required_external_inputs"])
    assert any("pipelines.import_official_feed" in item["command"] for item in result["cutover_sequence"])
    assert any("execution_mode back to paper" in item["action"] for item in result["rollback_plan"])
    assert (root / "live_cutover" / f"{run_date}.md").exists()
    assert load_json(root / "live_cutover" / "current.json")[0]["status"] == "blocked"


def test_live_cutover_package_can_report_real_money_ready_when_all_gates_pass(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_live": True, "message": "official feed"}])
    write_json(root / "live_env" / "current.json", [{"status": "pass"}])
    write_json(root / "oanda_account" / "current.json", [{"status": "pass", "account_ready": True, "instrument_ready": True}])
    write_json(root / "broker_preflight" / "current.json", [{"provider": "oanda_rest", "ready": True, "dry_run": False}])
    write_json(root / "live_readiness" / "current.json", [{"status": "pass", "live_ready": True, "checks": []}])
    write_json(root / "live_activation" / "current.json", [{"status": "real_money_ready", "dry_run_ready": True, "real_money_ready": True, "real_money_checks": [], "approval": {"approved": True}}])
    write_json(root / "live_switch_plan" / "current.json", [{"status": "ready", "steps": []}])

    result = LiveCutoverPackage(root, tmp_path / "missing.db").run(run_date)

    assert result["status"] == "real_money_ready"
    assert result["blockers"] == []
