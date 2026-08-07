from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

import pipelines.dashboard_server as dashboard_server
import services.cycle_risk_envelope as risk_envelope_module
import services.strategy_control_plane as strategy_control_plane_module
from services.cycle_risk_envelope import CycleRiskEnvelopeError, CycleRiskEnvelopeStore
from services.dualtrack_config import dualtrack_config
from services.paper_supervisor_classifier import STRUCTURAL, TRANSIENT, classify_blocker
from services.strategy_control_plane import StrategyControlPlane
from services.supervisor_execution_profile import PAPER_CONTINUOUS


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


def _ai_proposal() -> dict:
    preview = _preview()
    return {
        "proposal_id": "proposal-ai-boundary",
        "cycle_id": "2026-07-30_DAY",
        "source": "ai",
        "preview_id": preview["preview_id"],
        "direction": "neutral",
        "style": "steady",
        "strategy_type": "grid",
        "range": {"low": 4000, "high": 4100},
        "key_levels": [4000, 4100],
        "grid": dict(preview["grid"]),
        "signal": {"confidence": 0.8},
        "tp_sl": {"mode": "per_grid"},
        "risk_budget": dict(preview["risk"]),
        "intraday_rules": [],
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


def _outer_policy_payload(**overrides: object) -> dict:
    return {
        "policy_id": "park-grid-policy",
        "version": 1,
        "strategy_type": "grid",
        "direction": "neutral",
        "summary": "Park-approved Grid policy boundary",
        "expires_at": "2026-08-30T01:00:00+00:00",
        "limits": _limits(max_notional_per_grid="60"),
        **overrides,
    }


def _outer_policy_v2_payload(**overrides: object) -> dict:
    return {
        "schema_version": "paper-strategy-policy-boundary-v2",
        "policy_id": "park-grid-policy",
        "version": 2,
        "strategy_type": "grid",
        "allowed_directions": ["long", "neutral", "short"],
        "summary": "Park-approved Paper Grid direction set",
        **overrides,
    }


def _binding_payload(policy: dict, **overrides: object) -> dict:
    return {
        "binding_id": "paper-supervisor-grid",
        "binding_version": 1,
        "policy_id": policy["policy_id"],
        "policy_version": policy["version"],
        "policy_digest": policy["policy_digest"],
        "summary": "Bind the Paper Supervisor to Park policy v1",
        **overrides,
    }


def _verified_park_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    monkeypatch.setenv("GOLDBOT_ACCESS_EMAIL", "park@example.com")
    monkeypatch.setattr(
        risk_envelope_module,
        "authenticated_access_identity",
        lambda headers: (
            {
                "email": "park@example.com",
                "subject": "park-subject",
                "issued_at": 1785382800,
                "expires_at": 1785987600,
                "issuer": "https://park.cloudflareaccess.com",
            }
            if headers.get("Cf-Access-Jwt-Assertion")
            == "signed-park-assertion"
            else None
        ),
    )
    return {
        "email": "park@example.com",
        "transport": "public_gateway",
        "_access_assertion": "signed-park-assertion",
    }


def _bound_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CycleRiskEnvelopeStore, dict, dict]:
    actor = _verified_park_actor(monkeypatch)
    writer = CycleRiskEnvelopeStore(tmp_path)
    policy = writer.authorize_outer_policy(
        payload=_outer_policy_payload(),
        actor=actor,
        now="2026-07-30T01:00:00+00:00",
    )
    binding = writer.bind_supervisor_outer_policy(
        payload=_binding_payload(policy),
        actor=actor,
        now="2026-07-30T01:01:00+00:00",
    )
    store = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding["binding_id"],
            "binding_version": binding["binding_version"],
            "binding_digest": binding["binding_digest"],
        },
    )
    return store, policy, binding


def test_expired_policy_renews_only_with_source_bound_paper_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    writer = CycleRiskEnvelopeStore(tmp_path)
    policy = writer.authorize_outer_policy(
        payload=_outer_policy_payload(
            expires_at="2026-07-30T02:00:00+00:00"
        ),
        actor=actor,
        now="2026-07-30T01:00:00+00:00",
    )
    binding = writer.bind_supervisor_outer_policy(
        payload=_binding_payload(policy),
        actor=actor,
        now="2026-07-30T01:01:00+00:00",
    )
    reference = {
        "binding_id": binding["binding_id"],
        "binding_version": binding["binding_version"],
        "binding_digest": binding["binding_digest"],
    }
    source = {
        "source_sha": "a" * 40,
        "source_tree_sha": "b" * 40,
        "tracked_tree_clean": True,
    }
    fail_closed = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref=reference,
    )
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_expired",
    ):
        fail_closed.verify_supervisor_outer_policy(
            at="2026-07-30T03:00:00+00:00"
        )

    continuous = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref=reference,
        execution_profile=PAPER_CONTINUOUS,
        source_attestation=lambda: source,
    )
    renewed = continuous.renew_expired_policy_for_paper_continuity(
        now="2026-07-30T03:00:00+00:00"
    )
    assert renewed["renewed"] is True
    receipt = renewed["receipt"]
    assert receipt["policy"]["policy_digest"] == policy["policy_digest"]
    assert receipt["binding"]["binding_digest"] == binding["binding_digest"]
    assert receipt["source"] == source
    assert receipt["scope"] == "paper_only"
    assert receipt["real_money_eligible"] is False
    assert continuous.verify_supervisor_outer_policy(
        at="2026-07-30T03:01:00+00:00"
    )["passed"] is True

    different_source = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref=reference,
        execution_profile=PAPER_CONTINUOUS,
        source_attestation=lambda: {
            **source,
            "source_sha": "c" * 40,
        },
    )
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_expired",
    ):
        different_source.verify_supervisor_outer_policy(
            at="2026-07-30T03:01:00+00:00"
        )


def _reject_supervisor_candidate(
    store: CycleRiskEnvelopeStore,
    *,
    direction: str = "long",
    notional_per_grid: str = "50",
) -> tuple[dict, dict, CycleRiskEnvelopeError]:
    preview = {
        **_preview(
            grid__notional_per_grid=notional_per_grid,
        ),
        "direction": direction,
        "preview_id": "grid-preview-rejected",
    }
    proposal = {
        **_ai_proposal(),
        "proposal_id": "proposal-ai-rejected",
        "direction": direction,
        "preview_id": preview["preview_id"],
        "grid": dict(preview["grid"]),
        "risk_budget": dict(preview["risk"]),
    }
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_envelope_out_of_bounds",
    ) as caught:
        store.authorize_ai_candidate_envelope(
            cycle_id="2026-07-30_DAY",
            proposal=proposal,
            preview=preview,
            supervisor_attempt_id="supervisor-attempt-rejected-1",
            now="2026-07-30T01:02:00+00:00",
        )
    assert caught.value.evidence is not None
    return proposal, preview, caught.value


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
    store, policy, binding = _bound_store(tmp_path, monkeypatch)
    envelope = store.authorize_envelope(
        cycle_id="2026-07-30_DAY",
        plan=_plan(),
        payload={
            "authorization_kind": "ai_policy_within_preapproved_strategy_boundary",
            "limits": _limits(),
        },
        actor=None,
        now="2026-07-30T01:02:00+00:00",
    )
    assert envelope["outer_policy"]["policy_id"] == "park-grid-policy"
    assert envelope["outer_policy"]["binding_id"] == binding["binding_id"]
    assert all(row["pass"] for row in envelope["outer_policy_comparisons"])

    with pytest.raises(CycleRiskEnvelopeError, match="outer_strategy_policy_envelope_out_of_bounds"):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            payload={
                "authorization_kind": "ai_policy_within_preapproved_strategy_boundary",
                "limits": _limits(max_notional_per_grid="60.0000001"),
            },
            actor=None,
            now="2026-07-30T01:02:00+00:00",
        )

    with pytest.raises(CycleRiskEnvelopeError, match="outer_strategy_policy_envelope_out_of_bounds"):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan={**_plan(), "direction": "long"},
            payload={
                "authorization_kind": "ai_policy_within_preapproved_strategy_boundary",
                "limits": _limits(),
            },
            actor=None,
            now="2026-07-30T01:02:00+00:00",
        )

    verification = store.verify_preview(
        cycle_id="2026-07-30_DAY",
        plan=_plan(),
        envelope_authorization_id=envelope["envelope_authorization_id"],
        preview=_preview(),
        now="2026-07-30T01:03:00+00:00",
    )
    assert verification["passed"] is True
    assert policy["expires_at"] == "2026-08-30T01:00:00+00:00"
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_expired",
    ):
        store.verify_preview(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            envelope_authorization_id=envelope[
                "envelope_authorization_id"
            ],
            preview=_preview(),
            now=policy["expires_at"],
        )


