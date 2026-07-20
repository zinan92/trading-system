from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.journal_store import load_json, write_json
from services.schedule_status import ScheduleStatus
from services.system_vitals import SystemVitals


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _latest(rows: list[dict]) -> dict:
    return rows[-1] if rows and isinstance(rows[-1], dict) else {}


def _safe_load_json(path: Path) -> list[dict]:
    try:
        return load_json(path)
    except (OSError, ValueError):
        return []


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


_MAX_RECONCILIATION_AGE_SECONDS = 600.0
_FUTURE_SKEW_SECONDS = 120.0


class ExternalDeadmanPing:
    """Send a lightweight heartbeat to an external dead-man service.

    The external service owns the "missed ping" alert. This process only emits
    pings while the laptop is alive; if the box sleeps or dies, pings stop.
    """

    def __init__(
        self,
        output_root: Path,
        market_db: Path,
        *,
        url: str | None = None,
        position_url: str | None = None,
        opener: Any = None,
        timeout_seconds: float = 10.0,
        schedule_status_provider: Any = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.market_db = Path(market_db)
        self.url = url if url is not None else os.getenv("TRADING_ORCHESTRATOR_DEADMAN_URL", "")
        self.position_url = position_url if position_url is not None else os.getenv("TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL", "")
        self.opener = opener or urllib.request.urlopen
        self.timeout_seconds = timeout_seconds
        self.schedule_status_provider = schedule_status_provider

    def run(self, run_date: str, *, dry_run: bool = False) -> dict:
        now = _utcnow()
        checked_at = now.isoformat()
        vitals = SystemVitals(self.output_root, self.market_db).run(run_date, persist=False)
        schedule_runtime = self._schedule_runtime(run_date)
        exposure = self._exposure_snapshot(now=now)
        severity = "critical" if exposure["has_open_position"] else "normal"
        selected_url = self._selected_url(severity)
        ping = self._ping(selected_url, severity, exposure, vitals, schedule_runtime, dry_run=dry_run)
        payload = {
            "schema_version": "external-deadman-ping-v1",
            "run_date": run_date,
            "checked_at": checked_at,
            "status": ping["status"],
            "severity": severity,
            "configured": bool(selected_url),
            "url_kind": "position" if severity == "critical" and self.position_url else "default",
            "position_aware": True,
            "exposure": exposure,
            "always_on": vitals.get("always_on", {}),
            "schedule_runtime": schedule_runtime,
            "ping": ping,
            "note": "External dead-man alert is triggered by missed pings outside this laptop.",
        }
        write_json(self.output_root / "deadman_ping" / "current.json", [payload])
        write_json(self.output_root / "deadman_ping" / f"{run_date}.json", [payload])
        return payload

    def _selected_url(self, severity: str) -> str:
        if severity == "critical" and self.position_url:
            return self.position_url
        return self.url or self.position_url

    def _ping(self, url: str, severity: str, exposure: dict, vitals: dict, schedule_runtime: dict, *, dry_run: bool) -> dict:
        if not url:
            return {
                "status": "not_configured",
                "delivered": False,
                "message": "TRADING_ORCHESTRATOR_DEADMAN_URL is not configured",
            }
        failure_signal = self._always_on_blocked(vitals) or self._schedule_runtime_failed(schedule_runtime)
        target_url = self._healthchecks_fail_url(url) if failure_signal else url
        full_url = self._url_with_query(
            target_url,
            {
                "severity": severity,
                "position_open": "1" if exposure["has_open_position"] else "0",
                "always_on_status": str((vitals.get("always_on") or {}).get("status") or ""),
                "schedule_status": str(schedule_runtime.get("status") or ""),
            },
        )
        if dry_run:
            return {
                "status": "dry_run_fail" if failure_signal else "dry_run",
                "delivered": False,
                "url": full_url,
                "failure_signal": failure_signal,
                "success_ping": not failure_signal,
                "message": "dry run; ping not sent",
            }
        try:
            request = urllib.request.Request(full_url, method="GET")
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                ok = 200 <= status_code < 300
                return {
                    "status": ("fail_sent" if ok else "fail_signal_failed") if failure_signal else ("sent" if ok else "failed"),
                    "delivered": ok,
                    "status_code": status_code,
                    "url": full_url,
                    "failure_signal": failure_signal,
                    "success_ping": not failure_signal,
                    "message": self._ping_message(ok=ok, failure_signal=failure_signal),
                }
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            return {
                "status": "fail_signal_failed" if failure_signal else "failed",
                "delivered": False,
                "url": full_url,
                "failure_signal": failure_signal,
                "success_ping": not failure_signal,
                "message": f"{type(exc).__name__}: {exc}",
            }

    def _always_on_blocked(self, vitals: dict) -> bool:
        always_on = vitals.get("always_on") if isinstance(vitals.get("always_on"), dict) else {}
        return bool(always_on.get("blocks_new_orders")) or str(always_on.get("status") or "").startswith("BLOCKED_")

    def _schedule_runtime(self, run_date: str) -> dict:
        schedule_path = self.output_root / "schedules" / "current.json"
        if self.schedule_status_provider is None and not schedule_path.exists():
            return {"status": "not_configured", "runtime_failed_jobs": []}
        try:
            result = (
                self.schedule_status_provider(run_date)
                if self.schedule_status_provider is not None
                else ScheduleStatus(self.output_root).run(run_date)
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return {"status": "check_failed", "runtime_failed_jobs": [], "error": f"{type(exc).__name__}: {exc}"}
        return {
            "status": str(result.get("status") or "unknown"),
            "runtime_failed_jobs": list(result.get("runtime_failed_jobs") or []),
            "healthy_current_count": int(result.get("healthy_current_count") or 0),
            "required_count": int(result.get("required_count") or 0),
        }

    def _schedule_runtime_failed(self, schedule_runtime: dict) -> bool:
        return str(schedule_runtime.get("status") or "") not in {"active", "not_configured"}

    def _healthchecks_fail_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.rstrip("/")
        if not path.endswith("/fail"):
            path = f"{path}/fail"
        return urllib.parse.urlunparse(parsed._replace(path=path))

    def _ping_message(self, *, ok: bool, failure_signal: bool) -> str:
        if failure_signal and ok:
            return "dead-man failure signal accepted"
        if failure_signal:
            return "dead-man failure signal returned non-2xx"
        return "dead-man ping accepted" if ok else "dead-man ping returned non-2xx"

    def _url_with_query(self, url: str, params: dict[str, str]) -> str:
        parsed = urllib.parse.urlparse(url)
        existing = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        existing.update(params)
        query = urllib.parse.urlencode(existing)
        return urllib.parse.urlunparse(parsed._replace(query=query))

    def _exposure_snapshot(self, *, now: datetime) -> dict:
        live = _latest(_safe_load_json(self.output_root / "live_reconciliation" / "current.json"))
        freshness = self._reconciliation_freshness(live, now=now)
        exchange_positions = [
            row
            for row in (live.get("exchange_positions") or [])
            if isinstance(row, dict) and abs(self._float(row.get("position_amt"))) > 0
        ]
        strategy_open = []
        for path in sorted((self.output_root / "strategies").glob("*/paper_trades/current.json")):
            strategy_id = path.parent.parent.name
            for row in _safe_load_json(path):
                if isinstance(row, dict) and str(row.get("status", "")).lower() == "open":
                    strategy_open.append({"strategy_id": strategy_id, "trade_id": row.get("trade_id", "")})
        position_unknown = not freshness["fresh"]
        has_open_position = position_unknown or bool(exchange_positions) or bool(live.get("suspected_naked_position"))
        return {
            "has_open_position": has_open_position,
            "position_unknown": position_unknown,
            "exchange_position_count": len(exchange_positions),
            "exchange_positions": exchange_positions,
            "suspected_naked_position": bool(live.get("suspected_naked_position")),
            "confirmation_status": live.get("confirmation_status", ""),
            "system_state": live.get("system_state", ""),
            "reconciliation_fresh": freshness["fresh"],
            "reconciliation_freshness": freshness,
            "strategy_open_trade_count": len(strategy_open),
            "strategy_open_trades": strategy_open[:20],
            "source": "live_reconciliation.current",
        }

    def _reconciliation_freshness(self, live: dict, *, now: datetime) -> dict:
        if not live:
            return {
                "fresh": False,
                "reason_code": "live_reconciliation_missing",
                "max_age_seconds": _MAX_RECONCILIATION_AGE_SECONDS,
            }
        raw = live.get("checked_at") or live.get("generated_at") or live.get("updated_at")
        observed = _parse_ts(raw)
        if raw and observed is None:
            return {
                "fresh": False,
                "reason_code": "live_reconciliation_timestamp_invalid",
                "raw_timestamp": str(raw),
                "max_age_seconds": _MAX_RECONCILIATION_AGE_SECONDS,
            }
        if observed is None:
            return {
                "fresh": False,
                "reason_code": "live_reconciliation_timestamp_missing",
                "max_age_seconds": _MAX_RECONCILIATION_AGE_SECONDS,
            }
        age = (now - observed).total_seconds()
        base = {
            "timestamp": observed.isoformat(),
            "age_seconds": round(age, 2),
            "max_age_seconds": _MAX_RECONCILIATION_AGE_SECONDS,
        }
        if age < -_FUTURE_SKEW_SECONDS:
            return {**base, "fresh": False, "reason_code": "live_reconciliation_timestamp_future"}
        if age > _MAX_RECONCILIATION_AGE_SECONDS:
            return {**base, "fresh": False, "reason_code": "live_reconciliation_stale"}
        return {**base, "fresh": True, "reason_code": "fresh"}

    def _float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
