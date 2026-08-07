from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import services.cycle_risk_envelope as risk_envelope_module
import services.strategy_control_plane as strategy_control_plane_module
from schemas.accounting import build_accounting_snapshot
from services.cycle_risk_envelope import (
    CycleRiskEnvelopeError,
    CycleRiskEnvelopeStore,
)
from services.dualtrack_execution_adapter import build_execution_engine_adapter
from services.dualtrack_config import dualtrack_config
from services.grid_sizing import GridPreviewInfeasibleError
from services.paper_degradation_events import PaperDegradationEventStore
from services.paper_supervisor_store import PaperSupervisorStore
from services.paper_supervisor_recovery import (
    PAPER_CONTINUITY_PROPOSAL_SOURCE,
    authoritative_paper_equity,
    build_dca_recovery_candidate,
    build_grid_recovery_candidate,
    load_immediate_previous_verified_plan,
    paper_continuity_proposal_digest,
    verified_ai_source_proposal,
)
from services.strategy_control_plane import StrategyControlPlane
from services.strategy_plan_execution import build_plan_grid_entry_commands
from services.supervisor_execution_profile import PAPER_CONTINUOUS


def _market(close: float = 110.0) -> dict:
    def bars(timeframe: str, span: float) -> list[dict]:
        result = []
        for index in range(20):
            value = close - 1.0 + index * 0.05
            result.append(
                {
                    "timestamp": (
                        f"2026-06-{index + 1:02d}T00:00:00+00:00"
                        if timeframe == "1d"
                        else f"2026-07-02T{(index % 6) * 4:02d}:00:00+00:00"
                    ),
                    "open": value - 0.1,
                    "high": value + span / 2.0,
                    "low": value - span / 2.0,
                    "close": value,
                }
            )
        return result

    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "binance_usdm",
        "source_mode": "binance_usdm",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": close,
        "latest_timestamp": "2026-08-06T01:00:00+00:00",
        "bars": bars("1m", 1.0),
        "strategy_timeframes": {
            "1d": {
                "timeframe": "1d",
                "provider": "derived:binance_usdm",
                "is_synthetic": False,
                "bars": bars("1d", 10.0),
            },
            "4h": {
                "timeframe": "4h",
                "provider": "derived:binance_usdm",
                "is_synthetic": False,
                "bars": bars("4h", 4.0),
            },
        },
    }


def _ai_field_sources(source: str = "ai") -> dict[str, str]:
    return {
        field: source
        for field in (
            "direction",
            "style",
            "range",
            "key_levels",
            "grid",
            "signal",
            "tp_sl",
            "risk_budget",
            "intraday_rules",
        )
    }


def test_authoritative_paper_equity_has_no_historical_fallback() -> None:
    assert authoritative_paper_equity(
        {"account": {"equity": None, "ending_cash": "9000"}}
    ) == 9000.0
    with pytest.raises(
        ValueError,
        match="authoritative_execution_account_missing",
    ):
        authoritative_paper_equity(
            {"account": {"equity": 0, "ending_cash": "nan"}}
        )


def test_grid_recovery_recenters_and_caps_from_authoritative_equity() -> None:
    source_plan = {
        "cycle_id": "2026-08-05_NIGHT",
        "strategy_plan_id": "strategy-plan-prior",
        "version": 3,
        "strategy_type": "grid",
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 4000, "high": 4100},
        "grid": {
            "count": 38,
            "mode": "arithmetic",
            "notional_per_grid": 10000,
            "out_of_range": "exit_only",
        },
    }
    candidate = build_grid_recovery_candidate(
        cycle_id="2026-08-06_DAY",
        source_plan=source_plan,
        source_proposal={},
        provenance={"source_kind": "test"},
        market=_market(110.0),
        authoritative_equity=5000.0,
        config=dualtrack_config(),
        outer_policy={
            "limits": {
                "max_actual_leverage": "20",
                "max_full_depth_loss": "1000000",
                "max_notional_per_grid": "1000",
                "min_grid_count": "0",
                "max_grid_count": "unbounded",
            }
        },
        supervisor_attempt_id="attempt-new",
        provider_readiness=None,
    )

    preview = candidate["preview"]
    assert preview["range"]["low"] < 110.0 < preview["range"]["high"]
    assert preview["range"] != source_plan["range"]
    assert preview["grid"]["notional_per_grid"] <= 1000.0
    assert preview["risk"]["equity"] == 5000.0
    assert preview["supervisor_preview_nonce"] == "attempt-new"
    assert candidate["recovery"]["risk_repriced"] is True
    assert candidate["proposal"]["analysis"]["new_ai_judgment"] is False

    second = build_grid_recovery_candidate(
        cycle_id="2026-08-06_DAY",
        source_plan=source_plan,
        source_proposal={},
        provenance={"source_kind": "test"},
        market=_market(110.0),
        authoritative_equity=5000.0,
        config=dualtrack_config(),
        outer_policy={
            "limits": {
                "max_actual_leverage": "20",
                "max_full_depth_loss": "1000000",
                "max_notional_per_grid": "1000",
                "min_grid_count": "0",
                "max_grid_count": "unbounded",
            }
        },
        supervisor_attempt_id="attempt-next",
        provider_readiness=None,
    )
    assert second["preview"]["preview_id"] != preview["preview_id"]
    assert second["proposal"]["proposal_id"] != candidate["proposal"][
        "proposal_id"
    ]


