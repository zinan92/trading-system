from pathlib import Path

from services.alert_delivery_drill import AlertDeliveryDrill
from services.health_check import HealthCheck
from services.journal_store import load_json


class _Sender:
    def __init__(self, configured=True, ok=True, channel="telegram") -> None:
        self.configured = configured
        self.ok = ok
        self.channel = channel
        self.required_env = [f"{channel.upper()}_WEBHOOK_URL"]
        self.sent = []

    def send(self, text: str) -> dict:
        self.sent.append(text)
        return {"ok": self.ok, "channel": self.channel, "message_id": "m1" if self.ok else ""}


def test_alert_delivery_drill_blocks_when_telegram_unconfigured(tmp_path: Path):
    sender = _Sender(configured=False)

    result = AlertDeliveryDrill(tmp_path / "outputs", sender=sender).run("2026-06-23")

    assert result["status"] == "blocked"
    assert result["delivered"] is False
    assert sender.sent == []
    saved = load_json(tmp_path / "outputs" / "alert_delivery_drill" / "current.json")[0]
    assert saved["status"] == "blocked"


def test_alert_delivery_drill_passes_when_sender_delivers(tmp_path: Path):
    sender = _Sender(configured=True, ok=True)

    result = AlertDeliveryDrill(tmp_path / "outputs", sender=sender).run("2026-06-23", message="test alert")

    assert result["status"] == "pass"
    assert result["delivered"] is True
    assert sender.sent == ["test alert"]
    saved = load_json(tmp_path / "outputs" / "alert_delivery_drill" / "2026-06-23.json")[0]
    assert saved["delivery"]["message_id"] == "m1"


def test_health_alert_delivery_accepts_successful_feishu_drill(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing.env"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_ALERT_CHANNEL", "feishu")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/test")

    missing = HealthCheck(tmp_path / "outputs", tmp_path / "missing.db")._alert_delivery_check()
    assert missing["status"] == "warn"
    assert missing["details"]["channel"] == "feishu"
    assert "no successful delivery drill" in missing["message"]

    AlertDeliveryDrill(tmp_path / "outputs", sender=_Sender(configured=True, ok=True, channel="feishu")).run("2026-06-23")
    passed = HealthCheck(tmp_path / "outputs", tmp_path / "missing.db")._alert_delivery_check()

    assert passed["status"] == "ok"
    assert passed["details"]["channel"] == "feishu"


def test_health_alert_delivery_requires_successful_drill(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing.env"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_ALERT_CHANNEL", "telegram")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_FEISHU_SECRET", raising=False)

    missing = HealthCheck(tmp_path / "outputs", tmp_path / "missing.db")._alert_delivery_check()
    assert missing["status"] == "warn"
    assert "no successful delivery drill" in missing["message"]

    AlertDeliveryDrill(tmp_path / "outputs", sender=_Sender(configured=True, ok=True)).run("2026-06-23")
    passed = HealthCheck(tmp_path / "outputs", tmp_path / "missing.db")._alert_delivery_check()

    assert passed["status"] == "ok"
    assert passed["details"]["drill"]["delivered"] is True
