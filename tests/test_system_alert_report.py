from pathlib import Path

import pytest

from services.system_alert_report import SystemAlertReport, verify_system_alert_receipt


class _FakeSender:
    channel = "feishu"
    configured = True

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    def send(self, text: str) -> dict:
        self.sent.append(text)
        return {"ok": self.ok, "channel": self.channel, "code": 0 if self.ok else 999}


class _FakeHealthCheck:
    payload: dict = {}

    def __init__(self, output_root: Path, market_db: Path) -> None:
        self.output_root = output_root
        self.market_db = market_db

    def run(self, run_date: str) -> dict:
        return self.payload


def test_system_alert_sends_short_ok(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("services.system_alert_report.HealthCheck", _FakeHealthCheck)
    _FakeHealthCheck.payload = {
        "run_date": "2026-07-03",
        "status": "ok",
        "checks": [
            {"name": "market_db", "status": "ok", "message": "db ok", "details": {}},
            {"name": "data_source", "status": "ok", "message": "data ok", "details": {}},
            {"name": "runner", "status": "ok", "message": "runner ok", "details": {}},
            {"name": "active_demo_reconciliation", "status": "ok", "message": "flat", "details": {}},
            {"name": "alert_delivery", "status": "ok", "message": "delivered", "details": {}},
        ],
    }
    sender = _FakeSender()

    result = SystemAlertReport(tmp_path / "outputs", tmp_path / "market.db", sender=sender).run("2026-07-03", send=True)

    assert result["status"] == "ok"
    assert result["delivered"] is True
    assert "OK。系统健康，无需处理。" in sender.sent[0]
    assert "故障" not in sender.sent[0]
    assert verify_system_alert_receipt(tmp_path / "outputs", "2026-07-03")["status"] == "ok"


def test_system_alert_explains_failure_and_check_path(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("services.system_alert_report.HealthCheck", _FakeHealthCheck)
    _FakeHealthCheck.payload = {
        "run_date": "2026-07-03",
        "status": "error",
        "checks": [
            {"name": "market_db", "status": "ok", "message": "db ok", "details": {}},
            {
                "name": "active_demo_reconciliation",
                "status": "error",
                "message": "venue state unknown",
                "details": {"artifact": "outputs/strategies/gold_1m_macd/live_reconciliation/current.json"},
            },
        ],
    }
    sender = _FakeSender()

    result = SystemAlertReport(tmp_path / "outputs", tmp_path / "market.db", sender=sender).run("2026-07-03", send=True)

    assert result["status"] == "fail"
    assert result["hard_issue_count"] == 1
    assert "故障：1 项需要处理。" in sender.sent[0]
    assert "active_demo_reconciliation" in sender.sent[0]
    assert "outputs/strategies/gold_1m_macd/live_reconciliation/current.json" in sender.sent[0]
    assert "正常：行情库" in sender.sent[0]


def test_verify_system_alert_receipt_requires_delivery(tmp_path: Path):
    with pytest.raises(RuntimeError, match="Delivered system alert receipt not found"):
        verify_system_alert_receipt(tmp_path / "outputs", "2026-07-03")