def test_outer_policy_rejection_is_immutable_deny_only_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, policy, binding = _bound_store(tmp_path, monkeypatch)
    proposal, preview, error = _reject_supervisor_candidate(store)

    evidence = dict(error.evidence or {})
    receipt = store.outer_policy_rejection(
        cycle_id="2026-07-30_DAY",
        rejection_id=evidence["rejection_id"],
        rejection_digest=evidence["rejection_digest"],
    )

    assert receipt["authorization_effect"] == "deny_only"
    assert receipt["passed"] is False
    assert receipt["candidate"] == {
        "supervisor_attempt_id": "supervisor-attempt-rejected-1",
        "proposal_id": proposal["proposal_id"],
        "proposal_digest": (
            risk_envelope_module._proposal_plan_digest(proposal)
        ),
        "preview_id": preview["preview_id"],
        "preview_digest": risk_envelope_module._digest(preview),
        "facts_digest": None,
        "confirmation_digest": None,
        "strategy_type": "grid",
        "direction": "long",
        "limits": _limits(
            max_notional_per_grid="50",
            min_grid_count="10",
            max_grid_count="10",
        ),
    }
    assert receipt["outer_policy"] == {
        "policy_id": policy["policy_id"],
        "version": policy["version"],
        "policy_digest": policy["policy_digest"],
        "authorized_at": policy["authorized_at"],
        "expires_at": policy["expires_at"],
        "binding_id": binding["binding_id"],
        "binding_version": binding["binding_version"],
        "binding_digest": binding["binding_digest"],
        "bound_at": binding["bound_at"],
    }
    assert {row["field"] for row in receipt["comparisons"]} == {
        "strategy_type",
        "direction",
        "max_actual_leverage",
        "max_full_depth_loss",
        "max_notional_per_grid",
        "min_grid_count",
        "max_grid_count",
    }
    assert next(
        row
        for row in receipt["comparisons"]
        if row["field"] == "direction"
    )["pass"] is False
    assert not (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "2026-07-30_DAY.json"
    ).exists()


def test_rejected_candidate_clears_only_under_different_sufficient_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    store_v1, _policy_v1, binding_v1 = _bound_store(
        tmp_path,
        monkeypatch,
    )
    _proposal, _preview_row, error = _reject_supervisor_candidate(
        store_v1
    )
    evidence = dict(error.evidence or {})

    unchanged = store_v1.recheck_outer_policy_rejection(
        cycle_id="2026-07-30_DAY",
        rejection_id=evidence["rejection_id"],
        rejection_digest=evidence["rejection_digest"],
        at="2026-07-30T01:03:00+00:00",
    )
    assert unchanged["binding_changed"] is False
    assert unchanged["passed"] is False

    v2_writer = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v1["binding_id"],
            "binding_version": binding_v1["binding_version"],
            "binding_digest": binding_v1["binding_digest"],
        },
    )
    policy_v2 = v2_writer.authorize_outer_policy(
        payload=_outer_policy_v2_payload(),
        actor=actor,
        now="2026-07-31T01:00:00+00:00",
    )
    binding_v2 = v2_writer.bind_supervisor_outer_policy(
        payload=_binding_payload(
            policy_v2,
            binding_version=2,
            summary="Bind Paper Supervisor to Park policy v2",
        ),
        actor=actor,
        now="2026-07-31T01:01:00+00:00",
    )
    store_v2 = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v2["binding_id"],
            "binding_version": binding_v2["binding_version"],
            "binding_digest": binding_v2["binding_digest"],
        },
    )

    remediated = store_v2.recheck_outer_policy_rejection(
        cycle_id="2026-07-30_DAY",
        rejection_id=evidence["rejection_id"],
        rejection_digest=evidence["rejection_digest"],
        at="2026-07-31T01:02:00+00:00",
    )

    assert remediated["binding_changed"] is True
    assert remediated["passed"] is True
    assert all(row["pass"] for row in remediated["comparisons"])
    assert remediated["control_actions_executed"] == 0
    assert store_v2.verify_outer_policy_recheck_proof(
        proof=remediated,
        cycle_id="2026-07-30_DAY",
        rejection_id=evidence["rejection_id"],
        rejection_digest=evidence["rejection_digest"],
    ) == remediated


@pytest.mark.parametrize(
    "mutation",
    [
        "comparisons",
        "binding_changed",
        "control_actions",
        "rejection_identity",
        "digest",
    ],
)
def test_outer_policy_recheck_proof_rejects_malformed_or_forged_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    store_v1, _policy_v1, binding_v1 = _bound_store(
        tmp_path,
        monkeypatch,
    )
    _proposal, _preview_row, error = _reject_supervisor_candidate(
        store_v1
    )
    evidence = dict(error.evidence or {})
    policy_v2 = store_v1.authorize_outer_policy(
        payload=_outer_policy_v2_payload(),
        actor=actor,
        now="2026-07-31T01:00:00+00:00",
    )
    binding_v2 = store_v1.bind_supervisor_outer_policy(
        payload=_binding_payload(
            policy_v2,
            binding_version=2,
            summary="Bind Paper Supervisor to Park policy v2",
        ),
        actor=actor,
        now="2026-07-31T01:01:00+00:00",
    )
    store_v2 = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v2["binding_id"],
            "binding_version": binding_v2["binding_version"],
            "binding_digest": binding_v2["binding_digest"],
        },
    )
    proof = store_v2.recheck_outer_policy_rejection(
        cycle_id="2026-07-30_DAY",
        rejection_id=evidence["rejection_id"],
        rejection_digest=evidence["rejection_digest"],
        at="2026-07-31T01:02:00+00:00",
    )
    forged = json.loads(json.dumps(proof))
    if mutation == "comparisons":
        forged["comparisons"] = []
    elif mutation == "binding_changed":
        forged["binding_changed"] = False
    elif mutation == "control_actions":
        forged["control_actions_executed"] = 1
    elif mutation == "rejection_identity":
        forged["rejection_id"] = "outer-policy-rejection-substitute"
    else:
        forged["recheck_digest"] = "0" * 64
    if mutation != "digest":
        forged["recheck_digest"] = risk_envelope_module._digest(
            {
                key: value
                for key, value in forged.items()
                if key != "recheck_digest"
            }
        )

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="attempt_store_corrupt",
    ):
        store_v2.verify_outer_policy_recheck_proof(
            proof=forged,
            cycle_id="2026-07-30_DAY",
            rejection_id=evidence["rejection_id"],
            rejection_digest=evidence["rejection_digest"],
        )


