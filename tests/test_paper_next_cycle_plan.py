from __future__ import annotations

import json
import threading
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from services.paper_next_cycle_plan import (
    NextCyclePlanError,
    NextCyclePlanPrecomputer,
    VerifiedWaitingPlanStore,
    build_verified_waiting_plan,
    staged_facts_status,
)
from services.paper_supervisor import PaperSupervisor
from services.paper_supervisor_read_model import (
    build_paper_supervisor_polling_summary,
)
from services.paper_supervisor_store import PaperSupervisorStore
from services.paper_start_facts import build_start_facts
from services.supervisor_execution_profile import PAPER_CONTINUOUS


OBSERVED_AT = "2026-08-07T12:30:00+00:00"
CURRENT = "2026-08-07_DAY"
TARGET = "2026-08-07_NIGHT"


def _market(price: float = 4050.0) -> dict:
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "binance_usdm_futures",
        "source_mode": "execution_venue",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": price,
        "latest_timestamp": OBSERVED_AT,
        "bars": [{"close": price}],
        "strategy_timeframes": {
            "1d": {"bars": [[1, price]]},
            "4h": {"bars": [[1, price]]},
            "1h": {"bars": [[1, price]]},
            "15m": {"bars": [[1, price]]},
        },
    }


def _facts(*, price: float = 4050.0, equity: float = 10000.0) -> dict:
    return build_start_facts(
        cycle_id=TARGET,
        observed_at=OBSERVED_AT,
        market=_market(price),
        execution_snapshot={
            "account": {
                "equity": equity,
                "ending_cash": equity,
                "starting_cash": 10000.0,
            },
            "orders": [],
            "positions": [],
            "fills": [],
        },
        execution_adapter_name="nautilus_paper",
        execution_contract={
            "schema_version": "dualtrack-execution-contract-v1",
            "execution_instrument_id": "XAUUSDT",
            "price_precision": 2,
        },
        outer_policy_preflight={
            "binding_id": "paper-supervisor-grid",
            "binding_version": 2,
            "binding_digest": "a" * 64,
            "policy_id": "park-paper-grid",
            "policy_version": 2,
            "policy_digest": "b" * 64,
            "policy_expires_at": "2026-08-31T00:00:00+00:00",
            "passed": True,
        },
        source_attestation={
            "source_sha": "c" * 40,
            "source_tree_sha": "d" * 40,
            "tracked_tree_clean": True,
        },
    )


def _candidate(facts: dict) -> dict:
    return {
        "proposal_id": "proposal-ai-next-cycle",
        "proposal_digest": "1" * 64,
        "preview_id": "preview-next-cycle",
        "preview_digest": "2" * 64,
        "facts_digest": None,
        "confirmation_digest": None,
        "strategy_type": "grid",
        "direction": "neutral",
        "limits": {
            "max_actual_leverage": "10",
            "max_full_depth_loss": "1000",
            "max_notional_per_grid": "5000",
            "min_grid_count": "20",
            "max_grid_count": "20",
        },
        "start_facts_digest": facts["start_facts_digest"],
    }


def _plan(facts: dict) -> dict:
    return {
        "schema_version": "strategy-plan-v1",
        "strategy_plan_id": "strategy-plan-2026-08-07_NIGHT-1-test",
        "cycle_id": TARGET,
        "version": 1,
        "status": "active",
        "locked_at": OBSERVED_AT,
        "strategy_type": "grid",
        "direction": "neutral",
        "style": "steady",
        "source_proposal_ids": ["proposal-ai-next-cycle"],
        "field_sources": {},
        "range": {"low": 4000.0, "high": 4100.0},
        "grid": {"count": 20, "notional_per_grid": 5000.0},
        "risk_budget": {"actual_leverage": 10.0},
        "start_facts_digest": facts["start_facts_digest"],
        "cycle_risk_envelope_id": "envelope-next-cycle",
    }


def _envelope() -> dict:
    return {
        "cycle_id": TARGET,
        "envelope_authorization_id": "envelope-next-cycle",
        "authorization_digest": "3" * 64,
    }


def _artifact(facts: dict | None = None) -> dict:
    start_facts = facts or _facts()
    return build_verified_waiting_plan(
        current_cycle_id=CURRENT,
        target_cycle_id=TARGET,
        generated_at=OBSERVED_AT,
        validated_at=OBSERVED_AT,
        valid_from="2026-08-07T13:00:00+00:00",
        expires_at="2026-08-08T01:00:00+00:00",
        execution_profile=PAPER_CONTINUOUS,
        start_facts=start_facts,
        candidate_identity=_candidate(start_facts),
        projected_plan=_plan(start_facts),
        envelope=_envelope(),
    )


class _RiskEnvelopes:
    @staticmethod
    def verify_candidate_plan_identity(**_kwargs):
        return {"passed": True}


