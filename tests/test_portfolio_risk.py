from pathlib import Path

from services.journal_store import write_json
from services.portfolio_risk import PortfolioRiskState


def test_portfolio_risk_blocks_candidate_above_daily_cap(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-07-01"
    write_json(
        root / "journal_decisions" / f"{run_date}.json",
        [
            {
                "decision_status": "executed_paper",
                "paper_order": {"order_id": "p1"},
                "risk_snapshot": {"max_loss_pct": 3.0},
            }
        ],
    )

    allowed, risk = PortfolioRiskState(root).should_allow_ticket(run_date, {"max_loss_pct": 2.0})

    assert allowed is False
    assert risk["used_loss_pct"] == 3.0
    assert "daily risk cap exceeded" in risk["block_reason"]
