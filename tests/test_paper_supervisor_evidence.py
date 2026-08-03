from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from services.paper_supervisor_evidence import (
    RUNNING_EVIDENCE_SCHEMA_V1,
    RUNNING_EVIDENCE_SCHEMA_V2,
    RunningEvidenceError,
    draft_running_evidence,
    finalize_running_evidence,
    same_running_identity,
    validate_running_evidence,
)


T0 = datetime(2026, 7, 31, 8, tzinfo=timezone.utc)
CYCLE = "2026-07-31_DAY"
PLAN_ID = "strategy-plan-2026-07-31_DAY-1-proof"
FINGERPRINT = "a" * 64


def _draft(
    *,
    evidence_at: datetime = T0,
    heartbeat_at: datetime = T0,
    running: bool = True,
) -> dict:
    return draft_running_evidence(
        cycle_id=CYCLE,
        evidence_at=evidence_at.isoformat(),
        heartbeat_recorded_at=heartbeat_at.isoformat(),
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
                            "strategy_plan_id": PLAN_ID,
                            "strategy_plan_version": 1,
                        },
                    }
                ],
            }
        ],
        current_slots=(
            [
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
                            "strategy_plan_id": PLAN_ID,
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
            if running
            else []
        ),
        reconciliation={"execution": "ok", "accounting": "pass"},
    )


def test_store_seal_recomputes_running_proof() -> None:
    evidence = finalize_running_evidence(
        _draft(),
        persisted_at=T0.isoformat(),
    )

    assert evidence["running_proven"] is True
    assert evidence["proof_status"] == "proven"
    assert validate_running_evidence(evidence) == evidence


def test_forged_running_claim_is_rejected() -> None:
    evidence = finalize_running_evidence(
        _draft(running=False),
        persisted_at=T0.isoformat(),
    )
    forged = deepcopy(evidence)
    forged["running_proven"] = True
    forged["proof_status"] = "proven"

    with pytest.raises(
        RunningEvidenceError,
        match="running_evidence_invalid",
    ):
        validate_running_evidence(forged)


def test_stale_heartbeat_and_slow_persist_are_not_proven() -> None:
    stale = finalize_running_evidence(
        _draft(heartbeat_at=T0 - timedelta(seconds=181)),
        persisted_at=T0.isoformat(),
    )
    slow = finalize_running_evidence(
        _draft(),
        persisted_at=(T0 + timedelta(seconds=121)).isoformat(),
    )

    assert stale["running_proven"] is False
    assert stale["proof_status"] == "not_proven"
    assert slow["running_proven"] is False
    assert slow["proof_status"] == "not_proven"


def test_identity_comparison_requires_exact_plan_and_slots() -> None:
    left = finalize_running_evidence(
        _draft(),
        persisted_at=T0.isoformat(),
    )
    right = finalize_running_evidence(
        _draft(evidence_at=T0 + timedelta(seconds=60)),
        persisted_at=(T0 + timedelta(seconds=60)).isoformat(),
    )
    changed = deepcopy(right)
    changed["plan_identity"]["strategy_plan_id"] = (
        "strategy-plan-2026-07-31_DAY-2-other"
    )
    changed["runtime"]["strategy_plan_id"] = (
        "strategy-plan-2026-07-31_DAY-2-other"
    )

    assert same_running_identity(left, right) is True
    assert same_running_identity(left, changed) is False


def test_running_proof_rejects_position_from_another_plan() -> None:
    draft = _draft()
    current = draft["current_slots"][0]
    current["representative_kind"] = "open_position"
    current["representative_id"] = "trade-1"
    current["position"] = {
        "trade_id": "command-1",
        "strategy_plan_id": "strategy-plan-other",
        "strategy_plan_version": 1,
        "side": "long",
        "order_quantity": "1",
    }
    evidence = finalize_running_evidence(
        draft,
        persisted_at=T0.isoformat(),
    )

    assert evidence["running_proven"] is False


