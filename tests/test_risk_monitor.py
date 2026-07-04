from pathlib import Path

from services.journal_store import load_json, write_json
from services.risk_monitor import RiskMonitor


def test_risk_monitor_warns_for_public_data_and_open_drawdown(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": False, "latest_provider": "gold-api.com"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 2, "open_unrealized_r": -0.75, "net_pnl_marked": -120}}])
    write_json(root / "equity_curve" / "current.json", [{"daily_pnl_pct": -0.25, "daily_pnl": -25, "current_drawdown_pct": -0.25}])
    write_json(root / "strategy_guardrails" / "current.json", [{"status": "warn", "allow_new_paper_order": True, "summary": {"warnings": 2}}])
    write_json(root / "paper_execution_blocks" / f"{run_date}.json", [])

    result = RiskMonitor(root).run(run_date)

    assert result["status"] == "warn"
    assert result["kill_switch_active"] is False
    assert result["allow_paper_auto_approve"] is False
    assert result["allow_manual_review"] is True
    assert result["summary"]["auto_approval_blocks"] == 3
    assert any("manual review required" in reason for reason in result["summary"]["auto_approval_block_reasons"])
    assert any(item["name"] == "open_unrealized_r" and item["status"] == "warn" for item in result["checks"])
    assert load_json(root / "risk_monitor" / "current.json")[0]["run_date"] == run_date
    assert (root / "risk_monitor" / f"{run_date}.md").exists()


def _benign_risk_artifacts(root: Path, run_date: str) -> None:
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": True, "latest_provider": "gold-api.com"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 0, "open_unrealized_r": 0, "net_pnl_marked": 0}}])
    write_json(root / "equity_curve" / "current.json", [{"daily_pnl_pct": 0, "daily_pnl": 0, "current_drawdown_pct": 0}])
    write_json(root / "paper_execution_blocks" / f"{run_date}.json", [])


def test_learning_state_warn_does_not_block_auto_approve(tmp_path: Path):
    # A guardrail warn driven ONLY by the <20-closed-samples learning state (which
    # self-declares blocks_paper_sampling=False) must NOT freeze auto-approval, else
    # the system can never collect the samples it is waiting for.
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _benign_risk_artifacts(root, run_date)
    write_json(
        root / "strategy_guardrails" / "current.json",
        [{
            "status": "warn",
            "allow_new_paper_order": True,
            "checks": [
                {"name": "learning_state", "status": "warn", "summary": "collecting 20 closed trades", "evidence": {"blocks_paper_sampling": False, "closed_trade_count": 5}},
                {"name": "open_exposure", "status": "pass", "summary": "ok", "evidence": {"open_trade_count": 0}},
            ],
            "summary": {"warnings": 1},
        }],
    )

    result = RiskMonitor(root).run(run_date)

    assert result["allow_paper_auto_approve"] is True
    guardrail_check = next(item for item in result["checks"] if item["name"] == "strategy_guardrails")
    assert guardrail_check["status"] == "pass"


def test_real_caution_warn_still_blocks_auto_approve(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _benign_risk_artifacts(root, run_date)
    write_json(
        root / "strategy_guardrails" / "current.json",
        [{
            "status": "warn",
            "allow_new_paper_order": True,
            "checks": [
                {"name": "learning_state", "status": "warn", "summary": "collecting", "evidence": {"blocks_paper_sampling": False}},
                {"name": "open_exposure", "status": "warn", "summary": "2 open paper trades exist; review exposure before adding.", "evidence": {"open_trade_count": 2}},
            ],
            "summary": {"warnings": 2},
        }],
    )

    result = RiskMonitor(root).run(run_date)

    assert result["allow_paper_auto_approve"] is False
    guardrail_check = next(item for item in result["checks"] if item["name"] == "strategy_guardrails")
    assert guardrail_check["status"] == "warn"


def test_risk_monitor_allows_paper_auto_approve_for_public_data_only_warning(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": False, "latest_provider": "gold-api.com"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 0, "open_unrealized_r": 0, "net_pnl_marked": 0}}])
    write_json(root / "equity_curve" / "current.json", [{"daily_pnl_pct": 0, "daily_pnl": 0, "current_drawdown_pct": 0}])
    write_json(root / "strategy_guardrails" / "current.json", [{"status": "pass", "allow_new_paper_order": True, "summary": {}}])
    write_json(root / "paper_execution_blocks" / f"{run_date}.json", [])

    result = RiskMonitor(root).run(run_date)

    assert result["status"] == "warn"
    assert result["kill_switch_active"] is False
    assert result["allow_paper_auto_approve"] is True
    assert result["summary"]["auto_approval_blocks"] == 0


def test_risk_monitor_blocks_when_data_source_not_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": False, "message": "stale quote"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 0, "open_unrealized_r": 0}}])
    write_json(root / "equity_curve" / "current.json", [{"daily_pnl_pct": 0, "daily_pnl": 0, "current_drawdown_pct": 0}])
    write_json(root / "strategy_guardrails" / "current.json", [{"status": "pass", "allow_new_paper_order": True}])

    result = RiskMonitor(root).run(run_date)

    assert result["status"] == "block"
    assert result["kill_switch_active"] is True
    assert result["allow_paper_auto_approve"] is False
    assert "stale quote" in result["summary"]["block_reasons"][0]


def test_risk_monitor_blocks_on_daily_loss_limit(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": True, "latest_provider": "mt5_csv"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 0, "open_unrealized_r": 0}}])
    write_json(root / "equity_curve" / "current.json", [{"daily_pnl_pct": -4.1, "daily_pnl": -410, "current_drawdown_pct": -4.1}])
    write_json(root / "strategy_guardrails" / "current.json", [{"status": "pass", "allow_new_paper_order": True}])

    result = RiskMonitor(root).run(run_date)

    assert result["status"] == "block"
    assert result["kill_switch_active"] is True
    assert any(item["name"] == "daily_loss" and item["status"] == "block" for item in result["checks"])


def test_risk_monitor_does_not_kill_on_old_lifetime_drawdown(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-22"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": True, "latest_provider": "binance_usdm"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 0, "open_unrealized_r": 0}}])
    write_json(
        root / "equity_curve" / "current.json",
        [{"daily_pnl_pct": 0.0, "daily_pnl": 0.0, "current_drawdown_pct": -3.22, "max_drawdown_pct": -3.22}],
    )
    write_json(root / "strategy_guardrails" / "current.json", [{"status": "pass", "allow_new_paper_order": True}])
    write_json(root / "paper_execution_blocks" / f"{run_date}.json", [])

    result = RiskMonitor(root).run(run_date)

    assert result["status"] == "pass"
    assert result["kill_switch_active"] is False
    assert any(item["name"] == "daily_loss" and item["status"] == "pass" for item in result["checks"])
