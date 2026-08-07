"""Read-only invariant checks for the required Cloud Paper systemd timers."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.cloud_ai_provider import CloudAIProviderReadiness
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
    "gridmind-ai-provider-readiness.timer": {
        "service": "gridmind-ai-provider-readiness.service",
        "on_calendar": None,
        "on_unit_inactive_sec": "300",
        # The timer has no next trigger while its oneshot service is still
        # active.  That narrow state is accepted and receipted; an idle timer
        # without a next trigger remains blocked.
        "requires_next_trigger": True,
    },
    "gridmind-next-cycle-plan.timer": {
        "service": "gridmind-next-cycle-plan.service",
        "on_calendar": None,
        "on_unit_inactive_sec": "300",
        "requires_next_trigger": True,
    },
}

SERVICE_TIMER_UNITS = {
    "daily-24h": "gridmind-daily-24h.timer",
    "deadman-ping": "gridmind-deadman-ping.timer",
    "ai-provider-readiness": "gridmind-ai-provider-readiness.timer",
    "next-cycle-plan": "gridmind-next-cycle-plan.timer",
}


class CloudTimerContract:
    """Inspect, never mutate, the timer invariants needed by Cloud Paper."""

    def __init__(
        self,
        output_root: Path,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        now: Callable[[], datetime] | None = None,
        current_readiness_provider: Callable[[], dict[str, Any]] | None = None,
        last_success_readiness_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.command_runner = command_runner or subprocess.run
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.current_readiness_provider = current_readiness_provider or (
            lambda: self._provider_readiness("readiness_current.json")
        )
        self.last_success_readiness_provider = last_success_readiness_provider or (
            lambda: self._provider_readiness("readiness_last_success.json")
        )

    def run(self, *, persist: bool = True) -> dict[str, Any]:
        checks = [self._check(name, expected) for name, expected in REQUIRED_TIMERS.items()]
        status_by_unit = {str(row.get("unit") or ""): row.get("status") for row in checks}
        service_status = {
            service: status_by_unit.get(unit, "blocked")
            for service, unit in SERVICE_TIMER_UNITS.items()
        }
        payload = {
            "schema_version": "cloud-paper-timer-contract-v1",
            "checked_at": self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass" if all(row["status"] == "pass" for row in checks) else "blocked",
            "checks": checks,
            "service_status": service_status,
            # Provider evidence is observational here.  A failed refresh is
            # projected by Supervisor/health when it blocks convergence; it
            # must never prevent the dead-man service from sending that alert.
            "provider_readiness": {
                "current": self._bounded_readiness(self.current_readiness_provider),
                "latest_success": self._bounded_readiness(
                    self.last_success_readiness_provider
                ),
            },
            "control_actions_executed": 0,
        }
        if persist:
            write_json(self.output_root / "cloud" / "timer_contract" / "current.json", [payload])
        return payload

    def _check(self, unit: str, expected: dict[str, Any]) -> dict[str, Any]:
        try:
            shown = self._run([
                "systemctl", "show", unit, "--no-pager",
                "--property=Id,LoadState,UnitFileState,ActiveState,NextElapseUSecRealtime,NextElapseUSecMonotonic,LastTriggerUSec,Triggers,FragmentPath",
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
            next_trigger_realtime = values.get(
                "NextElapseUSecRealtime", ""
            ).strip()
            next_trigger_monotonic = values.get(
                "NextElapseUSecMonotonic", ""
            ).strip()
            next_trigger = next_trigger_realtime or next_trigger_monotonic
            next_trigger_clock = (
                "realtime"
                if next_trigger_realtime
                else "monotonic"
                if next_trigger_monotonic
                else None
            )
            next_trigger_pending_service_completion = False
            if expected.get("requires_next_trigger") and not next_trigger:
                service_state = self._show_active_state(expected["service"])
                if service_state not in {"active", "activating"}:
                    raise RuntimeError("timer_next_trigger_missing")
                next_trigger_pending_service_completion = True
            fragment_path = str(values.get("FragmentPath") or "")
            if fragment_path != f"/etc/systemd/system/{unit}":
                raise RuntimeError("timer_load_path_mismatch")
            triggers = str(values.get("Triggers") or "")
            if expected["service"] not in triggers.split():
                raise RuntimeError("timer_trigger_identity_mismatch")
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
                "next_trigger": next_trigger or None,
                "next_trigger_clock": next_trigger_clock,
                "next_trigger_realtime": next_trigger_realtime or None,
                "next_trigger_monotonic": next_trigger_monotonic or None,
                "next_trigger_pending_service_completion": (
                    next_trigger_pending_service_completion
                ),
                "last_trigger": values.get("LastTriggerUSec", "") or None,
                "fragment_path": fragment_path,
                "effective_unit_content_sha256": hashlib.sha256(
                    cat.encode("utf-8")
                ).hexdigest(),
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

    def _show_active_state(self, unit: str) -> str:
        shown = self._run(
            [
                "systemctl",
                "show",
                unit,
                "--no-pager",
                "--property=ActiveState",
            ]
        )
        values = dict(
            line.split("=", 1) for line in shown.splitlines() if "=" in line
        )
        return str(values.get("ActiveState") or "")

    def _provider_readiness(self, filename: str) -> dict[str, Any]:
        verifier = CloudAIProviderReadiness(
            self.output_root,
            now=self.now,
        )
        verifier.path = (
            self.output_root / "cloud" / "provider" / filename
        )
        return verifier.verify()

    @staticmethod
    def _bounded_readiness(
        provider: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            result = provider()
        except Exception as exc:  # noqa: BLE001 - evidence uncertainty is explicit.
            return {
                "ok": False,
                "blocker": "cloud_ai_provider_readiness_observation_failed",
                "detail": type(exc).__name__,
            }
        if not isinstance(result, dict):
            return {
                "ok": False,
                "blocker": "cloud_ai_provider_readiness_observation_invalid",
            }
        provider_row = (
            result.get("provider")
            if isinstance(result.get("provider"), dict)
            else {}
        )
        return {
            key: value
            for key, value in {
                "ok": result.get("ok") is True,
                "status": result.get("status"),
                "blocker": result.get("blocker"),
                "failure_code": result.get("failure_code"),
                "artifact": result.get("artifact"),
                "checked_at": result.get("checked_at"),
                "expires_at": result.get("expires_at"),
                "source_sha": result.get("source_sha"),
                "source_tree_sha": result.get("source_tree_sha"),
                "readiness_digest": result.get("readiness_digest"),
                "provider_name": provider_row.get("name"),
                "provider_version": provider_row.get("version"),
                "provider_auth_status": provider_row.get("auth_status"),
                "executable_sha256": provider_row.get("executable_sha256"),
            }.items()
            if value is not None
        }