def test_changed_binding_does_not_clear_numeric_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    store_v1, _policy_v1, binding_v1 = _bound_store(
        tmp_path,
        monkeypatch,
    )
    _proposal, _preview_row, error = _reject_supervisor_candidate(
        store_v1,
        direction="neutral",
        notional_per_grid="60.0000001",
    )
    evidence = dict(error.evidence or {})
    v2_writer = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v1["binding_id"],
            "binding_version": binding_v1["binding_version"],
            "binding_digest": binding_v1["binding_digest"],
        },
    )
    policy_v2 = v2_writer.authorize_outer_policy(
        payload=_outer_policy_v2_payload(),
        actor=actor,
        now="2026-07-31T01:00:00+00:00",
    )
    binding_v2 = v2_writer.bind_supervisor_outer_policy(
        payload=_binding_payload(
            policy_v2,
            binding_version=2,
            summary="Bind Paper Supervisor to Park policy v2",
        ),
        actor=actor,
        now="2026-07-31T01:01:00+00:00",
    )
    store_v2 = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v2["binding_id"],
            "binding_version": binding_v2["binding_version"],
            "binding_digest": binding_v2["binding_digest"],
        },
    )

    result = store_v2.recheck_outer_policy_rejection(
        cycle_id="2026-07-30_DAY",
        rejection_id=evidence["rejection_id"],
        rejection_digest=evidence["rejection_digest"],
        at="2026-07-31T01:02:00+00:00",
    )

    assert result["binding_changed"] is True
    assert result["passed"] is False
    assert next(
        row
        for row in result["comparisons"]
        if row["field"] == "max_notional_per_grid"
    )["pass"] is False


@pytest.mark.parametrize("mutation", ["missing", "digest", "tampered"])
def test_rejection_evidence_fails_closed_when_not_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    store, _policy, _binding = _bound_store(tmp_path, monkeypatch)
    _proposal, _preview_row, error = _reject_supervisor_candidate(store)
    evidence = dict(error.evidence or {})
    rejection_id = evidence["rejection_id"]
    rejection_digest = evidence["rejection_digest"]
    if mutation == "missing":
        rejection_id = "outer-policy-rejection-missing"
    elif mutation == "digest":
        rejection_digest = "0" * 64
    else:
        path = (
            tmp_path
            / "dualtrack"
            / "supervisor"
            / "risk_envelopes"
            / "outer_policy_rejections"
            / "2026-07-30_DAY.json"
        )
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows[0]["candidate"]["direction"] = "neutral"
        path.write_text(json.dumps(rows), encoding="utf-8")

    with pytest.raises(CycleRiskEnvelopeError):
        store.recheck_outer_policy_rejection(
            cycle_id="2026-07-30_DAY",
            rejection_id=rejection_id,
            rejection_digest=rejection_digest,
            at="2026-07-30T01:03:00+00:00",
        )


def test_legacy_rejection_resolution_requires_verified_park_actor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    store = CycleRiskEnvelopeStore(tmp_path)
    payload = {
        "resolution_id": "legacy-resolution-1",
        "resolution_version": 1,
        "blocked_at": "2026-07-30T01:02:00+00:00",
        "machine_code": (
            "outer_strategy_policy_envelope_out_of_bounds"
        ),
        "summary": "Park explicitly resolves the receipt-less blocker",
    }

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.authorize_legacy_rejection_resolution(
            cycle_id="2026-07-30_DAY",
            payload=payload,
            actor={
                "email": "park@example.com",
                "transport": "public_gateway",
            },
            now="2026-07-30T01:03:00+00:00",
        )

    resolution = store.authorize_legacy_rejection_resolution(
        cycle_id="2026-07-30_DAY",
        payload=payload,
        actor=actor,
        now="2026-07-30T01:03:00+00:00",
    )
    loaded = store.legacy_rejection_resolution(
        cycle_id="2026-07-30_DAY",
        machine_code=payload["machine_code"],
        blocked_at=payload["blocked_at"],
    )
    assert loaded == resolution
    assert resolution["authorization_kind"] == (
        "park_explicit_legacy_structural_resolution"
    )


def test_outer_policy_v2_allows_only_explicit_grid_direction_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    writer = CycleRiskEnvelopeStore(tmp_path)
    policy_v1 = writer.authorize_outer_policy(
        payload=_outer_policy_payload(),
        actor=actor,
        now="2026-07-30T01:00:00+00:00",
    )
    binding_v1 = writer.bind_supervisor_outer_policy(
        payload=_binding_payload(policy_v1),
        actor=actor,
        now="2026-07-30T01:01:00+00:00",
    )
    v2_writer = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v1["binding_id"],
            "binding_version": binding_v1["binding_version"],
            "binding_digest": binding_v1["binding_digest"],
        },
    )
    policy_v2 = v2_writer.authorize_outer_policy(
        payload=_outer_policy_v2_payload(),
        actor=actor,
        now="2026-07-31T01:00:00+00:00",
    )
    binding_v2 = v2_writer.bind_supervisor_outer_policy(
        payload=_binding_payload(
            policy_v2,
            binding_version=2,
            summary="Bind Paper Supervisor to Park Grid policy v2",
        ),
        actor=actor,
        now="2026-07-31T01:01:00+00:00",
    )
    store = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v2["binding_id"],
            "binding_version": binding_v2["binding_version"],
            "binding_digest": binding_v2["binding_digest"],
        },
    )

    policy_rows = json.loads(
        (
            tmp_path
            / "dualtrack"
            / "supervisor"
            / "risk_envelopes"
            / "outer_strategy_policies"
            / "park-grid-policy.json"
        ).read_text(encoding="utf-8")
    )
    assert policy_rows == [policy_v1, policy_v2]
    assert policy_v1["schema_version"] == (
        "paper-strategy-policy-boundary-v1"
    )
    assert policy_v1["direction"] == "neutral"
    assert "allowed_directions" not in policy_v1
    assert policy_v2["allowed_directions"] == [
        "long",
        "neutral",
        "short",
    ]
    assert policy_v2["limits"] == policy_v1["limits"]
    assert policy_v2["expires_at"] == policy_v1["expires_at"]
    assert policy_v2["inherited_from"] == {
        "binding_id": binding_v1["binding_id"],
        "binding_version": binding_v1["binding_version"],
        "binding_digest": binding_v1["binding_digest"],
        "policy_id": policy_v1["policy_id"],
        "policy_version": policy_v1["version"],
        "policy_digest": policy_v1["policy_digest"],
        "policy_schema_version": policy_v1["schema_version"],
    }

    for direction in ("long", "neutral", "short"):
        preview = {
            **_preview(),
            "direction": direction,
            "preview_id": f"grid-preview-v2-{direction}",
        }
        proposal = {
            **_ai_proposal(),
            "proposal_id": f"proposal-v2-{direction}",
            "direction": direction,
            "preview_id": preview["preview_id"],
        }
        envelope = store.authorize_ai_candidate_envelope(
            cycle_id="2026-07-30_DAY",
            proposal=proposal,
            preview=preview,
            now="2026-07-31T01:02:00+00:00",
        )
        assert envelope["strategy_plan_id"] is None
        comparison = next(
            row
            for row in envelope["outer_policy_comparisons"]
            if row["field"] == "direction"
        )
        assert comparison == {
            "field": "direction",
            "operator": "in",
            "authorized_limit": ["long", "neutral", "short"],
            "observed_value": direction,
            "pass": True,
        }
    assert StrategyControlPlane(tmp_path).active_plan(
        "2026-07-30_DAY"
    ) is None

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_envelope_out_of_bounds",
    ):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan={**_plan(), "direction": "long"},
            payload={
                "authorization_kind": (
                    "ai_policy_within_preapproved_strategy_boundary"
                ),
                "limits": _limits(
                    max_notional_per_grid="60.0000001"
                ),
            },
            actor=None,
            now="2026-07-31T01:02:00+00:00",
        )

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_envelope_out_of_bounds",
    ):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan=_dca_plan(),
            payload={
                "authorization_kind": (
                    "ai_policy_within_preapproved_strategy_boundary"
                ),
                "limits": _dca_limits(),
            },
            actor=None,
            now="2026-07-31T01:02:00+00:00",
        )

    policy_rows[1]["limits"]["max_actual_leverage"] = "999"
    policy_rows[1]["policy_digest"] = risk_envelope_module._digest(
        {
            key: value
            for key, value in policy_rows[1].items()
            if key != "policy_digest"
        }
    )
    (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "outer_strategy_policies"
        / "park-grid-policy.json"
    ).write_text(json.dumps(policy_rows), encoding="utf-8")
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.outer_policy(
            policy_v2["policy_id"],
            policy_v2["version"],
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"allowed_directions": []},
        {"allowed_directions": "long"},
        {"allowed_directions": ["long", "long"]},
        {"allowed_directions": ["neutral", "long"]},
        {"allowed_directions": ["long", "sideways"]},
        {"allowed_directions": ["*"]},
        {"direction": "neutral"},
        {"limits": _limits(max_actual_leverage="999")},
        {"limits": _limits(max_full_depth_loss="999999")},
        {"limits": _limits(max_notional_per_grid="999999")},
        {"limits": _limits(min_grid_count="0")},
        {"limits": _limits(max_grid_count="999")},
        {"expires_at": "2026-08-29T01:00:00+00:00"},
        {"expires_at": "2027-08-30T01:00:00+00:00"},
        {"schema_version": "paper-strategy-policy-boundary-v3"},
    ),
)
def test_outer_policy_v2_rejects_noncanonical_or_mixed_direction_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    writer = CycleRiskEnvelopeStore(tmp_path)
    policy_v1 = writer.authorize_outer_policy(
        payload=_outer_policy_payload(),
        actor=actor,
        now="2026-07-30T01:00:00+00:00",
    )
    binding_v1 = writer.bind_supervisor_outer_policy(
        payload=_binding_payload(policy_v1),
        actor=actor,
        now="2026-07-30T01:01:00+00:00",
    )
    store = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v1["binding_id"],
            "binding_version": binding_v1["binding_version"],
            "binding_digest": binding_v1["binding_digest"],
        },
    )
    payload = _outer_policy_v2_payload()
    payload.update(mutation)
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.authorize_outer_policy(
            payload=payload,
            actor=actor,
            now="2026-07-31T01:00:00+00:00",
        )


