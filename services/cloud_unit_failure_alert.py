"""Persist systemd unit failures and signal the external dead-man fail endpoint."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = "cloud-unit-failure-event-v1"
_EVENTS_SUBDIR = Path("cloud") / "unit_failure_events"
_SAFE_UNIT = re.compile(r"^gridmind-[A-Za-z0-9_.@:-]+\.service$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class CloudUnitFailureAlert:
    """Record an OnFailure event before attempting the external signal."""

    def __init__(
        self,
        output_root: Path,
        *,
        url: str | None = None,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 10.0,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.output_root = Path(output_root)
        self.url = (
            url
            if url is not None
            else os.getenv("TRADING_ORCHESTRATOR_DEADMAN_URL", "")
            or os.getenv("TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL", "")
        )
        self.opener = opener or urllib.request.urlopen
        self.timeout_seconds = timeout_seconds
        self.now = now

    def run(self, failed_unit: str) -> dict[str, Any]:
        failed_unit = str(failed_unit or "").strip()
        if not _SAFE_UNIT.fullmatch(failed_unit):
            raise ValueError("invalid_gridmind_failed_unit")

        event_id = str(uuid.uuid4())
        occurred_at = self.now().isoformat()
        detected = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "phase": "detected",
            "occurred_at": occurred_at,
            "failed_unit": failed_unit,
            "machine_code": "cloud_unit_failed",
            "original_reason": "systemd_on_failure",
            "alternative_action": "signal_external_deadman_fail_endpoint",
            "execution_profile": "paper",
            "paper_only": True,
            "control_actions_executed": 0,
            "orders_created": 0,
            "positions_changed": 0,
            "secrets_included": False,
        }
        path = self._append(detected)
        delivery = self._deliver(event_id=event_id, failed_unit=failed_unit)
        result = {
            **detected,
            "phase": "delivery_result",
            "completed_at": self.now().isoformat(),
            "delivery": delivery,
            "event_path": str(path),
        }
        self._append(result)
        return result

    def _deliver(self, *, event_id: str, failed_unit: str) -> dict[str, Any]:
        if not self.url:
            return {
                "status": "not_configured",
                "delivered": False,
                "target_kind": "fail",
            }
        target = self._fail_url(self.url)
        target = self._with_query(
            target,
            {
                "source": "systemd_on_failure",
                "machine_code": "cloud_unit_failed",
                "failed_unit": failed_unit,
                "event_id": event_id,
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

    def _append(self, event: dict[str, Any]) -> Path:
        directory = self.output_root / _EVENTS_SUBDIR
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        day = str(event["occurred_at"])[:10]
        path = directory / f"{day}.jsonl"
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
        existing = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        existing.update(params)
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(existing)))
