from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.paper_supervisor_store import (
    PaperSupervisorStore,
    StartAuthoritySnapshot,
    SupervisorStoreError,
)
from services.paper_supervisor_evidence import (
    RUNNING_EVIDENCE_SCHEMA_V1,
    RUNNING_EVIDENCE_SCHEMA_V2,
    draft_running_evidence,
)


CYCLE = "2026-07-30_DAY"
PLAN = {
    "strategy_plan_id": "strategy-plan-2026-07-30_DAY-1",
    "strategy_plan_version": 1,
    "strategy_type": "grid",
    "direction": "neutral",
}
PREVIEW = "grid-preview-1"
PREPARED = "prepared-start-1"
FINGERPRINTS = [
    hashlib.sha256(f"order-{index}".encode()).hexdigest()
    for index in range(3)
]
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> PaperSupervisorStore:
    return PaperSupervisorStore(
        tmp_path / "outputs",
        now=lambda: NOW,
        hostname=lambda: "cloud-paper-1",
        pid=lambda: 42,
    )


def _running_evidence_draft() -> dict:
    command = {
        "command_id": "order-0",
        "fingerprint": FINGERPRINTS[0],
        "side": "buy",
        "quantity": "1",
        "price": "100",
        "generation": 1,
        "economics": {
            "event": "entry",
            "symbol": "GOLD",
            "order_type": "limit",
            "notional": "100",
            "sl": "90",
            "tp": "110",
            "strategy_plan_id": PLAN["strategy_plan_id"],
            "strategy_plan_version": PLAN["strategy_plan_version"],
        },
    }
    return draft_running_evidence(
        cycle_id=CYCLE,
        evidence_at=NOW.isoformat(),
        heartbeat_recorded_at=NOW.isoformat(),
        heartbeat_digest="b" * 64,
        authority_status="available",
        plan_identity=PLAN,
        runtime={
            "cycle_id": CYCLE,
            "strategy_plan_id": PLAN["strategy_plan_id"],
            "strategy_plan_version": PLAN["strategy_plan_version"],
            "desired_state": "running",
            "actual_state": "running",
            "accepted_order_count": 1,
        },
        expected_slots=[{
            "slot_id": "slot-1",
            "initial_command_id": "order-0",
            "expected_fingerprint": FINGERPRINTS[0],
            "authorized_commands": [command],
        }],
        current_slots=[{
            "slot_id": "slot-1",
            "representative_kind": "accepted_order",
            "representative_id": "order-0",
            "command": command,
            "ancestry": [{
                "generation": 1,
                "command_id": "order-0",
                "rearm_of_order_id": None,
                "lifecycle_status": "initial",
                "reorder_order_id": None,
            }],
            "position": None,
        }],
        reconciliation={"execution": "ok", "accounting": "pass"},
    )


def _intent(
    lease,
    *,
    attempt_id: str = "attempt-1",
    prepared_start_id: str = PREPARED,
) -> dict:
    return lease.record_start_intent(
        attempt_id=attempt_id,
        preview_id=PREVIEW,
        prepared_start_id=prepared_start_id,
        plan_identity=PLAN,
        expected_order_fingerprints=FINGERPRINTS,
    )


def _audit(
    *,
    result: str,
    error: str | None = None,
    prepared_start_id: str = PREPARED,
) -> dict:
    return {
        "schema_version": "strategy-control-event-v1",
        "cycle_id": CYCLE,
        "action": "start",
        "request": {
            "expected_preview_id": PREVIEW,
            "prepared_start_id": prepared_start_id,
        },
        "result": result,
        "error": error,
    }


