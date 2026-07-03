from __future__ import annotations

from pathlib import Path

import pytest

from services.journal_store import load_json, write_json
from services.mainnet_canary import MainnetCanary


LIVE_CONFIG = {
    "execution_mode": "live",
    "live_trading_enabled": True,
    "output_root": "outputs",
    "broker": {
        "provider": "binance_usdm",
        "environment": "live",
        "base_url": "https://fapi.binance.com",
        "dry_run": False,
        "request_dir": "live_order_requests",
        "instrument_map": {"GOLD": "XAUUSDT"},
        "reconcile_account_history": True,
    },
}

PAPER_CONFIG = {
    "execution_mode": "paper",
    "live_trading_enabled": False,
    "output_root": "outputs",
    "broker": {"provider": "binance_usdm", "dry_run": True},
}

CALIBRATED_RULES = {
    "default": {"daily_loss_stop_pct": 1.25},
    "live_money": {
        "daily_loss_limit_pct": 1.0,
        "single_order_max_notional": 110.0,
        "total_account_max_notional": 110.0,
        "max_daily_entry_orders": 1,
        "reference_equity": 100.0,
    },
}

PLACEHOLDER_RULES = {
    "default": {"daily_loss_stop_pct": 1.25},
    "live_money": {
        "daily_loss_limit_pct": 1.25,
        "single_order_max_notional": 25.0,
        "total_account_max_notional": 25.0,
        "max_daily_entry_orders": 2,
        "reference_equity": 5000.0,
    },
}

PLACEHOLDER_RULES_EFFECTIVE_SHAPE = {
    "default": {
        "daily_loss_stop_pct": 1.25,
        "live_money_guardrails": {
            "daily_loss_limit_pct": 1.25,
            "single_order_max_notional": 25.0,
            "total_account_max_notional": 25.0,
            "max_daily_entry_orders": 2,
            "reference_equity": 5000.0,
        },
    },
}

CALIBRATED_RULES_EFFECTIVE_SHAPE = {
    "default": {
        "daily_loss_stop_pct": 1.25,
        "live_money_guardrails": {
            "daily_loss_limit_pct": 1.0,
            "single_order_max_notional": 10.0,
            "total_account_max_notional": 10.0,
            "max_daily_entry_orders": 1,
            "reference_equity": 100.0,
        },
    },
}


def _seed_green_artifacts(root: Path, run_date: str) -> None:
    write_json(root / "live_activation" / f"{run_date}.json", [{"status": "real_money_ready", "real_money_ready": True}])
    write_json(root / "testnet_drill" / "current.json", [{"status": "pass", "base_url": "https://demo-fapi.binance.com"}])
    write_json(root / "live_approvals" / f"{run_date}.approved.json", [{"approved": True, "approved_by": "owner"}])


class _StubAdapter:
    name = "live"

    def __init__(self) -> None:
        self.requests: list = []

    def submit_order(self, request):
        from services.broker_adapter import PaperOrder

        self.requests.append(request)
        return PaperOrder(
            order_id="live_canary_1",
            ticket_id=request.ticket["ticket_id"],
            status="filled",
            requested_price=4000.0,
            fill_price=4000.5,
            quantity=0.001,
            filled_at="2026-07-03T00:00:00+00:00",
            rejection_reason="",
        )


def test_canary_reports_no_go_in_paper_mode_and_writes_nothing(tmp_path: Path):
    root = tmp_path / "outputs"
    canary = MainnetCanary(root, config=PAPER_CONFIG, risk_rules=CALIBRATED_RULES)

    report = canary.check("2026-07-03")

    assert report["go"] is False
    assert any("execution_mode" in reason for reason in report["blockers"])
    assert not (root / "journal_decisions").exists()


def test_canary_flags_placeholder_limits_as_no_go(tmp_path: Path):
    root = tmp_path / "outputs"
    _seed_green_artifacts(root, "2026-07-03")
    canary = MainnetCanary(root, config=LIVE_CONFIG, risk_rules=PLACEHOLDER_RULES)

    report = canary.check("2026-07-03")

    assert report["go"] is False
    assert any("placeholder" in reason for reason in report["blockers"])


def test_canary_flags_placeholder_limits_from_effective_guardrail_shape(tmp_path: Path):
    root = tmp_path / "outputs"
    _seed_green_artifacts(root, "2026-07-03")
    canary = MainnetCanary(root, config=LIVE_CONFIG, risk_rules=PLACEHOLDER_RULES_EFFECTIVE_SHAPE)

    report = canary.check("2026-07-03")

    assert report["go"] is False
    assert any("placeholder" in reason for reason in report["blockers"])