def test_outer_policy_v2_requires_the_current_exact_v1_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    writer = CycleRiskEnvelopeStore(tmp_path)
    policy_v1 = writer.authorize_outer_policy(
        payload=_outer_policy_payload(),
        actor=actor,
        now="2026-07-30T01:00:00+00:00",
    )
    policy_path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "outer_strategy_policies"
        / "park-grid-policy.json"
    )
    before = policy_path.read_bytes()

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_missing",
    ):
        writer.authorize_outer_policy(
            payload=_outer_policy_v2_payload(),
            actor=actor,
            now="2026-07-31T01:00:00+00:00",
        )
    assert policy_path.read_bytes() == before

    binding_v1 = writer.bind_supervisor_outer_policy(
        payload=_binding_payload(policy_v1),
        actor=actor,
        now="2026-07-30T01:01:00+00:00",
    )
    invalid_binding_store = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v1["binding_id"],
            "binding_version": binding_v1["binding_version"],
            "binding_digest": "0" * 64,
        },
    )
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        invalid_binding_store.authorize_outer_policy(
            payload=_outer_policy_v2_payload(),
            actor=actor,
            now="2026-07-31T01:00:00+00:00",
        )
    assert policy_path.read_bytes() == before


def test_policy_authorization_api_skips_market_and_account_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "policy authorization must not read market or account state"
        )

    monkeypatch.setattr(
        dashboard_server,
        "build_dualtrack_market_bars_response",
        forbidden,
    )
    monkeypatch.setattr(
        dashboard_server,
        "build_strategy_timeframes_response",
        forbidden,
    )
    monkeypatch.setattr(
        dashboard_server,
        "build_strategy_console_production_history",
        forbidden,
    )
    output = tmp_path / "outputs"
    plane_class = dashboard_server.StrategyControlPlane
    trusted_now = "2026-07-31T01:00:00+00:00"
    monkeypatch.setattr(
        dashboard_server,
        "StrategyControlPlane",
        lambda root: plane_class(
            root,
            authorization_clock=lambda: trusted_now,
        ),
    )
    policy_v1_result = (
        dashboard_server.build_strategy_console_control_response(
            {
                "cycle_id": "2026-07-30_DAY",
                "action": "authorize_outer_strategy_policy",
                "as_of": "2020-01-01T00:00:00+00:00",
                **_outer_policy_payload(),
            },
            output_root=output,
            actor=actor,
        )
    )
    policy_v1 = policy_v1_result["outer_strategy_policy"]
    binding_v1_result = (
        dashboard_server.build_strategy_console_control_response(
            {
                "cycle_id": "2026-07-30_DAY",
                "action": "bind_supervisor_outer_strategy_policy",
                "as_of": "2020-01-01T00:00:00+00:00",
                **_binding_payload(policy_v1),
            },
            output_root=output,
            actor=actor,
        )
    )
    binding_v1 = binding_v1_result[
        "outer_strategy_policy_binding"
    ]
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_ID",
        binding_v1["binding_id"],
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_VERSION",
        str(binding_v1["binding_version"]),
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_DIGEST",
        binding_v1["binding_digest"],
    )
    policy_result = dashboard_server.build_strategy_console_control_response(
        {
            "cycle_id": "2026-07-30_DAY",
            "action": "authorize_outer_strategy_policy",
            "as_of": "2020-01-01T00:00:00+00:00",
            **_outer_policy_v2_payload(),
        },
        output_root=output,
        actor=actor,
    )
    policy = policy_result["outer_strategy_policy"]
    binding_result = dashboard_server.build_strategy_console_control_response(
        {
            "cycle_id": "2026-07-30_DAY",
            "action": "bind_supervisor_outer_strategy_policy",
            "as_of": "2030-01-01T00:00:00+00:00",
            **_binding_payload(
                policy,
                binding_version=2,
                summary="Bind Paper Supervisor to Park Grid policy v2",
            ),
        },
        output_root=output,
        actor=actor,
    )
    resolution_result = (
        dashboard_server.build_strategy_console_control_response(
            {
                "cycle_id": "2026-07-30_DAY",
                "action": "resolve_legacy_outer_policy_rejection",
                "as_of": "2030-01-01T00:00:00+00:00",
                "resolution_id": "legacy-resolution-api-1",
                "resolution_version": 1,
                "blocked_at": "2026-07-30T01:02:00+00:00",
                "machine_code": (
                    "outer_strategy_policy_envelope_out_of_bounds"
                ),
                "summary": "Park resolves one receipt-less blocker",
            },
            output_root=output,
            actor=actor,
        )
    )

    assert policy["schema_version"] == (
        "paper-strategy-policy-boundary-v2"
    )
    assert policy_v1["authorized_at"] == trusted_now
    assert policy["authorized_at"] == trusted_now
    assert policy["limits"] == policy_v1["limits"]
    assert policy["expires_at"] == policy_v1["expires_at"]
    assert binding_result["outer_strategy_policy_binding"][
        "policy_digest"
    ] == policy["policy_digest"]
    assert resolution_result[
        "legacy_outer_policy_rejection_resolution"
    ]["authorized_at"] == trusted_now
    plane = StrategyControlPlane(output)
    assert plane.active_plan("2026-07-30_DAY") is None
    execution_root = output / "dualtrack" / "execution"
    assert not execution_root.exists() or not list(
        execution_root.rglob("*")
    )


