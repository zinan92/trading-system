from services.config_loader import ROOT


def test_paper_release_runbook_keeps_required_gates_evidence_and_live_boundary():
    text = (ROOT / "docs" / "runbooks" / "paper-release-rollback-v1.md").read_text(encoding="utf-8")

    for required in (
        "git fetch origin main",
        "git rev-parse HEAD",
        "/usr/bin/python3 -m pipelines.paper_predeploy_gate --json",
        '"status": "pass"',
        "paper_predeploy_gate: pass",
        "outputs/release_gates/paper_predeploy_current.json",
        '["source_sha"]',
        "com.wendy.trading-orchestrator.dashboard",
        "com.wendy.trading-orchestrator.dualtrack-live-tick",
        "outputs/schedules/install_current.json",
        "outputs/dualtrack/cutover/",
        "scheduler restart status as",
        "curl --fail --silent --show-error http://127.0.0.1:8765/api/trading-system/read-model",
        "python3 -m pytest -q tests/test_dashboard_gridmind_operator_journey_browser.py",
        "server maps that URL to the",
        "dashboard-gridmind.html",
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


def test_completed_progress_uses_an_immutable_evidence_baseline_not_moving_head():
    text = (ROOT / "docs" / "plans" / "implementation-progress-2026-07-24.md").read_text(
        encoding="utf-8"
    )

    assert "**Completion evidence baseline:**" in text
    assert "not\nintended to equal the moving repository HEAD" in text
    assert "**As of:**" not in text
