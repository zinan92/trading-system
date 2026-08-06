from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.journal_store import load_json, write_json
from services.paper_degradation_events import (
    PaperDegradationEventStore,
    PaperDegradationEvidenceError,
    build_continuity_evidence_from_observations,
)
from services.paper_supervisor_evidence import (
    draft_running_evidence,
    finalize_running_evidence,
)

CYCLE = "2026-08-06_DAY"
T0 = datetime(2026, 8, 6, 1, tzinfo=timezone.utc)
PLAN_ID = "strategy-plan-2026-08-06_DAY-1-proof"
FINGERPRINT = "a" * 64


def _event(store: PaperDegradationEventStore) -> dict:
    return store.record(
        event_id="degradation-attempt-1-market-moved",
        cycle_id=CYCLE,
        execution_profile="paper_continuous",
        bypassed_gate="prepared_start_market_gate",
        original_machine_code="prepared_start_market_moved",
        original_reason="prepared price no longer matches authority",
        alternative_action="discard_and_recompute_fresh_preview",
        occurred_at=T0.isoformat(),
    )


def test_degradation_event_is_hash_linked_and_idempotent(
    tmp_path: Path,
) -> None:
    store = PaperDegradationEventStore(tmp_path / "outputs")

    first = _event(store)
    second = _event(store)
    evidence = store.cycle_evidence(CYCLE)

    assert first == second
    assert evidence["events"] == [first]
    assert evidence["event_count"] == 1
    assert evidence["tail_event_digest"] == first["event_digest"]
    assert len(evidence["events_digest"]) == 64


def test_concurrent_repeated_event_writes_only_one_row(
    tmp_path: Path,
) -> None:
    store = PaperDegradationEventStore(tmp_path / "outputs")

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _index: _event(store), range(24)))

    assert len({row["event_digest"] for row in rows}) == 1
    assert len(store.events(CYCLE)) == 1


def test_reused_event_identity_with_different_facts_is_rejected(
    tmp_path: Path,
) -> None:
    store = PaperDegradationEventStore(tmp_path / "outputs")
    _event(store)

    with pytest.raises(
        PaperDegradationEvidenceError,
        match="paper_degradation_event_identity_conflict",
    ):
        store.record(
            event_id="degradation-attempt-1-market-moved",
            cycle_id=CYCLE,
            execution_profile="paper_continuous",
            bypassed_gate="prepared_start_market_gate",
            original_machine_code="prepared_start_market_moved",
            original_reason="different reason",
            alternative_action="discard_and_recompute_fresh_preview",
            occurred_at=T0.isoformat(),
        )


def test_tampered_degradation_history_fails_closed(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = PaperDegradationEventStore(output)
    _event(store)
    path = (
        output
        / "dualtrack"
        / "supervisor"
        / "degradation_events"
        / f"{CYCLE}.json"
    )
    rows = load_json(path)
    rows[0]["alternative_action"] = "silent_bypass"
    write_json(path, rows)

    with pytest.raises(
        PaperDegradationEvidenceError,
        match="paper_degradation_event_store_corrupt",
    ):
        store.events(CYCLE)


def test_continuity_evidence_proves_stopped_to_running_gap() -> None:
    stopped_at = T0
    running_at = T0 + timedelta(minutes=4, seconds=12)
    observations = [
        _observation(1, stopped_at, running=False, digest="1" * 64),
        _observation(2, running_at, running=True, digest="2" * 64),
    ]

    evidence = build_continuity_evidence_from_observations(
        observations,
        cycle_id=CYCLE,
    )

    assert evidence["transition_count"] == 1
    transition = evidence["transitions"][0]
    assert transition["stop_observed_at"] == stopped_at.isoformat()
    assert transition["running_proven_at"] == running_at.isoformat()
    assert transition["gap_seconds"] == 252
    assert transition["stop_observation_sha256"] == "1" * 64
    assert transition["running_observation_sha256"] == "2" * 64


def _observation(
    sequence: int,
    at: datetime,
    *,
    running: bool,
    digest: str,
) -> dict:
    draft = draft_running_evidence(
        cycle_id=CYCLE,
        evidence_at=at.isoformat(),
        heartbeat_recorded_at=at.isoformat(),
        heartbeat_digest="b" * 64,
        authority_status="available",
        plan_identity={
            "strategy_plan_id": PLAN_ID,
            "strategy_plan_version": 1,
            "strategy_type": "grid",
            "direction": "neutral",
        },
        runtime={
            "cycle_id": CYCLE,
            "strategy_plan_id": PLAN_ID,
            "strategy_plan_version": 1,
            "desired_state": "running" if running else "stopped",
            "actual_state": "running" if running else "stopped",
            "accepted_order_count": 1 if running else 0,
        },
        expected_slots=[
            {
                "slot_id": "slot-1",
                "initial_command_id": "command-1",
                "expected_fingerprint": FINGERPRINT,
                "authorized_commands": [_command()],
            }
        ],
        current_slots=(
            [
                {
                    "slot_id": "slot-1",
                    "representative_kind": "accepted_order",
                    "representative_id": "command-1",
                    "command": _command(),
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
            if running
            else []
        ),
        reconciliation={"execution": "ok", "accounting": "pass"},
    )
    evidence = finalize_running_evidence(
        draft,
        persisted_at=at.isoformat(),
    )
    return {
        "sequence": sequence,
        "cycle_id": CYCLE,
        "observation_sha256": digest,
        "payload": {"running_evidence": evidence},
    }


def _command() -> dict:
    return {
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
            "strategy_plan_id": PLAN_ID,
            "strategy_plan_version": 1,
        },
    }
