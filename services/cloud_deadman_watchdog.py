"""Lightweight, independent liveness observer for the external dead-man job."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.journal_store import load_json, write_json


SCHEMA_VERSION = "cloud-deadman-watchdog-v1"
MAX_RECEIPT_AGE_SECONDS = 600.0
FUTURE_SKEW_SECONDS = 120.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest(path: Path) -> dict[str, Any]:
    try:
        rows = load_json(path)
    except (OSError, ValueError):
        return {}
    return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}


class CloudDeadmanWatchdog:
    """Detect dead-man staleness without emitting a masking success ping."""

    def __init__(
        self,
        output_root: Path,
        *,
        url: str | None = None,
        opener: Callable[..., Any] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        now: Callable[[], datetime] = _utcnow,
        timeout_seconds: float = 10.0,
        max_receipt_age_seconds: float = MAX_RECEIPT_AGE_SECONDS,
    ) -> None:
        self.output_root = Path(output_root)
        self.url = (
            url
            if url is not None
            else os.getenv("TRADING_ORCHESTRATOR_DEADMAN_URL", "")
            or os.getenv("TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL", "")
        )
        self.opener = opener or urllib.request.urlopen
        self.command_runner = command_runner or subprocess.run
        self.now = now
        self.timeout_seconds = timeout_seconds
        self.max_receipt_age_seconds = float(max_receipt_age_seconds)

    def run(self) -> dict[str, Any]:
        observed = self.now().astimezone(timezone.utc).replace(microsecond=0)
        receipt = _latest(self.output_root / "deadman_ping" / "current.json")
        timer = self._timer_status()
        blocker = self._blocker(observed=observed, receipt=receipt, timer=timer)
        current_path = (
            self.output_root / "cloud" / "deadman_watchdog" / "current.json"
        )
        prior = _latest(current_path)
        if blocker is None:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "checked_at": observed.isoformat(),
                "status": "healthy",
                "machine_code": "deadman_liveness_fresh",
                "timer": timer,
                "receipt_checked_at": receipt.get("checked_at"),
                "success_ping_sent": False,
                "paper_only": True,
                "control_actions_executed": 0,
                "orders_created": 0,
                "positions_changed": 0,
                "secrets_included": False,
            }
            if prior.get("status") == "blocked":
                self._append_event(
                    {
                        **payload,
                        "event": "recovered",
                        "outage_id": prior.get("outage_id"),
                        "previous_machine_code": prior.get("machine_code"),
                    }
                )
            write_json(current_path, [payload])
            return payload

        same_outage = (
            prior.get("status") == "blocked"
            and prior.get("machine_code") == blocker["machine_code"]
            and bool(prior.get("outage_id"))
        )
        outage_id = (
            str(prior["outage_id"]) if same_outage else str(uuid.uuid4())
        )
        prior_delivery = (
            dict(prior.get("delivery") or {}) if same_outage else {}
        )
        if prior_delivery.get("delivered") is True:
            delivery = {
                **prior_delivery,
                "status": "duplicate_suppressed",
                "delivered": True,
            }
        else:
            detected = {
                "schema_version": SCHEMA_VERSION,
                "event": "detected",
                "outage_id": outage_id,
                "checked_at": observed.isoformat(),
                **blocker,
                "timer": timer,
                "success_ping_sent": False,
                "paper_only": True,
                "control_actions_executed": 0,
                "orders_created": 0,
                "positions_changed": 0,
                "secrets_included": False,
            }
            self._append_event(detected)
            delivery = self._deliver(outage_id=outage_id, blocker=blocker)
            self._append_event(
                {
                    **detected,
                    "event": "delivery_result",
                    "delivery": delivery,
                }
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "checked_at": observed.isoformat(),
            "status": "blocked",
            "outage_id": outage_id,
            **blocker,
            "timer": timer,
            "receipt_checked_at": receipt.get("checked_at"),
            "delivery": delivery,
            "success_ping_sent": False,
            "paper_only": True,
            "control_actions_executed": 0,
            "orders_created": 0,
            "positions_changed": 0,
            "secrets_included": False,
        }
        write_json(current_path, [payload])
        return payload

    def _timer_status(self) -> dict[str, Any]:
        command = [
            "systemctl",
            "show",
            "gridmind-deadman-ping.timer",
            "--no-pager",
            "--property=LoadState,UnitFileState,ActiveState",
        ]
        result = self.command_runner(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        values = dict(
            line.split("=", 1)
            for line in str(result.stdout or "").splitlines()
            if "=" in line
        )
        healthy = (
            result.returncode == 0
            and values.get("LoadState") == "loaded"
            and values.get("UnitFileState") == "enabled"
            and values.get("ActiveState") == "active"
        )
        return {
            "status": "pass" if healthy else "blocked",
            "load_state": values.get("LoadState"),
            "unit_file_state": values.get("UnitFileState"),
            "active_state": values.get("ActiveState"),
        }

    def _blocker(
        self,
        *,
        observed: datetime,
        receipt: dict[str, Any],
        timer: dict[str, Any],
    ) -> dict[str, Any] | None:
        if timer.get("status") != "pass":
            return {
                "machine_code": "deadman_timer_unavailable",
                "original_reason": "dead-man timer is not loaded, enabled and active",
                "alternative_action": "signal_external_deadman_fail_endpoint",
            }
        checked_at = _parse_ts(receipt.get("checked_at"))
        if checked_at is None:
            return {
                "machine_code": "deadman_success_receipt_missing",
                "original_reason": "dead-man success receipt is missing or invalid",
                "alternative_action": "signal_external_deadman_fail_endpoint",
            }
        age = (observed - checked_at).total_seconds()
        if age < -FUTURE_SKEW_SECONDS:
            return {
                "machine_code": "deadman_success_receipt_future",
                "original_reason": "dead-man success receipt timestamp is in the future",
                "alternative_action": "signal_external_deadman_fail_endpoint",
                "receipt_age_seconds": age,
            }
        if age > self.max_receipt_age_seconds:
            return {
                "machine_code": "deadman_success_receipt_stale",
                "original_reason": "dead-man success receipt exceeded the liveness budget",
                "alternative_action": "signal_external_deadman_fail_endpoint",
                "receipt_age_seconds": age,
                "max_receipt_age_seconds": self.max_receipt_age_seconds,
            }
        return None

    def _deliver(
        self,
        *,
        outage_id: str,
        blocker: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.url:
            return {
                "status": "not_configured",
                "delivered": False,
                "target_kind": "fail",
            }
        target = self._with_query(
            self._fail_url(self.url),
            {
                "source": "deadman_liveness_watchdog",
                "machine_code": str(blocker["machine_code"]),
                "outage_id": outage_id,
            },
        )
        try:
            request = urllib.request.Request(target, method="GET")
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                delivered = 200 <= status_code < 300
                return {
                    "status": "fail_sent" if delivered else "fail_signal_failed",
                    "delivered": delivered,
                    "status_code": status_code,
                    "target_kind": "fail",
                }
        except (OSError, urllib.error.URLError, TimeoutError):
            return {
                "status": "fail_signal_failed",
                "delivered": False,
                "target_kind": "fail",
            }

    def _append_event(self, payload: dict[str, Any]) -> Path:
        directory = self.output_root / "cloud" / "deadman_watchdog" / "events"
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{str(payload['checked_at'])[:10]}.jsonl"
        line = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return path

    @staticmethod
    def _fail_url(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.rstrip("/")
        if not path.endswith("/fail"):
            path = f"{path}/fail"
        return urllib.parse.urlunparse(parsed._replace(path=path))

    @staticmethod
    def _with_query(url: str, params: dict[str, str]) -> str:
        parsed = urllib.parse.urlparse(url)
        existing = dict(
            urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        )
        existing.update(params)
        return urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(existing))
        )
