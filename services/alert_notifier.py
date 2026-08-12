"""Alert notifier: turns the per-cycle HealthCheck rollup into proactive
notifications so the system surfaces problems itself instead of relying on a
human noticing them.

Design:
  * Pages only on hard states (fail / error / block). `warn` is recorded but
    not paged, to avoid alert fatigue.
  * Transition-based dedup: an alert fires when a check enters a bad state and a
    recovery fires when it leaves one. A bad state that persists across cycles
    is NOT re-paged every 5 minutes.
  * Delivery is pluggable. Feishu/Lark and Telegram are supported. If no
    credentials are configured the alert is still detected and recorded
    (channel="log", delivered=0) rather than silently dropped.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import write_json
from services.live_env import apply_live_env

# A check status in this set is actionable and pages. Everything else
# (pass / ok / warn / "") is treated as non-paging.
ALERT_STATES = {"fail", "error", "block"}


def _apply_runtime_env() -> None:
    """Cloud systemd injects the root-owned env before dropping privileges."""

    if os.getenv("GRIDMIND_RUNTIME_MODE") != "cloud":
        apply_live_env()


class TelegramSender:
    channel = "telegram"
    required_env = [
        "TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN",
        "TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID",
    ]

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        _apply_runtime_env()
        self.token = token or os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> dict:
        if not self.configured:
            return {"ok": False, "channel": "telegram", "reason": "not_configured"}
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as response:
                return {"ok": getattr(response, "status", 200) == 200, "channel": "telegram"}
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            return {"ok": False, "channel": "telegram", "reason": str(exc)}


class FeishuSender:
    channel = "feishu"
    required_env = [
        "TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL",
    ]

    def __init__(self, webhook_url: str | None = None, secret: str | None = None) -> None:
        _apply_runtime_env()
        self.webhook_url = (
            webhook_url
            or os.getenv("TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL")
            or os.getenv("TRADING_ORCHESTRATOR_LARK_WEBHOOK_URL")
            or os.getenv("FEISHU_WEBHOOK_URL")
            or os.getenv("LARK_WEBHOOK_URL")
        )
        self.secret = (
            secret
            or os.getenv("TRADING_ORCHESTRATOR_FEISHU_SECRET")
            or os.getenv("TRADING_ORCHESTRATOR_LARK_SECRET")
            or os.getenv("FEISHU_SECRET")
            or os.getenv("LARK_SECRET")
        )

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def send(self, text: str, card: dict | None = None) -> dict:
        if not self.configured:
            return {"ok": False, "channel": "feishu", "reason": "not_configured"}
        if card is not None:
            payload: dict = {"msg_type": "interactive", "card": card}
        else:
            payload = {
                "msg_type": "text",
                "content": {"text": text},
            }
        if self.secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = self._sign(timestamp)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            str(self.webhook_url),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                parsed = json.loads(body) if body else {}
                ok = getattr(response, "status", 200) == 200 and int(parsed.get("code", parsed.get("StatusCode", 0)) or 0) == 0
                return {
                    "ok": ok,
                    "channel": "feishu",
                    "status": getattr(response, "status", 200),
                    "code": parsed.get("code", parsed.get("StatusCode", 0)),
                    "message": parsed.get("msg") or parsed.get("StatusMessage") or "",
                }
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "channel": "feishu", "reason": str(exc)}

    def _sign(self, timestamp: str) -> str:
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")


def resolve_alert_sender():
    _apply_runtime_env()
    channel = str(os.getenv("TRADING_ORCHESTRATOR_ALERT_CHANNEL", "auto")).strip().lower()
    if channel in {"feishu", "lark"}:
        return FeishuSender()
    if channel == "telegram":
        return TelegramSender()
    feishu = FeishuSender()
    if feishu.configured:
        return feishu
    return TelegramSender()


class AlertNotifier:
    def __init__(self, output_root: Path, sender=None) -> None:
        self.output_root = Path(output_root)
        self.sender = sender if sender is not None else resolve_alert_sender()
        self.state_path = self.output_root / "alerts" / "state.json"

    def run(self, run_date: str, health_result: dict) -> dict:
        previous = self._load_state()
        checks = self._alert_checks((health_result or {}).get("checks", []) or [])
        current = {c.get("name", ""): c.get("status", "") for c in checks if c.get("name")}
        messages = {c.get("name", ""): c.get("message", "") for c in checks if c.get("name")}
        overall = (health_result or {}).get("status", "")

        events = []
        for name, status in current.items():
            prior = previous.get(name, "pass")
            now_bad = status in ALERT_STATES
            was_bad = prior in ALERT_STATES
            if now_bad and status != prior:
                events.append({"name": name, "kind": "firing", "from": prior, "to": status, "message": messages.get(name, "")})
            elif (not now_bad) and was_bad:
                events.append({"name": name, "kind": "recovery", "from": prior, "to": status, "message": messages.get(name, "")})

        delivered = 0
        deliveries = []
        configured = self.sender.configured
        for event in events:
            text = self._format(run_date, event, overall)
            if configured:
                result = self.sender.send(text)
                ok = bool(result.get("ok"))
                delivered += 1 if ok else 0
                deliveries.append({"channel": result.get("channel", getattr(self.sender, "channel", "unknown")), "ok": ok, "text": text})
            else:
                print(f"[ALERT] {text}")
                deliveries.append({"channel": "log", "ok": False, "text": text})

        channel = getattr(self.sender, "channel", "unknown") if configured else "log"
        self._save_state(current)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "overall_status": overall,
            "alert_count": len(events),
            "delivered": delivered,
            "channel": channel,
            "events": events,
            "deliveries": deliveries,
        }
        write_json(self.output_root / "alerts" / "current.json", [payload])
        write_json(self.output_root / "alerts" / f"{run_date}.json", [payload])
        return payload

    def _alert_checks(self, checks: list[dict]) -> list[dict]:
        expanded: list[dict] = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            expanded.append(check)
            if self._is_naked_position_suspected(check):
                expanded.append(
                    {
                        **check,
                        "name": "naked_position_suspected",
                        "message": check.get("message") or "suspected naked position",
                    }
                )
        return expanded

    def _is_naked_position_suspected(self, check: dict) -> bool:
        if check.get("name") != "active_demo_reconciliation":
            return False
        details = check.get("details") if isinstance(check.get("details"), dict) else {}
        reconciliation = details.get("reconciliation") if isinstance(details.get("reconciliation"), dict) else {}
        if reconciliation.get("reason_code") == "naked_position_suspected" or reconciliation.get("suspected_naked_position") is True:
            return True
        return "naked position" in str(check.get("message") or "").lower()

    def _format(self, run_date: str, event: dict, overall: str) -> str:
        if event["kind"] == "firing":
            return f"🔴 [{run_date}] {event['name']} FAILING ({event['from']}→{event['to']}): {event['message']} | health={overall}"
        return f"✅ [{run_date}] {event['name']} recovered ({event['from']}→{event['to']}): {event['message']} | health={overall}"

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def _save_state(self, current: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(current, indent=2), encoding="utf-8")


def run_alert_notifier(run_date: str, output_root: Path, health_result: dict, sender=None) -> dict:
    return AlertNotifier(output_root, sender=sender).run(run_date, health_result)