def test_dca_recovery_recenters_and_caps_without_grid_conversion() -> None:
    candidate = build_dca_recovery_candidate(
        cycle_id="2026-08-06_DAY",
        source_plan={
            "cycle_id": "2026-08-05_NIGHT",
            "strategy_plan_id": "strategy-plan-prior-dca",
            "version": 3,
            "strategy_type": "dca",
            "direction": "long",
            "style": "steady",
            "dca": {
                "max_additions": 6,
                "notional_per_addition": 2000,
            },
            "risk_budget": {"selected_leverage": 10},
        },
        source_proposal={
            "proposal_id": "proposal-prior-ai-dca",
            "source": "ai",
            "source_proposal_digest": "a" * 64,
            "root_ai_source_proposal_id": "proposal-prior-ai-dca",
            "root_ai_source_proposal_digest": "a" * 64,
        },
        provenance={"source_kind": "test"},
        market=_market(110.0),
        authoritative_equity=5000.0,
        config=dualtrack_config(),
        outer_policy={
            "strategy_type": "dca",
            "limits": {
                "max_actual_leverage": "20",
                "max_full_depth_loss": "1000000",
                "max_notional_per_addition": "1000",
                "max_total_possible_notional": "5000",
                "min_additions": "1",
                "max_additions": "6",
            },
        },
        supervisor_attempt_id="attempt-dca",
        provider_readiness=None,
    )

    assert candidate["proposal"]["source"] == (
        PAPER_CONTINUITY_PROPOSAL_SOURCE
    )
    assert candidate["preview"]["strategy_type"] == "dca"
    assert candidate["request"]["strategy_type"] == "dca"
    assert candidate["preview"]["market"]["price"] == 110.0
    assert candidate["preview"]["dca"]["notional_per_addition"] <= 1000
    assert candidate["preview"]["dca"]["total_possible_notional"] <= 5000
    assert candidate["recovery"]["risk_repriced"] is True


def test_previous_plan_fallback_requires_exact_verified_terminal_package(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="verified_prior_cycle_plan_missing"):
        load_immediate_previous_verified_plan(
            tmp_path,
            "2026-08-06_DAY",
        )


