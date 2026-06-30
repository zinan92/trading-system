from pathlib import Path

from services.journal_store import load_json, write_json
from services.paper_auto_approval_gate import PaperAutoApprovalGate


def _base_ready(root: Path, run_date: str) -> None:
    write_json(root / "journal_pending" / f"{run_date}.json", [{"ticket_id": "ticket_gold", "asset": "GOLD"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 0, "open_unrealized_r": 0, "net_pnl_marked": 0}}])
    write_json(root / "equity_curve" / "current.json", [{"daily_pnl_pct": 0, "daily_pnl": 0, "current_drawdown_pct": 0}])
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "ready_for_live": True, "latest_provider": "broker_csv"}])
    write_json(root / "learning_ledger" / "current.json", [{"closed_trade_count": 25, "review_days": 25, "risk_block_count": 0}])
    write_json(root / "strategy_change_proposals" / "current.json", [{"status": "stable"}])
    write_json(root / "paper_trades" / "current.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "paper_execution_blocks" / f"{run_date}.json", [])


def test_paper_auto_approval_gate_allows_when_risk_and_guardrails_pass(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _base_ready(root, run_date)

    result = PaperAutoApprovalGate(root).evaluate(run_date, auto_requested=True)

    assert result["status"] == "allow"
    assert result["allow_auto_approve"] is True
    assert result["selected_ticket_id"] == "ticket_gold"
    assert result["risk_monitor"]["allow_paper_auto_approve"] is True
    assert load_json(root / "paper_auto_approval_gate" / "current.json")[0]["status"] == "allow"


def test_paper_auto_approval_gate_blocks_on_risk_monitor_warnings(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _base_ready(root, run_date)
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 2, "open_unrealized_r": -0.7, "net_pnl_marked": -120}}])

    result = PaperAutoApprovalGate(root).evaluate(run_date, auto_requested=True)

    assert result["status"] == "block"
    assert result["allow_auto_approve"] is False
    assert any("manual review" in reason or "open paper trades" in reason for reason in result["reasons"])


def test_paper_auto_approval_gate_skips_when_not_requested(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _base_ready(root, run_date)

    result = PaperAutoApprovalGate(root).evaluate(run_date, auto_requested=False)

    assert result["status"] == "skipped"
    assert result["allow_auto_approve"] is False
    assert "paper auto-approve was not requested" in result["reasons"]
