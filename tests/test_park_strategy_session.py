from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from services.park_strategy_session import (
    ParkStrategyIdentityError,
    ParkStrategyIdentityJournal,
    recording_metadata,
    recording_window,
)


def test_beijing_windows_have_09_and_21_boundaries() -> None:
    assert recording_window("2026-08-14T08:59:59+08:00")["record_window_id"] == "2026-08-13_NIGHT"
    assert recording_window("2026-08-14T09:00:00+08:00")["record_window_id"] == "2026-08-14_DAY"
    assert recording_window("2026-08-14T20:59:59+08:00")["record_window_id"] == "2026-08-14_DAY"
    assert recording_window("2026-08-14T21:00:00+08:00")["record_window_id"] == "2026-08-14_NIGHT"


def test_same_session_revision_crosses_recording_window_without_identity_change(tmp_path: Path) -> None:
    journal = ParkStrategyIdentityJournal(tmp_path / "outputs")
    started = journal.start_clean_session(
        observed_at="2026-08-14T20:59:59+08:00",
        plan_digest="sha256:plan-1",
        reconciliation_healthy=True,
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
    )
    observed = journal.append_window_observation(
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        observed_at="2026-08-14T21:00:01+08:00",
    )

    assert started["record_window_id"] == "2026-08-14_DAY"
    assert observed["record_window_id"] == "2026-08-14_NIGHT"
    assert observed["strategy_session_id"] == started["strategy_session_id"]
    assert observed["strategy_revision_id"] == started["strategy_revision_id"]
    assert journal.active_session()["strategy_revision_id"] == "revision-1"


def test_identity_is_append_only_and_does_not_touch_legacy_runtime(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    legacy = root / "dualtrack" / "runner" / "2026-08-14_DAY.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"cycle_id":"2026-08-14_DAY","actual_state":"running"}\n', encoding="utf-8")
    before = legacy.read_bytes()
    journal = ParkStrategyIdentityJournal(root)
    journal.start_clean_session(
        observed_at=datetime(2026, 8, 14, 10, 0),
        plan_digest="sha256:plan-1",
        reconciliation_healthy=True,
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
    )
    assert legacy.read_bytes() == before
    assert not (root / "dualtrack" / "runner" / "2026-08-14_NIGHT.json").exists()


def test_clean_slate_rejects_runtime_exposure_and_unhealthy_reconciliation(tmp_path: Path) -> None:
    journal = ParkStrategyIdentityJournal(tmp_path / "outputs")
    for kwargs, message in (
        ({"reconciliation_healthy": False}, "healthy reconciliation"),
        ({"reconciliation_healthy": True, "open_positions": 1}, "no open positions"),
        ({"reconciliation_healthy": True, "unresolved_runtime": True}, "unresolved runtime"),
    ):
        with pytest.raises(ParkStrategyIdentityError, match=message):
            journal.start_clean_session(
                observed_at="2026-08-14T10:00:00+08:00",
                plan_digest="sha256:plan-1",
                **kwargs,
            )


def test_window_cannot_authorize_execution_effects_and_late_event_is_recorded(tmp_path: Path) -> None:
    journal = ParkStrategyIdentityJournal(tmp_path / "outputs")
    journal.start_clean_session(
        observed_at="2026-08-14T10:00:00+08:00",
        plan_digest="sha256:plan-1",
        reconciliation_healthy=True,
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
    )
    with pytest.raises(ParkStrategyIdentityError, match="flatten_positions"):
        journal.append_window_observation(
            strategy_session_id="session-1",
            strategy_revision_id="revision-1",
            observed_at="2026-08-14T11:00:00+08:00",
            effects=["flatten_positions"],
        )
    row = journal.append_window_observation(
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        observed_at="2026-08-14T09:30:00+08:00",
        late_amendment=True,
    )
    assert row["event"] == "record_window_amendment"
    assert row["late_amendment"] is True

    lines = (tmp_path / "outputs" / "park_strategy" / "identity.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["strategy_session_id"] == "session-1" for line in lines)


def test_revision_identity_cannot_be_replaced(tmp_path: Path) -> None:
    journal = ParkStrategyIdentityJournal(tmp_path / "outputs")
    journal.start_clean_session(
        observed_at="2026-08-14T10:00:00+08:00",
        plan_digest="sha256:plan-1",
        reconciliation_healthy=True,
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
    )
    with pytest.raises(ParkStrategyIdentityError, match="immutable"):
        journal.append_window_observation(
            strategy_session_id="session-1",
            strategy_revision_id="revision-2",
            observed_at="2026-08-14T22:00:00+08:00",
        )


def test_recording_metadata_contains_no_execution_control_fields() -> None:
    metadata = recording_metadata(
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        observed_at="2026-08-14T10:00:00+08:00",
    )
    assert set(metadata) == {
        "strategy_session_id",
        "strategy_revision_id",
        "record_window_id",
        "observed_at",
    }
