from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.paper_supervisor_store import (
    PaperSupervisorLeaseHeld,
    PaperSupervisorStore,
    PaperSupervisorStoreError,
)


def test_store_persists_start_intent_before_result_and_projects_attempts(tmp_path: Path) -> None:
    store = PaperSupervisorStore(tmp_path)
    cycle_id = "2026-07-30_DAY"

    with store.lease(cycle_id=cycle_id, owner_id="test-owner", now="2026-07-30T01:00:00Z"):
        intent = store.append_event(
            cycle_id,
            "start_intent",
            now="2026-07-30T01:00:01Z",
            attempt_id="attempt-1",
            episode_id="episode-1",
            fields={
                "preview_id": "preview-fresh-1",
                "prepared_start_id": "prepared-fresh-1",
                "plan_fingerprint": "plan-sha",
            },
        )
        assert store.unfinished_start_intent(cycle_id) == intent
        store.write_state(cycle_id, {"attempt_count": 1, "start_call_count": 1})
        store.append_event(
            cycle_id,
            "start_result",
            now="2026-07-30T01:00:02Z",
            attempt_id="attempt-1",
            fields={"result": "started"},
        )

    assert store.spent_prepared_start_ids(cycle_id) == {"prepared-fresh-1"}
    assert store.unfinished_start_intent(cycle_id) is None
    read_model = store.read_model(cycle_id)
    assert read_model["attempt_count"] == 1
    assert [row["kind"] for row in read_model["attempts"]] == ["start_intent", "start_result"]
    assert read_model["lease"]["owner_id"] == "test-owner"
    assert read_model["lease"]["released_at"]


def test_store_rejects_corrupt_or_nonsequential_event_log(tmp_path: Path) -> None:
    store = PaperSupervisorStore(tmp_path)
    cycle_id = "2026-07-30_DAY"
    path = store.events_path(cycle_id)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "paper-supervisor-event-v1",
                "cycle_id": cycle_id,
                "sequence": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PaperSupervisorStoreError, match="attempt_store_corrupt"):
        store.events(cycle_id)


def test_lease_is_single_flight_and_lock_file_is_not_replaced(tmp_path: Path) -> None:
    store = PaperSupervisorStore(tmp_path)
    cycle_id = "2026-07-30_DAY"

    with store.lease(cycle_id=cycle_id, owner_id="first"):
        lock_inode = store.lock_path.stat().st_ino
        with pytest.raises(PaperSupervisorLeaseHeld, match="paper_supervisor_lease_held"):
            with store.lease(cycle_id=cycle_id, owner_id="second"):
                pass
        assert store.lock_path.stat().st_ino == lock_inode

    with store.lease(cycle_id=cycle_id, owner_id="second"):
        assert store.lock_path.stat().st_ino == lock_inode


def test_observations_are_durable_daily_records(tmp_path: Path) -> None:
    store = PaperSupervisorStore(tmp_path)
    record = store.append_observation(
        "2026-07-30_DAY",
        observed_at="2026-07-30T01:02:03Z",
        healthy_running=True,
        fields={"plan_fingerprint": "plan-sha", "order_count": 10},
    )

    assert record["healthy_running"] is True
    stored = json.loads(store.observations_path("2026-07-30").read_text(encoding="utf-8"))
    assert stored["order_count"] == 10