class _StartFacts:
    def __init__(self, facts: dict) -> None:
        self.facts = facts

    def require(self, cycle_id: str, digest: str) -> dict:
        assert cycle_id == TARGET
        assert digest == self.facts["start_facts_digest"]
        return self.facts


class _Plane:
    def __init__(self, facts: dict) -> None:
        self.facts = facts
        self.start_facts = _StartFacts(facts)
        self.risk_envelopes = _RiskEnvelopes()

    @staticmethod
    def verify_supervisor_outer_policy() -> dict:
        return {"passed": True}

    @staticmethod
    def active_plan(_cycle_id: str):
        return None

    def supervisor_candidate_identity(self, *_args, **_kwargs) -> dict:
        return _candidate(self.facts)

    @staticmethod
    def authorize_supervisor_ai_envelope(*_args, **_kwargs) -> dict:
        return _envelope()

    def project_production_plan(self, *_args, **_kwargs) -> dict:
        return _plan(self.facts)


class _SupervisorRiskEnvelopes:
    def envelope(self, cycle_id: str, envelope_id: str) -> dict:
        assert cycle_id == TARGET
        assert envelope_id == "envelope-next-cycle"
        return _envelope()


class _SupervisorPlane:
    def __init__(self, facts: dict) -> None:
        self.start_facts = _StartFacts(facts)
        self.risk_envelopes = _SupervisorRiskEnvelopes()
        self.plan = None

    @staticmethod
    def verify_supervisor_outer_policy() -> dict:
        return {"passed": True}

    @staticmethod
    def proposals(cycle_id: str) -> list[dict]:
        assert cycle_id == TARGET
        return [{"proposal_id": "proposal-ai-next-cycle"}]

    def lock_production_plan(self, *_args, **kwargs) -> dict:
        self.plan = _plan(self.start_facts.facts)
        self.plan["locked_at"] = kwargs["now"]
        return deepcopy(self.plan)


class _Precomputer(NextCyclePlanPrecomputer):
    def _require_current_running(self, _cycle_id: str) -> None:
        return None


def _heartbeat(_cycle_id: str) -> dict:
    return {
        "cycle_id": CURRENT,
        "event": "live_tick_heartbeat",
        "ts": OBSERVED_AT,
        "detail": {
            "runner": "dualtrack-live-tick",
            "ledger_refreshed": True,
        },
    }


def test_precompute_is_append_only_zero_control_and_concurrent_idempotent(
    tmp_path: Path,
) -> None:
    facts = _facts()
    plane = _Plane(facts)
    calls = 0
    calls_lock = threading.Lock()

    def builder(target_cycle_id: str, observed_at: str) -> dict:
        nonlocal calls
        assert target_cycle_id == TARGET
        assert observed_at == OBSERVED_AT
        with calls_lock:
            calls += 1
        return {
            "proposal": {
                "proposal_id": "proposal-ai-next-cycle",
                "strategy_type": "grid",
            },
            "preview": {
                "preview_id": "preview-next-cycle",
                "strategy_type": "grid",
            },
        }

    service = _Precomputer(
        tmp_path,
        plane=plane,
        candidate_builder=builder,
        execution_profile=PAPER_CONTINUOUS,
        heartbeat_provider=_heartbeat,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: service.run(observed_at=OBSERVED_AT),
                range(2),
            )
        )

    assert calls == 1
    assert {row["status"] for row in results} == {
        "verified_waiting",
        "already_verified",
    }
    assert all(row["control_actions_executed"] == 0 for row in results)
    assert all(row["orders_created"] == 0 for row in results)
    stored = VerifiedWaitingPlanStore(tmp_path).load(TARGET)
    assert stored is not None
    assert stored["operations"]["plans_activated"] == 0
    assert stored["operations"]["prepared_starts_created"] == 0


def test_precompute_requires_running_proof_before_provider_call(
    tmp_path: Path,
) -> None:
    calls = 0

    def builder(_target: str, _observed: str) -> dict:
        nonlocal calls
        calls += 1
        return {}

    service = NextCyclePlanPrecomputer(
        tmp_path,
        plane=_Plane(_facts()),
        candidate_builder=builder,
        execution_profile=PAPER_CONTINUOUS,
        heartbeat_provider=_heartbeat,
    )
    with pytest.raises(
        NextCyclePlanError,
        match="next_cycle_current_not_running_proven",
    ):
        service.run(observed_at=OBSERVED_AT)
    assert calls == 0