def test_ai_candidate_envelope_is_persisted_before_plan_and_binds_later_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _policy, _binding = _bound_store(tmp_path, monkeypatch)
    proposal = _ai_proposal()
    envelope = store.authorize_ai_candidate_envelope(
        cycle_id="2026-07-30_DAY",
        proposal=proposal,
        preview=_preview(),
        now="2026-07-30T01:02:00+00:00",
    )

    assert envelope["strategy_plan_id"] is None
    assert envelope["source_proposal"]["proposal_id"] == proposal[
        "proposal_id"
    ]
    assert all(
        row["pass"]
        for row in envelope["outer_policy_comparisons"]
    )
    plan = {
        **proposal,
        "strategy_plan_id": "strategy-plan-ai-boundary",
        "version": 1,
        "source_proposal_ids": [proposal["proposal_id"]],
    }
    verified = store.verify_preview(
        cycle_id="2026-07-30_DAY",
        plan=plan,
        envelope_authorization_id=envelope[
            "envelope_authorization_id"
        ],
        preview=_preview(),
        now="2026-07-30T01:03:00+00:00",
    )
    assert verified["passed"] is True
    assert (
        verified["authorized_source_plan"]["strategy_plan_id"]
        == "strategy-plan-ai-boundary"
    )


def test_active_grid_envelope_accepts_distinct_fresh_execution_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _policy, _binding = _bound_store(tmp_path, monkeypatch)
    proposal = _ai_proposal()
    source_preview = _preview()
    envelope = store.authorize_ai_candidate_envelope(
        cycle_id="2026-07-30_DAY",
        proposal=proposal,
        preview=source_preview,
        now="2026-07-30T01:02:00+00:00",
    )
    plan = {
        **proposal,
        "strategy_plan_id": "strategy-plan-ai-boundary",
        "version": 1,
        "source_proposal_ids": [proposal["proposal_id"]],
    }
    fresh_preview = {
        **_preview(),
        "preview_id": "grid-preview-rebuilt",
        "market": {"latest_close": "4050.00"},
    }

    verified = store.verify_preview(
        cycle_id="2026-07-30_DAY",
        plan=plan,
        envelope_authorization_id=envelope["envelope_authorization_id"],
        preview=fresh_preview,
        now="2026-07-30T01:03:00+00:00",
    )

    assert verified["passed"] is True
    assert verified["preview_id"] == "grid-preview-rebuilt"
    assert verified["preview_facts_digest"] != envelope["source_proposal"][
        "preview_digest"
    ]
    assert verified["authorized_source_plan"]["source_proposal"][
        "preview_id"
    ] == source_preview["preview_id"]
    assert all(row["pass"] for row in verified["comparisons"])


def test_active_grid_fresh_preview_still_fails_exact_numeric_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _policy, _binding = _bound_store(tmp_path, monkeypatch)
    proposal = _ai_proposal()
    envelope = store.authorize_ai_candidate_envelope(
        cycle_id="2026-07-30_DAY",
        proposal=proposal,
        preview=_preview(),
        now="2026-07-30T01:02:00+00:00",
    )
    plan = {
        **proposal,
        "strategy_plan_id": "strategy-plan-ai-boundary",
        "version": 1,
        "source_proposal_ids": [proposal["proposal_id"]],
    }

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="risk_envelope_preview_out_of_bounds",
    ):
        store.verify_preview(
            cycle_id="2026-07-30_DAY",
            plan=plan,
            envelope_authorization_id=envelope[
                "envelope_authorization_id"
            ],
            preview={
                **_preview(grid__notional_per_grid="50.0000001"),
                "preview_id": "grid-preview-rebuilt",
            },
            now="2026-07-30T01:03:00+00:00",
        )


def test_control_plane_locks_the_exact_candidate_envelope_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _policy, _binding = _bound_store(tmp_path, monkeypatch)
    plane = StrategyControlPlane(
        tmp_path,
        authorization_clock=lambda: "2026-07-30T01:02:00+00:00",
    )
    plane.risk_envelopes = store
    proposal = plane.upsert_proposal(_ai_proposal())
    envelope = plane.authorize_supervisor_ai_envelope(
        "2026-07-30_DAY",
        proposal=proposal,
        preview=_preview(),
    )
    plan = plane.lock_production_plan(
        "2026-07-30_DAY",
        selected_proposal_id=proposal["proposal_id"],
        cycle_risk_envelope_id=envelope[
            "envelope_authorization_id"
        ],
        now="2026-07-30T01:02:01+00:00",
    )

    assert plan["cycle_risk_envelope_id"] == envelope[
        "envelope_authorization_id"
    ]
    assert store.verify_preview(
        cycle_id="2026-07-30_DAY",
        plan=plan,
        envelope_authorization_id=plan["cycle_risk_envelope_id"],
        preview=_preview(),
        now="2026-07-30T01:03:00+00:00",
    )["passed"] is True


def test_candidate_identity_conflict_stops_before_active_plan_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _policy, _binding = _bound_store(tmp_path, monkeypatch)
    plane = StrategyControlPlane(
        tmp_path,
        authorization_clock=lambda: "2026-07-30T01:02:00+00:00",
    )
    plane.risk_envelopes = store
    proposal = plane.upsert_proposal(_ai_proposal())
    envelope = plane.authorize_supervisor_ai_envelope(
        "2026-07-30_DAY",
        proposal=proposal,
        preview=_preview(),
    )
    plane.upsert_proposal(
        {
            **proposal,
            "signal": {"confidence": 0.1, "mutated": True},
        }
    )

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="plan_identity_conflict",
    ):
        plane.lock_production_plan(
            "2026-07-30_DAY",
            selected_proposal_id=proposal["proposal_id"],
            cycle_risk_envelope_id=envelope[
                "envelope_authorization_id"
            ],
            now="2026-07-30T01:02:01+00:00",
        )

    assert plane.active_plan("2026-07-30_DAY") is None
    assert not plane._plans_path("2026-07-30_DAY").exists()


def test_ai_candidate_direction_or_limits_fail_before_plan_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _policy, _binding = _bound_store(tmp_path, monkeypatch)
    proposal = _ai_proposal()
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="plan_identity_conflict",
    ):
        store.authorize_ai_candidate_envelope(
            cycle_id="2026-07-30_DAY",
            proposal={**proposal, "direction": "long"},
            preview=_preview(),
            now="2026-07-30T01:02:00+00:00",
        )
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_envelope_out_of_bounds",
    ):
        store.authorize_ai_candidate_envelope(
            cycle_id="2026-07-30_DAY",
            proposal=proposal,
            preview=_preview(grid__notional_per_grid="60.0000001"),
            now="2026-07-30T01:02:00+00:00",
        )
    assert not (
        tmp_path
        / "dualtrack"
        / "strategy_control"
        / "plans"
        / "2026-07-30_DAY.json"
    ).exists()


