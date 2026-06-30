from pathlib import Path

from services.backend_maturity_audit import BackendMaturityAudit, MATURITY_AUDIT_VERSION
from services.journal_store import load_json, write_json


def _base_state() -> dict:
    return {
        "system_vitals": {
            "overall": "alive",
            "vitals": [
                {"name": "data_feed", "status": "up"},
                {"name": "runner_liveness", "status": "up"},
                {"name": "strategy_evaluation", "status": "up"},
                {"name": "tp_sl_coverage", "status": "up"},
                {"name": "execution_blocker", "status": "up"},
                {"name": "no_trade_attribution", "status": "up"},
            ],
        },
        "dashboard_health": {"health_attention": []},
        "performance_board": {
            "active_demo_blocker": {"status": "clear", "blocked": False},
            "frequency_board": {"effective_strategy_count": 1},
            "strategies": [
                {
                    "strategy_id": "alpha",
                    "closed_trades": 2,
                    "daily_execution": {"executed_trade_count": 1},
                    "frequency_diagnostics": {"attribution": {"frequency_is_diagnostic_not_sla": True}},
                    "strategy_book": {"audit": {"status": "pass"}},
                    "edge_judgment": {"label": "insufficient_sample", "closed_trade_count": 2, "min_closed_trades": 20},
                }
            ],
        },
    }


def _strategy_state() -> dict:
    return {
        "strategy_detail": {
            "trade_record_audit": {"status": "pass", "trade_count": 1},
            "trade_record_cards": [{"trade_id": "t1"}],
            "strategy_book": {"audit": {"status": "pass"}},
            "edge_judgment": {"label": "insufficient_sample", "closed_trade_count": 2, "min_closed_trades": 20},
        }
    }


def _write_runtime(root: Path) -> None:
    write_json(root / "health" / "current.json", [{
        "status": "ok",
        "checks": [
            {"name": "active_demo_reconciliation", "status": "ok", "message": "reconciles"},
            {"name": "alert_delivery", "status": "ok", "message": "delivered"},
            {"name": "vitals_tp_sl_coverage", "status": "ok", "message": "protected"},
            {"name": "vitals_runner_liveness", "status": "ok", "message": "fresh"},
            {"name": "vitals_data_feed", "status": "ok", "message": "fresh"},
        ],
    }])
    write_json(root / "alert_delivery_drill" / "current.json", [{"status": "pass", "delivered": True, "channel": "feishu"}])
    write_json(root / "strategy_frequency" / "current.json", [{"schema_version": "strategy-frequency-governance-v2", "status": "within_portfolio_sample_target", "summary": {"portfolio_executed_count": 1}}])
    write_json(root / "strategy_promotion_gate" / "current.json", [{
        "status": "blocked",
        "promotion_allowed": False,
        "auto_apply": False,
        "paper_only": True,
        "live_config_change_allowed": False,
        "strategy_id": "alpha",
        "closed_trade_count": 2,
        "official_rows": 0,
        "data_truth_level": "execution_venue",
        "blockers": [{"name": "closed_trade_sample", "summary": "Need at least 20 closed paper trades before requesting parameter promotion.", "evidence": {"closed_trade_count": 2}}],
    }])


