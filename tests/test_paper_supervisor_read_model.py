from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import services.paper_supervisor_read_model as supervisor_read_model_module
from services.control_audit import (
    append_control_event,
    build_control_event,
)
from services.dualtrack_clock import cycle_window
from services.paper_supervisor_evidence import (
    draft_running_evidence,
    finalize_running_evidence,
)
from services.paper_supervisor_read_model import (
    _utilization_window,
    build_paper_supervisor_history_response,
    build_paper_supervisor_polling_summary,
    build_paper_supervisor_read_model,
    build_paper_supervisor_utilization,
)
from services.paper_supervisor_store import PaperSupervisorStore


UTC = timezone.utc
AS_OF = datetime(2026, 7, 31, 12, tzinfo=UTC)
CYCLE = cycle_window(AS_OF).cycle_id
FINGERPRINT = "a" * 64


def _evidence(
    at: datetime,
    *,
    running: bool = True,
) -> dict:
    cycle_id = cycle_window(at).cycle_id
    plan_id = f"strategy-plan-{cycle_id}-1-proof"
    expected = [
        {
            "slot_id": "slot-1",
            "initial_command_id": "command-1",
            "expected_fingerprint": FINGERPRINT,
            "authorized_commands": [
                {
                    "command_id": "command-1",
                    "fingerprint": FINGERPRINT,
                    "side": "buy",
                    "quantity": "1",
                    "price": "4000",
                    "generation": 1,
                    "economics": {
                        "event": "entry",
                        "symbol": "GOLD",
                        "order_type": "limit",
                        "notional": "4000",
                        "sl": "3900",
                        "tp": "4100",
                        "strategy_plan_id": plan_id,
                        "strategy_plan_version": 1,
                    },
                }
            ],
        }
    ]
    current = [
        {
            "slot_id": "slot-1",
            "representative_kind": "accepted_order",
            "representative_id": "command-1",
            "command": {
                "command_id": "command-1",
                "fingerprint": FINGERPRINT,
                "side": "buy",
                "quantity": "1",
                "price": "4000",
                "generation": 1,
                "economics": {
                    "event": "entry",
                    "symbol": "GOLD",
                    "order_type": "limit",
                    "notional": "4000",
                    "sl": "3900",
                    "tp": "4100",
                    "strategy_plan_id": plan_id,
                    "strategy_plan_version": 1,
                },
            },
            "ancestry": [
                {
                    "generation": 1,
                    "command_id": "command-1",
                    "rearm_of_order_id": None,
                    "lifecycle_status": "initial",
                    "reorder_order_id": None,
                }
            ],
            "position": None,
        }
    ]
    draft = draft_running_evidence(
        cycle_id=cycle_id,
        evidence_at=at.isoformat(),
        heartbeat_recorded_at=at.isoformat(),
        heartbeat_digest="b" * 64,
        authority_status="available",
        plan_identity={
            "strategy_plan_id": plan_id,
            "strategy_plan_version": 1,
            "strategy_type": "grid",
            "direction": "neutral",
        },
        runtime={
            "cycle_id": cycle_id,
            "strategy_plan_id": plan_id,
            "strategy_plan_version": 1,
            "desired_state": "running" if running else "stopped",
            "actual_state": "running" if running else "stopped",
            "accepted_order_count": 1 if running else 0,
        },
        expected_slots=expected,
        current_slots=current if running else [],
        reconciliation={"execution": "ok", "accounting": "pass"},
    )
    return finalize_running_evidence(
        draft,
        persisted_at=at.isoformat(),
    )


def _rows(
    start: datetime,
    end: datetime,
    *,
    step_seconds: int = 120,
    running: bool = True,
) -> list[dict]:
    rows = []
    cursor = start
    sequence_by_cycle: dict[str, int] = {}
    while cursor <= end:
        cycle_id = cycle_window(cursor).cycle_id
        sequence_by_cycle[cycle_id] = (
            sequence_by_cycle.get(cycle_id, 0) + 1
        )
        rows.append(
            {
                "cycle_id": cycle_id,
                "sequence": sequence_by_cycle[cycle_id],
                "payload": {
                    "running_evidence": _evidence(
                        cursor,
                        running=running,
                    )
                },
            }
        )
        cursor += timedelta(seconds=step_seconds)
    return rows


def test_utilization_counts_only_adjacent_same_cycle_proof() -> None:
    start = AS_OF - timedelta(hours=24)
    result = _utilization_window(
        _rows(start, AS_OF),
        start=start,
        end=AS_OF,
        source_errors=[],
    )

    assert result["evidence_status"] == "complete"
    assert result["percentage"] is not None
    assert 99.0 < result["percentage"] < 100.0
    assert result["missing_seconds"] == 0
    assert result["zero_interval_count"] >= 2