def test_ai_cannot_select_policy_or_fabricate_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, policy, _binding = _bound_store(tmp_path, monkeypatch)
    base = {
        "authorization_kind": (
            "ai_policy_within_preapproved_strategy_boundary"
        ),
        "limits": _limits(),
    }

    for forged in (
        {"outer_policy_id": policy["policy_id"]},
        {"facts_digest": "copied"},
        {"acknowledgement": True},
        {"risk_acknowledgements": {"confirmed": True}},
        {"limits": {**_limits(), "facts_digest": "copied"}},
        {"limits": {**_limits(), "acknowledgement": True}},
    ):
        with pytest.raises(
            CycleRiskEnvelopeError,
            match="risk_envelope_authorization_invalid",
        ):
            store.authorize_envelope(
                cycle_id="2026-07-30_DAY",
                plan=_plan(),
                payload={**base, **forged},
                actor=None,
                now="2026-07-30T01:02:00+00:00",
            )

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="risk_envelope_authorization_invalid",
    ):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            payload=base,
            actor={"email": "park@example.com", "transport": "ai"},
            now="2026-07-30T01:02:00+00:00",
        )


def test_policy_registry_rejects_corrupt_unselected_and_duplicate_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, policy, _binding = _bound_store(tmp_path, monkeypatch)
    policy_path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "outer_strategy_policies"
        / f"{policy['policy_id']}.json"
    )
    rows = json.loads(policy_path.read_text(encoding="utf-8"))
    corrupt = {**rows[0], "version": 2}
    rows.append(corrupt)
    policy_path.write_text(json.dumps(rows), encoding="utf-8")

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.outer_policy(policy["policy_id"], policy["version"])

    policy_path.write_text(json.dumps([policy, policy]), encoding="utf-8")
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.outer_policy(policy["policy_id"], policy["version"])


def test_outer_grid_policy_supports_explicit_unbounded_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    writer = CycleRiskEnvelopeStore(tmp_path)
    policy = writer.authorize_outer_policy(
        payload=_outer_policy_payload(
            limits=_limits(
                max_full_depth_loss="10068.18",
                min_grid_count="0",
                max_grid_count="unbounded",
            )
        ),
        actor=actor,
        now="2026-08-01T01:00:00+00:00",
    )
    binding = writer.bind_supervisor_outer_policy(
        payload=_binding_payload(policy),
        actor=actor,
        now="2026-08-01T01:01:00+00:00",
    )
    store = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding["binding_id"],
            "binding_version": binding["binding_version"],
            "binding_digest": binding["binding_digest"],
        },
    )

    envelope = store.authorize_envelope(
        cycle_id="2026-07-30_DAY",
        plan=_plan(),
        payload={
            "authorization_kind": (
                "ai_policy_within_preapproved_strategy_boundary"
            ),
            "limits": _limits(
                max_full_depth_loss="100",
                min_grid_count="10",
                max_grid_count="100",
            ),
        },
        actor=None,
        now="2026-08-01T01:02:00+00:00",
    )

    comparison = next(
        row
        for row in envelope["outer_policy_comparisons"]
        if row["field"] == "max_grid_count"
    )
    assert comparison == {
        "field": "max_grid_count",
        "operator": "unbounded",
        "authorized_limit": "unbounded",
        "observed_value": "100",
        "pass": True,
    }
    assert policy["limits"]["max_grid_count"] == "unbounded"


def test_unknown_grid_count_sentinel_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    store = CycleRiskEnvelopeStore(tmp_path)
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.authorize_outer_policy(
            payload=_outer_policy_payload(
                limits=_limits(max_grid_count="unlimited")
            ),
            actor=actor,
            now="2026-08-01T01:00:00+00:00",
        )


def test_ai_envelope_cannot_introduce_unbounded_grid_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _policy, _binding = _bound_store(tmp_path, monkeypatch)
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="risk_envelope_authorization_invalid",
    ):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            payload={
                "authorization_kind": (
                    "ai_policy_within_preapproved_strategy_boundary"
                ),
                "limits": _limits(max_grid_count="unbounded"),
            },
            actor=None,
            now="2026-08-01T01:02:00+00:00",
        )

def test_concurrent_policy_writes_keep_one_immutable_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    store = CycleRiskEnvelopeStore(tmp_path)
    base = _outer_policy_payload(
        policy_id="park-concurrent-policy",
    )

    def write(summary: str) -> str:
        try:
            store.authorize_outer_policy(
                payload={**base, "summary": summary},
                actor=actor,
                now="2026-07-30T01:00:00+00:00",
            )
        except CycleRiskEnvelopeError as exc:
            return exc.code
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(
            pool.map(write, ("boundary-a", "boundary-b"))
        )

    assert outcomes == ["accepted", "outer_strategy_policy_invalid"]
    path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "outer_strategy_policies"
        / "park-concurrent-policy.json"
    )
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["summary"] in {"boundary-a", "boundary-b"}


def test_binding_registry_rejects_non_object_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _policy, binding = _bound_store(tmp_path, monkeypatch)
    binding_path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "outer_strategy_policy_bindings"
        / f"{binding['binding_id']}.json"
    )
    rows = json.loads(binding_path.read_text(encoding="utf-8"))
    binding_path.write_text(json.dumps([*rows, "corrupt"]), encoding="utf-8")

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="attempt_store_corrupt",
    ):
        store.supervisor_binding(
            binding["binding_id"],
            binding["binding_version"],
        )


def test_envelope_registry_rejects_duplicate_authorization_identity(
    tmp_path: Path,
) -> None:
    store = CycleRiskEnvelopeStore(tmp_path)
    envelope = store.authorize_envelope(
        cycle_id="2026-07-30_DAY",
        plan=_plan(),
        payload={
            "authorization_kind": "human_explicit",
            "limits": _limits(),
        },
        actor={"email": "park@example.com"},
        now="2026-07-30T01:00:00+00:00",
    )
    path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "2026-07-30_DAY.json"
    )
    path.write_text(
        json.dumps([envelope, envelope]),
        encoding="utf-8",
    )

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="risk_envelope_authorization_invalid",
    ):
        store.envelope(
            "2026-07-30_DAY",
            envelope["envelope_authorization_id"],
        )


def test_scheduler_prepare_requires_ai_envelope_before_any_plan_or_order(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-30_DAY"

    with pytest.raises(ValueError, match="risk_envelope_missing"):
        plane.control(
            cycle_id,
            "prepare_start",
            {"direction": "neutral", "style": "steady"},
            market={},
            account={},
            actor={
                "type": "scheduler",
                "id": "dualtrack-live-tick",
            },
            now="2026-07-30T01:02:00+00:00",
        )

    assert plane.active_plan(cycle_id) is None
    assert not (
        output
        / "dualtrack"
        / "orders"
        / f"{cycle_id}_machine.json"
    ).exists()


def test_missing_expired_or_tampered_binding_fails_before_envelope_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_park_actor(monkeypatch)
    unbound = CycleRiskEnvelopeStore(tmp_path)
    payload = {
        "authorization_kind": (
            "ai_policy_within_preapproved_strategy_boundary"
        ),
        "limits": _limits(),
    }
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_missing",
    ):
        unbound.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            payload=payload,
            actor=None,
            now="2026-07-30T01:02:00+00:00",
        )

    store, _policy, binding = _bound_store(tmp_path, monkeypatch)
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_expired",
    ):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            payload=payload,
            actor=None,
            now="2026-08-30T01:00:00+00:00",
        )

    binding_path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "outer_strategy_policy_bindings"
        / f"{binding['binding_id']}.json"
    )
    rows = json.loads(binding_path.read_text(encoding="utf-8"))
    rows[0]["policy_digest"] = "0" * 64
    binding_path.write_text(
        json.dumps(rows),
        encoding="utf-8",
    )
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            payload=payload,
            actor=None,
            now="2026-07-30T01:02:00+00:00",
        )

    envelope_path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "2026-07-30_DAY.json"
    )
    assert not envelope_path.exists()


