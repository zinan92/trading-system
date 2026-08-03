from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.accounting_projection_core import project_execution_accounting
from services.journal_store import load_json, write_json
from services.schedule_status import ScheduleStatus
from services.system_vitals import SystemVitals
from services.cloud_health import CloudPaperHealth


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
_CLOUD_HEALTH_SCHEMA = "cloud-paper-health-v1"
_CLOUD_HEALTH_SEVERITIES = frozenset({"none", "insufficient", "warning", "critical"})
_CLOUD_HEALTH_REQUIRED_CHECKS = frozenset({
    "datafeed",
    "live_tick",
    "execution",
    "reconciliation",
    "daily_self_review",
    "backup",
    "scheduler_ownership",
    "source",
})


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
        cloud_health_provider: Any = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.market_db = Path(market_db)
        self.url = url if url is not None else os.getenv("TRADING_ORCHESTRATOR_DEADMAN_URL", "")
        self.position_url = position_url if position_url is not None else os.getenv("TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL", "")
        self.opener = opener or urllib.request.urlopen
        self.timeout_seconds = timeout_seconds
        self.schedule_status_provider = schedule_status_provider
        self.cloud_health_provider = cloud_health_provider

    def run(self, run_date: str, *, dry_run: bool = False) -> dict:
        now = _utcnow()
        checked_at = now.isoformat()
        vitals = SystemVitals(self.output_root, self.market_db).run(run_date, persist=False)
        schedule_runtime = self._schedule_runtime(run_date)
        cloud_health = self._cloud_health()
        exposure = self._exposure_snapshot(now=now)
        severity = "critical" if exposure["has_open_position"] else "normal"
        selected_url = self._selected_url(severity)
        ping = self._ping(
            selected_url,
            severity,
            exposure,
            vitals,
            schedule_runtime,
            cloud_health,
            dry_run=dry_run,
        )
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
            "cloud_health": cloud_health,
            "ping": ping,
            "liveness_authority": ping.get("liveness_authority"),
            "failure_signal_sources": list(ping.get("failure_signal_sources") or []),
            "note": "External dead-man alert is triggered by missed pings outside this runtime host.",
        }
        write_json(self.output_root / "deadman_ping" / "current.json", [payload])
        write_json(self.output_root / "deadman_ping" / f"{run_date}.json", [payload])
        return payload

    def _selected_url(self, severity: str) -> str:
        if severity == "critical" and self.position_url:
            return self.position_url
        return self.url or self.position_url

    def _ping(
        self,
        url: str,
        severity: str,
        exposure: dict,
        vitals: dict,
        schedule_runtime: dict,
        cloud_health: dict,
        *,
        dry_run: bool,
    ) -> dict:
        cloud_health_authoritative = self._cloud_health_authoritative(cloud_health)
        legacy_always_on_applicable = not cloud_health_authoritative
        always_on_failed = (
            legacy_always_on_applicable and self._always_on_blocked(vitals)
        )
        schedule_failed = self._schedule_runtime_failed(schedule_runtime)
        cloud_failed = self._cloud_health_failed(cloud_health)
        failure_signal_sources = [
            source
            for source, failed in (
                ("legacy_always_on", always_on_failed),
                ("schedule_runtime", schedule_failed),
                ("cloud_health", cloud_failed),
            )
            if failed
        ]
        routing_evidence = {
            "liveness_authority": (
                "cloud_health"
                if cloud_health_authoritative
                else "legacy_always_on_and_cloud_health_fail_closed"
            ),
            "cloud_health_authoritative": cloud_health_authoritative,
            "legacy_always_on_applicable": legacy_always_on_applicable,
            "failure_signal_sources": failure_signal_sources,
        }
        if not url:
            return {
                "status": "not_configured",
                "delivered": False,
                "message": "TRADING_ORCHESTRATOR_DEADMAN_URL is not configured",
                **routing_evidence,
            }
        failure_signal = bool(failure_signal_sources)
        target_url = self._healthchecks_fail_url(url) if failure_signal else url
        full_url = self._url_with_query(
            target_url,
            {
                "severity": severity,
                "position_open": "1" if exposure["has_open_position"] else "0",
                "always_on_status": str((vitals.get("always_on") or {}).get("status") or ""),
                "schedule_status": str(schedule_runtime.get("status") or ""),
                "cloud_status": str(cloud_health.get("status") or ""),
            },
        )
        target_kind = "fail" if failure_signal else "success"
        if dry_run:
            return {
                "status": "dry_run_fail" if failure_signal else "dry_run",
                "delivered": False,
                "target_kind": target_kind,
                "failure_signal": failure_signal,
                "success_ping": not failure_signal,
                "message": "dry run; ping not sent",
                **routing_evidence,
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
                    "target_kind": target_kind,
                    "failure_signal": failure_signal,
                    "success_ping": not failure_signal,
                    "message": self._ping_message(ok=ok, failure_signal=failure_signal),
                    **routing_evidence,
                }
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            return {
                "status": "fail_signal_failed" if failure_signal else "failed",
                "delivered": False,
                "target_kind": target_kind,
                "failure_signal": failure_signal,
                "success_ping": not failure_signal,
                "message": f"dead-man request failed: {type(exc).__name__}",
                **routing_evidence,
            }

    def _cloud_health(self) -> dict:
        if str(os.getenv("GRIDMIND_RUNTIME_MODE") or "").lower() != "cloud":
            return {"status": "not_applicable"}
        if self.cloud_health_provider is not None:
            return dict(self.cloud_health_provider())
        try:
            return CloudPaperHealth(
                output_root=self.output_root,
                backup_root=Path(
                    os.getenv("GRIDMIND_BACKUP_ROOT", "/var/lib/gridmind/backups")
                ),
            ).run(persist=True)
        except Exception as exc:  # noqa: BLE001 - dead-man must still fail signal.
            return {
                "status": "blocked",
                "incidents": [
                    {
                        "stage": "cloud_health",
                        "code": "cloud_health_check_failed",
                        "summary": f"Cloud health check failed: {type(exc).__name__}",
                        "next_action": "Inspect the Cloud health receipt and service logs.",
                    }
                ],
            }

    @staticmethod
    def _cloud_health_failed(cloud_health: dict) -> bool:
        if str(cloud_health.get("runtime_mode") or "").lower() == "cloud":
            if not ExternalDeadmanPing._cloud_health_authoritative(cloud_health):
                return True
            return str(cloud_health.get("severity") or "") == "critical"
        # Cloud health may be degraded for warning-only observability items
        # (including the first incomplete utilization window).  Only the
        # explicit critical severity drives the external /fail endpoint.
        if str(cloud_health.get("severity") or ""):
            return str(cloud_health.get("severity")) == "critical"
        # Backward-compatible fail-closed behavior for pre-#468 receipts that
        # do not carry the severity field.
        return str(cloud_health.get("status") or "") not in {
            "healthy",
            "not_applicable",
        }

    @staticmethod
    def _cloud_health_authoritative(cloud_health: dict) -> bool:
        if (
            cloud_health.get("schema_version") != _CLOUD_HEALTH_SCHEMA
            or str(cloud_health.get("runtime_mode") or "").lower() != "cloud"
            or cloud_health.get("paper_only") is not True
            or cloud_health.get("control_actions_executed") != 0
            or cloud_health.get("secrets_included") is not False
            or str(cloud_health.get("severity") or "")
            not in _CLOUD_HEALTH_SEVERITIES
        ):
            return False
        checks = cloud_health.get("checks")
        if not isinstance(checks, dict):
            return False
        if not _CLOUD_HEALTH_REQUIRED_CHECKS.issubset(checks):
            return False
        convergence_checks = {"supervisor", "cycle_decision"} & set(checks)
        if not convergence_checks:
            return False
        for name in _CLOUD_HEALTH_REQUIRED_CHECKS | convergence_checks:
            row = checks.get(name)
            if (
                not isinstance(row, dict)
                or not str(row.get("status") or "")
                or not str(row.get("code") or "")
                or str(row.get("severity") or "")
                not in _CLOUD_HEALTH_SEVERITIES
            ):
                return False
        return True

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
        if self._cloud_paper_owner_active():
            return self._paper_exposure_snapshot(now=now)
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

    def _cloud_paper_owner_active(self) -> bool:
        ownership = _latest(
            _safe_load_json(self.output_root / "cloud" / "scheduler_ownership" / "current.json")
        )
        return (
            ownership.get("status") == "active"
            and ownership.get("active_owner_id") == "cloud-primary"
            and ownership.get("dual_owner_allowed") is False
        )

    def _paper_exposure_snapshot(self, *, now: datetime) -> dict:
        runtime = _latest(
            _safe_load_json(self.output_root / "dualtrack" / "strategy_control" / "runtime.json")
        )
        cycle_id = str(runtime.get("cycle_id") or "").strip()
        snapshot_path = (
            self.output_root
            / "dualtrack"
            / "nautilus_authoritative"
            / "snapshots"
            / f"{cycle_id}.json"
        )
        snapshot = _latest(_safe_load_json(snapshot_path)) if cycle_id else {}
        freshness = self._artifact_freshness(snapshot_path, now=now)
        engine_reconciliation = (
            snapshot.get("reconciliation")
            if isinstance(snapshot.get("reconciliation"), dict)
            else {}
        )
        accounting_error = ""
        try:
            accounting = project_execution_accounting(snapshot).to_dict() if snapshot else {}
        except Exception as exc:  # noqa: BLE001 - malformed exposure evidence is fail-closed.
            accounting = {}
            accounting_error = f"{type(exc).__name__}: {exc}"
        accounting_reconciliation = (
            accounting.get("reconciliation")
            if isinstance(accounting.get("reconciliation"), dict)
            else {}
        )
        identity_matches = bool(
            cycle_id
            and snapshot
            and str(snapshot.get("cycle_id") or "") == cycle_id
            and str(snapshot.get("engine") or "") == "nautilus_paper"
        )
        engine_ok = (
            engine_reconciliation.get("status") == "ok"
            and not engine_reconciliation.get("issues")
        )
        accounting_ok = accounting_reconciliation.get("status") == "pass"
        position_unknown = not (
            freshness["fresh"] and identity_matches and engine_ok and accounting_ok
        )
        positions = [
            row
            for row in (accounting.get("positions") or [])
            if isinstance(row, dict)
            and str(row.get("status") or "").lower() == "open"
            and abs(self._float(row.get("remaining_quantity") or row.get("quantity"))) > 0
        ]
        return {
            "has_open_position": position_unknown or bool(positions),
            "position_unknown": position_unknown,
            "exchange_position_count": 0,
            "exchange_positions": [],
            "suspected_naked_position": False,
            "confirmation_status": "paper_execution_verified" if not position_unknown else "paper_execution_unknown",
            "system_state": str(runtime.get("actual_state") or "unknown"),
            "reconciliation_fresh": freshness["fresh"],
            "reconciliation_freshness": freshness,
            "strategy_open_trade_count": len(positions),
            "strategy_open_trades": positions[:20],
            "source": "nautilus_authoritative.snapshot",
            "source_path": str(snapshot_path),
            "cycle_id": cycle_id,
            "runtime_plan_id": runtime.get("strategy_plan_id"),
            "identity_matches": identity_matches,
            "engine_reconciliation_status": engine_reconciliation.get("status", "missing"),
            "accounting_reconciliation_status": accounting_reconciliation.get("status", "missing"),
            "accounting_error": accounting_error,
        }

    def _artifact_freshness(self, path: Path, *, now: datetime) -> dict:
        if not path.exists():
            return {
                "fresh": False,
                "reason_code": "paper_execution_snapshot_missing",
                "max_age_seconds": _MAX_RECONCILIATION_AGE_SECONDS,
            }
        try:
            observed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return {
                "fresh": False,
                "reason_code": "paper_execution_snapshot_unreadable",
                "max_age_seconds": _MAX_RECONCILIATION_AGE_SECONDS,
            }
        age = (now - observed).total_seconds()
        base = {
            "timestamp": observed.isoformat(),
            "age_seconds": round(age, 2),
            "max_age_seconds": _MAX_RECONCILIATION_AGE_SECONDS,
        }
        if age < -_FUTURE_SKEW_SECONDS:
            return {**base, "fresh": False, "reason_code": "paper_execution_snapshot_future"}
        if age > _MAX_RECONCILIATION_AGE_SECONDS:
            return {**base, "fresh": False, "reason_code": "paper_execution_snapshot_stale"}
        return {**base, "fresh": True, "reason_code": "fresh"}

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
