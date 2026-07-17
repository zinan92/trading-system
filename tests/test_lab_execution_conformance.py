from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from services.dualtrack_nautilus_parity_contract import FIXTURE_CLASSES, PLATFORM_PARITY_SCHEMA
from services.execution_conformance import (
    CANDIDATE_RECEIPT_SCHEMA,
    EXECUTION_SCENARIO_SCHEMA,
    build_candidate_execution_receipt,
)
from services.journal_store import write_json
from services.lab_execution_conformance import (
    LAB_EXECUTION_CONFORMANCE_TOKEN_SCHEMA,
    build_lab_execution_conformance_token,
    lab_execution_conformance_token_blockers,
    load_lab_execution_conformance,
)
from services.lab_promotion import promotion_gate


CYCLE_ID = "2026-07-18_DAY"
SCENARIO_ID = "execution-scenario-lab-a3"
CANDIDATE_ID = "grid-candidate-lab-a3"


def _entry() -> dict:
    return {
        "exp_id": "lab-grid-a3",
        "status": "valid",
        "family": "grid",
        "strategy_ref": {"strategy_id": "gold_grid"},
        "execution_candidate": {
            "scenario_id": SCENARIO_ID,
            "candidate_id": CANDIDATE_ID,
        },
        "holdout_consumed": True,
        "objective": {
            "walkforward": {"passed": True, "metrics": {"sortino": 1.2}},
            "holdout": {"passed": True, "metrics": {"sortino": 0.8}},
        },
    }


def _candidate_receipt() -> dict:
    scenario = {
        "schema_version": EXECUTION_SCENARIO_SCHEMA,
        "scenario_id": SCENARIO_ID,
        "candidate_id": CANDIDATE_ID,
        "cycle_id": CYCLE_ID,
        "plan_identity": {
            "strategy_plan_id": "plan-lab-a3",
            "strategy_plan_version": 1,
            "available_at": "2026-07-18T01:00:00+00:00",
        },
        "contracts": {
            "execution_contract_hash": "sha256:execution-a3",
            "fee_contract_hash": "sha256:fees-a3",
        },
        "hashes": {
            "plan_hash": "sha256:plan-a3",
            "command_hash": "sha256:commands-a3",
            "market_event_hash": "sha256:events-a3",
            "input_hash": "sha256:input-a3",
        },
    }
    plan_trace = {
        "strategy_plan_id": "plan-lab-a3",
        "strategy_plan_version": 1,
    }
    snapshot = {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_paper",
        "cycle_id": CYCLE_ID,
        "orders": [{
            "order_id": "order-lab-a3",
            "state": "accepted",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 100.0,
            "quantity": 1.0,
            "ts": "2026-07-18T01:00:00+00:00",
            **plan_trace,
        }],
        "fills": [],
        "positions": [],
        "account": {
            "starting_cash": 10_000.0,
            "realized_pnl": 0.0,
            "ending_cash": 10_000.0,
            "equity": 10_000.0,
            "margin": 0.0,
            "exposure": 0.0,
            "slippage": 0.0,
            "fees": 0.0,
            "funding": 0.0,
        },
        "pnl": {"realized": 0.0, "unrealized": 0.0},
        "capabilities": {"replay_version": "dualtrack-nautilus-replay-v6"},
    }
    reconciliation = {
        "schema_version": "dualtrack-execution-reconciliation-v1",
        "engine": "nautilus_paper",
        "cycle_id": CYCLE_ID,
        "status": "ok",
        "issues": [],
    }
    receipt = build_candidate_execution_receipt(
        scenario=scenario,
        snapshot=snapshot,
        reconciliation=reconciliation,
        storage_namespace="strategy_shadow_lab_a3",
    )
    assert receipt["schema_version"] == CANDIDATE_RECEIPT_SCHEMA
    assert receipt["status"] == "pass"
    return receipt


def _platform_parity() -> dict:
    return {
        "schema_version": PLATFORM_PARITY_SCHEMA,
        "scope": "paper_shadow_only",
        "status": "pass",
        "blockers": [],
        "classes": [
            {"class": name, "status": "pass", "scenarios": []}
            for name in FIXTURE_CLASSES
        ],
        "real_money_eligible": False,
    }


def test_verified_candidate_plus_current_platform_parity_unlocks_only_paper_eligibility() -> None:
    entry = _entry()
    token = build_lab_execution_conformance_token(
        entry=entry,
        candidate_receipt=_candidate_receipt(),
        platform_parity=_platform_parity(),
    )

    assert token["schema_version"] == LAB_EXECUTION_CONFORMANCE_TOKEN_SCHEMA
    assert token["status"] == "pass"
    assert token["blockers"] == []
    assert lab_execution_conformance_token_blockers(token, entry=entry) == []
    promoted = promotion_gate(entry, execution_conformance=token)
    assert promoted["paper_eligible"] is True
    assert promoted["status"] == "paper_eligible"
    assert promoted["execution_conformance"]["token_id"] == token["token_id"]


def test_mismatched_candidate_or_blocked_platform_parity_fails_closed() -> None:
    entry = _entry()
    receipt = _candidate_receipt()
    receipt["scenario_id"] = "wrong-scenario"
    parity = _platform_parity()
    parity["status"] = "blocked"
    parity["blockers"] = ["fees_slippage_margin_exposure_pnl"]

    token = build_lab_execution_conformance_token(
        entry=entry,
        candidate_receipt=receipt,
        platform_parity=parity,
    )

    assert token["status"] == "blocked"
    assert "candidate_receipt_integrity_mismatch" in token["blockers"]
    assert "candidate_receipt_scenario_mismatch" in token["blockers"]
    assert "platform_parity_not_pass" in token["blockers"]
    assert promotion_gate(entry, execution_conformance=token)["paper_eligible"] is False


def test_legacy_shadow_schema_is_never_execution_conformance() -> None:
    token = build_lab_execution_conformance_token(
        entry=_entry(),
        candidate_receipt={"schema_version": "strategy-shadow-run-v1"},
        platform_parity=_platform_parity(),
    )

    assert token["status"] == "blocked"
    assert token["blockers"] == ["unsupported_candidate_receipt_schema"]


def test_loader_reads_only_canonical_candidate_receipt_and_current_parity(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    write_json(
        output / "dualtrack" / "strategy_shadows" / "receipts" / f"{SCENARIO_ID}.json",
        [_candidate_receipt()],
    )
    write_json(
        output / "dualtrack" / "nautilus" / "parity" / "current.json",
        [_platform_parity()],
    )

    token = load_lab_execution_conformance(output, _entry())

    assert token["status"] == "pass"
    assert lab_execution_conformance_token_blockers(token, entry=_entry()) == []

    tampered = deepcopy(token)
    tampered["candidate_receipt_id"] = "candidate-receipt-tampered"
    assert "lab_execution_conformance_integrity_mismatch" in lab_execution_conformance_token_blockers(
        tampered,
        entry=_entry(),
    )