def test_canary_accepts_calibrated_effective_guardrail_shape(tmp_path: Path):
    root = tmp_path / "outputs"
    _seed_green_artifacts(root, "2026-07-03")
    canary = MainnetCanary(root, config=LIVE_CONFIG, risk_rules=CALIBRATED_RULES_EFFECTIVE_SHAPE)

    report = canary.check("2026-07-03")

    assert "risk_rules live_money still holds placeholder TESTNET limits" not in " ".join(report["blockers"])


def test_canary_requires_missing_artifacts(tmp_path: Path):
    root = tmp_path / "outputs"
    canary = MainnetCanary(root, config=LIVE_CONFIG, risk_rules=CALIBRATED_RULES)

    report = canary.check("2026-07-03")

    assert report["go"] is False
    joined = " ".join(report["blockers"])
    assert "live_activation" in joined
    assert "testnet_drill" in joined
    assert "approval" in joined


def test_canary_submit_refuses_without_confirm_flag(tmp_path: Path):
    root = tmp_path / "outputs"
    _seed_green_artifacts(root, "2026-07-03")
    canary = MainnetCanary(root, config=LIVE_CONFIG, risk_rules=CALIBRATED_RULES)

    with pytest.raises(RuntimeError, match="confirm"):
        canary.submit("2026-07-03", side="buy", quantity=0.001, stop_loss=3950.0, take_profit=4050.0, entry_price=4000.0, confirm=False)


def test_canary_submit_refuses_in_paper_mode_never_records_silent_executed(tmp_path: Path):
    """decision=executed with execution_mode=paper would record a no-op 'executed'
    journal row with no order — the canary must refuse before recording."""
    root = tmp_path / "outputs"
    _seed_green_artifacts(root, "2026-07-03")
    canary = MainnetCanary(root, config=PAPER_CONFIG, risk_rules=CALIBRATED_RULES)

    with pytest.raises(RuntimeError, match="NO-GO"):
        canary.submit("2026-07-03", side="buy", quantity=0.001, stop_loss=3950.0, take_profit=4050.0, entry_price=4000.0, confirm=True)

    assert not (root / "journal_decisions").exists()


def test_canary_submits_exactly_one_minimum_order_when_green(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    run_date = "2026-07-03"
    _seed_green_artifacts(root, run_date)
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_live": True, "ready_for_paper": True, "message": "ok"}])
    stub = _StubAdapter()
    monkeypatch.setattr("services.broker_adapter.build_broker_adapter", lambda output_root=None: stub)
    monkeypatch.setattr("services.journal_store.load_pipeline_config", lambda: LIVE_CONFIG)
    canary = MainnetCanary(root, config=LIVE_CONFIG, risk_rules=CALIBRATED_RULES)

    result = canary.submit(run_date, side="buy", quantity=0.001, stop_loss=3950.0, take_profit=4050.0, entry_price=4000.0, confirm=True)

    assert result["status"] == "submitted"
    assert result["broker_order"]["status"] == "filled"
    assert len(stub.requests) == 1
    decisions = load_json(root / "journal_decisions" / f"{run_date}.json")
    assert len(decisions) == 1 and decisions[0]["decision_status"] == "executed"
    canary_report = load_json(root / "mainnet_canary" / f"{run_date}.json")
    assert canary_report[-1]["result"]["status"] == "submitted"


def test_canary_refuses_second_order_same_day(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    run_date = "2026-07-03"
    _seed_green_artifacts(root, run_date)
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_live": True, "ready_for_paper": True, "message": "ok"}])
    stub = _StubAdapter()
    monkeypatch.setattr("services.broker_adapter.build_broker_adapter", lambda output_root=None: stub)
    monkeypatch.setattr("services.journal_store.load_pipeline_config", lambda: LIVE_CONFIG)
    canary = MainnetCanary(root, config=LIVE_CONFIG, risk_rules=CALIBRATED_RULES)
    canary.submit(run_date, side="buy", quantity=0.001, stop_loss=3950.0, take_profit=4050.0, entry_price=4000.0, confirm=True)

    with pytest.raises(RuntimeError, match="one canary"):
        canary.submit(run_date, side="buy", quantity=0.001, stop_loss=3950.0, take_profit=4050.0, entry_price=4000.0, confirm=True)

    assert len(stub.requests) == 1
