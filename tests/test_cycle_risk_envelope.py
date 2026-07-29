from __future__ import annotations

from pathlib import Path

import pytest

from services.cycle_risk_envelope import CycleRiskEnvelopeError, CycleRiskEnvelopeStore
from services.paper_supervisor_classifier import STRUCTURAL, TRANSIENT, classify_blocker
from services.strategy_control_plane import StrategyControlPlane


def _plan() -> dict:
    return {
        "cycle_id": "2026-07-30_DAY",
        "strategy_plan_id": "strategy-plan-2026-07-30_DAY-1-test",
        "version": 1,
        "strategy_type": "grid",
        "direction": "neutral",
        "range": {"low": 4000, "high": 4100},
        "grid": {"count": 10},
        "risk_budget": {"leverage": 3},
    }


def _limits(**overrides: str) -> dict:
    return {
        "max_actual_leverage": "3",
        "max_full_depth_loss": "100",
        "max_notional_per_grid": "50",
        "min_grid_count": "8",
        "max_grid_count": "12",
        **overrides,
    }


def _preview(**overrides: object) -> dict:
    grid = {"count": 10, "notional_per_grid": "50"}
    risk = {"actual_leverage": "3", "max_loss": "100"}
    for key, value in overrides.items():
        section, field = key.split("__", 1)
        (grid if section == "grid" else risk)[field] = value
    return {
        "strategy_type": "grid",
        "direction": "neutral",
        "preview_id": "grid-preview-fresh",
        "grid": grid,
        "risk": risk,
    }


def _dca_plan() -> dict:
    return {
        "cycle_id": "2026-07-30_DAY",
        "strategy_plan_id": "strategy-plan-2026-07-30_DAY-1-dca",
        "version": 1,
        "strategy_type": "dca",
        "direction": "long",
        "dca": {"max_additions": 2},
        "risk_budget": {"leverage": 3},
    }


def _dca_limits() -> dict:
    return {
        "max_actual_leverage": "3",
        "max_full_depth_loss": "100",
        "max_notional_per_addition": "50",
        "max_total_possible_notional": "150",
        "min_additions": "1",
        "max_additions": "3",
    }


def _dca_preview() -> dict:
    return {
        "strategy_type": "dca",
        "direction": "long",
        "preview_id": "dca-preview-fresh",
        "dca": {
            "notional_per_addition": "50",
            "total_possible_notional": "150",
            "max_additions": 2,
        },
        "risk": {
            "actual_leverage_at_full_depth": "3",
            "maximum_loss_at_full_depth": "100",
        },
    }


def test_human_envelope_records_exact_preview_comparisons(tmp_path: Path) -> None:
    store = CycleRiskEnvelopeStore(tmp_path)
    envelope = store.authorize_envelope(
        cycle_id="2026-07-30_DAY",
        plan=_plan(),
        payload={"authorization_kind": "human_explicit", "limits": _limits()},
        actor={"email": "park@example.com", "transport": "public_gateway"},
        now="2026-07-30T01:00:00+00:00",
    )

    verified = store.verify_preview(
        cycle_id="2026-07-30_DAY",
        plan=_plan(),
        envelope_authorization_id=envelope["envelope_authorization_id"],
        preview=_preview(),
    )

    assert verified["passed"] is True
    assert verified["preview_facts_digest"]
    assert {row["field"] for row in verified["comparisons"]} == {
        "actual_leverage",
        "full_depth_loss",
        "notional_per_grid",
        "grid_count",
    }
    assert all(row["pass"] for row in verified["comparisons"])


def test_envelope_rejects_even_small_out_of_bound_value(tmp_path: Path) -> None:
    store = CycleRiskEnvelopeStore(tmp_path)
    envelope = store.authorize_envelope(
        cycle_id="2026-07-30_DAY",
        plan=_plan(),
        payload={"authorization_kind": "human_explicit", "limits": _limits()},
        actor={"email": "park@example.com"},
    )

    with pytest.raises(CycleRiskEnvelopeError, match="risk_envelope_preview_out_of_bounds"):
        store.verify_preview(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            envelope_authorization_id=envelope["envelope_authorization_id"],
            preview=_preview(grid__notional_per_grid="50.0000001"),
        )


def test_ai_envelope_must_be_nested_inside_human_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOLDBOT_ACCESS_EMAIL", "park@example.com")
    store = CycleRiskEnvelopeStore(tmp_path)
    policy = store.authorize_outer_policy(
        payload={
            "policy_id": "park-grid-policy",
            "version": 1,
            "strategy_type": "grid",
            "direction": "neutral",
            "summary": "Park-approved Grid policy boundary",
            "limits": _limits(max_notional_per_grid="60"),
        },
        actor={"email": "park@example.com"},
    )
    envelope = store.authorize_envelope(
        cycle_id="2026-07-30_DAY",
        plan=_plan(),
        payload={
            "authorization_kind": "ai_policy_within_preapproved_strategy_boundary",
            "outer_policy_id": policy["policy_id"],
            "outer_policy_version": policy["version"],
            "limits": _limits(),
        },
        actor=None,
    )
    assert envelope["outer_policy"]["policy_id"] == "park-grid-policy"
    assert all(row["pass"] for row in envelope["outer_policy_comparisons"])

    with pytest.raises(CycleRiskEnvelopeError, match="outer_strategy_policy_envelope_out_of_bounds"):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            payload={
                "authorization_kind": "ai_policy_within_preapproved_strategy_boundary",
                "outer_policy_id": policy["policy_id"],
                "outer_policy_version": policy["version"],
                "limits": _limits(max_notional_per_grid="60.0000001"),
            },
            actor=None,
        )

    with pytest.raises(CycleRiskEnvelopeError, match="outer_strategy_policy_envelope_out_of_bounds"):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan={**_plan(), "direction": "long"},
            payload={
                "authorization_kind": "ai_policy_within_preapproved_strategy_boundary",
                "outer_policy_id": policy["policy_id"],
                "outer_policy_version": policy["version"],
                "limits": _limits(),
            },
            actor=None,
        )