def test_previous_plan_fallback_uses_immediate_verified_package_only(
    tmp_path: Path,
) -> None:
    cycle_id = "2026-08-05_NIGHT"
    proposal = {
        "proposal_id": "proposal-prior-ai",
        "source": "ai",
    }
    package = {
        "schema_version": "strategy-cycle-package-v1",
        "cycle_id": cycle_id,
        "status": "closed",
        "strategy_plan": {
            "cycle_id": cycle_id,
            "strategy_plan_id": "strategy-plan-prior",
            "version": 2,
            "strategy_type": "grid",
            "source_proposal_ids": ["proposal-prior-ai"],
            "field_sources": _ai_field_sources(),
        },
        "proposals": [proposal],
    }
    package["package_hash"] = hashlib.sha256(
        json.dumps(
            package,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    path = (
        tmp_path
        / "dualtrack"
        / "strategy_cycle_packages"
        / f"{cycle_id}.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([package]), encoding="utf-8")

    loaded = load_immediate_previous_verified_plan(
        tmp_path,
        "2026-08-06_DAY",
    )

    assert loaded["plan"]["strategy_plan_id"] == "strategy-plan-prior"
    assert loaded["source_proposal"]["proposal_id"] == proposal["proposal_id"]
    assert loaded["source_proposal"]["root_ai_source_proposal_id"] == (
        proposal["proposal_id"]
    )
    assert loaded["provenance"]["source_cycle_id"] == cycle_id
    assert loaded["provenance"]["source_package_hash"] == package[
        "package_hash"
    ]


def test_recovery_lineage_can_cross_more_than_one_cycle(
    tmp_path: Path,
) -> None:
    def digest(value: dict) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    def package(
        cycle_id: str,
        proposal: dict,
        *,
        plan_id: str,
        version: int,
    ) -> dict:
        row = {
            "schema_version": "strategy-cycle-package-v1",
            "cycle_id": cycle_id,
            "status": "closed",
            "strategy_plan": {
                "cycle_id": cycle_id,
                "strategy_plan_id": plan_id,
                "version": version,
                "strategy_type": "grid",
                "source_proposal_ids": [proposal["proposal_id"]],
                "field_sources": _ai_field_sources(proposal["source"]),
            },
            "proposals": [proposal],
        }
        row["package_hash"] = digest(row)
        path = (
            tmp_path
            / "dualtrack"
            / "strategy_cycle_packages"
            / f"{cycle_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([row]), encoding="utf-8")
        return row

    root = {
        "proposal_id": "proposal-ai-root",
        "cycle_id": "2026-08-04_NIGHT",
        "source": "ai",
    }
    root_digest = digest(root)
    first_package = package(
        "2026-08-04_NIGHT",
        root,
        plan_id="strategy-plan-ai-root",
        version=1,
    )
    first_recovery = {
        "proposal_id": "proposal-paper-continuity-first",
        "cycle_id": "2026-08-05_DAY",
        "source": PAPER_CONTINUITY_PROPOSAL_SOURCE,
        "analysis": {
            "new_ai_judgment": False,
            "inherited_intent_source": "ai",
            "inherited_source_proposal_id": root["proposal_id"],
            "inherited_source_proposal_digest": root_digest,
            "root_ai_source_proposal_id": root["proposal_id"],
            "root_ai_source_proposal_digest": root_digest,
            "paper_continuity_recovery": {
                "source_kind": (
                    "verified_immediate_previous_cycle_package"
                ),
                "source_cycle_id": "2026-08-04_NIGHT",
                "source_strategy_plan_id": "strategy-plan-ai-root",
                "source_strategy_plan_version": 1,
                "source_package_hash": first_package["package_hash"],
            },
        },
    }
    first_recovery["recovery_proposal_digest"] = (
        paper_continuity_proposal_digest(first_recovery)
    )
    first_recovery_digest = digest(first_recovery)
    second_package = package(
        "2026-08-05_DAY",
        first_recovery,
        plan_id="strategy-plan-recovery-first",
        version=2,
    )
    second_recovery = {
        "proposal_id": "proposal-paper-continuity-second",
        "cycle_id": "2026-08-05_NIGHT",
        "source": PAPER_CONTINUITY_PROPOSAL_SOURCE,
        "analysis": {
            "new_ai_judgment": False,
            "inherited_intent_source": "ai",
            "inherited_source_proposal_id": first_recovery["proposal_id"],
            "inherited_source_proposal_digest": first_recovery_digest,
            "root_ai_source_proposal_id": root["proposal_id"],
            "root_ai_source_proposal_digest": root_digest,
            "paper_continuity_recovery": {
                "source_kind": (
                    "verified_immediate_previous_cycle_package"
                ),
                "source_cycle_id": "2026-08-05_DAY",
                "source_strategy_plan_id": (
                    "strategy-plan-recovery-first"
                ),
                "source_strategy_plan_version": 2,
                "source_package_hash": second_package["package_hash"],
            },
        },
    }
    second_recovery["recovery_proposal_digest"] = (
        paper_continuity_proposal_digest(second_recovery)
    )
    package(
        "2026-08-05_NIGHT",
        second_recovery,
        plan_id="strategy-plan-recovery-second",
        version=3,
    )

    loaded = load_immediate_previous_verified_plan(
        tmp_path,
        "2026-08-06_DAY",
    )

    assert loaded["source_proposal"]["proposal_id"] == (
        second_recovery["proposal_id"]
    )
    assert loaded["source_proposal"]["root_ai_source_proposal_id"] == (
        root["proposal_id"]
    )

    forged = dict(second_recovery)
    forged["recovery_proposal_digest"] = "forged"
    with pytest.raises(
        ValueError,
        match="verified_ai_strategy_intent_missing",
    ):
        verified_ai_source_proposal(
            {
                "strategy_type": "grid",
                "source_proposal_ids": [forged["proposal_id"]],
                "field_sources": _ai_field_sources(forged["source"]),
            },
            [forged],
            output_root=tmp_path,
            package_cycle_id="2026-08-05_NIGHT",
        )


def test_previous_plan_fallback_rejects_non_ai_or_ambiguous_source(
    tmp_path: Path,
) -> None:
    cycle_id = "2026-08-05_NIGHT"
    package = {
        "schema_version": "strategy-cycle-package-v1",
        "cycle_id": cycle_id,
        "status": "closed",
        "strategy_plan": {
            "cycle_id": cycle_id,
            "strategy_plan_id": "strategy-plan-prior",
            "version": 2,
            "strategy_type": "grid",
            "source_proposal_ids": ["proposal-prior-human"],
        },
        "proposals": [
            {
                "proposal_id": "proposal-prior-human",
                "source": "human",
            }
        ],
    }
    package["package_hash"] = hashlib.sha256(
        json.dumps(
            package,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    path = (
        tmp_path
        / "dualtrack"
        / "strategy_cycle_packages"
        / f"{cycle_id}.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([package]), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="verified_ai_strategy_intent_missing",
    ):
        load_immediate_previous_verified_plan(
            tmp_path,
            "2026-08-06_DAY",
        )

    with pytest.raises(
        ValueError,
        match="verified_ai_strategy_intent_missing",
    ):
        verified_ai_source_proposal(
            {
                "source_proposal_ids": [
                    "proposal-ai-one",
                    "proposal-ai-two",
                ]
            },
            [
                {"proposal_id": "proposal-ai-one", "source": "ai"},
                {"proposal_id": "proposal-ai-two", "source": "ai"},
            ],
        )


def test_control_plane_recovery_candidate_authorizes_and_locks_exact_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    actor = {
        "email": "park@example.com",
        "transport": "public_gateway",
        "_access_assertion": "signed-park-assertion",
    }
    writer = CycleRiskEnvelopeStore(tmp_path)
    policy = writer.authorize_outer_policy(
        payload={
            "policy_id": "park-grid-policy",
            "version": 1,
            "strategy_type": "grid",
            "direction": "neutral",
            "summary": "Park-approved Grid policy boundary",
            "expires_at": "2026-08-30T01:00:00+00:00",
            "limits": {
                "max_actual_leverage": "20",
                "max_full_depth_loss": "1000000",
                "max_notional_per_grid": "1000",
                "min_grid_count": "0",
                "max_grid_count": "unbounded",
            },
        },
        actor=actor,
        now="2026-08-06T00:00:00+00:00",
    )
    binding = writer.bind_supervisor_outer_policy(
        payload={
            "binding_id": "paper-supervisor-grid",
            "binding_version": 1,
            "policy_id": policy["policy_id"],
            "policy_version": policy["version"],
            "policy_digest": policy["policy_digest"],
            "summary": "Bind the Paper Supervisor to Park policy v1",
        },
        actor=actor,
        now="2026-08-06T00:01:00+00:00",
    )
    def clock() -> str:
        return "2026-08-06T01:00:00+00:00"
    plane = StrategyControlPlane(
        tmp_path,
        execution_profile=PAPER_CONTINUOUS,
        authorization_clock=clock,
        source_attestation=lambda: {
            "source_sha": "a" * 40,
            "source_tree_sha": "b" * 40,
            "tracked_tree_clean": True,
        },
    )
    plane.risk_envelopes = CycleRiskEnvelopeStore(
        tmp_path,
        supervisor_policy_binding_ref={
            "binding_id": binding["binding_id"],
            "binding_version": binding["binding_version"],
            "binding_digest": binding["binding_digest"],
        },
        execution_profile=PAPER_CONTINUOUS,
        authorization_clock=clock,
        source_attestation=lambda: {
            "source_sha": "a" * 40,
            "source_tree_sha": "b" * 40,
            "tracked_tree_clean": True,
        },
    )
    source = plane.upsert_proposal(
        {
            "proposal_id": "proposal-prior-ai",
            "cycle_id": "2026-08-06_DAY",
            "source": "ai",
            "direction": "neutral",
            "style": "steady",
            "strategy_type": "grid",
            "range": {"low": 4000, "high": 4100},
            "grid": {
                "count": 38,
                "mode": "arithmetic",
                "notional_per_grid": 10000,
                "out_of_range": "exit_only",
            },
        },
        now=clock(),
    )
    plane.lock_production_plan(
        "2026-08-06_DAY",
        selected_proposal_id=source["proposal_id"],
        now=clock(),
    )
    recovery_event = PaperDegradationEventStore(tmp_path).record(
        event_id=(
            "attempt-real-control-plane:recenter-and-reprice:"
            "runtime_not_running_proven"
        ),
        cycle_id="2026-08-06_DAY",
        execution_profile=PAPER_CONTINUOUS,
        bypassed_gate="stale_strategy_candidate_gate",
        original_machine_code="runtime_not_running_proven",
        original_reason="test recovery candidate requires current facts",
        alternative_action=(
            "rebuild_candidate_from_current_market_and_"
            "authoritative_paper_equity"
        ),
        occurred_at=clock(),
    )
    recovery_refs = [
        {
            "event_id": recovery_event["event_id"],
            "event_digest": recovery_event["event_digest"],
        }
    ]

    start_facts = plane.capture_start_facts(
        "2026-08-06_DAY",
        observed_at=clock(),
        market=_market(110.0),
        execution_snapshot={
            "account": {
                "equity": 5000.0,
                "ending_cash": 5000.0,
                "starting_cash": 5000.0,
            },
            "orders": [],
            "positions": [],
        },
        execution_adapter_name="nautilus_paper",
    )
    candidate = plane.build_paper_continuity_candidate(
        "2026-08-06_DAY",
        market=_market(110.0),
        start_facts=start_facts,
        supervisor_attempt_id="attempt-real-control-plane",
        provider_readiness=None,
        degradation_event_refs=recovery_refs,
        now=clock(),
    )
    envelope = plane.authorize_supervisor_ai_envelope(
        "2026-08-06_DAY",
        proposal=candidate["proposal"],
        preview=candidate["preview"],
        supervisor_attempt_id="attempt-real-control-plane",
    )
    facts_digest = start_facts["start_facts_digest"]
    assert candidate["proposal"]["start_facts_digest"] == facts_digest
    assert candidate["preview"]["start_facts_digest"] == facts_digest
    assert envelope["source_proposal"]["start_facts_digest"] == facts_digest
    assert candidate["proposal"]["source"] == (
        PAPER_CONTINUITY_PROPOSAL_SOURCE
    )
    assert envelope["authorization_kind"] == (
        "paper_continuity_within_preapproved_strategy_boundary"
    )
    with pytest.raises(CycleRiskEnvelopeError, match="plan_identity_conflict"):
        CycleRiskEnvelopeStore(tmp_path).supervisor_candidate_identity(
            cycle_id="2026-08-06_DAY",
            proposal=candidate["proposal"],
            preview=candidate["preview"],
            supervisor_attempt_id="attempt-real-control-plane",
        )
    locked = plane.lock_production_plan(
        "2026-08-06_DAY",
        selected_proposal_id=candidate["proposal"]["proposal_id"],
        cycle_risk_envelope_id=envelope["envelope_authorization_id"],
        now=clock(),
    )
    fail_closed_store = CycleRiskEnvelopeStore(tmp_path)
    with pytest.raises(CycleRiskEnvelopeError, match="plan_identity_conflict"):
        fail_closed_store.verify_candidate_plan_identity(
            cycle_id="2026-08-06_DAY",
            envelope_authorization_id=envelope[
                "envelope_authorization_id"
            ],
            plan=locked,
            now=clock(),
        )
    with pytest.raises(CycleRiskEnvelopeError, match="plan_identity_conflict"):
        fail_closed_store.verify_preview(
            cycle_id="2026-08-06_DAY",
            plan=locked,
            envelope_authorization_id=envelope[
                "envelope_authorization_id"
            ],
            preview=candidate["preview"],
            now=clock(),
        )

    profit_event = PaperDegradationEventStore(tmp_path).record(
        event_id="attempt-real-control-plane:profit-target-degraded",
        cycle_id="2026-08-06_DAY",
        execution_profile=PAPER_CONTINUOUS,
        bypassed_gate="grid_profit_target_gate",
        original_machine_code="grid_profit_target_not_met",
        original_reason="test boundary cap lowers the profit target",
        alternative_action=(
            "accept_lower_paper_profit_target_and_continue"
        ),
        occurred_at=clock(),
    )
    continuity_refs = [
        *recovery_refs,
        {
            "event_id": profit_event["event_id"],
            "event_digest": profit_event["event_digest"],
        },
    ]

    assert locked["range"] == candidate["preview"]["range"]
    assert locked["grid"]["notional_per_grid"] <= 1000.0
    assert locked["cycle_risk_envelope_id"] == envelope[
        "envelope_authorization_id"
    ]
    assert locked["source_proposal_ids"] == [
        candidate["proposal"]["proposal_id"]
    ]
    assert locked["start_facts_digest"] == facts_digest
    request = {
        "direction": locked["direction"],
        "style": locked["style"],
        "strategy_type": "grid",
        "cycle_risk_envelope_id": locked[
            "cycle_risk_envelope_id"
        ],
        "range": dict(locked["range"]),
        "grid": {
            "count": locked["grid"]["count"],
            "mode": locked["grid"]["mode"],
            "notional_per_grid": locked["grid"][
                "notional_per_grid"
            ],
            "notional_mode": "manual",
            "out_of_range": locked["grid"]["out_of_range"],
        },
        "supervisor_attempt_id": "attempt-real-control-plane",
        "paper_continuity_degradation_event_refs": continuity_refs,
    }
    with pytest.raises(GridPreviewInfeasibleError):
        plane._adaptive_start_preview(
            "2026-08-06_DAY",
            request,
            market=_market(110.0),
            account={"equity": 5000.0},
            now=clock(),
        )
    degraded_preview = plane._adaptive_start_preview(
        "2026-08-06_DAY",
        {
            **request,
            "paper_continuity_allow_lower_profit_target": True,
        },
        market=_market(110.0),
        account={"equity": 5000.0},
        now=clock(),
    )
    assert degraded_preview["grid"]["profit_target_met"] is False
    assert degraded_preview["risk"]["capital_budget_exceeded"] is False

    execution = build_execution_engine_adapter(tmp_path)
    accounting = build_accounting_snapshot(
        source_type="execution_snapshot",
        source_name="nautilus_paper",
        source_schema_version="dualtrack-execution-v1",
        scope={"cycle_id": "2026-08-06_DAY"},
        currency="USDT",
        orders=[],
        fills=[],
        positions=[],
        trades=[],
        counts={},
        pnl={"net_realized_pnl": 0.0, "unrealized_pnl": 0.0},
        account={
            "starting_balance": 5000.0,
            "ending_cash": 5000.0,
            "equity": 5000.0,
        },
        completeness={"status": "complete", "limitations": []},
        reconciliation={"status": "pass", "issues": []},
    ).to_dict()
    risk_account = {"equity": 5000.0, "accounting_snapshot": accounting}

    class EmptyPaperAdapter:
        name = "nautilus_paper"

        @staticmethod
        def snapshot(cycle_id: str) -> dict:
            return execution.snapshot(cycle_id)

        @staticmethod
        def reconcile(cycle_id: str) -> dict:
            return execution.reconcile(cycle_id)

    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: EmptyPaperAdapter(),
    )
    monkeypatch.setattr(
        strategy_control_plane_module,
        "current_cloud_ai_provider_readiness",
        lambda *_args, **_kwargs: {"readiness_digest": "c" * 64},
    )
    monkeypatch.setattr(
        plane,
        "_require_paper_execution_tick",
        lambda *_args, **_kwargs: None,
    )
    store = PaperSupervisorStore(tmp_path)
    with store.try_lease(
        "2026-08-06_DAY",
        holder_id="integration-test",
    ) as lease:
        assert lease is not None
        lease.record_pre_intent_started(
            attempt_id="attempt-real-control-plane",
            observed_at=clock(),
            phase_scope="create_or_prepare",
        )
        prepared = plane.control(
            "2026-08-06_DAY",
            "prepare_start",
            {
                **request,
                "paper_continuity_allow_lower_profit_target": True,
            },
            market=_market(110.0),
            account={"equity": 5000.0},
            current_start_facts=start_facts,
            now=clock(),
        )

    assert prepared["preview"]["grid"]["profit_target_met"] is False
    assert prepared["preview"]["start_facts_digest"] == facts_digest
    assert prepared[
        "paper_continuity_allow_lower_profit_target"
    ] is True
    stored_prepared = plane._load_prepared_start(
        "2026-08-06_DAY",
        prepared["prepared_start_id"],
    )
    assert stored_prepared[
        "paper_continuity_allow_lower_profit_target"
    ] is True
    adjusted = plane._plan_from_preview(
        locked,
        degraded_preview,
        now=clock(),
    )
    assert adjusted["start_facts_digest"] == facts_digest
    commands = build_plan_grid_entry_commands(
        adjusted,
        timestamp=clock(),
    )
    risk_request = plane._grid_risk_request(
        "2026-08-06_DAY",
        action_class="increase_exposure",
        intent="start_grid",
        plan=adjusted,
        commands=commands,
        account=risk_account,
        market=_market(110.0),
        adapter=EmptyPaperAdapter(),
        timestamp=clock(),
        replaced_order_ids=None,
        retained_order_ids=None,
    )
    raw_risk = plane.risk_port.evaluate(risk_request).to_dict()
    assert {
        row["code"] for row in raw_risk["blockers"]
    } == {"grid_profit_target_not_met"}
    canonical_risk = plane._authorize_grid_mutation(
        "2026-08-06_DAY",
        action_class="increase_exposure",
        intent="start_grid",
        plan=adjusted,
        commands=commands,
        account=risk_account,
        market=_market(110.0),
        adapter=EmptyPaperAdapter(),
        timestamp=clock(),
        paper_continuity_allow_lower_profit_target=True,
    )
    assert canonical_risk["paper_continuity_override"][
        "overridden_blocker_codes"
    ] == ["grid_profit_target_not_met"]
    assert canonical_risk["outcome"] == "block"
    without_capability = dict(stored_prepared)
    without_capability.pop(
        "paper_continuity_allow_lower_profit_target"
    )
    assert plane._prepared_start_content_id(without_capability) != prepared[
        "prepared_start_id"
    ]
    canonical_block = {
        "decision_id": "risk-decision-profit-only",
        "action_class": "increase_exposure",
        "blockers": [{"code": "grid_profit_target_not_met"}],
    }
    override = plane._require_paper_continuity_profit_only_decision(
        canonical_block,
        adapter_name="nautilus_paper",
    )
    assert override["overridden_blocker_codes"] == [
        "grid_profit_target_not_met"
    ]
    with pytest.raises(
        ValueError,
        match="paper_continuity_profit_override_invalid",
    ):
        plane._require_paper_continuity_profit_only_decision(
            {
                **canonical_block,
                "blockers": [
                    {"code": "grid_profit_target_not_met"},
                    {"code": "market_snapshot_not_trusted"},
                ],
            },
            adapter_name="nautilus_paper",
        )

    fail_closed_plane = StrategyControlPlane(
        tmp_path,
        authorization_clock=clock,
    )
    with pytest.raises(
        ValueError,
        match="paper_continuity_profile_required",
    ):
        fail_closed_plane._adaptive_start_preview(
            "2026-08-06_DAY",
            {
                **request,
                "paper_continuity_allow_lower_profit_target": True,
            },
            market=_market(110.0),
            account={"equity": 5000.0},
            now=clock(),
        )
    with pytest.raises(
        ValueError,
        match="paper_continuity_profit_override_invalid",
    ):
        fail_closed_plane._require_paper_continuity_profit_only_decision(
            canonical_block,
            adapter_name="nautilus_paper",
        )