def test_unknown_and_missing_intervals_count_as_zero() -> None:
    start = AS_OF - timedelta(minutes=10)
    rows = _rows(start, AS_OF)
    rows[2]["payload"]["running_evidence"] = _evidence(
        start + timedelta(minutes=4),
        running=False,
    )
    del rows[3]

    result = _utilization_window(
        rows,
        start=start,
        end=AS_OF,
        source_errors=[],
    )

    assert result["evidence_status"] == "insufficient"
    assert result["running_seconds"] is None
    assert result["conservative_running_seconds"] < 10 * 60
    assert result["missing_seconds"] >= 4 * 60
    assert result["zero_interval_count"] >= 2
    assert "interior_evidence_gap" in result["insufficient_reasons"]


def test_incomplete_window_stays_insufficient_without_head_extrapolation() -> None:
    start = AS_OF - timedelta(hours=24)
    result = _utilization_window(
        _rows(start + timedelta(hours=1), AS_OF),
        start=start,
        end=AS_OF,
        source_errors=[],
    )

    assert result["evidence_status"] == "insufficient"
    assert result["percentage"] is None
    assert result["conservative_percentage"] < 100
    assert "window_head_missing" in result["insufficient_reasons"]


def test_control_events_never_create_running_utilization(
    tmp_path: Path,
) -> None:
    append_control_event(
        tmp_path,
        build_control_event(
            cycle_id=CYCLE,
            action="start",
            actor=None,
            payload={},
            result="accepted",
            error=None,
            runtime={
                "actual_state": "running",
                "desired_state": "running",
            },
            now=(AS_OF - timedelta(hours=12)).isoformat(),
        ),
    )

    result = build_paper_supervisor_utilization(
        tmp_path,
        as_of=AS_OF,
    )

    assert result["source"] == "paper_supervisor_running_evidence"
    assert result["windows"]["24h"]["evidence_status"] == "insufficient"
    assert result["windows"]["24h"]["percentage"] is None
    assert (
        result["windows"]["24h"]["conservative_running_seconds"]
        == 0
    )


def test_utilization_uses_observation_snapshot_reader(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "services.paper_supervisor_read_model._cycle_ids_for_window",
        lambda _start, _end: [CYCLE],
    )

    def read_observation_snapshot(
        _store: PaperSupervisorStore,
        cycle_id: str,
    ) -> list[dict]:
        calls.append(cycle_id)
        return []

    monkeypatch.setattr(
        PaperSupervisorStore,
        "read_cycle_observation_snapshot",
        read_observation_snapshot,
    )
    monkeypatch.setattr(
        PaperSupervisorStore,
        "read_cycle_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("utilization must not build full cycle snapshots")
        ),
    )

    result = build_paper_supervisor_utilization(
        tmp_path,
        as_of=AS_OF,
    )

    assert calls == [CYCLE]
    assert result["windows"]["24h"]["evidence_status"] == "insufficient"


def test_current_cycle_exposes_complete_immutable_history(
    tmp_path: Path,
) -> None:
    store = PaperSupervisorStore(
        tmp_path,
        now=lambda: AS_OF,
    )
    with store.try_lease(CYCLE, holder_id="read-model-test") as lease:
        assert lease is not None
        lease.claim_tick(
            source_tick_key=f"{CYCLE}:heartbeat:1",
            heartbeat_digest="c" * 64,
            trust="fresh",
            claimed_at=AS_OF.isoformat(),
        )
        lease.record_typed_heartbeat(
            observation_id="heartbeat-observation-1",
            observed_at=AS_OF.isoformat(),
            status="fresh",
            machine_code="heartbeat_fresh",
            reason="complete",
            heartbeat_recorded_at=AS_OF.isoformat(),
            heartbeat_digest="c" * 64,
        )
        lease.record_pre_intent_started(
            attempt_id="supervisor-attempt-1",
            observed_at=AS_OF.isoformat(),
            phase_scope="create_or_prepare",
        )
        lease.record_pre_intent_finished(
            attempt_id="supervisor-attempt-1",
            result="transient",
            machine_code="prepared_start_market_moved",
            classification="transient",
            observed_at=AS_OF.isoformat(),
        )
        store.commit_episode_observation(
            lease,
            state={
                "cycle_id": CYCLE,
                "mode": "backing_off",
                "events": [],
                "blocker": None,
                "alert_required": False,
            },
            payload={
                "status": "backing_off",
                "observed_at": AS_OF.isoformat(),
                "machine_code": "prepared_start_market_moved",
                "classification": "transient",
                "running_evidence": {
                    key: value
                    for key, value in _evidence(
                        AS_OF,
                        running=False,
                    ).items()
                    if key
                    not in {
                        "persisted_at",
                        "running_proven",
                        "proof_status",
                        "expected_slot_digest",
                    }
                },
            },
        )

    result = build_paper_supervisor_read_model(
        tmp_path,
        cycle_id=CYCLE,
        as_of=AS_OF,
    )
    current = result["current_cycle"]

    assert result["status"] == "available"
    assert current["attempt_count"] == 1
    assert current["last_attempt"]["machine_code"] == (
        "prepared_start_market_moved"
    )
    assert current["history"]["event_count"] == 4
    assert current["history"]["observation_count"] == 1
    assert len(current["history"]["events"]) == 4
    assert len(current["history"]["observations"]) == 1

    # The compact episode checkpoint names this observation hash.  Losing the
    # otherwise well-formed JSONL line must therefore make the read model
    # unavailable instead of quietly publishing a shortened history.
    (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "convergence"
        / "observations"
        / f"{CYCLE}.jsonl"
    ).unlink()
    deleted = build_paper_supervisor_read_model(
        tmp_path,
        cycle_id=CYCLE,
        as_of=AS_OF,
    )

    assert deleted["status"] == "unavailable"
    assert deleted["source_errors"] == [
        {
            "cycle_id": CYCLE,
            "machine_code": "attempt_store_corrupt",
        }
    ]