def test_tampered_policy_is_rejected_when_preview_is_reverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, policy, _binding = _bound_store(tmp_path, monkeypatch)
    envelope = store.authorize_envelope(
        cycle_id="2026-07-30_DAY",
        plan=_plan(),
        payload={
            "authorization_kind": (
                "ai_policy_within_preapproved_strategy_boundary"
            ),
            "limits": _limits(),
        },
        actor=None,
        now="2026-07-30T01:02:00+00:00",
    )
    policy_path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "risk_envelopes"
        / "outer_strategy_policies"
        / f"{policy['policy_id']}.json"
    )
    rows = json.loads(policy_path.read_text(encoding="utf-8"))
    rows[0]["summary"] = "tampered"
    policy_path.write_text(json.dumps(rows), encoding="utf-8")

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.verify_preview(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            envelope_authorization_id=envelope[
                "envelope_authorization_id"
            ],
            preview=_preview(),
            now="2026-07-30T01:03:00+00:00",
        )


def test_only_park_can_write_policy_or_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOLDBOT_ACCESS_EMAIL", "park@example.com")
    store = CycleRiskEnvelopeStore(tmp_path)
    for actor in (
        {"email": "ai@example.com", "transport": "ai"},
        {"email": "park@example.com", "transport": "ai"},
        {
            "email": "park@example.com",
            "transport": "public_gateway",
        },
    ):
        with pytest.raises(
            CycleRiskEnvelopeError,
            match="outer_strategy_policy_invalid",
        ):
            store.authorize_outer_policy(
                payload=_outer_policy_payload(),
                actor=actor,
                now="2026-07-30T01:00:00+00:00",
            )

    verified_actor = _verified_park_actor(monkeypatch)
    policy = store.authorize_outer_policy(
        payload=_outer_policy_payload(),
        actor=verified_actor,
        now="2026-07-30T01:00:00+00:00",
    )
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.bind_supervisor_outer_policy(
            payload=_binding_payload(policy),
            actor={"email": "ai@example.com", "transport": "ai"},
            now="2026-07-30T01:01:00+00:00",
        )


def test_policy_preflight_uses_trusted_server_clock_not_caller_as_of(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _policy, binding = _bound_store(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_ID",
        binding["binding_id"],
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_VERSION",
        str(binding["binding_version"]),
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_DIGEST",
        binding["binding_digest"],
    )
    plane = StrategyControlPlane(
        tmp_path,
        authorization_clock=lambda: "2026-08-30T01:00:00+00:00",
    )

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_expired",
    ):
        plane.verify_supervisor_outer_policy(
            at="2026-07-30T01:02:00+00:00",
        )


def test_policy_writes_use_domain_clock_not_control_as_of(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    trusted_now = "2026-07-30T06:30:00+00:00"
    plane = StrategyControlPlane(
        tmp_path,
        authorization_clock=lambda: trusted_now,
    )
    policy_result = plane.control(
        "2026-07-30_DAY",
        "authorize_outer_strategy_policy",
        _outer_policy_payload(),
        actor=actor,
        now="2020-01-01T00:00:00+00:00",
    )
    policy = policy_result["outer_strategy_policy"]
    binding_result = plane.control(
        "2026-07-30_DAY",
        "bind_supervisor_outer_strategy_policy",
        _binding_payload(policy),
        actor=actor,
        now="2030-01-01T00:00:00+00:00",
    )

    assert policy["authorized_at"] == trusted_now
    assert (
        binding_result["outer_strategy_policy_binding"]["bound_at"]
        == trusted_now
    )


def test_partial_environment_binding_is_invalid_not_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_ID",
        "paper-supervisor-grid",
    )
    store = CycleRiskEnvelopeStore(tmp_path)
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        store.authorize_envelope(
            cycle_id="2026-07-30_DAY",
            plan=_plan(),
            payload={
                "authorization_kind": (
                    "ai_policy_within_preapproved_strategy_boundary"
                ),
                "limits": _limits(),
            },
            actor=None,
            now="2026-07-30T01:02:00+00:00",
        )


def test_cloud_tracked_binding_precedes_stale_host_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_v1, _policy_v1, binding_v1 = _bound_store(
        tmp_path,
        monkeypatch,
    )
    policy_v2 = store_v1.authorize_outer_policy(
        payload=_outer_policy_v2_payload(),
        actor=_verified_park_actor(monkeypatch),
        now="2026-07-31T01:00:00+00:00",
    )
    binding_v2 = store_v1.bind_supervisor_outer_policy(
        payload=_binding_payload(
            policy_v2,
            binding_version=2,
            summary="Bind Paper Supervisor to Park policy v2",
        ),
        actor=_verified_park_actor(monkeypatch),
        now="2026-07-31T01:01:00+00:00",
    )
    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_ID",
        binding_v1["binding_id"],
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_VERSION",
        str(binding_v1["binding_version"]),
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_DIGEST",
        binding_v1["binding_digest"],
    )
    config = deepcopy(dualtrack_config())
    config["convergence"]["outer_policy_binding"] = {
        "binding_id": binding_v2["binding_id"],
        "binding_version": binding_v2["binding_version"],
        "binding_digest": binding_v2["binding_digest"],
    }
    monkeypatch.setattr(
        strategy_control_plane_module,
        "dualtrack_config",
        lambda: config,
    )

    plane = StrategyControlPlane(
        tmp_path,
        authorization_clock=lambda: "2026-07-31T01:02:00+00:00",
    )
    verified = plane.verify_supervisor_outer_policy()

    assert verified["binding_version"] == 2
    selected_policy = plane.risk_envelopes.outer_policy(
        verified["policy_id"],
        verified["policy_version"],
    )
    assert selected_policy["allowed_directions"] == [
        "long",
        "neutral",
        "short",
    ]


@pytest.mark.parametrize(
    "selector",
    [
        {},
        {
            "binding_id": "paper-supervisor-grid",
            "binding_version": 2,
        },
        {
            "binding_id": "paper-supervisor-grid",
            "binding_version": 2,
            "binding_digest": "not-a-digest",
        },
    ],
)
def test_invalid_cloud_tracked_binding_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: dict,
) -> None:
    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    config = deepcopy(dualtrack_config())
    config["convergence"]["outer_policy_binding"] = selector
    monkeypatch.setattr(
        strategy_control_plane_module,
        "dualtrack_config",
        lambda: config,
    )

    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        StrategyControlPlane(tmp_path)


def test_repo_cloud_binding_selector_is_exact_v2_release() -> None:
    assert dualtrack_config()["convergence"][
        "outer_policy_binding"
    ] == {
        "binding_id": "paper-supervisor-grid",
        "binding_version": 2,
        "binding_digest": (
            "38e57cb38a01f6d9a3e5b9fcf72a0cb"
            "8cd823179841f3dcb012261ab0798dcdb"
        ),
    }