def test_boundary_facts_validation_accepts_planning_only_change_but_not_price(
) -> None:
    bound = _facts()
    planning_only = json.loads(json.dumps(bound))
    planning_only["observed_at"] = "2026-08-07T12:31:00+00:00"
    # Use a separately valid StartFacts whose planning bundle changed while
    # executable price/account/source remained exact.
    planning_market = _market()
    planning_market["strategy_timeframes"]["4h"]["bars"] = [[2, 4050.0]]
    planning_only = build_start_facts(
        cycle_id=TARGET,
        observed_at="2026-08-07T12:31:00+00:00",
        market=planning_market,
        execution_snapshot={
            "account": {"equity": 10000.0, "ending_cash": 10000.0, "starting_cash": 10000.0},
            "orders": [],
            "positions": [],
            "fills": [],
        },
        execution_adapter_name="nautilus_paper",
        execution_contract=bound["execution_contract"]["value"],
        outer_policy_preflight={**bound["outer_policy"], "passed": True},
        source_attestation=bound["source"],
    )
    artifact = _artifact(bound)

    assert staged_facts_status(
        artifact,
        bound,
        planning_only,
        cycle_id=TARGET,
        observed_at="2026-08-07T13:00:01+00:00",
    ) == {"valid": True, "reason": "verified_waiting_valid"}
    assert staged_facts_status(
        artifact,
        bound,
        _facts(price=4050.01),
        cycle_id=TARGET,
        observed_at="2026-08-07T13:00:01+00:00",
    ) == {
        "valid": False,
        "reason": "next_cycle_verified_plan_facts_stale",
    }


def test_store_rejects_tamper_and_projects_boundary_audit(tmp_path: Path) -> None:
    store = VerifiedWaitingPlanStore(tmp_path)
    artifact = store.record(_artifact())
    event = store.record_boundary_event(
        target_cycle_id=TARGET,
        artifact_digest=artifact["artifact_digest"],
        outcome="adopted",
        reason="verified_waiting_valid",
        observed_at="2026-08-07T13:00:01+00:00",
        current_start_facts_digest=artifact["start_facts_digest"],
        boundary_ai_provider_calls=0,
        deterministic_rebuild_used=False,
    )
    projection = store.projection(TARGET)
    assert projection["status"] == "verified_waiting"
    assert projection["boundary_events"] == [event]
    assert event["boundary_ai_provider_calls"] == 0

    path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "next_cycle_plans"
        / f"{TARGET}.json"
    )
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[0]["plan"]["projected"]["grid"]["count"] = 999
    path.write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(
        NextCyclePlanError,
        match="next_cycle_verified_plan_corrupt",
    ):
        store.load(TARGET)


def test_supervisor_adopts_valid_waiting_plan_with_zero_boundary_ai_calls(
    tmp_path: Path,
) -> None:
    facts = _facts()
    store = VerifiedWaitingPlanStore(tmp_path)
    artifact = store.record(_artifact(facts))
    plane = _SupervisorPlane(facts)
    calls: list[str] = []

    def control(action: str, _payload: dict) -> dict:
        calls.append(action)
        assert action == "validate_verified_waiting_plan"
        return {
            "current_start_facts": facts,
            "control_actions_executed": 0,
            "orders_created": 0,
            "plans_activated": 0,
            "prepared_starts_created": 0,
        }

    supervisor = PaperSupervisor(
        tmp_path,
        plane=plane,
        execution=object(),
        control=control,
        accounting_reconciliation=lambda: "pass",
        execution_profile=PAPER_CONTINUOUS,
    )
    with PaperSupervisorStore(tmp_path).try_lease(
        TARGET,
        holder_id="verified-waiting-adoption",
    ) as lease:
        assert lease is not None
        lease.record_pre_intent_started(
            attempt_id="supervisor-attempt-waiting",
            observed_at="2026-08-07T13:00:01+00:00",
            phase_scope="create_or_prepare",
        )
        plan, request = supervisor._create_plan(
            TARGET,
            lease=lease,
            attempt_id="supervisor-attempt-waiting",
            observed_at="2026-08-07T13:00:01+00:00",
        )

    assert calls == ["validate_verified_waiting_plan"]
    assert plan["strategy_plan_id"] == artifact["plan"]["strategy_plan_id"]
    assert request["boundary_ai_provider_calls"] == 0
    assert request["verified_waiting_validation"] == "adopted"
    assert request["deterministic_rebuild_used"] is False
    event = store.boundary_events(TARGET)[-1]
    assert event["outcome"] == "adopted"
    assert event["boundary_ai_provider_calls"] == 0


def test_polling_read_model_exposes_waiting_successor(tmp_path: Path) -> None:
    artifact = VerifiedWaitingPlanStore(tmp_path).record(_artifact())

    model = build_paper_supervisor_polling_summary(
        tmp_path,
        cycle_id=CURRENT,
        as_of=OBSERVED_AT,
    )

    assert model["status"] == "not_started"
    staged = model["next_cycle_plan"]
    assert staged["status"] == "verified_waiting"
    assert staged["target_cycle_id"] == TARGET
    assert staged["artifact_digest"] == artifact["artifact_digest"]
    assert staged["successor_candidate"]["status"] == "verified_waiting"
