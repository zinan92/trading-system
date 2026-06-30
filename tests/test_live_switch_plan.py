from pathlib import Path

from services.journal_store import load_json, write_json
from services.live_switch_plan import LiveSwitchPlan, run_live_switch_plan


def test_live_switch_plan_surfaces_blocked_steps(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": False, "message": "public feed only"}])
    write_json(root / "live_env" / "current.json", [{"status": "warn", "missing_keys": ["OANDA_API_TOKEN"]}])
    write_json(root / "oanda_account" / "current.json", [{"status": "skipped", "instrument_ready": False}])
    write_json(root / "broker_preflight" / "current.json", [{"provider": "manual_gateway", "ready": True}])
    write_json(root / "live_readiness" / "current.json", [{"status": "fail", "live_ready": False}])
    write_json(root / "live_activation" / "current.json", [{"status": "blocked", "dry_run_ready": False, "real_money_ready": False}])
    write_json(root / "schedules" / "status_current.json", [{"status": "active"}])

    result = LiveSwitchPlan(root).run(run_date)

    assert result["status"] == "blocked"
    steps = {item["name"]: item for item in result["steps"]}
    assert steps["schedule"]["status"] == "done"
    assert steps["official_5m_data"]["status"] == "blocked"
    assert steps["live_env"]["status"] == "blocked"
    assert steps["broker_provider"]["status"] == "blocked"
    assert any("pipelines.live_readiness" in command for command in result["commands"])
    assert (root / "live_switch_plan" / f"{run_date}.md").exists()
    assert load_json(root / "live_switch_plan" / "current.json")[0]["status"] == "blocked"


def test_live_switch_plan_accepts_execution_venue_market_data(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-22"
    write_json(
        root / "data_source_preflight" / "current.json",
        [
            {
                "ready_for_live": True,
                "ready_for_paper": True,
                "live_data_mode": "execution_venue",
                "latest_provider": "binance_usdm",
                "official_rows": 0,
                "execution_venue_rows": 500,
            }
        ],
    )
    write_json(root / "live_env" / "current.json", [{"status": "pass"}])
    write_json(root / "oanda_account" / "current.json", [{"status": "pass", "instrument_ready": True}])
    write_json(root / "broker_preflight" / "current.json", [{"provider": "binance_usdm", "ready": True}])
    write_json(root / "live_readiness" / "current.json", [{"status": "fail", "live_ready": False}])
    write_json(root / "live_activation" / "current.json", [{"status": "blocked", "dry_run_ready": False, "real_money_ready": False}])
    write_json(root / "schedules" / "status_current.json", [{"status": "active"}])

    result = LiveSwitchPlan(root).run(run_date)

    steps = {item["name"]: item for item in result["steps"]}
    assert result["status"] == "blocked"
    assert steps["official_5m_data"]["status"] == "done"


def test_live_switch_plan_passes_when_all_live_inputs_are_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "data_source_preflight" / "current.json",
        [
            {
                "ready_for_live": True,
                "message": "official feed",
                "live_data_mode": "official_broker",
                "official_rows": 240,
                "latest_provider": "mt5_csv",
                "official_broker_providers": ["mt5_csv"],
            }
        ],
    )
    write_json(root / "live_env" / "current.json", [{"status": "pass"}])
    write_json(root / "oanda_account" / "current.json", [{"status": "pass", "instrument_ready": True}])
    write_json(root / "broker_preflight" / "current.json", [{"provider": "oanda_rest", "ready": True}])
    write_json(root / "live_readiness" / "current.json", [{"status": "pass", "live_ready": True}])
    write_json(root / "live_activation" / "current.json", [{"status": "ready", "dry_run_ready": True, "real_money_ready": True, "approval": {"approved": True}}])
    write_json(root / "schedules" / "status_current.json", [{"status": "active"}])

    result = run_live_switch_plan(run_date, root)

    assert result["status"] == "ready"
    assert all(item["status"] == "done" for item in result["steps"])