def test_polling_summary_reads_only_checkpointed_tail_and_history_is_explicit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = PaperSupervisorStore(tmp_path, now=lambda: AS_OF)
    with store.try_lease(CYCLE, holder_id="bounded-poll-test") as lease:
        assert lease is not None
        lease.claim_tick(
            source_tick_key=f"{CYCLE}:heartbeat:bounded",
            heartbeat_digest="d" * 64,
            trust="fresh",
            claimed_at=AS_OF.isoformat(),
        )
        lease.record_pre_intent_started(
            attempt_id="supervisor-attempt-bounded",
            observed_at=AS_OF.isoformat(),
            phase_scope="create_or_prepare",
        )
        lease.record_pre_intent_finished(
            attempt_id="supervisor-attempt-bounded",
            result="transient",
            machine_code="prepared_start_market_moved",
            classification="transient",
            observed_at=AS_OF.isoformat(),
        )
        store.commit_episode_observation(
            lease,
            state={
                "cycle_id": CYCLE,
                "mode": "backing_off",
                "episode": {
                    "episode_id": "episode-bounded",
                    "next_attempt_at": (AS_OF + timedelta(minutes=5)).isoformat(),
                },
                "budgets": {"clean_refusal_observations": 1},
                "events": [],
                "blocker": None,
                "alert_required": False,
            },
            payload={
                "status": "backing_off",
                "observed_at": AS_OF.isoformat(),
                "machine_code": "prepared_start_market_moved",
                "classification": "transient",
            },
        )

    monkeypatch.setattr(
        PaperSupervisorStore,
        "read_cycle_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("polling must not scan immutable history")
        ),
    )
    summary = build_paper_supervisor_polling_summary(
        tmp_path,
        cycle_id=CYCLE,
        as_of=AS_OF,
        count_summary={"attempt_count": 1, "start_intent_count": 0},
    )

    assert summary["status"] == "available"
    assert summary["current_cycle"]["attempt_count"] == 1
    assert summary["current_cycle"]["history"] == {
        "status": "available_on_demand",
        "complete": False,
        "endpoint": "/api/trading-system/supervisor-history",
        "cycle_id": CYCLE,
        "event_count": 3,
        "observation_count": 1,
        "tail_observation_sha256": summary["current_cycle"]["history"][
            "tail_observation_sha256"
        ],
    }

    monkeypatch.undo()
    monkeypatch.setattr(
        supervisor_read_model_module,
        "_build_utilization",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError(
                "explicit current-cycle history must not rebuild utilization"
            )
        ),
    )
    response = build_paper_supervisor_history_response(
        tmp_path,
        cycle_id=CYCLE,
        as_of=AS_OF,
    )
    assert response["schema_version"] == (
        "paper-supervisor-history-response-v2"
    )
    assert response["completeness"]["status"] == "complete"
    assert response["completeness"]["event_count"] == 3
    assert response["completeness"]["observation_count"] == 1
    assert len(
        response["supervisor"]["current_cycle"]["history"]["events"]
    ) == 3
    assert response["supervisor"]["schema_version"] == (
        "paper-supervisor-current-cycle-audit-v1"
    )
    assert "utilization" not in response["supervisor"]


def test_corrupt_observation_is_explicitly_unavailable(
    tmp_path: Path,
) -> None:
    path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "convergence"
        / "observations"
        / f"{CYCLE}.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text('{"truncated":', encoding="utf-8")

    result = build_paper_supervisor_read_model(
        tmp_path,
        cycle_id=CYCLE,
        as_of=AS_OF,
    )

    assert result["status"] == "unavailable"
    assert result["current_cycle"]["status"] == "unavailable"
    assert result["source_errors"][0]["cycle_id"] == CYCLE
    assert result["source_errors"][0]["machine_code"] in {
        "attempt_store_busy",
        "attempt_store_corrupt",
    }
