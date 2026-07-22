from pathlib import Path

from pipelines.dashboard_server import _current_strategy_risk_decision
from services.journal_store import write_json


def test_dashboard_api_reads_the_active_dca_risk_receipt(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    write_json(
        output / "dca_risk_decisions" / "2026-07-22_DAY.json",
        [{"decision_id": "dca-risk-active", "outcome": "acknowledged"}],
    )
    source = {
        "cycle": {"cycle_id": "2026-07-22_DAY"},
        "production_plan": {"strategy_type": "dca"},
        "runtime": {"risk_decision_id": "dca-risk-active"},
    }

    assert _current_strategy_risk_decision(output, source) == {
        "decision_id": "dca-risk-active",
        "outcome": "acknowledged",
    }