def test_running_proof_accepts_filled_slot_as_exact_open_position() -> None:
    draft = _draft()
    current = draft["current_slots"][0]
    current["representative_kind"] = "open_position"
    current["representative_id"] = "command-1"
    current["position"] = {
        "trade_id": "command-1",
        "strategy_plan_id": PLAN_ID,
        "strategy_plan_version": 1,
        "side": "long",
        "order_quantity": "1",
    }

    evidence = finalize_running_evidence(
        draft,
        persisted_at=T0.isoformat(),
    )

    assert evidence["schema_version"] == RUNNING_EVIDENCE_SCHEMA_V2
    assert evidence["running_proven"] is True


def test_legacy_v1_filled_slot_keeps_its_original_derivation() -> None:
    draft = _draft()
    current = draft["current_slots"][0]
    current["representative_kind"] = "open_position"
    current["representative_id"] = "command-1"
    current["position"] = {
        "trade_id": "command-1",
        "strategy_plan_id": PLAN_ID,
        "strategy_plan_version": 1,
        "side": "long",
        "order_quantity": "1",
    }
    # This is the exact v1 interpretation: only currently accepted open
    # orders contributed to the persisted accepted-order count.
    draft["runtime"]["accepted_order_count"] = 0
    draft["schema_version"] = RUNNING_EVIDENCE_SCHEMA_V1
    draft["persisted_at"] = T0.isoformat()
    draft["expected_slot_digest"] = hashlib.sha256(
        json.dumps(
            draft["expected_slots"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    draft["running_proven"] = True
    draft["proof_status"] = "proven"

    assert validate_running_evidence(draft) == draft

    forged = deepcopy(draft)
    forged["runtime"]["accepted_order_count"] = 1
    with pytest.raises(RunningEvidenceError, match="running_evidence_invalid"):
        validate_running_evidence(forged)


def test_unknown_evidence_schema_fails_closed() -> None:
    evidence = finalize_running_evidence(
        _draft(),
        persisted_at=T0.isoformat(),
    )
    evidence["schema_version"] = "paper-supervisor-running-evidence-v999"

    with pytest.raises(RunningEvidenceError, match="running_evidence_invalid"):
        validate_running_evidence(evidence)


def test_writer_refuses_to_emit_legacy_v1_evidence() -> None:
    draft = _draft()
    draft["schema_version"] = RUNNING_EVIDENCE_SCHEMA_V1

    with pytest.raises(
        RunningEvidenceError,
        match="running_evidence_schema_not_current",
    ):
        finalize_running_evidence(draft, persisted_at=T0.isoformat())


def test_running_proof_requires_latest_authorized_rearm_with_same_economics() -> None:
    draft = _draft()
    expected = draft["expected_slots"][0]["authorized_commands"]
    expected.append(
        {
            "command_id": "command-2",
            "fingerprint": "c" * 64,
            "side": "buy",
            "quantity": "1",
            "price": "4000",
            "generation": 2,
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
    )
    stale = finalize_running_evidence(
        deepcopy(draft),
        persisted_at=T0.isoformat(),
    )
    changed_economics = deepcopy(draft)
    changed_authorized = changed_economics["expected_slots"][0][
        "authorized_commands"
    ]
    changed_authorized[1]["economics"]["tp"] = "9000"
    current = changed_economics["current_slots"][0]
    current["representative_id"] = "command-2"
    current["command"] = deepcopy(changed_authorized[1])
    current["ancestry"] = [
        {
            "generation": 1,
            "command_id": "command-1",
            "rearm_of_order_id": None,
            "lifecycle_status": "completed_rearmed",
            "reorder_order_id": "command-2",
        },
        {
            "generation": 2,
            "command_id": "command-2",
            "rearm_of_order_id": "command-1",
            "lifecycle_status": "active",
            "reorder_order_id": None,
        },
    ]
    changed = finalize_running_evidence(
        changed_economics,
        persisted_at=T0.isoformat(),
    )

    assert stale["running_proven"] is False
    assert changed["running_proven"] is False
