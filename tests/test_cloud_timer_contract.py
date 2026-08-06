from __future__ import annotations

import subprocess
from pathlib import Path

from services.cloud_timer_contract import CloudTimerContract


def _readiness() -> dict:
    return {
        "ok": True,
        "status": "pass",
        "checked_at": "2026-08-06T00:00:00+00:00",
        "expires_at": "2026-08-07T00:00:00+00:00",
        "source_sha": "a" * 40,
        "source_tree_sha": "b" * 40,
        "readiness_digest": "c" * 64,
        "provider": {
            "name": "codex_cli_chatgpt",
            "version": "codex-cli test",
            "auth_status": "logged_in",
            "executable_sha256": "d" * 64,
        },
    }


def _runner(command, **_kwargs):
    unit = command[2]
    if command[1] == "show" and unit.endswith(".service"):
        return subprocess.CompletedProcess(
            command,
            0,
            "ActiveState=inactive\n",
            "",
        )
    if command[1] == "show":
        next_trigger = (
            "Fri 2026-08-07 01:03:00 CST"
            if unit == "gridmind-daily-24h.timer"
            else "Fri 2026-08-07 10:00:00 CST"
            if unit == "gridmind-ai-provider-readiness.timer"
            else ""
        )
        service = unit.removesuffix(".timer") + ".service"
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(
                [
                    f"Id={unit}",
                    "LoadState=loaded",
                    "UnitFileState=enabled",
                    "ActiveState=active",
                    f"NextElapseUSecRealtime={next_trigger}",
                    "LastTriggerUSec=Thu 2026-08-06 04:00:00 CST",
                    f"Triggers={service}",
                    f"FragmentPath=/etc/systemd/system/{unit}",
                ]
            ),
            "",
        )
    if unit == "gridmind-daily-24h.timer":
        schedule = "OnCalendar=*-*-* 01:03:00 UTC"
    elif unit == "gridmind-ai-provider-readiness.timer":
        schedule = "OnUnitInactiveSec=300"
    else:
        schedule = "OnUnitInactiveSec=300"
    service = unit.removesuffix(".timer") + ".service"
    return subprocess.CompletedProcess(
        command,
        0,
        f"[Timer]\n{schedule}\nUnit={service}\n",
        "",
    )


def _contract(tmp_path: Path, *, runner=_runner) -> CloudTimerContract:
    return CloudTimerContract(
        tmp_path,
        command_runner=runner,
        current_readiness_provider=_readiness,
        last_success_readiness_provider=_readiness,
    )


def test_cloud_timer_contract_requires_canonical_enabled_active_timers(
    tmp_path: Path,
):
    result = _contract(tmp_path).run()

    assert result["status"] == "pass"
    assert result["control_actions_executed"] == 0
    assert {row["unit"] for row in result["checks"]} == {
        "gridmind-daily-24h.timer",
        "gridmind-deadman-ping.timer",
        "gridmind-ai-provider-readiness.timer",
    }
    readiness = next(
        row
        for row in result["checks"]
        if row["unit"] == "gridmind-ai-provider-readiness.timer"
    )
    assert readiness["fragment_path"] == (
        "/etc/systemd/system/gridmind-ai-provider-readiness.timer"
    )
    assert len(readiness["effective_unit_content_sha256"]) == 64
    assert readiness["next_trigger"]
    assert result["provider_readiness"]["latest_success"]["ok"] is True
    assert result["service_status"]["ai-provider-readiness"] == "pass"


def test_cloud_timer_contract_fails_closed_without_daily_next_trigger(
    tmp_path: Path,
):
    def missing_next(command, **kwargs):
        result = _runner(command, **kwargs)
        if command[1] == "show" and command[2] == "gridmind-daily-24h.timer":
            result.stdout = result.stdout.replace(
                "NextElapseUSecRealtime=Fri 2026-08-07 01:03:00 CST",
                "NextElapseUSecRealtime=",
            )
        return result

    result = _contract(tmp_path, runner=missing_next).run()

    assert result["status"] == "blocked"
    assert any(
        row.get("reason") == "timer_next_trigger_missing"
        for row in result["checks"]
    )


def test_cloud_timer_contract_allows_next_trigger_to_wait_for_active_oneshot(
    tmp_path: Path,
):
    def active_oneshot(command, **kwargs):
        result = _runner(command, **kwargs)
        if command[1] == "show" and command[2] == (
            "gridmind-ai-provider-readiness.timer"
        ):
            result.stdout = result.stdout.replace(
                "NextElapseUSecRealtime=Fri 2026-08-07 10:00:00 CST",
                "NextElapseUSecRealtime=",
            )
        if command[1] == "show" and command[2] == (
            "gridmind-ai-provider-readiness.service"
        ):
            result.stdout = "ActiveState=activating\n"
        return result

    result = _contract(tmp_path, runner=active_oneshot).run()

    assert result["status"] == "pass"
    readiness = next(
        row
        for row in result["checks"]
        if row["unit"] == "gridmind-ai-provider-readiness.timer"
    )
    assert readiness["next_trigger"] is None
    assert readiness["next_trigger_pending_service_completion"] is True


def test_cloud_timer_contract_requires_deadman_cadence(tmp_path: Path):
    def missing_cadence(command, **kwargs):
        result = _runner(command, **kwargs)
        if (
            command[1] == "cat"
            and command[2] == "gridmind-deadman-ping.timer"
        ):
            result.stdout = result.stdout.replace(
                "OnUnitInactiveSec=300",
                "OnUnitInactiveSec=600",
            )
        return result

    result = _contract(tmp_path, runner=missing_cadence).run()

    assert result["status"] == "blocked"
    assert any(
        row.get("reason") == "timer_cadence_mismatch"
        for row in result["checks"]
    )


def test_provider_failure_is_observed_without_disabling_deadman_contract(
    tmp_path: Path,
):
    contract = CloudTimerContract(
        tmp_path,
        command_runner=_runner,
        current_readiness_provider=lambda: {
            "ok": False,
            "blocker": "cloud_ai_provider_readiness_not_passing",
            "failure_code": "strategy_recommendation_provider_timeout",
        },
        last_success_readiness_provider=_readiness,
    )

    result = contract.run()

    assert result["status"] == "pass"
    assert result["provider_readiness"]["current"]["ok"] is False
    assert result["provider_readiness"]["latest_success"]["ok"] is True
    assert result["service_status"]["deadman-ping"] == "pass"