def _authority(
    *,
    running: bool,
    active_plan: dict | None = None,
    fingerprints: list[str] | None = None,
    positions: int = 0,
    audit: list[dict] | None = None,
    reconciliation: dict | None = None,
) -> StartAuthoritySnapshot:
    runtime = {
        "actual_state": "running" if running else "stopped",
        "desired_state": "running" if running else "stopped",
        "strategy_plan_id": PLAN["strategy_plan_id"],
        "strategy_plan_version": PLAN["strategy_plan_version"],
        "preview_id": PREVIEW if running else None,
        "prepared_start_id": PREPARED if running else None,
    }
    return StartAuthoritySnapshot(
        cycle_id=CYCLE,
        active_plan=active_plan or PLAN,
        runtime=runtime,
        accepted_order_fingerprints=(
            list(FINGERPRINTS)
            if fingerprints is None and running
            else list(fingerprints or [])
        ),
        accepted_order_identities=(
            [
                {
                    "order_id": f"order-{index}",
                    "fingerprint": fingerprint,
                    "side": "buy",
                    "quantity": "1",
                }
                for index, fingerprint in enumerate(
                    list(FINGERPRINTS)
                    if fingerprints is None and running
                    else list(fingerprints or [])
                )
            ]
        ),
        authorized_order_identities=(
            [
                {
                    "order_id": f"order-{index}",
                    "fingerprint": fingerprint,
                    "side": "buy",
                    "quantity": "1",
                }
                for index, fingerprint in enumerate(
                    list(FINGERPRINTS)
                    if fingerprints is None and running
                    else list(fingerprints or [])
                )
            ]
        ),
        open_position_identities=(
            [
                {
                    "position_id": f"position-{index}",
                    "trade_id": f"order-{index}",
                    "entry_fill_ids": [f"fill-{index}"],
                    "strategy_plan_id": PLAN["strategy_plan_id"],
                    "strategy_plan_version": (
                        PLAN["strategy_plan_version"]
                    ),
                    "side": "long",
                    "entry_price": "100",
                    "entry_quantity": "1",
                    "order_quantity": "1",
                }
                for index in range(positions)
            ]
        ),
        open_position_count=positions,
        reconciliation=(
            reconciliation
            if reconciliation is not None
            else {"execution": "ok", "accounting": "pass"}
        ),
        control_events=(
            audit
            if audit is not None
            else [_audit(result="accepted" if running else "rejected")]
        ),
    )


def test_stable_nonblocking_lease_and_projection_do_not_replace_lock_inode(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        inode = store.lock_path.stat().st_ino
        code = (
            "from pathlib import Path\n"
            "from services.paper_supervisor_store import PaperSupervisorStore\n"
            f"s=PaperSupervisorStore(Path({str(tmp_path / 'outputs')!r}))\n"
            f"with s.try_lease({CYCLE!r}, holder_id='owner-b') as lease:\n"
            " print('acquired' if lease else 'lease_held')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=True,
        )
        assert result.stdout.strip() == "lease_held"
    with store.try_lease(CYCLE, holder_id="owner-c") as next_lease:
        assert next_lease is not None
        assert store.lock_path.stat().st_ino == inode
    projection = json.loads(store.lease_path.read_text(encoding="utf-8"))
    assert projection["status"] == "released"
    assert projection["lock_inode"] == inode
    assert projection["lock_inode_is_stable"] is True


def test_start_intent_is_fsynced_before_operation_and_result_is_projected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    real_fsync = os.fsync
    fsync_calls: list[int] = []

    def observed_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observed_fsync)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        before = len(fsync_calls)

        def operation() -> dict:
            assert len(fsync_calls) > before
            events = store.events(CYCLE)
            assert [row["event_type"] for row in events] == [
                "start_intent"
            ]
            assert (
                events[0]["payload"]["prepared_start_id"] == PREPARED
            )
            return {"result": "accepted", "accepted_orders": 3}

        lease.execute_start(
            attempt_id="attempt-1",
            preview_id=PREVIEW,
            prepared_start_id=PREPARED,
            plan_identity=PLAN,
            expected_order_fingerprints=FINGERPRINTS,
            operation=operation,
        )

    events = store.events(CYCLE)
    assert [row["event_type"] for row in events] == [
        "start_intent",
        "start_result",
    ]
    assert events[1]["previous_event_sha256"] == events[0][
        "event_sha256"
    ]
    state = store.current_state(CYCLE)
    assert state["unfinished_intent"] is None
    assert state["spent_prepared_start_ids"] == [PREPARED]
    assert state["attempts"][0]["terminal_result"] == "accepted"


