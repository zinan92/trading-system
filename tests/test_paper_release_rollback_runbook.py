from services.config_loader import ROOT


def test_paper_release_runbook_keeps_required_gates_evidence_and_live_boundary():
    text = (ROOT / "docs" / "runbooks" / "paper-release-rollback-v1.md").read_text(encoding="utf-8")

    for required in (
        "git fetch origin main",
        "/usr/bin/python3 -m pipelines.paper_predeploy_gate --json",
        "outputs/release_gates/paper_predeploy_current.json",
        "com.wendy.trading-orchestrator.dashboard",
        "com.wendy.trading-orchestrator.dualtrack-live-tick",
        "curl --fail --silent --show-error http://127.0.0.1:8765/api/trading-system/read-model",
        "python3 -m pytest -q tests/test_dashboard_gridmind_operator_journey_browser.py",
        "docs/evidence/",
        "live/real-money",
        "reconciliation",
    ):
        assert required in text


def test_paper_release_runbook_distinguishes_delivery_health_data_and_execution_evidence():
    text = (ROOT / "docs" / "runbooks" / "paper-release-rollback-v1.md").read_text(encoding="utf-8")

    for surface in ("| Delivery |", "| Health |", "| Data |", "| Execution |"):
        assert surface in text
    assert "not proof that data is fresh or that an order executed" in text