def test_policy_rotation_retains_prior_exact_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, policy_v1, binding_v1 = _bound_store(tmp_path, monkeypatch)
    actor = _verified_park_actor(monkeypatch)
    writer = CycleRiskEnvelopeStore(tmp_path)
    policy_v2 = writer.authorize_outer_policy(
        payload=_outer_policy_payload(
            version=2,
            summary="Park-approved Grid policy boundary v2",
            limits=_limits(max_notional_per_grid="55"),
        ),
        actor=actor,
        now="2026-07-31T01:00:00+00:00",
    )
    binding_v2 = writer.bind_supervisor_outer_policy(
        payload=_binding_payload(
            policy_v2,
            binding_version=2,
            summary="Bind the Paper Supervisor to Park policy v2",
        ),
        actor=actor,
        now="2026-07-31T01:01:00+00:00",
    )

    assert store.outer_policy(
        policy_v1["policy_id"],
        policy_v1["version"],
    ) == policy_v1
    assert store.supervisor_binding(
        binding_v1["binding_id"],
        binding_v1["binding_version"],
    ) == binding_v1
    assert writer.outer_policy(
        policy_v2["policy_id"],
        policy_v2["version"],
    ) == policy_v2
    assert writer.supervisor_binding(
        binding_v2["binding_id"],
        binding_v2["binding_version"],
    ) == binding_v2

    old_envelope = store.authorize_ai_candidate_envelope(
        cycle_id="2026-07-30_DAY",
        proposal=_ai_proposal(),
        preview=_preview(),
        now="2026-07-31T01:02:00+00:00",
    )
    rotated = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding_v2["binding_id"],
            "binding_version": binding_v2["binding_version"],
            "binding_digest": binding_v2["binding_digest"],
        },
    )
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="outer_strategy_policy_invalid",
    ):
        rotated.verify_preview(
            cycle_id="2026-07-30_DAY",
            plan={
                **_ai_proposal(),
                "strategy_plan_id": "strategy-plan-v1",
                "version": 1,
                "source_proposal_ids": [
                    _ai_proposal()["proposal_id"]
                ],
            },
            envelope_authorization_id=old_envelope[
                "envelope_authorization_id"
            ],
            preview=_preview(),
            now="2026-07-31T01:03:00+00:00",
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


def test_dca_ai_candidate_verifies_before_plan_creation_then_binds_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _verified_park_actor(monkeypatch)
    writer = CycleRiskEnvelopeStore(tmp_path)
    policy = writer.authorize_outer_policy(
        payload={
            "policy_id": "park-dca-policy",
            "version": 1,
            "strategy_type": "dca",
            "direction": "long",
            "summary": "Park-approved DCA boundary",
            "expires_at": "2026-08-30T01:00:00+00:00",
            "limits": _dca_limits(),
        },
        actor=actor,
        now="2026-07-30T01:00:00+00:00",
    )
    binding = writer.bind_supervisor_outer_policy(
        payload={
            **_binding_payload(policy),
            "binding_id": "paper-supervisor-dca",
        },
        actor=actor,
        now="2026-07-30T01:01:00+00:00",
    )
    store = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding["binding_id"],
            "binding_version": binding["binding_version"],
            "binding_digest": binding["binding_digest"],
        },
    )
    proposal = {
        "proposal_id": "proposal-ai-dca",
        "cycle_id": "2026-07-30_DAY",
        "source": "ai",
        "preview_id": "dca-preview-fresh",
        "direction": "long",
        "style": "steady",
        "strategy_type": "dca",
        "range": {},
        "key_levels": [],
        "grid": {},
        "signal": {},
        "tp_sl": {},
        "risk_budget": {},
        "intraday_rules": [],
    }
    envelope = store.authorize_ai_candidate_envelope(
        cycle_id="2026-07-30_DAY",
        proposal=proposal,
        preview=_dca_preview(),
        now="2026-07-30T01:02:00+00:00",
    )
    verification = store.verify_preview(
        cycle_id="2026-07-30_DAY",
        plan={},
        envelope_authorization_id=envelope[
            "envelope_authorization_id"
        ],
        preview=_dca_preview(),
        proposal=proposal,
        now="2026-07-30T01:03:00+00:00",
    )
    linked = store.bind_execution_plan(
        verification,
        {
            **_dca_plan(),
            "strategy_plan_id": "strategy-plan-dca-execution",
            "version": 1,
            "preview_id": "dca-preview-fresh",
            "dca": {
                "max_additions": 2,
                "notional_per_addition": "50",
                "total_possible_notional": "150",
                "entries": [],
                "aggregate_take_profit": {},
            },
            "risk_budget": {
                "actual_leverage_at_full_depth": "3",
                "maximum_loss_at_full_depth": "100",
            },
        },
    )
    assert linked["execution_plan"]["strategy_plan_id"] == (
        "strategy-plan-dca-execution"
    )
    with pytest.raises(
        CycleRiskEnvelopeError,
        match="plan_identity_conflict",
    ):
        store.bind_execution_plan(
            verification,
            {
                **_dca_plan(),
                "strategy_plan_id": "strategy-plan-dca-mutated",
                "version": 1,
                "preview_id": "dca-preview-fresh",
                "dca": {
                    "max_additions": 2,
                    "notional_per_addition": "50",
                    "total_possible_notional": "150",
                    "entry_levels": [4000, 3990],
                    "entries": [],
                    "aggregate_take_profit": {},
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
        control_code="frozen_grid_preview_market_moved"
    )["classification"] == TRANSIENT
    assert classify_blocker(
        control_code=(
            "no grid between 30 and 70 levels can deliver planned net "
            "profit of 10.00 USD per grid within 10x capacity"
        )
    )["machine_code"] == "unknown_blocker"
    assert classify_blocker(
        evidence={"tick_health": "missing", "tick_episode_seconds": 600}
    )["machine_code"] == "execution_tick_heartbeat_temporarily_missing"
    assert classify_blocker(
        evidence={"tick_health": "missing", "tick_episode_seconds": 601}
    ) == {
            "classifier_version": "paper-supervisor-blocker-v5",
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
    expired = classify_blocker(
        evidence={"outer_strategy_policy": "expired"},
    )
    assert expired["machine_code"] == "outer_strategy_policy_expired"
    assert expired["classification"] == STRUCTURAL
    for code in (
        "outer_strategy_policy_missing",
        "outer_strategy_policy_expired",
        "outer_strategy_policy_invalid",
        "outer_strategy_policy_envelope_out_of_bounds",
    ):
        classified = classify_blocker(control_code=code)
        assert classified["machine_code"] == code
        assert classified["classification"] == STRUCTURAL


def test_control_plane_audits_human_policy_and_cycle_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _verified_park_actor(monkeypatch)
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-30_DAY"
    plan = _plan()
    plan["status"] = "active"
    plane._write_plan(plan)
    policy = plane.control(
        cycle_id,
        "authorize_outer_strategy_policy",
        _outer_policy_payload(limits=_limits()),
        actor=actor,
        now="2026-07-30T01:00:00+00:00",
    )
    binding = plane.control(
        cycle_id,
        "bind_supervisor_outer_strategy_policy",
        _binding_payload(policy["outer_strategy_policy"]),
        actor=actor,
        now="2026-07-30T01:01:00+00:00",
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
    assert (
        binding["outer_strategy_policy_binding"]["policy_digest"]
        == policy["outer_strategy_policy"]["policy_digest"]
    )
    assert envelope["cycle_risk_envelope"]["strategy_plan_id"] == plan["strategy_plan_id"]
    assert envelope["audit_recorded"] is True
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "outputs").rglob("*")
        if path.is_file()
    )
    assert "signed-park-assertion" not in persisted
