from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.alert_notifier import resolve_alert_sender
from services.config_loader import ROOT, load_pipeline_config
from services.health_check import HealthCheck
from services.journal_store import load_json, write_json


HARD_STATES = {"error", "fail", "block"}
OK_CORE_CHECKS = {
    "market_db": "行情库",
    "data_source": "行情源",
    "runner": "runner",
    "active_demo_reconciliation": "demo 对账",
    "alert_delivery": "告警通道",
    "secrets_posture": "密钥",
}


class SystemAlertReport:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None, sender=None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.market_db = Path(market_db or ROOT / str(config.get("local_market_db", "data/market_data.db")))
        self.sender = sender if sender is not None else resolve_alert_sender()

    def run(self, run_date: str, send: bool = False) -> dict[str, Any]:
        health = HealthCheck(self.output_root, self.market_db).run(run_date)
        hard = self._checks_with_status(health, HARD_STATES)
        warnings = self._checks_with_status(health, {"warn"})
        ok = self._ok_core_checks(health)
        status = "fail" if hard else ("warn" if warnings else "ok")
        message = self._format_message(run_date, status, hard, warnings, ok)
        delivery = {"ok": False, "channel": "log", "reason": "not_sent"}
        delivered = False
        channel = "log"
        if send:
            if getattr(self.sender, "configured", False):
                delivery = self.sender.send(message)
                delivered = bool(delivery.get("ok"))
                channel = str(delivery.get("channel", getattr(self.sender, "channel", "unknown")))
            else:
                print(f"[SYSTEM_ALERT] {message}")
                delivery = {"ok": False, "channel": "log", "reason": "not_configured"}
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "health_status": health.get("status", ""),
            "delivered": delivered,
            "channel": channel,
            "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            "message": message,
            "hard_issue_count": len(hard),
            "warning_count": len(warnings),
            "hard_issues": self._issue_payloads(hard),
            "warnings": self._issue_payloads(warnings[:5]),
            "ok_core": ok,
            "delivery": delivery,
        }
        write_json(self.output_root / "system_alert_reports" / "current.json", [payload])
        write_json(self.output_root / "system_alert_reports" / f"{run_date}.json", [payload])
        return payload

    def _format_message(
        self,
        run_date: str,
        status: str,
        hard: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        ok: list[str],
    ) -> str:
        if status == "ok":
            return f"黄金系统告警 - {run_date}\nOK。系统健康，无需处理。\n正常：{self._ok_text(ok)}"
        if status == "warn":
            lines = [
                f"黄金系统告警 - {run_date}",
                "注意：没有硬故障，但有非阻断 warning。",
                *[self._issue_line(item) for item in warnings[:3]],
                f"正常：{self._ok_text(ok)}",
                "检查：先看 outputs/health/current.json；只有出现 error/fail/block 才按故障处理。",
            ]
            return "\n".join(lines)
        lines = [
            f"黄金系统告警 - {run_date}",
            f"故障：{len(hard)} 项需要处理。",
            *[self._issue_line(item) for item in hard[:5]],
            f"正常：{self._ok_text(ok)}",
            "检查：先看 outputs/health/current.json，再按上面每项的 artifact/command 处理。",
        ]
        return "\n".join(lines)

    def _issue_line(self, item: dict[str, Any]) -> str:
        target = self._inspection_hint(item)
        suffix = f"；检查：{target}" if target else ""
        return f"- {item.get('name')}: {item.get('message')}{suffix}"

    def _inspection_hint(self, item: dict[str, Any]) -> str:
        details = item.get("details", {}) if isinstance(item.get("details"), dict) else {}
        if details.get("command"):
            return str(details["command"])
        if details.get("artifact"):
            return str(details["artifact"])
        missing = details.get("missing")
        if isinstance(missing, list) and missing:
            return str(missing[0])
        return "outputs/health/current.json"

    def _checks_with_status(self, health: dict[str, Any], statuses: set[str]) -> list[dict[str, Any]]:
        return [
            item
            for item in health.get("checks", [])
            if str(item.get("status", "")).lower() in statuses
        ]

    def _ok_core_checks(self, health: dict[str, Any]) -> list[str]:
        ok = []
        for item in health.get("checks", []):
            name = str(item.get("name", ""))
            status = str(item.get("status", "")).lower()
            if name in OK_CORE_CHECKS and status in {"ok", "pass"}:
                ok.append(OK_CORE_CHECKS[name])
        return ok

    def _ok_text(self, ok: list[str]) -> str:
        return "、".join(ok) if ok else "核心项未全绿，见上面问题"

    def _issue_payloads(self, checks: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "name": str(item.get("name", "")),
                "status": str(item.get("status", "")),
                "message": str(item.get("message", "")),
                "inspect": self._inspection_hint(item),
            }
            for item in checks
        ]


def verify_system_alert_receipt(output_root: Path, run_date: str) -> dict[str, Any]:
    path = Path(output_root) / "system_alert_reports" / f"{run_date}.json"
    rows = load_json(path)
    for row in reversed(rows):
        if row.get("delivered") is True and row.get("message_sha256"):
            return row
    raise RuntimeError(f"Delivered system alert receipt not found: {path}")
