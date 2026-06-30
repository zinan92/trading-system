import json
from pathlib import Path

from services.alert_notifier import AlertNotifier, FeishuSender, TelegramSender, resolve_alert_sender


class _FakeSender:
    def __init__(self, configured: bool = True) -> None:
        self._configured = configured
        self.sent: list[str] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def send(self, text: str) -> dict:
        self.sent.append(text)
        return {"ok": True, "channel": "telegram"}


def _health(status: str, checks: list[tuple[str, str, str]]) -> dict:
    return {
        "status": status,
        "checks": [{"name": n, "status": s, "message": m, "details": {}} for n, s, m in checks],
    }


def test_fires_on_transition_to_fail(tmp_path: Path):
    root = tmp_path / "outputs"
    sender = _FakeSender()
    health = _health("error", [
        ("paper_reconciliation", "fail", "trades reference missing paper orders"),
        ("data_source", "pass", "ok"),
    ])

    res = AlertNotifier(root, sender=sender).run("2026-05-29", health)

    assert res["alert_count"] == 1
    assert res["delivered"] == 1
    assert len(sender.sent) == 1
    assert "paper_reconciliation" in sender.sent[0]


def test_dedups_on_repeat(tmp_path: Path):
    root = tmp_path / "outputs"
    sender = _FakeSender()
    notifier = AlertNotifier(root, sender=sender)
    health = _health("error", [("paper_reconciliation", "fail", "x")])

    notifier.run("2026-05-29", health)   # fires once
    sender.sent.clear()
    res2 = notifier.run("2026-05-29", health)  # same state -> no transition

    assert res2["alert_count"] == 0
    assert sender.sent == []


def test_recovery_alert_when_back_to_ok(tmp_path: Path):
    root = tmp_path / "outputs"
    sender = _FakeSender()
    notifier = AlertNotifier(root, sender=sender)
    notifier.run("2026-05-29", _health("error", [("data_gap", "fail", "45h gap")]))
    sender.sent.clear()

    res = notifier.run("2026-05-29", _health("ok", [("data_gap", "pass", "clean")]))

    assert res["alert_count"] == 1
    assert "recover" in sender.sent[0].lower()


def test_naked_position_upgrade_fires_after_generic_reconciliation_error(tmp_path: Path):
    root = tmp_path / "outputs"
    sender = _FakeSender()
    notifier = AlertNotifier(root, sender=sender)
    generic = {
        "status": "error",
        "checks": [
            {
                "name": "active_demo_reconciliation",
                "status": "error",
                "message": "active demo reconciliation cannot confirm venue state for gold_1m_chan: TimeoutError",
                "details": {
                    "reconciliation": {
                        "confirmation_status": "cannot_confirm",
                        "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
                        "reason_code": "venue_state_unknown",
                        "suspected_naked_position": False,
                    }
                },
            }
        ],
    }
    naked = {
        "status": "error",
        "checks": [
            {
                "name": "active_demo_reconciliation",
                "status": "error",
                "message": "active demo reconciliation cannot confirm venue state for gold_1m_chan: suspected naked position; TimeoutError",
                "details": {
                    "reconciliation": {
                        "confirmation_status": "cannot_confirm",
                        "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
                        "reason_code": "naked_position_suspected",
                        "suspected_naked_position": True,
                    }
                },
            }
        ],
    }

    first = notifier.run("2026-06-30", generic)
    sender.sent.clear()
    second = notifier.run("2026-06-30", naked)

    assert first["alert_count"] == 1
    assert second["alert_count"] == 1
    assert second["events"][0]["name"] == "naked_position_suspected"
    assert "naked position" in second["events"][0]["message"]
    assert "naked_position_suspected" in sender.sent[0]
    assert "naked position" in sender.sent[0]


def test_no_alert_when_all_healthy(tmp_path: Path):
    root = tmp_path / "outputs"
    sender = _FakeSender()
    res = AlertNotifier(root, sender=sender).run("2026-05-29", _health("ok", [("data_source", "pass", "ok")]))
    assert res["alert_count"] == 0
    assert sender.sent == []


def test_warn_does_not_page(tmp_path: Path):
    # warn is informational, not actionable — recorded but never paged.
    root = tmp_path / "outputs"
    sender = _FakeSender()
    res = AlertNotifier(root, sender=sender).run("2026-05-29", _health("warn", [("oanda_feed", "warn", "skipped")]))
    assert res["alert_count"] == 0
    assert sender.sent == []


def test_falls_back_to_log_when_unconfigured(tmp_path: Path):
    # No delivery channel configured -> still detects + records the alert, but
    # marks it undelivered rather than silently dropping it.
    root = tmp_path / "outputs"
    sender = _FakeSender(configured=False)
    res = AlertNotifier(root, sender=sender).run("2026-05-29", _health("error", [("data_gap", "fail", "gap")]))

    assert res["alert_count"] == 1
    assert res["delivered"] == 0
    assert res["channel"] == "log"
    artifact = json.loads((root / "alerts" / "2026-05-29.json").read_text(encoding="utf-8"))
    assert artifact[0]["events"][0]["name"] == "data_gap"


def test_telegram_sender_loads_project_live_env(tmp_path: Path, monkeypatch):
    env = tmp_path / "live.env"
    env.write_text(
        "TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN=token-from-file\n"
        "TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID=chat-from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID", raising=False)

    sender = TelegramSender()

    assert sender.configured is True
    assert sender.token == "token-from-file"
    assert sender.chat_id == "chat-from-file"


def test_feishu_sender_loads_project_live_env(tmp_path: Path, monkeypatch):
    env = tmp_path / "live.env"
    env.write_text(
        "TRADING_ORCHESTRATOR_ALERT_CHANNEL=feishu\n"
        "TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/test\n"
        "TRADING_ORCHESTRATOR_FEISHU_SECRET=secret-from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_FEISHU_SECRET", raising=False)

    sender = resolve_alert_sender()

    assert isinstance(sender, FeishuSender)
    assert sender.configured is True
    assert sender.webhook_url.endswith("/test")
    assert sender.secret == "secret-from-file"


def test_feishu_sender_posts_signed_text_payload(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"code":0,"msg":"success"}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr("services.alert_notifier.time.time", lambda: 1700000000)
    monkeypatch.setattr("services.alert_notifier.urllib.request.urlopen", fake_urlopen)
    sender = FeishuSender(webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test", secret="secret")

    result = sender.send("trading alert")

    assert result["ok"] is True
    assert captured["url"].endswith("/test")
    assert captured["payload"]["msg_type"] == "text"
    assert captured["payload"]["content"]["text"] == "trading alert"
    assert captured["payload"]["timestamp"] == "1700000000"
    assert captured["payload"]["sign"] == sender._sign("1700000000")