def test_crash_after_intent_permanently_spends_prepared_id_and_blocks_replay(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None

        def lost_response() -> dict:
            calls.append("called")
            raise TimeoutError("response lost")

        with pytest.raises(TimeoutError, match="response lost"):
            lease.execute_start(
                attempt_id="attempt-1",
                preview_id=PREVIEW,
                prepared_start_id=PREPARED,
                plan_identity=PLAN,
                expected_order_fingerprints=FINGERPRINTS,
                operation=lost_response,
            )
    assert calls == ["called"]
    assert store.unfinished_intent(CYCLE)["attempt_id"] == "attempt-1"

    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        with pytest.raises(
            SupervisorStoreError,
            match="unfinished_start_intent_requires_recovery",
        ):
            lease.execute_start(
                attempt_id="attempt-2",
                preview_id="grid-preview-2",
                prepared_start_id=PREPARED,
                plan_identity=PLAN,
                expected_order_fingerprints=FINGERPRINTS,
                operation=lambda: calls.append("replayed") or {
                    "result": "accepted"
                },
            )
    assert calls == ["called"]


def test_resolved_intent_still_permanently_spends_prepared_id(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        lease.execute_start(
            attempt_id="attempt-1",
            preview_id=PREVIEW,
            prepared_start_id=PREPARED,
            plan_identity=PLAN,
            expected_order_fingerprints=FINGERPRINTS,
            operation=lambda: {"result": "rejected"},
        )
    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        with pytest.raises(
            SupervisorStoreError,
            match="prepared_start_id_already_spent",
        ):
            lease.execute_start(
                attempt_id="attempt-2",
                preview_id="grid-preview-2",
                prepared_start_id=PREPARED,
                plan_identity=PLAN,
                expected_order_fingerprints=FINGERPRINTS,
                operation=lambda: calls.append("replayed") or {
                    "result": "accepted"
                },
            )
    assert calls == []


def test_unfinished_intent_recovers_executed_only_from_all_exact_authorities(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        _intent(lease)

    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        result = lease.recover_unfinished_intent(_authority(running=True))

    assert result["resolution"] == "executed"
    assert result["orders_created_by_recovery"] == 0
    assert result["same_prepared_start_retry_allowed"] is False
    assert result["fresh_attempt_classification_required"] is False
    assert store.unfinished_intent(CYCLE) is None
    assert store.current_state(CYCLE)["attempts"][0][
        "terminal_result"
    ] == "executed"


def test_unfinished_intent_recovers_exact_immediate_fill_as_executed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        _intent(lease)
    authority = replace(
        _authority(running=True, fingerprints=FINGERPRINTS[:2]),
        authorized_order_identities=[
            {
                "order_id": f"order-{index}",
                "fingerprint": fingerprint,
                "side": "buy",
                "quantity": "1",
            }
            for index, fingerprint in enumerate(FINGERPRINTS)
        ],
        open_position_identities=[
            {
                "position_id": "position-2",
                "trade_id": "order-2",
                "entry_fill_ids": ["fill-2"],
                "strategy_plan_id": PLAN["strategy_plan_id"],
                "strategy_plan_version": PLAN[
                    "strategy_plan_version"
                ],
                "side": "long",
                "entry_price": "100",
                "entry_quantity": "1",
                "order_quantity": "1",
            }
        ],
        open_position_count=1,
    )

    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        result = lease.recover_unfinished_intent(authority)

    assert result["resolution"] == "executed"
    assert result["orders_created_by_recovery"] == 0


def test_unfinished_intent_never_ignores_an_extra_open_position(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        _intent(lease)

    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        result = lease.recover_unfinished_intent(
            _authority(running=True, positions=1)
        )

    assert result["resolution"] == "control_outcome_unknown"
    assert result["machine_code"] == "control_outcome_unknown"


def test_unfinished_intent_rejects_position_direction_authority_mismatch(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        _intent(lease)
    authority = replace(
        _authority(running=True, fingerprints=FINGERPRINTS[:2]),
        authorized_order_identities=[
            {
                "order_id": f"order-{index}",
                "fingerprint": fingerprint,
                "side": "buy",
                "quantity": "1",
            }
            for index, fingerprint in enumerate(FINGERPRINTS)
        ],
        open_position_identities=[
            {
                "position_id": "position-2",
                "trade_id": "order-2",
                "entry_fill_ids": ["fill-2"],
                "strategy_plan_id": PLAN["strategy_plan_id"],
                "strategy_plan_version": PLAN[
                    "strategy_plan_version"
                ],
                "side": "short",
                "entry_price": "100",
                "entry_quantity": "1",
                "order_quantity": "1",
            }
        ],
        open_position_count=1,
    )

    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        result = lease.recover_unfinished_intent(authority)

    assert result["resolution"] == "control_outcome_unknown"
    assert result["machine_code"] == "control_outcome_unknown"


def test_proven_zero_order_rejection_is_clean_but_never_replays(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        _intent(lease)
    authority = _authority(
        running=False,
        audit=[
            _audit(
                result="rejected",
                error="prepared_start_market_moved",
            )
        ],
    )
    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        result = lease.recover_unfinished_intent(authority)

    assert result == {
        "resolution": "clean_rejection",
        "machine_code": "prepared_start_market_moved",
        "authority_digest": result["authority_digest"],
        "same_prepared_start_retry_allowed": False,
        "fresh_attempt_classification_required": True,
        "orders_created_by_recovery": 0,
    }
    assert len(result["authority_digest"]) == 64


def test_dca_clean_rejection_allows_exactly_empty_pre_start_plan(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    dca_plan = {
        **PLAN,
        "strategy_type": "dca",
        "direction": "long",
    }
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        lease.record_start_intent(
            attempt_id="attempt-dca-1",
            preview_id=PREVIEW,
            prepared_start_id=PREPARED,
            plan_identity=dca_plan,
            pre_start_plan_identity=None,
            expected_order_fingerprints=FINGERPRINTS,
        )
    authority = StartAuthoritySnapshot(
        cycle_id=CYCLE,
        active_plan={},
        runtime={
            "actual_state": "stopped",
            "desired_state": "stopped",
            "strategy_plan_id": None,
            "strategy_plan_version": None,
            "preview_id": None,
            "prepared_start_id": None,
        },
        accepted_order_fingerprints=[],
        open_position_count=0,
        reconciliation={"execution": "ok", "accounting": "pass"},
        control_events=[
            _audit(
                result="rejected",
                error="prepared_start_market_moved",
            )
        ],
    )

    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        result = lease.recover_unfinished_intent(authority)

    assert result["resolution"] == "clean_rejection"
    assert result["orders_created_by_recovery"] == 0


@pytest.mark.parametrize(
    "authority",
    [
        _authority(running=True, fingerprints=FINGERPRINTS[:2]),
        _authority(running=True, audit=[]),
        _authority(
            running=False,
            audit=[
                _audit(
                    result="rejected",
                    error="prepared_start_market_moved",
                )
            ],
            positions=1,
        ),
        _authority(
            running=False,
            audit=[
                _audit(
                    result="rejected",
                    error="prepared_start_market_moved",
                )
            ],
            reconciliation={"execution": "drift", "accounting": "pass"},
        ),
        _authority(
            running=False,
            audit=[
                _audit(
                    result="rejected",
                    error="prepared_start_market_moved",
                ),
                _audit(
                    result="accepted",
                    error=None,
                ),
            ],
        ),
        _authority(
            running=True,
            active_plan={**PLAN, "direction": "long"},
        ),
    ],
)
def test_ambiguous_partial_or_conflicting_authority_is_structural_unknown(
    tmp_path: Path,
    authority: StartAuthoritySnapshot,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        _intent(lease)
    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        result = lease.recover_unfinished_intent(authority)

    assert result["resolution"] == "control_outcome_unknown"
    assert result["machine_code"] == "control_outcome_unknown"
    assert result["same_prepared_start_retry_allowed"] is False
    assert result["fresh_attempt_classification_required"] is False
    assert result["orders_created_by_recovery"] == 0


def test_valid_event_log_deterministically_recovers_from_stale_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    original = store._write_state
    writes = 0

    def crash_before_projection(cycle_id: str, state: dict) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise SupervisorStoreError("simulated_projection_crash")
        original(cycle_id, state)

    monkeypatch.setattr(store, "_write_state", crash_before_projection)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        with pytest.raises(
            SupervisorStoreError,
            match="simulated_projection_crash",
        ):
            _intent(lease)

    recovered = _store(tmp_path).current_state(CYCLE)
    assert recovered["last_sequence"] == 1
    assert recovered["unfinished_intent"]["attempt_id"] == "attempt-1"
    assert recovered["spent_prepared_start_ids"] == [PREPARED]


@pytest.mark.parametrize(
    "target",
    ["events_json", "event_sequence", "state"],
)
def test_corrupt_or_nonsequential_store_fails_closed(
    tmp_path: Path,
    target: str,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        _intent(lease)
    if target == "events_json":
        path = store.root / "events" / f"{CYCLE}.jsonl"
        path.write_text("{\n", encoding="utf-8")
    elif target == "event_sequence":
        path = store.root / "events" / f"{CYCLE}.jsonl"
        event = json.loads(path.read_text(encoding="utf-8"))
        event["sequence"] = 2
        event["event_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in event.items()
                    if key != "event_sha256"
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        path.write_text(
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        path = store.root / "states" / f"{CYCLE}.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["last_sequence"] = 99
        state["state_digest"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in state.items()
                    if key != "state_digest"
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(SupervisorStoreError, match="attempt_store_corrupt"):
        store.current_state(CYCLE)


def test_two_processes_can_invoke_the_injected_start_only_once(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "outputs"
    marker = tmp_path / "operation-calls.txt"
    repo = Path(__file__).resolve().parents[1]
    code = f"""
import os
import time
from pathlib import Path
from services.paper_supervisor_store import PaperSupervisorStore, SupervisorStoreError

store = PaperSupervisorStore(Path({str(output_root)!r}))
with store.try_lease({CYCLE!r}, holder_id=f"worker-{{os.getpid()}}") as lease:
    if lease is not None:
        def operation():
            with Path({str(marker)!r}).open("a", encoding="utf-8") as handle:
                handle.write("called\\n")
                handle.flush()
                os.fsync(handle.fileno())
            time.sleep(0.2)
            return {{"result": "accepted"}}
        try:
            lease.execute_start(
                attempt_id=f"attempt-{{os.getpid()}}",
                preview_id={PREVIEW!r},
                prepared_start_id={PREPARED!r},
                plan_identity={PLAN!r},
                expected_order_fingerprints={FINGERPRINTS!r},
                operation=operation,
            )
        except SupervisorStoreError:
            pass
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=10) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], results
    assert marker.read_text(encoding="utf-8").splitlines() == ["called"]
    events = PaperSupervisorStore(output_root).events(CYCLE)
    assert [row["event_type"] for row in events] == [
        "start_intent",
        "start_result",
    ]


def test_observation_append_rejects_deleted_middle_link(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        for index in range(3):
            store.append_observation(
                lease,
                {"status": "healthy", "sequence": index + 1},
            )
    path = store.root / "observations" / f"{CYCLE}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join([lines[0], lines[2]]) + "\n",
        encoding="utf-8",
    )

    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        with pytest.raises(
            SupervisorStoreError,
            match="attempt_store_corrupt",
        ):
            store.append_observation(
                lease,
                {"status": "healthy", "sequence": 4},
            )


def test_observation_chain_reads_mixed_v1_and_v2_running_evidence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        first = store.append_observation(
            lease,
            {"running_evidence": _running_evidence_draft()},
        )

    path = store.root / "observations" / f"{CYCLE}.jsonl"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["payload"]["running_evidence"]["schema_version"] = (
        RUNNING_EVIDENCE_SCHEMA_V1
    )
    legacy["observation_sha256"] = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in legacy.items()
                if key != "observation_sha256"
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        second = store.append_observation(
            lease,
            {"running_evidence": _running_evidence_draft()},
        )

    rows = store.observations(CYCLE)
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert rows[0]["payload"]["running_evidence"]["schema_version"] == (
        RUNNING_EVIDENCE_SCHEMA_V1
    )
    assert rows[1]["payload"]["running_evidence"]["schema_version"] == (
        RUNNING_EVIDENCE_SCHEMA_V2
    )


def test_episode_checkpoint_rejects_deleted_observation_tail(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = {"cycle_id": CYCLE, "mode": "ready"}
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        state = store.commit_episode_observation(
            lease,
            state=state,
            payload={"status": "healthy", "sequence": 1},
        )
        store.commit_episode_observation(
            lease,
            state=state,
            payload={"status": "healthy", "sequence": 2},
        )
    path = store.root / "observations" / f"{CYCLE}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n", encoding="utf-8")

    with pytest.raises(
        SupervisorStoreError,
        match="attempt_store_corrupt",
    ):
        store.episode_state(CYCLE)


def test_episode_recovers_append_when_checkpoint_write_crashes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    original_write = store.write_episode_state
    store.write_episode_state = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("simulated checkpoint crash")
    )
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        with pytest.raises(OSError, match="simulated checkpoint crash"):
            store.commit_episode_observation(
                lease,
                state={
                    "cycle_id": CYCLE,
                    "mode": "probing",
                    "episode": {
                        "consecutive_transient_failures": 5,
                    },
                },
                payload={"status": "backing_off"},
            )
    store.write_episode_state = original_write

    recovered = store.episode_state(CYCLE)

    assert recovered is not None
    assert recovered["mode"] == "probing"
    assert (
        recovered["episode"]["consecutive_transient_failures"]
        == 5
    )
    assert recovered["last_observation_sha256"]


def test_prepared_start_id_is_spent_across_cycles(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        _intent(lease)
    next_cycle = "2026-07-31_NIGHT"
    with store.try_lease(next_cycle, holder_id="owner-b") as lease:
        assert lease is not None
        with pytest.raises(
            SupervisorStoreError,
            match="prepared_start_id_already_spent",
        ):
            lease.record_start_intent(
                attempt_id="attempt-2",
                preview_id="grid-preview-2",
                prepared_start_id=PREPARED,
                plan_identity=PLAN,
                expected_order_fingerprints=FINGERPRINTS,
            )


def test_recovery_rejects_receipt_ids_not_bound_to_authoritative_commands(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.try_lease(CYCLE, holder_id="owner-a") as lease:
        assert lease is not None
        _intent(lease)
    authority = _authority(running=True)
    forged = replace(
        authority,
        accepted_order_identities=[
            {
                "order_id": f"forged-{index}",
                "fingerprint": fingerprint,
                "side": "buy",
                "quantity": "1",
            }
            for index, fingerprint in enumerate(FINGERPRINTS)
        ],
    )

    with store.try_lease(CYCLE, holder_id="owner-b") as lease:
        assert lease is not None
        result = lease.recover_unfinished_intent(forged)

    assert result is not None
    assert result["resolution"] == "control_outcome_unknown"
