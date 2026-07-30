"""Read-only invariant checks for the required Cloud Paper systemd timers."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.journal_store import write_json


REQUIRED_TIMERS = {
    "gridmind-daily-24h.timer": {
        "service": "gridmind-daily-24h.service",
        "on_calendar": "*-*-* 01:03:00 UTC",
        "requires_next_trigger": True,
    },
    "gridmind-deadman-ping.timer": {
        "service": "gridmind-deadman-ping.service",
        "on_calendar": None,
        # OnUnitInactiveSec timers have no fixed NextElapseUSecRealtime value.
        # Their cadence must be verified from the deployed unit instead.
        "on_unit_inactive_sec": "300",
        "requires_next_trigger": False,
    },
}


class CloudTimerContract:
    """Inspect, never mutate, the two timer invariants needed by Paper."""

    def __init__(
        self,
        output_root: Path,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.command_runner = command_runner or subprocess.run
        self.now = now or (lambda: datetime.now(timezone.utc))

    def run(self, *, persist: bool = True) -> dict[str, Any]:
        checks = [self._check(name, expected) for name, expected in REQUIRED_TIMERS.items()]
        payload = {
            "schema_version": "cloud-paper-timer-contract-v1",
            "checked_at": self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass" if all(row["status"] == "pass" for row in checks) else "blocked",
            "checks": checks,
            "control_actions_executed": 0,
        }
        if persist:
            write_json(self.output_root / "cloud" / "timer_contract" / "current.json", [payload])
        return payload

    def _check(self, unit: str, expected: dict[str, Any]) -> dict[str, Any]:
        try:
            shown = self._run([
                "systemctl", "show", unit, "--no-pager",
                "--property=Id,LoadState,UnitFileState,ActiveState,NextElapseUSecRealtime,Triggers",
            ])
            values = dict(
                line.split("=", 1) for line in shown.splitlines() if "=" in line
            )
            if values.get("Id") != unit or values.get("LoadState") != "loaded":
                raise RuntimeError("unit_missing_or_wrong_identity")
            if values.get("UnitFileState") != "enabled":
                raise RuntimeError("timer_not_enabled")
            if values.get("ActiveState") != "active":
                raise RuntimeError("timer_not_active")
            if expected.get("requires_next_trigger") and not values.get("NextElapseUSecRealtime", "").strip():
                raise RuntimeError("timer_next_trigger_missing")
            cat = self._run(["systemctl", "cat", unit, "--no-pager"])
            if f"Unit={expected['service']}" not in cat:
                raise RuntimeError("timer_target_mismatch")
            calendar = expected.get("on_calendar")
            if calendar and f"OnCalendar={calendar}" not in cat:
                raise RuntimeError("timer_schedule_mismatch")
            inactive_sec = expected.get("on_unit_inactive_sec")
            if inactive_sec and f"OnUnitInactiveSec={inactive_sec}" not in cat:
                raise RuntimeError("timer_cadence_mismatch")
            return {
                "unit": unit,
                "status": "pass",
                "enabled": True,
                "active": True,
                "next_trigger": values.get("NextElapseUSecRealtime", "") or None,
                "service": expected["service"],
                "on_calendar": calendar,
                "on_unit_inactive_sec": inactive_sec,
            }
        except Exception as exc:  # noqa: BLE001 - timer uncertainty must fail closed.
            return {
                "unit": unit,
                "status": "blocked",
                "reason": str(exc),
                "next_action": "Inspect the exact deployed timer; do not enable or start a guessed unit.",
            }

    def _run(self, command: list[str]) -> str:
        result = self.command_runner(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"systemctl_failed:{command[1]}")
        return str(result.stdout or "")