def test_dca_envelope_uses_full_depth_loss_field_and_links_execution_plan(tmp_path: Path) -> None:
    store = CycleRiskEnvelopeStore(tmp_path)
    envelope = store.authorize_envelope(
        cycle_id="2026-07-30_DAY",
        plan=_dca_plan(),
        payload={"authorization_kind": "human_explicit", "limits": _dca_limits()},
        actor={"email": "park@example.com"},
    )
    verification = store.verify_preview(
        cycle_id="2026-07-30_DAY",
        plan=_dca_plan(),
        envelope_authorization_id=envelope["envelope_authorization_id"],
        preview=_dca_preview(),
    )
    linked = store.bind_execution_plan(
        verification,
        {
            **_dca_plan(),
            "strategy_plan_id": "strategy-plan-2026-07-30_DAY-2-dca",
            "version": 2,
            "preview_id": verification["preview_id"],
            "dca": {
                "max_additions": 2,
                "notional_per_addition": "50",
                "total_possible_notional": "150",
            },
            "risk_budget": {
                "actual_leverage_at_full_depth": "3",
                "maximum_loss_at_full_depth": "100",
            },
        },
    )
    assert linked["execution_plan"]["strategy_plan_id"].endswith("-2-dca")
    assert all(row["pass"] for row in verification["comparisons"])
    receipt = store.record_start_verification(
        cycle_id="2026-07-30_DAY",
        verification=linked,
        prepared_start_id="prepared-start-fresh",
    )
    assert receipt["comparisons"] == linked["comparisons"]
    assert receipt["execution_plan"] == linked["execution_plan"]

    with pytest.raises(CycleRiskEnvelopeError, match="plan_identity_conflict"):
        store.bind_execution_plan(
            verification,
            {
                **_dca_plan(),
                "strategy_plan_id": "strategy-plan-2026-07-30_DAY-3-dca",
                "version": 3,
                "preview_id": "dca-preview-other",
                "dca": {
                    "max_additions": 2,
                    "notional_per_addition": "50",
                    "total_possible_notional": "150",
                },
                "risk_budget": {
                    "actual_leverage_at_full_depth": "3",
                    "maximum_loss_at_full_depth": "100",
                },
            },
        )


def test_classifier_is_closed_and_tick_dead_after_ten_minutes() -> None:
    assert classify_blocker(control_code="prepared_start_market_moved")["classification"] == TRANSIENT
    assert classify_blocker(
        evidence={"tick_health": "missing", "tick_episode_seconds": 600}
    )["machine_code"] == "execution_tick_heartbeat_temporarily_missing"
    assert classify_blocker(
        evidence={"tick_health": "missing", "tick_episode_seconds": 601}
    ) == {
        "classifier_version": "paper-supervisor-blocker-v1",
        "machine_code": "execution_tick_scheduler_down",
        "classification": STRUCTURAL,
        "raw_control_code": None,
        "evidence": {"tick_health": "missing", "tick_episode_seconds": 601},
    }
    unknown = classify_blocker(control_code="new_unclassified_failure")
    assert unknown["machine_code"] == "unknown_blocker"
    assert unknown["classification"] == STRUCTURAL
    prose = classify_blocker(control_code="paper ledger reconciliation failed")
    assert prose["machine_code"] == "unknown_blocker"
    assert classify_blocker(evidence={"control_outcome": "unknown"})["machine_code"] == "control_outcome_unknown"


def test_control_plane_audits_human_policy_and_cycle_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOLDBOT_ACCESS_EMAIL", "park@example.com")
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-30_DAY"
    plan = _plan()
    plan["status"] = "active"
    plane._write_plan(plan)
    actor = {"email": "park@example.com", "transport": "public_gateway"}
    policy = plane.control(
        cycle_id,
        "authorize_outer_strategy_policy",
        {
            "policy_id": "park-grid-policy",
            "version": 1,
            "strategy_type": "grid",
            "direction": "neutral",
            "summary": "Park-approved Grid policy boundary",
            "limits": _limits(),
        },
        actor=actor,
    )
    envelope = plane.control(
        cycle_id,
        "authorize_cycle_risk_envelope",
        {
            "authorization_kind": "human_explicit",
            "limits": _limits(),
        },
        actor=actor,
    )

    assert policy["outer_strategy_policy"]["policy_id"] == "park-grid-policy"
    assert envelope["cycle_risk_envelope"]["strategy_plan_id"] == plan["strategy_plan_id"]
    assert envelope["audit_recorded"] is True
