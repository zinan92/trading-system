from __future__ import annotations

import subprocess
from pathlib import Path

from services.cloud_timer_contract import CloudTimerContract


def _runner(command, **_kwargs):
    unit = command[2]
    if command[1] == "show":
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join([
                f"Id={unit}", "LoadState=loaded", "UnitFileState=enabled",
                "ActiveState=active", "NextElapseUSecRealtime=Fri 2026-07-31 01:03:00 CST",
            ]),
            "",
        )
    calendar = "OnCalendar=*-*-* 01:03:00 UTC" if unit == "gridmind-daily-24h.timer" else "OnUnitInactiveSec=300"
    service = unit.removesuffix(".timer") + ".service"
    return subprocess.CompletedProcess(command, 0, f"[Timer]\n{calendar}\nUnit={service}\n", "")


def test_cloud_timer_contract_requires_canonical_enabled_active_timers(tmp_path: Path):
    result = CloudTimerContract(tmp_path, command_runner=_runner).run()

    assert result["status"] == "pass"
    assert result["control_actions_executed"] == 0
    assert {row["unit"] for row in result["checks"]} == {
        "gridmind-daily-24h.timer", "gridmind-deadman-ping.timer",
    }


def test_cloud_timer_contract_fails_closed_without_next_trigger(tmp_path: Path):
    def missing_next(command, **kwargs):
        result = _runner(command, **kwargs)
        if command[1] == "show":
            result.stdout = result.stdout.replace("NextElapseUSecRealtime=Fri 2026-07-31 01:03:00 CST", "NextElapseUSecRealtime=")
        return result

    result = CloudTimerContract(tmp_path, command_runner=missing_next).run()

    assert result["status"] == "blocked"
    assert any(row.get("reason") == "timer_next_trigger_missing" for row in result["checks"])
