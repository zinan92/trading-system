from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.journal_store import write_json


def _now() -> datetime:
    return datetime(2026, 7, 8, 4, 0, tzinfo=timezone.utc)


def _healthy_system() -> dict:
    return {
        "generated_at": _now().isoformat(),
        "overall": "RUN",
        "checks": [
            {"id": "schedule", "status": "RUN", "reason": "launchd schedule active", "room": "ops", "cta": "查看调度"},
            {"id": "data_freshness", "status": "RUN", "reason": "GOLD latest bar fresh", "room": "ops", "cta": "查看行情"},
        ],
    }


def _cycle_payload(*, lock_deadline: str, end: str, has_plan: bool = True) -> dict:
    return {
        "cycle_id": "2026-07-08_DAY",
        "kind": "DAY",
        "start": "2026-07-08T01:00:00+00:00",
        "end": end,
        "lock_deadline": lock_deadline,
        "countdown_seconds": 900,
        "effective_plan_status": {
            "has_effective_plan": has_plan,
            "machine_stands_down": not has_plan,
            "author": "human" if has_plan else "",
        },
    }


def _patch_inputs(monkeypatch: pytest.MonkeyPatch, *, system: dict | None = None, cycle: dict | None = None) -> None:
    from services import command_center

    monkeypatch.setattr(command_center, "build_system_state", lambda output_root=None, as_of=None: system or _healthy_system())
    monkeypatch.setattr(
        command_center,
        "_build_cycle_payload",
        lambda output_root, now: cycle
        or _cycle_payload(lock_deadline="2026-07-08T03:00:00+00:00", end="2026-07-08T13:00:00+00:00"),
    )
    monkeypatch.setattr(
        command_center,
        "_build_cycle_liveness",
        lambda output_root, now: {"status": "fresh", "reason": "latest_required_boundary_is_covered"},
    )


@pytest.mark.parametrize(
    ("cycle", "expected_phase"),
    [
        (_cycle_payload(lock_deadline="2026-07-08T05:00:00+00:00", end="2026-07-08T13:00:00+00:00"), "blind"),
        (_cycle_payload(lock_deadline="2026-07-08T03:00:00+00:00", end="2026-07-08T13:00:00+00:00"), "intraday"),
        (_cycle_payload(lock_deadline="2026-07-08T01:00:00+00:00", end="2026-07-08T03:00:00+00:00"), "revealed"),
    ],
)
def test_command_center_healthy_inputs_derive_phase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict, expected_phase: str) -> None:
    from services.command_center import build_command_center_state

    _patch_inputs(monkeypatch, cycle=cycle)

    payload = build_command_center_state(output_root=tmp_path / "outputs", as_of=_now())

    assert payload["system"]["overall"] == "RUN"
    assert payload["cycle"]["phase"] == expected_phase


def test_command_center_system_state_failure_is_200_shape_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from services import command_center

    def broken_system(output_root=None, as_of=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(command_center, "build_system_state", broken_system)
    monkeypatch.setattr(
        command_center,
        "_build_cycle_payload",
        lambda output_root, now: _cycle_payload(lock_deadline="2026-07-08T03:00:00+00:00", end="2026-07-08T13:00:00+00:00"),
    )
    monkeypatch.setattr(
        command_center,
        "_build_cycle_liveness",
        lambda output_root, now: {"status": "fresh", "reason": "latest_required_boundary_is_covered"},
    )

    payload = command_center.build_command_center_state(output_root=tmp_path / "outputs", as_of=_now())

    assert payload["system"]["overall"] == "UNKNOWN"
    assert payload["system"]["checks"][0]["id"] == "system_state"


def test_command_center_missing_bias_summary_is_empty_calibration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from services.command_center import build_command_center_state

    _patch_inputs(monkeypatch)

    payload = build_command_center_state(output_root=tmp_path / "outputs", as_of=_now())

    assert payload["calibration"]["available"] is False
    assert payload["calibration"]["hit_rate"] is None
    assert payload["calibration"]["last_10"] == []


def test_command_center_blocked_system_picks_first_blocker_for_next_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from services.command_center import build_command_center_state

    system = {
        "generated_at": _now().isoformat(),
        "overall": "BLOCKED",
        "checks": [
            {"id": "schedule", "status": "RUN", "reason": "ok", "room": "ops", "cta": "查看调度"},
            {"id": "naked_position", "status": "BLOCKED", "reason": "naked position suspected", "room": "ops", "cta": "处理裸头寸"},
        ],
    }
    _patch_inputs(monkeypatch, system=system)

    payload = build_command_center_state(output_root=tmp_path / "outputs", as_of=_now())

    assert payload["next_action"] == {"kind": "fix", "label": "naked position suspected", "target": "ops"}


def test_command_center_blind_without_effective_plan_requests_blind_answer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from services.command_center import build_command_center_state

    cycle = _cycle_payload(
        lock_deadline="2026-07-08T05:00:00+00:00",
        end="2026-07-08T13:00:00+00:00",
        has_plan=False,
    )
    _patch_inputs(monkeypatch, cycle=cycle)

    payload = build_command_center_state(output_root=tmp_path / "outputs", as_of=_now())

    assert payload["next_action"] == {
        "kind": "blind_answer",
        "label": "提交本周期盲答作战单",
        "target": "dualtrack",
    }


def test_command_center_ledger_multi_day_scoreboard_sums_tracks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from services.command_center import build_command_center_state

    output = tmp_path / "outputs"
    _patch_inputs(monkeypatch)
    write_json(
        output / "dualtrack" / "ledger" / "daily" / "2026-07-07.json",
        [
            {
                "date": "2026-07-07",
                "cycles": {
                    "2026-07-07_DAY": {"machine": 10.5, "human": -2.0},
                    "2026-07-07_NIGHT": {"machine": -1.25, "human": 3.75},
                },
                "tracks": {"machine": {"realized_pnl": 9.25}, "human": {"realized_pnl": 1.75}},
                "total_pnl": 11.0,
            }
        ],
    )
    write_json(
        output / "dualtrack" / "ledger" / "daily" / "2026-07-08.json",
        [
            {
                "date": "2026-07-08",
                "cycles": {"2026-07-08_DAY": {"machine": 4.0, "human": 6.0}},
                "tracks": {"machine": {"realized_pnl": 4.0}, "human": {"realized_pnl": 6.0}},
                "total_pnl": 10.0,
            }
        ],
    )

    payload = build_command_center_state(output_root=output, as_of=_now())

    assert payload["scoreboard"]["total_machine_pnl"] == pytest.approx(13.25)
    assert payload["scoreboard"]["total_human_pnl"] == pytest.approx(7.75)
    assert payload["scoreboard"]["delta"] == pytest.approx(5.5)
    assert payload["scoreboard"]["cycles_scored"] == 3
    assert payload["scoreboard"]["last_cycle"]["cycle_id"] == "2026-07-08_DAY"