def test_backend_maturity_audit_passes_platform_axis(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    _write_runtime(root)
    audit = BackendMaturityAudit(output_root=root, market_db=tmp_path / "missing.db")
    monkeypatch.setattr(audit, "_base_snapshot", lambda run_date: _base_state())
    monkeypatch.setattr(audit, "_strategy_snapshot", lambda run_date, strategy_id: _strategy_state())

    result = audit.run("2026-06-24")

    assert result["schema_version"] == MATURITY_AUDIT_VERSION
    assert result["status"] == "pass"
    assert [check["status"] for check in result["checks"][:6]] == ["pass"] * 6
    assert result["checks"][6]["name"] == "M6_pm_allocation_gate"
    assert result["checks"][6]["status"] == "locked"
    assert result["summary"]["pm_allocation_status"] == "locked"
    assert load_json(root / "backend_maturity" / "current.json")[0]["status"] == "pass"


def test_backend_maturity_audit_fails_when_trade_cards_incomplete(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    _write_runtime(root)
    audit = BackendMaturityAudit(output_root=root, market_db=tmp_path / "missing.db")
    bad_strategy = _strategy_state()
    bad_strategy["strategy_detail"]["trade_record_audit"] = {"status": "incomplete", "missing_fields": ["entry_reason"]}
    monkeypatch.setattr(audit, "_base_snapshot", lambda run_date: _base_state())
    monkeypatch.setattr(audit, "_strategy_snapshot", lambda run_date, strategy_id: bad_strategy)

    result = audit.run("2026-06-24")

    assert result["status"] == "warn"
    m1 = next(check for check in result["checks"] if check["name"] == "M1_trade_record_cards")
    assert m1["status"] == "fail"
    assert m1["evidence"]["bad"][0]["strategy_id"] == "alpha"


def test_backend_maturity_audit_fails_m5_when_active_demo_reconciliation_errors(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    _write_runtime(root)
    audit = BackendMaturityAudit(output_root=root, market_db=tmp_path / "missing.db")
    state = _base_state()
    state["performance_board"]["active_demo_blocker"] = {
        "blocked": True,
        "status": "reconciliation_error",
        "reason": "Binance demo reconciliation failed: TimeoutError: The read operation timed out",
    }
    monkeypatch.setattr(audit, "_base_snapshot", lambda run_date: state)
    monkeypatch.setattr(audit, "_strategy_snapshot", lambda run_date, strategy_id: _strategy_state())

    result = audit.run("2026-06-24")
    m5 = next(check for check in result["checks"] if check["name"] == "M5_execution_safety")

    assert result["status"] == "warn"
    assert m5["status"] == "fail"
    assert m5["evidence"]["bad"][0]["name"] == "active_demo_blocker"
    assert "TimeoutError" in m5["evidence"]["bad"][0]["message"]


def test_backend_maturity_audit_marks_m6_reviewable_without_claiming_auto_apply(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    _write_runtime(root)
    write_json(root / "strategy_promotion_gate" / "current.json", [{
        "status": "reviewable",
        "promotion_allowed": True,
        "auto_apply": False,
        "paper_only": True,
        "live_config_change_allowed": False,
        "strategy_id": "alpha",
        "closed_trade_count": 24,
        "official_rows": 120,
        "data_truth_level": "official_broker",
        "blockers": [],
    }])
    audit = BackendMaturityAudit(output_root=root, market_db=tmp_path / "missing.db")
    monkeypatch.setattr(audit, "_base_snapshot", lambda run_date: _base_state())
    monkeypatch.setattr(audit, "_strategy_snapshot", lambda run_date, strategy_id: _strategy_state())

    result = audit.run("2026-06-24")
    m6 = next(check for check in result["checks"] if check["name"] == "M6_pm_allocation_gate")

    assert result["status"] == "pass"
    assert result["summary"]["pm_allocation_status"] == "reviewable"
    assert m6["status"] == "reviewable"
    assert m6["evidence"]["promotion_allowed"] is True
    assert m6["evidence"]["auto_apply"] is False


def test_backend_maturity_audit_fails_m6_auto_apply_red_line(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    _write_runtime(root)
    write_json(root / "strategy_promotion_gate" / "current.json", [{
        "status": "reviewable",
        "promotion_allowed": True,
        "auto_apply": True,
        "paper_only": False,
        "live_config_change_allowed": True,
        "strategy_id": "alpha",
        "closed_trade_count": 24,
        "official_rows": 120,
        "data_truth_level": "official_broker",
        "blockers": [],
    }])
    audit = BackendMaturityAudit(output_root=root, market_db=tmp_path / "missing.db")
    monkeypatch.setattr(audit, "_base_snapshot", lambda run_date: _base_state())
    monkeypatch.setattr(audit, "_strategy_snapshot", lambda run_date, strategy_id: _strategy_state())

    result = audit.run("2026-06-24")
    m6 = next(check for check in result["checks"] if check["name"] == "M6_pm_allocation_gate")

    assert result["status"] == "fail"
    assert result["summary"]["failed"] == 1
    assert m6["status"] == "fail"
    assert m6["evidence"]["auto_apply"] is True
    assert m6["evidence"]["live_config_change_allowed"] is True
