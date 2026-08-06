from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import services.strategy_control_plane as strategy_control_plane_module
from pipelines.dualtrack_cycle_runner import DualTrackCycleRunner
from services.control_audit import (
    append_control_event,
    build_control_event,
)
from services.cycle_risk_envelope import (
    CycleRiskEnvelopeError,
    CycleRiskEnvelopeStore,
)
from services.paper_supervisor import (
    PaperSupervisor,
    SupervisorAttemptDeadline,
    _digest,
)
from services.paper_degradation_events import PaperDegradationEventStore
from services.supervisor_execution_profile import PAPER_CONTINUOUS
from services.paper_supervisor_classifier import classify_blocker
from services.paper_supervisor_store import PaperSupervisorStore
from services.dualtrack_execution_adapter import (
    build_execution_engine_adapter,
)
from services.paper_supervisor_heartbeat import (
    validate_complete_tick_heartbeat,
)
from services.paper_supervisor_identity import (
    build_start_intent_contract,
)
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG
from tests.test_strategy_control_plane import (
    account_context,
    adaptive_grid_payload,
    market,
    proposal,
)
from services.strategy_control_plane import StrategyControlPlane
from services.strategy_recommendation import RecommendationProviderError
from services.cloud_ai_provider import (
    require_cloud_ai_provider_readiness,
)


CYCLE = "2026-07-30_DAY"
T0 = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


def _heartbeat(at: datetime = T0) -> dict:
    return {
        "ts": at.isoformat(),
        "cycle_id": CYCLE,
        "event": "live_tick_heartbeat",
        "detail": {
            "runner": "dualtrack-live-tick",
            "ledger_refreshed": True,
        },
    }


def _plan(version: int, *, preview_id: str) -> dict:
    plan_id = f"strategy-plan-{CYCLE}-{version}-{preview_id}"
    orders = [
        {
            "preview_order_id": f"grid-{index}",
            "side": "buy",
            "price": 100.0 - index,
            "quantity": 1.0,
            "notional": 100.0 - index,
            "sl": 90.0,
            "tp": 110.0,
        }
        for index in range(3)
    ]
    return {
        "schema_version": "strategy-plan-v1",
        "strategy_plan_id": plan_id,
        "cycle_id": CYCLE,
        "version": version,
        "status": "active",
        "strategy_type": "grid",
        "direction": "neutral",
        "style": "steady",
        "locked_at": T0.isoformat(),
        "grid": {"count": len(orders), "orders": orders},
        "execution_context": {
            "market": {"price": 100.0, "symbol": "GOLD"}
        },
        "cycle_risk_envelope_id": "envelope-1",
    }


def _commands(plan: dict) -> list[dict]:
    return [
        {
            "symbol": "GOLD",
            "side": row["side"],
            "event": "entry",
            "order_type": "limit",
            "price": row["price"],
            "quantity": row["quantity"],
            "notional": row["notional"],
            "sl": row["sl"],
            "tp": row["tp"],
            "source_fill_id": (
                f"strategy-grid:{plan['strategy_plan_id']}:"
                f"{row['preview_order_id']}"
            ),
            "strategy_plan_id": plan["strategy_plan_id"],
            "strategy_plan_version": plan["version"],
        }
        for row in plan["grid"]["orders"]
    ]


def _candidate_identity(
    attempt_id: str,
    *,
    proposal_id: str = "proposal-rejected",
    proposal_digest: str = "b" * 64,
    preview_id: str = "preview-rejected",
    preview_digest: str = "c" * 64,
    facts_digest: str | None = "facts-rejected",
    confirmation_digest: str | None = "d" * 64,
) -> dict:
    return {
        "supervisor_attempt_id": attempt_id,
        "proposal_id": proposal_id,
        "proposal_digest": proposal_digest,
        "preview_id": preview_id,
        "preview_digest": preview_digest,
        "facts_digest": facts_digest,
        "confirmation_digest": confirmation_digest,
        "strategy_type": "grid",
        "direction": "long",
        "limits": {
            "max_actual_leverage": "10",
            "max_full_depth_loss": "1000",
            "max_notional_per_grid": "100",
            "min_grid_count": "10",
            "max_grid_count": "10",
        },
    }


class FakePlane:
    def __init__(self) -> None:
        self.risk_envelopes = SimpleNamespace(
            find_outer_policy_rejection_for_attempt=(
                lambda **_kwargs: None
            )
        )
        self.plan = _plan(1, preview_id="seed")
        self.runtime = {
            "cycle_id": CYCLE,
            "desired_state": "stopped",
            "actual_state": "stopped",
            "strategy_plan_id": self.plan["strategy_plan_id"],
            "strategy_plan_version": self.plan["version"],
            "accepted_order_count": 0,
            "accepted_order_count_known": True,
        }

    def active_plan(self, cycle_id: str) -> dict:
        assert cycle_id == CYCLE
        return deepcopy(self.plan)

    def persisted_runtime_state(self) -> dict:
        return deepcopy(self.runtime)

    def verify_supervisor_outer_policy(self) -> dict:
        return {"status": "verified", "policy_id": "policy-1"}

    def authorize_supervisor_ai_envelope(
        self,
        cycle_id: str,
        *,
        proposal: dict,
        preview: dict,
        supervisor_attempt_id: str | None = None,
    ) -> dict:
        assert cycle_id == CYCLE
        assert proposal["proposal_id"] == "proposal-1"
        assert preview["preview_id"] == "recommendation-preview-1"
        assert str(supervisor_attempt_id).startswith(
            "supervisor-attempt-"
        )
        return {"envelope_authorization_id": "envelope-1"}

    def supervisor_candidate_identity(
        self,
        cycle_id: str,
        *,
        proposal: dict,
        preview: dict,
        supervisor_attempt_id: str | None = None,
    ) -> dict:
        assert cycle_id == CYCLE
        return {
            "supervisor_attempt_id": supervisor_attempt_id,
            "proposal_id": proposal["proposal_id"],
            "proposal_digest": _digest(proposal),
            "preview_id": preview["preview_id"],
            "preview_digest": _digest(preview),
            "facts_digest": None,
            "confirmation_digest": None,
            "strategy_type": "grid",
            "direction": "neutral",
            "limits": {
                "max_actual_leverage": "10",
                "max_full_depth_loss": "1000",
                "max_notional_per_grid": "100",
                "min_grid_count": "3",
                "max_grid_count": "3",
            },
        }

    def lock_production_plan(
        self,
        cycle_id: str,
        *,
        selected_proposal_id: str,
        cycle_risk_envelope_id: str,
        now: str,
    ) -> dict:
        assert cycle_id == CYCLE
        assert selected_proposal_id == "proposal-1"
        assert cycle_risk_envelope_id == "envelope-1"
        self.plan = _plan(1, preview_id="locked")
        self.plan["cycle_risk_envelope_id"] = cycle_risk_envelope_id
        self.runtime.update(
            {
                "strategy_plan_id": self.plan["strategy_plan_id"],
                "strategy_plan_version": self.plan["version"],
            }
        )
        return deepcopy(self.plan)


class FakeExecution:
    name = "nautilus_paper"

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.positions: list[dict] = []

    def snapshot(self, cycle_id: str) -> dict:
        assert cycle_id == CYCLE
        return {
            "orders": deepcopy(self.orders),
            "fills": deepcopy(self.fills),
            "positions": deepcopy(self.positions),
        }

    def reconcile(self, cycle_id: str) -> dict:
        assert cycle_id == CYCLE
        return {"status": "ok", "issues": []}


class FakePublicControl:
    def __init__(
        self,
        output_root: Path,
        plane: FakePlane,
        execution: FakeExecution,
        *,
        start_outcomes: list[str],
    ) -> None:
        self.output_root = output_root
        self.plane = plane
        self.execution = execution
        self.start_outcomes = list(start_outcomes)
        self.prepare_ids: list[str] = []
        self.start_ids: list[str] = []
        self.calls: list[str] = []
        self.prepare_payloads: list[dict] = []
        self.prepared: dict[str, dict] = {}
        self.intent_seen_before_start = False

    def __call__(self, action: str, payload: dict) -> dict:
        self.calls.append(action)
        if action in {
            "refresh_recommendation",
            "paper_continuity_candidate",
        }:
            strategy_type = str(
                (self.plane.plan or {}).get("strategy_type") or "grid"
            ).lower()
            direction = (
                "long"
                if strategy_type == "dca"
                else "neutral"
            )
            return {
                "recommendation": {
                    "direction": direction,
                    "style": "steady",
                    "strategy_type": strategy_type,
                },
                "proposal": {
                    "proposal_id": "proposal-1",
                    "direction": direction,
                    "style": "steady",
                    "strategy_type": strategy_type,
                    **(
                        {
                            "dca": {
                                "entry_levels": [100.0, 99.0, 98.0],
                                "target_price": 102.0,
                                "stop_price": 96.0,
                                "notional_per_addition": 100.0,
                                "max_additions": 3,
                                "loop_enabled": False,
                            }
                        }
                        if strategy_type == "dca"
                        else {}
                    ),
                },
                "preview": {
                    "preview_id": "recommendation-preview-1",
                    "strategy_type": strategy_type,
                    **(
                        {"risk": {"selected_leverage": 3.0}}
                        if strategy_type == "dca"
                        else {}
                    ),
                },
            }
        if action == "prepare_start":
            self.prepare_payloads.append(deepcopy(payload))
            sequence = len(self.prepare_ids) + 1
            preview_id = f"preview-{sequence}"
            prepared_id = f"prepared-{sequence}"
            future = _plan(
                self.plane.plan["version"] + 1,
                preview_id=preview_id,
            )
            contract = build_start_intent_contract(
                plan=future,
                commands=_commands(future),
                pre_start_plan=self.plane.plan,
            )
            row = {
                "prepared_start_id": prepared_id,
                "preview": {
                    "preview_id": preview_id,
                    "manual_confirmation": {"required": False},
                },
                "start_intent_contract": contract,
                "future_plan": future,
            }
            if payload.get("paper_continuity_provider_fallback") is True:
                row["paper_continuity_provider_fallback"] = True
            if payload.get("paper_continuity_dca_carry_forward") is True:
                row["paper_continuity_dca_carry_forward"] = True
                row["preview"]["strategy_type"] = "dca"
            if (
                payload.get(
                    "paper_continuity_allow_lower_profit_target"
                )
                is True
            ):
                row[
                    "paper_continuity_allow_lower_profit_target"
                ] = True
            self.prepare_ids.append(prepared_id)
            self.prepared[prepared_id] = row
            return deepcopy(row)
        if action == "stop":
            self.execution.orders = []
            self.execution.positions = []
            self.plane.runtime.update(
                {
                    "desired_state": "stopped",
                    "actual_state": "stopped",
                    "accepted_order_count": 0,
                    "accepted_order_count_known": True,
                    "prepared_start_id": None,
                    "preview_id": None,
                }
            )
            return {
                "runtime": deepcopy(self.plane.runtime),
                "cancelled_orders": 0,
                "flattened_positions": 0,
                "reconciliation": {"status": "ok", "issues": []},
            }
        if action != "start":
            raise AssertionError(f"unexpected action: {action}")
        prepared_id = str(payload["prepared_start_id"])
        self.intent_seen_before_start = (
            PaperSupervisorStore(
                self.output_root
            ).unfinished_intent(CYCLE)
            is not None
        )
        self.start_ids.append(prepared_id)
        outcome = self.start_outcomes.pop(0)
        prepared = self.prepared[prepared_id]
        if outcome == "market_moved":
            self._audit(
                payload,
                result="rejected",
                error="prepared_start_market_moved",
            )
            raise ValueError("prepared_start_market_moved")
        future = deepcopy(prepared["future_plan"])
        commands = _commands(future)
        command_rows = [
            {
                "command_id": f"order-{index}",
                "cycle_id": CYCLE,
                "command": command,
            }
            for index, command in enumerate(commands)
        ]
        command_path = (
            self.output_root
            / "dualtrack"
            / "nautilus_authoritative"
            / "commands"
            / f"{CYCLE}.json"
        )
        command_path.parent.mkdir(parents=True, exist_ok=True)
        command_path.write_text(
            json.dumps(command_rows),
            encoding="utf-8",
        )
        self.plane.plan = future
        self.execution.orders = [
            {
                **command,
                "order_id": f"order-{index}",
                "state": "accepted",
            }
            for index, command in enumerate(commands)
        ]
        self.plane.runtime = {
            "cycle_id": CYCLE,
            "desired_state": "running",
            "actual_state": "running",
            "strategy_plan_id": future["strategy_plan_id"],
            "strategy_plan_version": future["version"],
            "preview_id": prepared["preview"]["preview_id"],
            "prepared_start_id": prepared_id,
            "accepted_order_count": len(commands),
            "accepted_order_count_known": True,
        }
        self._audit(payload, result="accepted", error=None)
        return {
            "runtime": deepcopy(self.plane.runtime),
            "plan": deepcopy(future),
            "created_orders": len(commands),
            "accepted_orders": len(commands),
            "filled_orders": 0,
            "audit_recorded": True,
        }

    def _audit(
        self,
        payload: dict,
        *,
        result: str,
        error: str | None,
    ) -> None:
        append_control_event(
            self.output_root,
            build_control_event(
                cycle_id=CYCLE,
                action="start",
                actor={"client": "paper-supervisor"},
                payload=payload,
                result=result,
                error=error,
                runtime=self.plane.runtime,
                now=T0.isoformat(),
            ),
        )


def _supervisor(
    tmp_path: Path,
    *,
    outcomes: list[str],
    execution_profile: str = "fail_closed",
) -> tuple[PaperSupervisor, FakePublicControl, FakePlane]:
    output = tmp_path / "outputs"
    plane = FakePlane()
    execution = FakeExecution()
    control = FakePublicControl(
        output,
        plane,
        execution,
        start_outcomes=outcomes,
    )
    return (
        PaperSupervisor(
            output,
            plane=plane,
            execution=execution,
            control=control,
            accounting_reconciliation=lambda: "pass",
            execution_profile=execution_profile,
        ),
        control,
        plane,
    )


def _continuous_supervisor(
    tmp_path: Path,
    *,
    outcomes: list[str],
) -> tuple[PaperSupervisor, FakePublicControl, FakePlane]:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=outcomes,
        execution_profile=PAPER_CONTINUOUS,
    )
    return supervisor, control, plane


def _readiness_result(digest: str = "a" * 64) -> dict:
    return {
        "ok": True,
        "readiness_digest": digest,
        "source_sha": "b" * 40,
        "source_tree_sha": "c" * 40,
        "provider": {"executable_sha256": "d" * 64},
        "checked_at": T0.isoformat(),
        "expires_at": (T0 + timedelta(hours=24)).isoformat(),
    }


def test_provider_readiness_unavailable_blocks_only_new_entry(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    plane = FakePlane()
    execution = FakeExecution()
    control = FakePublicControl(
        output,
        plane,
        execution,
        start_outcomes=["accepted"],
    )
    supervisor = PaperSupervisor(
        output,
        plane=plane,
        execution=execution,
        control=control,
        accounting_reconciliation=lambda: "pass",
        provider_readiness_verifier=lambda: {
            "ok": False,
            "blocker": "cloud_ai_provider_readiness_stale",
        },
    )

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert result["status"] == "backing_off"
    assert result["machine_code"] == (
        "cloud_ai_provider_readiness_unavailable"
    )
    assert result["control_actions_executed"] == 0
    assert control.calls == []
    assert execution.orders == []
    assert supervisor.store.unfinished_intent(CYCLE) is None


def test_paper_continuous_provider_outage_reuses_plan_with_audited_fallback(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _continuous_supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    supervisor.provider_readiness_verifier = lambda: {
        "ok": False,
        "blocker": "cloud_ai_provider_readiness_stale",
    }

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(T0),
    )

    assert result["terminal_status"] == "executed"
    assert control.calls[:3] == [
        "paper_continuity_candidate",
        "prepare_start",
        "start",
    ]
    assert control.prepare_payloads[0][
        "paper_continuity_provider_fallback"
    ] is True
    events = PaperDegradationEventStore(
        supervisor.output_root
    ).events(CYCLE)
    assert any(
        row["bypassed_gate"] == "cloud_ai_provider_readiness_gate"
        and row["alternative_action"]
        == "reuse_verified_strategy_intent_without_new_ai_call"
        for row in events
    )
    assert any(
        row["alternative_action"]
        == "rebuild_candidate_from_current_market_and_"
        "authoritative_paper_equity"
        for row in events
    )


def test_paper_continuity_dca_recovery_records_confirmation_degradation(
    tmp_path: Path,
) -> None:
    supervisor, _, _ = _continuous_supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    supervisor.plane.plan = None

    def dca_candidate(action: str, payload: dict) -> dict:
        assert action == "paper_continuity_candidate"
        assert payload["supervisor_attempt_id"] == "supervisor-attempt-dca"
        return {
            "recommendation": {
                "direction": "long",
                "style": "steady",
                "strategy_type": "dca",
            },
            "proposal": {
                "proposal_id": "proposal-1",
                "direction": "long",
                "style": "steady",
                "strategy_type": "dca",
                "dca": {
                    "entry_levels": [100.0, 99.0, 98.0],
                    "target_price": 102.0,
                    "stop_price": 96.0,
                    "notional_per_addition": 100.0,
                    "max_additions": 3,
                    "loop_enabled": False,
                },
            },
            "preview": {
                "preview_id": "recommendation-preview-1",
                "strategy_type": "dca",
                "risk": {"selected_leverage": 3.0},
            },
            "recovery": {"risk_repriced": False},
        }

    supervisor.control = dca_candidate
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="dca-degradation-test",
    ) as lease:
        assert lease is not None
        lease.record_pre_intent_started(
            attempt_id="supervisor-attempt-dca",
            observed_at=T0.isoformat(),
            phase_scope="create_or_prepare",
        )
        _, request = supervisor._create_plan(
            CYCLE,
            lease=lease,
            attempt_id="supervisor-attempt-dca",
            observed_at=T0.isoformat(),
            recovery_candidate=True,
        )

    assert request["strategy_type"] == "dca"
    assert request["paper_continuity_dca_carry_forward"] is True
    events = PaperDegradationEventStore(
        supervisor.output_root
    ).events(CYCLE)
    dca_event = next(
        row
        for row in events
        if row["bypassed_gate"] == "dca_manual_risk_confirmation_gate"
    )
    assert dca_event["original_machine_code"] == (
        "manual_risk_confirmation_required"
    )
    assert dca_event["alternative_action"] == (
        "reuse_verified_dca_intent_inside_exact_outer_policy"
    )


def test_paper_continuous_never_resets_unknown_control_outcome(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _continuous_supervisor(
        tmp_path,
        outcomes=["accepted", "accepted"],
    )
    control._audit = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(T0),
    )
    second_at = T0 + timedelta(seconds=301)
    second = supervisor.converge_once(
        CYCLE,
        observed_at=second_at.isoformat(),
        heartbeat=_heartbeat(second_at),
    )

    assert first["machine_code"] == (
        "partial_execution_or_cleanup_required"
    )
    assert second["status"] == "blocked_structural"
    assert second["machine_code"] == (
        "partial_execution_or_cleanup_required"
    )
    assert control.calls.count("start") == 1


def test_paper_continuous_active_dca_uses_family_preserving_recovery(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _continuous_supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.plan["strategy_type"] = "dca"
    plane.plan["direction"] = "long"
    plane.plan["dca"] = {
        "entries": [{"price": 99.0}],
        "target_price": 110.0,
        "stop_price": 90.0,
        "notional_per_addition": 100.0,
        "max_additions": 1,
        "loop_enabled": False,
    }

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(T0),
    )

    assert result["terminal_status"] == "executed"
    assert control.calls[:3] == [
        "paper_continuity_candidate",
        "prepare_start",
        "start",
    ]
    assert control.prepare_payloads[0]["strategy_type"] == "dca"
    assert control.prepare_payloads[0][
        "paper_continuity_dca_carry_forward"
    ] is True


def test_invalid_provider_readiness_is_structural_without_control(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    plane = FakePlane()
    execution = FakeExecution()
    control = FakePublicControl(
        output,
        plane,
        execution,
        start_outcomes=["accepted"],
    )
    supervisor = PaperSupervisor(
        output,
        plane=plane,
        execution=execution,
        control=control,
        accounting_reconciliation=lambda: "pass",
        provider_readiness_verifier=lambda: {
            "ok": False,
            "blocker": "cloud_ai_provider_readiness_digest_invalid",
        },
    )

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == "cloud_ai_provider_readiness_invalid"
    assert result["control_actions_executed"] == 0
    assert control.calls == []
    assert execution.orders == []


def test_changed_readiness_after_prepare_refuses_before_start_intent(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    plane = FakePlane()
    execution = FakeExecution()
    underlying = FakePublicControl(
        output,
        plane,
        execution,
        start_outcomes=["accepted"],
    )
    proof = require_cloud_ai_provider_readiness(
        lambda: _readiness_result("a" * 64)
    )
    verifier_calls = 0

    def verifier() -> dict:
        nonlocal verifier_calls
        verifier_calls += 1
        return _readiness_result(
            "a" * 64 if verifier_calls == 1 else "e" * 64
        )

    def control(action: str, payload: dict) -> dict:
        result = underlying(action, payload)
        if action == "prepare_start":
            result["provider_readiness"] = proof
        return result

    supervisor = PaperSupervisor(
        output,
        plane=plane,
        execution=execution,
        control=control,
        accounting_reconciliation=lambda: "pass",
        provider_readiness_verifier=verifier,
    )

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert result["status"] == "backing_off"
    assert result["machine_code"] == (
        "cloud_ai_provider_readiness_unavailable"
    )
    assert underlying.calls == ["prepare_start"]
    assert underlying.start_ids == []
    assert execution.orders == []
    assert supervisor.store.unfinished_intent(CYCLE) is None


def test_running_authority_is_adopted_without_readiness_lookup(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"

    def forbidden_readiness() -> dict:
        raise AssertionError("running adoption must not consult provider readiness")

    adopting = PaperSupervisor(
        supervisor.output_root,
        plane=plane,
        execution=supervisor.execution,
        control=control,
        accounting_reconciliation=lambda: "pass",
        store=supervisor.store,
        provider_readiness_verifier=forbidden_readiness,
    )
    at = T0 + timedelta(minutes=1)
    second = adopting.converge_once(
        CYCLE,
        observed_at=at.isoformat(),
        heartbeat=_heartbeat(at),
    )

    assert second["terminal_status"] == "adopted_existing"
    assert control.calls.count("start") == 1


def test_market_moved_uses_fresh_preview_and_prepared_start_then_recovers(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["market_moved", "accepted"],
    )
    supervisor.store.now = lambda: T0

    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    second_at = T0 + timedelta(seconds=61)
    second = supervisor.converge_once(
        CYCLE,
        observed_at=second_at.isoformat(),
        heartbeat=_heartbeat(second_at),
    )
    third = supervisor.converge_once(
        CYCLE,
        observed_at=(second_at + timedelta(seconds=61)).isoformat(),
        heartbeat=_heartbeat(second_at + timedelta(seconds=61)),
    )

    assert first["machine_code"] == "prepared_start_market_moved"
    assert second["status"] == "executed"
    assert third["terminal_status"] == "adopted_existing"
    assert control.prepare_ids == ["prepared-1", "prepared-2"]
    assert control.start_ids == ["prepared-1", "prepared-2"]
    assert len(set(control.start_ids)) == 2
    assert control.calls.count("start") == 2
    assert control.intent_seen_before_start is True


def test_paper_continuous_retries_clean_market_refusal_every_300_seconds(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _continuous_supervisor(
        tmp_path,
        outcomes=["market_moved", "accepted"],
    )
    supervisor.store.now = lambda: T0

    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(T0),
    )
    before_due = T0 + timedelta(seconds=299)
    second = supervisor.converge_once(
        CYCLE,
        observed_at=before_due.isoformat(),
        heartbeat=_heartbeat(before_due),
    )
    due = T0 + timedelta(seconds=300)
    third = supervisor.converge_once(
        CYCLE,
        observed_at=due.isoformat(),
        heartbeat=_heartbeat(due),
    )

    assert first["machine_code"] == "prepared_start_market_moved"
    assert second["status"] == "watchdog_waiting"
    assert third["terminal_status"] == "executed"
    assert control.prepare_ids == ["prepared-1", "prepared-2"]
    assert control.start_ids == ["prepared-1", "prepared-2"]
    events = PaperDegradationEventStore(
        supervisor.output_root
    ).events(CYCLE)
    assert events[0]["original_machine_code"] == "cycle_boundary_reset"
    assert [
        row["alternative_action"]
        for row in events
        if row["alternative_action"] == "force_fresh_full_start_flow"
    ] == ["force_fresh_full_start_flow"] * 2


def test_paper_continuous_retries_structural_blocker_after_watchdog_interval(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _continuous_supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    original = supervisor.control
    prepare_calls = 0

    def structural_once(action: str, payload: dict) -> dict:
        nonlocal prepare_calls
        if action == "prepare_start":
            prepare_calls += 1
            if prepare_calls == 1:
                raise ValueError("risk_envelope_missing")
        return original(action, payload)

    supervisor.control = structural_once
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(T0),
    )
    due = T0 + timedelta(seconds=300)
    second = supervisor.converge_once(
        CYCLE,
        observed_at=due.isoformat(),
        heartbeat=_heartbeat(due),
    )

    assert first["status"] == "blocked_structural"
    assert first["machine_code"] == "risk_envelope_missing"
    assert second["terminal_status"] == "executed"
    assert prepare_calls == 2
    assert control.start_ids == ["prepared-1"]
    events = PaperDegradationEventStore(
        supervisor.output_root
    ).events(CYCLE)
    assert any(
        row["original_machine_code"] == "risk_envelope_missing"
        and row["alternative_action"]
        == "clear_latch_and_schedule_fresh_full_start"
        for row in events
    )


def test_paper_continuous_resets_unproven_runtime_then_starts_fresh(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _continuous_supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.runtime.update(
        {
            "desired_state": "running",
            "actual_state": "running",
            "accepted_order_count": 0,
            "accepted_order_count_known": True,
        }
    )

    reset = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(T0),
    )
    before_due = T0 + timedelta(seconds=299)
    waiting = supervisor.converge_once(
        CYCLE,
        observed_at=before_due.isoformat(),
        heartbeat=_heartbeat(before_due),
    )
    due = T0 + timedelta(seconds=300)
    started = supervisor.converge_once(
        CYCLE,
        observed_at=due.isoformat(),
        heartbeat=_heartbeat(due),
    )

    assert reset["terminal_status"] == "watchdog_runtime_reset"
    assert reset["control_actions_executed"] == 1
    assert waiting["status"] == "watchdog_waiting"
    assert started["terminal_status"] == "executed"
    assert control.calls.count("stop") == 1
    assert control.calls.count("start") == 1
    events = PaperDegradationEventStore(
        supervisor.output_root
    ).events(CYCLE)
    assert any(
        row["alternative_action"]
        == "stop_nonproven_runtime_before_fresh_start"
        for row in events
    )


def test_paper_continuous_never_controls_without_fresh_heartbeat(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _continuous_supervisor(
        tmp_path,
        outcomes=["accepted"],
    )

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=None,
    )

    assert result["status"] == "backing_off"
    assert result["machine_code"] == (
        "execution_tick_heartbeat_temporarily_missing"
    )
    assert result["control_actions_executed"] == 0
    assert control.calls == []


def test_paper_continuous_never_starts_when_trusted_market_gate_rejects(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _continuous_supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    original = supervisor.control
    prepare_calls = 0

    def untrusted_market(action: str, payload: dict) -> dict:
        nonlocal prepare_calls
        if action == "prepare_start":
            prepare_calls += 1
            raise ValueError("trusted_market_provenance_invalid")
        return original(action, payload)

    supervisor.control = untrusted_market
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(T0),
    )
    due = T0 + timedelta(seconds=300)
    second = supervisor.converge_once(
        CYCLE,
        observed_at=due.isoformat(),
        heartbeat=_heartbeat(due),
    )

    assert first["machine_code"] == "trusted_market_provenance_invalid"
    assert second["machine_code"] == "trusted_market_provenance_invalid"
    assert prepare_calls == 2
    assert control.start_ids == []
    assert supervisor.execution.orders == []
    events = PaperDegradationEventStore(
        supervisor.output_root
    ).events(CYCLE)
    assert not any(
        row["original_machine_code"]
        == "trusted_market_provenance_invalid"
        and row["bypassed_gate"] == "structural_blocker_latch"
        for row in events
    )


def test_frozen_grid_market_move_is_transient_before_start_intent(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    plane = FakePlane()
    execution = FakeExecution()
    underlying = FakePublicControl(
        output,
        plane,
        execution,
        start_outcomes=["accepted"],
    )
    prepare_calls = 0

    class FrozenMarketMove(ValueError):
        code = "frozen_grid_preview_market_moved"

    def control(action: str, payload: dict) -> dict:
        nonlocal prepare_calls
        if action == "prepare_start":
            prepare_calls += 1
            if prepare_calls == 1:
                raise FrozenMarketMove(
                    "human prose must not drive classification"
                )
        return underlying(action, payload)

    supervisor = PaperSupervisor(
        output,
        plane=plane,
        execution=execution,
        control=control,
        accounting_reconciliation=lambda: "pass",
    )
    supervisor.store.now = lambda: T0
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(T0),
    )

    assert first["status"] == "backing_off"
    assert first["machine_code"] == (
        "frozen_grid_preview_market_moved"
    )
    assert supervisor.store.current_state(CYCLE)["attempt_count"] == 0
    assert underlying.prepare_ids == []
    assert underlying.start_ids == []

    recovered_at = T0 + timedelta(seconds=61)
    supervisor.store.now = lambda: recovered_at
    second = supervisor.converge_once(
        CYCLE,
        observed_at=recovered_at.isoformat(),
        heartbeat=_heartbeat(recovered_at),
    )
    assert second["status"] == "executed"
    assert underlying.prepare_ids == ["prepared-1"]
    assert underlying.start_ids == ["prepared-1"]


def test_active_grid_request_freezes_geometry_for_fresh_supervisor_preview() -> None:
    plan = {
        "cycle_id": CYCLE,
        "strategy_plan_id": "strategy-plan-current",
        "version": 3,
        "strategy_type": "grid",
        "direction": "neutral",
        "style": "steady",
        "range": {
            "low": 3900,
            "high": 4200,
            "scope": "full",
            "split_price": 4050,
            "source_envelope": {"low": 3900, "high": 4200},
        },
        "grid": {
            "count": 38,
            "mode": "arithmetic",
            "notional_per_grid": 5000,
            "notional_mode": "auto",
            "out_of_range": "exit_only",
            "leverage": 10,
        },
    }

    request = PaperSupervisor._request_from_plan(plan)

    assert request["range"] == plan["range"]
    assert request["grid"] == {
        "count": 38,
        "mode": "arithmetic",
        "notional_per_grid": 5000,
        "out_of_range": "exit_only",
        "notional_mode": "manual",
    }
    assert request["risk_budget"] == {"leverage": 10}


def test_convergence_builds_existing_plan_request_from_full_plan_not_identity(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    expected_range = {
        "low": 90.0,
        "high": 110.0,
        "scope": "full",
        "split_price": 100.0,
        "source_envelope": {"low": 90.0, "high": 110.0},
    }
    plane.plan["range"] = expected_range
    plane.plan["grid"].update(
        {
            "mode": "arithmetic",
            "notional_per_grid": 100.0,
            "out_of_range": "exit_only",
            "leverage": 10,
        }
    )
    supervisor.store.now = lambda: T0

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert result["status"] == "executed"
    payload = control.prepare_payloads[0]
    assert payload["cycle_risk_envelope_id"] == "envelope-1"
    assert payload["range"] == expected_range
    assert payload["grid"]["count"] == 3
    assert payload["grid"]["notional_per_grid"] == 100.0
    assert payload["risk_budget"] == {"leverage": 10}


@pytest.mark.parametrize("verification_passes", [True, False])
def test_stale_plan_identity_blocker_rechecks_exact_envelope_gate(
    tmp_path: Path,
    verification_passes: bool,
) -> None:
    calls: list[dict] = []

    class FakeRiskEnvelopes:
        def verify_candidate_plan_identity(self, **kwargs: dict) -> None:
            calls.append(kwargs)
            if not verification_passes:
                raise ValueError("plan_identity_conflict")

    plan = {
        "cycle_id": CYCLE,
        "strategy_plan_id": "strategy-plan-current",
        "version": 3,
        "cycle_risk_envelope_id": "envelope-current",
        "direction": "neutral",
        "strategy_type": "grid",
    }
    plane = SimpleNamespace(risk_envelopes=FakeRiskEnvelopes())
    supervisor = PaperSupervisor(
        tmp_path / "outputs",
        plane=plane,
        execution=object(),
        control=lambda *_args, **_kwargs: {},
        accounting_reconciliation=lambda: "pass",
    )
    state = supervisor.episodes.new_cycle(CYCLE, observed_at=T0)
    state = supervisor.episodes.record_structural_blocker(
        state,
        classification=classify_blocker(
            control_code="plan_identity_conflict"
        ),
        observed_at=T0,
    )
    authority = SimpleNamespace(
        cycle_id=CYCLE,
        active_plan=plan,
        accepted_order_fingerprints=(),
        authorized_order_identities=(),
        open_position_count=0,
        reconciliation={"execution": "ok", "accounting": "pass"},
    )

    updated, cleared = supervisor._recheck_structural(
        state,
        authority=authority,
        observed_at=(T0 + timedelta(minutes=1)).isoformat(),
    )

    assert cleared is verification_passes
    assert calls and calls[0]["envelope_authorization_id"] == "envelope-current"
    assert calls[0]["plan"] == plan
    assert calls[0]["now"] == (T0 + timedelta(minutes=1)).isoformat()
    assert updated["mode"] == (
        "ready" if verification_passes else "blocked_structural"
    )
    if verification_passes:
        assert updated["blocker"] is None
    else:
        assert updated["blocker"]["machine_code"] == "plan_identity_conflict"


@pytest.mark.parametrize(
    ("recheck_passes", "authority_mutation", "expected"),
    [
        (True, None, True),
        (False, None, False),
        (True, "exposure", False),
        (True, "reconciliation", False),
        (True, "unknown_control", False),
        (True, "active_plan", False),
    ],
)
def test_outer_policy_recheck_uses_bound_rejection_and_clean_authority(
    tmp_path: Path,
    recheck_passes: bool,
    authority_mutation: str | None,
    expected: bool,
) -> None:
    calls: list[dict] = []
    attempt_id = "supervisor-attempt-policy-rejected"
    candidate = {
        "supervisor_attempt_id": attempt_id,
        "proposal_id": "proposal-rejected",
        "proposal_digest": "b" * 64,
        "preview_id": "preview-rejected",
        "preview_digest": "c" * 64,
        "facts_digest": "facts-rejected",
        "confirmation_digest": "d" * 64,
        "strategy_type": "grid",
        "direction": "long",
        "limits": {
            "max_actual_leverage": "10",
            "max_full_depth_loss": "1000",
            "max_notional_per_grid": "100",
            "min_grid_count": "10",
            "max_grid_count": "10",
        },
    }
    receipt = {
        "cycle_id": CYCLE,
        "rejection_id": "outer-policy-rejection-exact",
        "rejection_digest": "a" * 64,
        "machine_code": (
            "outer_strategy_policy_envelope_out_of_bounds"
        ),
        "candidate": candidate,
    }

    class FakeRiskEnvelopes:
        def outer_policy_rejection_for_attempt(
            self,
            **kwargs: dict,
        ) -> dict:
            assert kwargs["supervisor_attempt_id"] == attempt_id
            return deepcopy(receipt)

        def recheck_outer_policy_rejection(
            self,
            **kwargs: dict,
        ) -> dict:
            calls.append(kwargs)
            return {
                "schema_version": (
                    "paper-supervisor-outer-policy-recheck-v1"
                ),
                "rejection_id": kwargs["rejection_id"],
                "rejection_digest": kwargs["rejection_digest"],
                "comparisons": [
                    {
                        "field": "direction",
                        "operator": "in",
                        "authorized_limit": [
                            "long",
                            "neutral",
                            "short",
                        ],
                        "observed_value": "long",
                        "pass": recheck_passes,
                    }
                ],
                "passed": recheck_passes,
                "control_actions_executed": 0,
                "recheck_digest": "c" * 64,
            }

        @staticmethod
        def verify_outer_policy_recheck_proof(
            *,
            proof: dict,
            **_kwargs: dict,
        ) -> dict:
            return deepcopy(proof)

    supervisor = PaperSupervisor(
        tmp_path / "outputs",
        plane=SimpleNamespace(
            risk_envelopes=FakeRiskEnvelopes(),
            verify_supervisor_outer_policy=lambda: {
                "status": "verified"
            },
        ),
        execution=object(),
        control=lambda *_args, **_kwargs: pytest.fail(
            "structural recheck must execute zero controls"
        ),
        accounting_reconciliation=lambda: "pass",
    )
    state = supervisor.episodes.record_structural_blocker(
        supervisor.episodes.new_cycle(CYCLE, observed_at=T0),
        classification=classify_blocker(
            control_code=(
                "outer_strategy_policy_envelope_out_of_bounds"
            )
        ),
        observed_at=T0,
        evidence={
            "rejection_id": "outer-policy-rejection-exact",
            "rejection_digest": "a" * 64,
        },
    )
    projection = {
        "attempts": (
            [
                {
                    "terminal_result": "control_outcome_unknown",
                    "terminal_machine_code": (
                        "control_outcome_unknown"
                    ),
                }
            ]
            if authority_mutation == "unknown_control"
            else []
        ),
        "unfinished_intent": None,
        "pre_intent_attempts": [
            {
                "attempt_id": attempt_id,
                "candidate_identity": candidate,
                "terminal_result": "structural",
                "terminal_classification": "structural",
                "terminal_machine_code": (
                    "outer_strategy_policy_envelope_out_of_bounds"
                ),
                "terminal_observed_at": T0.isoformat(),
                "terminal_evidence": {
                    "rejection_id": (
                        "outer-policy-rejection-exact"
                    ),
                    "rejection_digest": "a" * 64,
                },
            }
        ],
    }
    supervisor.store.current_state = (
        lambda _cycle_id: deepcopy(projection)
    )
    authority = SimpleNamespace(
        cycle_id=CYCLE,
        active_plan=(
            {"strategy_plan_id": "unexpected"}
            if authority_mutation == "active_plan"
            else {}
        ),
        runtime={
            "desired_state": "stopped",
            "actual_state": "stopped",
            "accepted_order_count": 0,
        },
        accepted_order_fingerprints=(
            ("order",)
            if authority_mutation == "exposure"
            else ()
        ),
        open_position_count=0,
        reconciliation={
            "execution": (
                "drift"
                if authority_mutation == "reconciliation"
                else "ok"
            ),
            "accounting": "pass",
        },
    )

    updated, cleared = supervisor._recheck_structural(
        state,
        authority=authority,
        observed_at=(T0 + timedelta(minutes=1)).isoformat(),
    )

    assert cleared is expected
    assert updated["mode"] == (
        "ready" if expected else "blocked_structural"
    )
    assert len(calls) == (
        1 if authority_mutation is None else 0
    )
    if authority_mutation is None:
        assert calls[0]["rejection_id"] == (
            "outer-policy-rejection-exact"
        )
        assert calls[0]["rejection_digest"] == "a" * 64
        assert updated["events"][-1]["detail"]["evidence"][
            "control_actions_executed"
        ] == 0


def test_outer_policy_rejection_reference_is_bound_into_episode_wal(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.plan = {}
    plane.runtime.update(
        {
            "strategy_plan_id": None,
            "strategy_plan_version": None,
        }
    )
    expected_evidence = {
        "rejection_id": "outer-policy-rejection-wal",
        "rejection_digest": "a" * 64,
    }
    receipt_holder: dict[str, dict] = {}

    class FakeRiskEnvelopes:
        @staticmethod
        def find_outer_policy_rejection_for_attempt(
            **_kwargs: dict,
        ) -> dict | None:
            return deepcopy(receipt_holder.get("receipt"))

    plane.risk_envelopes = FakeRiskEnvelopes()

    def reject_candidate(
        _cycle_id: str,
        *,
        proposal: dict,
        preview: dict,
        supervisor_attempt_id: str | None = None,
    ) -> dict:
        assert proposal["proposal_id"] == "proposal-1"
        assert preview["preview_id"] == "recommendation-preview-1"
        assert str(supervisor_attempt_id).startswith(
            "supervisor-attempt-"
        )
        receipt_holder["receipt"] = {
            "cycle_id": CYCLE,
            **expected_evidence,
            "machine_code": (
                "outer_strategy_policy_envelope_out_of_bounds"
            ),
            "candidate": plane.supervisor_candidate_identity(
                CYCLE,
                proposal=proposal,
                preview=preview,
                supervisor_attempt_id=supervisor_attempt_id,
            ),
        }
        raise CycleRiskEnvelopeError(
            "outer_strategy_policy_envelope_out_of_bounds",
            evidence=expected_evidence,
        )

    plane.authorize_supervisor_ai_envelope = reject_candidate

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    state = supervisor.store.episode_state(CYCLE)
    projection = supervisor.store.current_state(CYCLE)
    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == (
        "outer_strategy_policy_envelope_out_of_bounds"
    )
    assert state is not None
    assert state["blocker"]["evidence"] == expected_evidence
    assert projection["pre_intent_attempts"][0][
        "terminal_evidence"
    ] == expected_evidence
    assert control.calls == ["refresh_recommendation"]


@pytest.mark.parametrize(
    ("receipt_state", "expected_code"),
    [
        (
            "exact",
            "outer_strategy_policy_envelope_out_of_bounds",
        ),
        ("missing", "attempt_store_corrupt"),
        ("mismatched", "attempt_store_corrupt"),
    ],
)
def test_crash_after_candidate_marker_recovers_without_ai_or_control(
    tmp_path: Path,
    receipt_state: str,
    expected_code: str,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.plan = {}
    plane.runtime.update(
        {
            "strategy_plan_id": None,
            "strategy_plan_version": None,
            "accepted_order_count": 0,
        }
    )
    attempt_id = "supervisor-attempt-crashed-policy-rejection"
    candidate = _candidate_identity(attempt_id)
    risk_store = CycleRiskEnvelopeStore(tmp_path / "outputs")
    evidence: dict[str, str] | None = None
    if receipt_state != "missing":
        receipt_candidate = (
            {**candidate, "preview_id": "preview-substitute"}
            if receipt_state == "mismatched"
            else candidate
        )
        comparisons = [
            {
                "field": "strategy_type",
                "operator": "==",
                "authorized_limit": "grid",
                "observed_value": "grid",
                "pass": True,
            },
            {
                "field": "direction",
                "operator": "==",
                "authorized_limit": "neutral",
                "observed_value": "long",
                "pass": False,
            },
            *[
                {
                    "field": field,
                    "operator": (
                        ">=" if field.startswith("min_") else "<="
                    ),
                    "authorized_limit": value,
                    "observed_value": value,
                    "pass": True,
                }
                for field, value in receipt_candidate["limits"].items()
            ],
        ]
    plane.risk_envelopes = risk_store
    original_heartbeat = _heartbeat()
    original_health = validate_complete_tick_heartbeat(
        original_heartbeat,
        cycle_id=CYCLE,
        observed_at=T0.isoformat(),
    )
    source_tick_key, trust = supervisor._source_tick_identity(
        CYCLE,
        health=original_health,
    )
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="crashed-policy-owner",
    ) as lease:
        assert lease is not None
        lease.claim_tick(
            source_tick_key=source_tick_key,
            heartbeat_digest=_digest(original_heartbeat),
            trust=trust,
            claimed_at=T0.isoformat(),
            heartbeat_recorded_at=original_health.get("recorded_at"),
        )
        lease.record_pre_intent_started(
            attempt_id=attempt_id,
            observed_at=T0.isoformat(),
            phase_scope="create_or_prepare",
        )
        lease.record_pre_intent_candidate_observed(
            attempt_id=attempt_id,
            observed_at=T0.isoformat(),
            candidate_identity=candidate,
        )

    if receipt_state != "missing":
        receipt = risk_store._persist_outer_policy_rejection(
            cycle_id=CYCLE,
            candidate=receipt_candidate,
            binding={
                "binding_id": "paper-supervisor-grid",
                "binding_version": 1,
                "binding_digest": "7" * 64,
                "bound_at": T0.isoformat(),
            },
            outer_policy={
                "policy_id": "park-grid-policy",
                "version": 1,
                "policy_digest": "8" * 64,
                "authorized_at": T0.isoformat(),
                "expires_at": (
                    T0 + timedelta(days=30)
                ).isoformat(),
            },
            comparisons=comparisons,
            rejected_at=T0.isoformat(),
        )
        evidence = {
            "rejection_id": receipt["rejection_id"],
            "rejection_digest": receipt["rejection_digest"],
        }

    later = T0 + timedelta(seconds=1)
    result = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == expected_code
    assert result["control_actions_executed"] == 0
    assert control.calls == []
    assert supervisor.execution.orders == []
    projection = supervisor.store.current_state(CYCLE)
    recovered = projection["pre_intent_attempts"][0]
    assert recovered["recovered_after_crash"] is True
    assert recovered["terminal_machine_code"] == expected_code
    if receipt_state == "exact":
        assert recovered["terminal_evidence"] == evidence
    else:
        assert "terminal_evidence" not in recovered


def test_durable_rejection_receipt_overrides_before_intent_deadline(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.plan = {}
    plane.runtime.update(
        {
            "strategy_plan_id": None,
            "strategy_plan_version": None,
            "accepted_order_count": 0,
        }
    )
    evidence = {
        "rejection_id": "outer-policy-rejection-timeout-race",
        "rejection_digest": "a" * 64,
    }
    receipt_holder: dict[str, dict] = {}

    class FakeRiskEnvelopes:
        @staticmethod
        def find_outer_policy_rejection_for_attempt(
            **_kwargs: dict,
        ) -> dict | None:
            return deepcopy(receipt_holder.get("receipt"))

    plane.risk_envelopes = FakeRiskEnvelopes()

    def persist_then_timeout(
        _cycle_id: str,
        *,
        proposal: dict,
        preview: dict,
        supervisor_attempt_id: str,
    ) -> dict:
        receipt_holder["receipt"] = {
            "cycle_id": CYCLE,
            **evidence,
            "machine_code": (
                "outer_strategy_policy_envelope_out_of_bounds"
            ),
            "candidate": plane.supervisor_candidate_identity(
                CYCLE,
                proposal=proposal,
                preview=preview,
                supervisor_attempt_id=supervisor_attempt_id,
            ),
        }
        raise SupervisorAttemptDeadline()

    plane.authorize_supervisor_ai_envelope = persist_then_timeout

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == (
        "outer_strategy_policy_envelope_out_of_bounds"
    )
    assert control.calls == ["refresh_recommendation"]
    terminal = supervisor.store.current_state(CYCLE)[
        "pre_intent_attempts"
    ][0]
    assert terminal["terminal_evidence"] == evidence


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_marker",
        "terminal_attempt",
        "terminal_evidence",
        "terminal_time",
        "receipt_candidate",
        "duplicate_terminal",
    ],
)
def test_outer_policy_recheck_rejects_substituted_wal_linkage(
    tmp_path: Path,
    mutation: str,
) -> None:
    supervisor, _control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    attempt_id = "supervisor-attempt-bound-rejection"
    evidence = {
        "rejection_id": "outer-policy-rejection-bound",
        "rejection_digest": "a" * 64,
    }
    candidate = _candidate_identity(attempt_id)
    blocker = {
        "machine_code": (
            "outer_strategy_policy_envelope_out_of_bounds"
        ),
        "blocked_at": T0.isoformat(),
        "evidence": evidence,
    }
    terminal = {
        "attempt_id": attempt_id,
        "candidate_identity": candidate,
        "terminal_result": "structural",
        "terminal_classification": "structural",
        "terminal_machine_code": (
            "outer_strategy_policy_envelope_out_of_bounds"
        ),
        "terminal_observed_at": T0.isoformat(),
        "terminal_evidence": evidence,
    }
    receipt = {
        "cycle_id": CYCLE,
        **evidence,
        "machine_code": (
            "outer_strategy_policy_envelope_out_of_bounds"
        ),
        "candidate": candidate,
    }
    if mutation == "missing_marker":
        terminal.pop("candidate_identity")
    elif mutation == "terminal_attempt":
        terminal["attempt_id"] = "supervisor-attempt-substitute"
    elif mutation == "terminal_evidence":
        terminal["terminal_evidence"] = {
            **evidence,
            "rejection_digest": "f" * 64,
        }
    elif mutation == "terminal_time":
        terminal["terminal_observed_at"] = (
            T0 + timedelta(seconds=1)
        ).isoformat()
    elif mutation == "receipt_candidate":
        receipt["candidate"] = {
            **candidate,
            "preview_id": "preview-substitute",
        }
    rows = [terminal]
    if mutation == "duplicate_terminal":
        rows.append(deepcopy(terminal))
    supervisor.store.current_state = lambda _cycle_id: {
        "pre_intent_attempts": deepcopy(rows)
    }

    with pytest.raises(ValueError, match="attempt_store_corrupt"):
        supervisor._bound_outer_policy_rejection(
            CYCLE,
            blocker=blocker,
            loader=lambda **_kwargs: deepcopy(receipt),
        )


def test_receiptless_outer_policy_blocker_requires_exact_park_resolution(
    tmp_path: Path,
) -> None:
    resolution = {"value": None}

    class FakeRiskEnvelopes:
        def legacy_rejection_resolution(self, **_kwargs: dict):
            return deepcopy(resolution["value"])

    supervisor = PaperSupervisor(
        tmp_path / "outputs",
        plane=SimpleNamespace(
            risk_envelopes=FakeRiskEnvelopes(),
            verify_supervisor_outer_policy=lambda: {
                "status": "verified"
            },
        ),
        execution=object(),
        control=lambda *_args, **_kwargs: pytest.fail(
            "legacy recheck must execute zero controls"
        ),
        accounting_reconciliation=lambda: "pass",
    )
    supervisor.store.current_state = lambda _cycle_id: {
        "attempts": [],
        "unfinished_intent": None,
    }
    authority = SimpleNamespace(
        cycle_id=CYCLE,
        active_plan={},
        runtime={
            "desired_state": "stopped",
            "actual_state": "stopped",
            "accepted_order_count": 0,
        },
        accepted_order_fingerprints=(),
        open_position_count=0,
        reconciliation={"execution": "ok", "accounting": "pass"},
    )

    first_state = supervisor.episodes.record_structural_blocker(
        supervisor.episodes.new_cycle(CYCLE, observed_at=T0),
        classification=classify_blocker(
            control_code=(
                "outer_strategy_policy_envelope_out_of_bounds"
            )
        ),
        observed_at=T0,
    )
    unchanged, first_cleared = supervisor._recheck_structural(
        first_state,
        authority=authority,
        observed_at=(T0 + timedelta(minutes=1)).isoformat(),
    )
    assert first_cleared is False
    assert unchanged["mode"] == "blocked_structural"

    resolution["value"] = {
        "resolution_id": "park-legacy-resolution-1",
        "resolution_version": 1,
        "resolution_digest": "d" * 64,
    }
    cleared_state, second_cleared = supervisor._recheck_structural(
        unchanged,
        authority=authority,
        observed_at=(T0 + timedelta(minutes=2)).isoformat(),
    )
    assert second_cleared is True
    assert cleared_state["mode"] == "ready"
    assert cleared_state["events"][-1]["detail"]["evidence"][
        "resolution_digest"
    ] == "d" * 64


def test_concurrent_outer_policy_rechecks_persist_one_clearance(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.plan = {}
    plane.runtime.update(
        {
            "desired_state": "stopped",
            "actual_state": "stopped",
            "strategy_plan_id": None,
            "strategy_plan_version": None,
            "accepted_order_count": 0,
        }
    )

    attempt_id = "supervisor-attempt-concurrent-rejected"
    evidence = {
        "rejection_id": "outer-policy-rejection-concurrent",
        "rejection_digest": "a" * 64,
    }
    rejected_candidate = {
        "supervisor_attempt_id": attempt_id,
        "proposal_id": "proposal-prior-rejected",
        "proposal_digest": "b" * 64,
        "preview_id": "preview-prior-rejected",
        "preview_digest": "c" * 64,
        "facts_digest": "facts-prior-rejected",
        "confirmation_digest": "d" * 64,
        "strategy_type": "grid",
        "direction": "long",
        "limits": {
            "max_actual_leverage": "10",
            "max_full_depth_loss": "1000",
            "max_notional_per_grid": "100",
            "min_grid_count": "10",
            "max_grid_count": "10",
        },
    }
    receipt = {
        "cycle_id": CYCLE,
        **evidence,
        "machine_code": (
            "outer_strategy_policy_envelope_out_of_bounds"
        ),
        "candidate": rejected_candidate,
    }

    class FakeRiskEnvelopes:
        @staticmethod
        def outer_policy_rejection_for_attempt(**kwargs: dict) -> dict:
            assert kwargs["supervisor_attempt_id"] == attempt_id
            return deepcopy(receipt)

        @staticmethod
        def outer_policy_rejection(**kwargs: dict) -> dict:
            assert kwargs["rejection_id"] == evidence["rejection_id"]
            assert kwargs["rejection_digest"] == evidence[
                "rejection_digest"
            ]
            return deepcopy(receipt)

        @staticmethod
        def recheck_outer_policy_rejection(**kwargs: dict) -> dict:
            return {
                "schema_version": (
                    "paper-supervisor-outer-policy-recheck-v1"
                ),
                "rejection_id": kwargs["rejection_id"],
                "rejection_digest": kwargs["rejection_digest"],
                "comparisons": [
                    {
                        "field": "direction",
                        "operator": "in",
                        "authorized_limit": [
                            "long",
                            "neutral",
                            "short",
                        ],
                        "observed_value": "long",
                        "pass": True,
                    }
                ],
                "passed": True,
                "control_actions_executed": 0,
                "recheck_digest": "e" * 64,
            }

        @staticmethod
        def verify_outer_policy_recheck_proof(
            *,
            proof: dict,
            **_kwargs: dict,
        ) -> dict:
            return deepcopy(proof)

    plane.risk_envelopes = FakeRiskEnvelopes()
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="persist-policy-rejection-wal",
    ) as lease:
        assert lease is not None
        lease.record_pre_intent_started(
            attempt_id=attempt_id,
            observed_at=T0.isoformat(),
            phase_scope="create_or_prepare",
        )
        lease.record_pre_intent_candidate_observed(
            attempt_id=attempt_id,
            observed_at=T0.isoformat(),
            candidate_identity=rejected_candidate,
        )
        lease.record_pre_intent_finished(
            attempt_id=attempt_id,
            result="structural",
            machine_code=(
                "outer_strategy_policy_envelope_out_of_bounds"
            ),
            classification="structural",
            observed_at=T0.isoformat(),
            evidence=evidence,
        )
    blocked = supervisor._reconcile_operational_wal(
        supervisor.episodes.new_cycle(CYCLE, observed_at=T0),
        cycle_id=CYCLE,
    )
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="persist-policy-blocker",
    ) as lease:
        assert lease is not None
        supervisor.store.commit_episode_observation(
            lease,
            state=blocked,
            payload={
                "status": "blocked_structural",
                "machine_code": (
                    "outer_strategy_policy_envelope_out_of_bounds"
                ),
                "classification": "structural",
                "control_actions_executed": 0,
            },
        )
    later = T0 + timedelta(minutes=1)
    authority = SimpleNamespace(
        cycle_id=CYCLE,
        active_plan={},
        runtime=deepcopy(plane.runtime),
        accepted_order_fingerprints=(),
        open_position_count=0,
        reconciliation={"execution": "ok", "accounting": "pass"},
    )

    def recheck(index: int) -> dict:
        with supervisor.store.try_lease(
            CYCLE,
            holder_id=f"concurrent-recheck-{index}",
        ) as lease:
            if lease is None:
                return {"status": "lease_held", "control_actions_executed": 0}
            current = supervisor.store.episode_state(CYCLE)
            assert current is not None
            if current["mode"] != "blocked_structural":
                return {
                    "status": "already_cleared",
                    "control_actions_executed": 0,
                }
            updated, cleared = supervisor._recheck_structural(
                current,
                authority=authority,
                observed_at=later.isoformat(),
            )
            assert cleared is True
            supervisor.store.commit_episode_observation(
                lease,
                state=updated,
                payload={
                    "status": "structural_cleared",
                    "control_actions_executed": 0,
                },
            )
            return {
                "status": "structural_cleared",
                "control_actions_executed": 0,
            }

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(recheck, range(2)))

    final_state = supervisor.store.episode_state(CYCLE)
    assert final_state is not None
    assert sum(
        event["event_type"] == "structural_blocker_cleared"
        for event in final_state["events"]
    ) == 1
    assert any(
        result["status"] == "structural_cleared"
        for result in results
    )
    assert all(
        result["control_actions_executed"] == 0
        for result in results
    )
    assert control.calls == []
    next_tick = later + timedelta(seconds=61)
    converged = supervisor.converge_once(
        CYCLE,
        observed_at=next_tick.isoformat(),
        heartbeat=_heartbeat(next_tick),
    )
    assert converged["status"] == "executed"
    assert control.calls == [
        "refresh_recommendation",
        "prepare_start",
        "start",
    ]
    assert len(supervisor.execution.orders) == 3


@pytest.mark.parametrize(
    "reused_field",
    [
        "proposal_id",
        "proposal_digest",
        "preview_id",
        "preview_digest",
        "facts_digest",
        "confirmation_digest",
    ],
)
def test_cleared_rejection_tombstone_blocks_every_old_candidate_identity(
    tmp_path: Path,
    reused_field: str,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.plan = {}
    plane.runtime.update(
        {
            "desired_state": "stopped",
            "actual_state": "stopped",
            "strategy_plan_id": None,
            "strategy_plan_version": None,
            "accepted_order_count": 0,
        }
    )
    evidence = {
        "rejection_id": "outer-policy-rejection-tombstone",
        "rejection_digest": "a" * 64,
    }
    current_candidate = _candidate_identity(
        "placeholder",
        proposal_id="proposal-current",
        proposal_digest="1" * 64,
        preview_id="preview-current",
        preview_digest="2" * 64,
        facts_digest="facts-current",
        confirmation_digest="3" * 64,
    )
    rejected_candidate = _candidate_identity(
        "supervisor-attempt-prior-rejected",
        proposal_id="proposal-prior",
        proposal_digest="4" * 64,
        preview_id="preview-prior",
        preview_digest="5" * 64,
        facts_digest="facts-prior",
        confirmation_digest="6" * 64,
    )
    rejected_candidate[reused_field] = current_candidate[reused_field]
    receipt = {
        "cycle_id": CYCLE,
        **evidence,
        "machine_code": (
            "outer_strategy_policy_envelope_out_of_bounds"
        ),
        "candidate": rejected_candidate,
    }

    class FakeRiskEnvelopes:
        @staticmethod
        def outer_policy_rejection(**_kwargs: dict) -> dict:
            return deepcopy(receipt)

        @staticmethod
        def find_outer_policy_rejection_for_attempt(
            **_kwargs: dict,
        ) -> None:
            return None

    plane.risk_envelopes = FakeRiskEnvelopes()

    def current_identity(
        _cycle_id: str,
        *,
        supervisor_attempt_id: str,
        **_kwargs: dict,
    ) -> dict:
        return {
            **current_candidate,
            "supervisor_attempt_id": supervisor_attempt_id,
        }

    plane.supervisor_candidate_identity = current_identity
    plane.authorize_supervisor_ai_envelope = lambda *_args, **_kwargs: (
        pytest.fail("rejected candidate must not be authorized again")
    )
    cleared = supervisor.episodes.record_structural_blocker(
        supervisor.episodes.new_cycle(CYCLE, observed_at=T0),
        classification=classify_blocker(
            control_code=(
                "outer_strategy_policy_envelope_out_of_bounds"
            )
        ),
        observed_at=T0,
        evidence=evidence,
    )
    cleared = supervisor.episodes.recheck_structural_blocker(
        cleared,
        machine_code=(
            "outer_strategy_policy_envelope_out_of_bounds"
        ),
        condition_cleared=True,
        observed_at=(T0 + timedelta(seconds=1)).isoformat(),
        evidence={
            "schema_version": (
                "paper-supervisor-outer-policy-recheck-v1"
            ),
            **evidence,
            "passed": True,
            "control_actions_executed": 0,
        },
    )
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="persist-rejection-tombstone",
    ) as lease:
        assert lease is not None
        supervisor.store.write_episode_state(lease, cleared)

    later = T0 + timedelta(seconds=61)
    result = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == "plan_identity_conflict"
    assert result["control_actions_executed"] == 0
    assert control.calls == ["refresh_recommendation"]
    assert control.prepare_ids == []
    assert control.start_ids == []
    assert supervisor.execution.orders == []


def test_legacy_unknown_clean_pre_intent_rechecks_without_control_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_id = CYCLE
    attempt_id = "supervisor-attempt-legacy"
    full_plan = {
        "cycle_id": cycle_id,
        "strategy_plan_id": "strategy-plan-current",
        "version": 1,
        "strategy_type": "grid",
        "direction": "neutral",
        "style": "steady",
        "cycle_risk_envelope_id": "envelope-current",
        "range": {
            "low": 3900.0,
            "high": 4200.0,
            "scope": "neutral_side",
            "split_price": 4050.0,
            "source_envelope": {"low": 3900.0, "high": 4200.0},
        },
        "grid": {
            "count": 38,
            "mode": "arithmetic",
            "notional_per_grid": 5000.0,
            "leverage": 10,
        },
    }
    diagnostic_calls: list[dict] = []
    plane = SimpleNamespace(
        active_plan=lambda _cycle_id: deepcopy(full_plan)
    )
    supervisor = PaperSupervisor(
        tmp_path / "outputs",
        plane=plane,
        execution=object(),
        control=lambda *_args, **_kwargs: pytest.fail(
            "structural recheck must execute zero controls"
        ),
        accounting_reconciliation=lambda: "pass",
        pre_intent_diagnostic=lambda payload, diagnostic_as_of: (
            diagnostic_calls.append(
                {
                    "payload": deepcopy(payload),
                    "as_of": diagnostic_as_of,
                }
            )
            or {
                "status": "blocked",
                "code": "frozen_grid_preview_market_moved",
            }
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "_provider_failure_cleared",
        lambda *_args, **_kwargs: False,
    )
    projection = {
        "pre_intent_attempts": [
            {
                "attempt_id": attempt_id,
                "terminal_observed_at": T0.isoformat(),
                "terminal_machine_code": "unknown_blocker",
                "terminal_result": "structural",
                "prepare_succeeded_sequence": None,
            }
        ],
        "attempts": [],
        "unfinished_intent": None,
    }
    monkeypatch.setattr(
        supervisor.store,
        "current_state",
        lambda _cycle_id: deepcopy(projection),
    )
    state = supervisor.episodes.new_cycle(
        cycle_id,
        observed_at=T0,
    )
    state = supervisor.episodes.record_structural_blocker(
        state,
        classification=classify_blocker(
            control_code="unclassified legacy sentence"
        ),
        observed_at=T0,
    )
    authority = SimpleNamespace(
        cycle_id=cycle_id,
        active_plan=supervisor._plan_identity(full_plan),
        runtime={
            "desired_state": "stopped",
            "actual_state": "stopped",
            "accepted_order_count": 0,
        },
        accepted_order_fingerprints=(),
        authorized_order_identities=(),
        open_position_count=0,
        reconciliation={"execution": "ok", "accounting": "pass"},
        control_events=(
            {
                "schema_version": "strategy-control-event-v1",
                "ts": T0.isoformat(),
                "cycle_id": cycle_id,
                "action": "prepare_start",
                "request": {"supervisor_attempt_id": attempt_id},
                "result": "rejected",
                # Deliberately unrelated prose: clearance must not inspect it.
                "error": "arbitrary historical words",
            },
        ),
    )

    updated, cleared = supervisor._recheck_structural(
        state,
        authority=authority,
        observed_at=(T0 + timedelta(minutes=1)).isoformat(),
    )

    assert cleared is True
    assert updated["mode"] == "ready"
    assert updated["blocker"] is None
    assert len(diagnostic_calls) == 1
    assert diagnostic_calls[0]["payload"]["cycle_risk_envelope_id"] == (
        "envelope-current"
    )
    assert diagnostic_calls[0]["as_of"] == T0.isoformat()


@pytest.mark.parametrize(
    "mutation",
    [
        "attempt_mismatch",
        "duplicate_audit",
        "accepted_audit",
        "start_intent",
        "runtime_orders",
        "exposure",
        "reconciliation",
        "authorized_orders",
        "currently_feasible",
        "unknown_diagnostic",
    ],
)
def test_legacy_unknown_recheck_stays_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    attempt_id = "supervisor-attempt-legacy"
    plan = {
        "cycle_id": CYCLE,
        "strategy_plan_id": "strategy-plan-current",
        "version": 1,
        "strategy_type": "grid",
        "direction": "neutral",
        "style": "steady",
        "cycle_risk_envelope_id": "envelope-current",
        "range": {"low": 100, "high": 120, "split_price": 110},
        "grid": {
            "count": 38,
            "mode": "arithmetic",
            "notional_per_grid": 100,
            "leverage": 10,
        },
    }
    supervisor = PaperSupervisor(
        tmp_path / "outputs",
        plane=SimpleNamespace(
            active_plan=lambda _cycle_id: deepcopy(plan)
        ),
        execution=object(),
        control=lambda *_args, **_kwargs: pytest.fail(
            "structural recheck must execute zero controls"
        ),
        accounting_reconciliation=lambda: "pass",
        pre_intent_diagnostic=(
            (
                lambda _payload, _as_of: (_ for _ in ()).throw(
                    ValueError("other")
                )
            )
            if mutation == "unknown_diagnostic"
            else (
                (lambda _payload, _as_of: {"status": "feasible", "code": None})
                if mutation == "currently_feasible"
                else lambda _payload, _as_of: {
                    "status": "blocked",
                    "code": "frozen_grid_preview_market_moved",
                }
            )
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "_provider_failure_cleared",
        lambda *_args, **_kwargs: False,
    )
    projection = {
        "pre_intent_attempts": [
            {
                "attempt_id": attempt_id,
                "terminal_observed_at": T0.isoformat(),
                "terminal_machine_code": "unknown_blocker",
                "terminal_result": "structural",
                "prepare_succeeded_sequence": None,
            }
        ],
        "attempts": [],
        "unfinished_intent": None,
    }
    if mutation == "start_intent":
        projection["attempts"] = [{"attempt_id": attempt_id}]
    monkeypatch.setattr(
        supervisor.store,
        "current_state",
        lambda _cycle_id: deepcopy(projection),
    )
    state = supervisor.episodes.record_structural_blocker(
        supervisor.episodes.new_cycle(CYCLE, observed_at=T0),
        classification=classify_blocker(control_code="legacy unknown"),
        observed_at=T0,
    )
    audit = {
        "ts": T0.isoformat(),
        "cycle_id": CYCLE,
        "action": "prepare_start",
        "request": {
            "supervisor_attempt_id": (
                "different"
                if mutation == "attempt_mismatch"
                else attempt_id
            )
        },
        "result": (
            "accepted" if mutation == "accepted_audit" else "rejected"
        ),
        "error": "anything",
    }
    audits = [audit, deepcopy(audit)] if mutation == "duplicate_audit" else [audit]
    authority = SimpleNamespace(
        cycle_id=CYCLE,
        active_plan=supervisor._plan_identity(plan),
        runtime={
            "desired_state": "stopped",
            "actual_state": "stopped",
            "accepted_order_count": 1 if mutation == "runtime_orders" else 0,
        },
        accepted_order_fingerprints=("order",) if mutation == "exposure" else (),
        authorized_order_identities=(
            ({"order_id": "authorized"},)
            if mutation == "authorized_orders"
            else ()
        ),
        open_position_count=0,
        reconciliation={
            "execution": "drift" if mutation == "reconciliation" else "ok",
            "accounting": "pass",
        },
        control_events=audits,
    )

    updated, cleared = supervisor._recheck_structural(
        state,
        authority=authority,
        observed_at=(T0 + timedelta(minutes=1)).isoformat(),
    )
    assert cleared is False
    assert updated["mode"] == "blocked_structural"
    assert updated["blocker"]["machine_code"] == "unknown_blocker"


def test_missing_rollover_event_and_absent_plan_converge_from_state(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.plan = {}
    plane.runtime.update(
        {
            "strategy_plan_id": None,
            "strategy_plan_version": None,
        }
    )

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert result["status"] == "executed"
    assert control.calls == [
        "refresh_recommendation",
        "prepare_start",
        "start",
    ]
    assert not (
        tmp_path
        / "outputs"
        / "dualtrack"
        / "rollover"
    ).exists()


def test_typed_provider_timeout_reaches_probe_mode_without_side_effects(
    tmp_path: Path,
) -> None:
    supervisor, _control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.plan = {}
    plane.runtime.update(
        {
            "strategy_plan_id": None,
            "strategy_plan_version": None,
        }
    )
    calls: list[str] = []
    details = iter(
        [
            "first timeout text",
            "second wording",
            "third wording",
            "fourth wording",
            "fifth wording",
        ]
    )

    def provider_failure(action: str, _payload: dict) -> dict:
        calls.append(action)
        assert action == "refresh_recommendation"
        raise RecommendationProviderError(
            "strategy_recommendation_provider_timeout",
            next(details),
        )

    supervisor.control = provider_failure
    observed_at = T0
    supervisor.store.now = lambda: observed_at
    for index in range(5):
        result = supervisor.converge_once(
            CYCLE,
            observed_at=observed_at.isoformat(),
            heartbeat=_heartbeat(observed_at),
        )
        assert result["machine_code"] == (
            "strategy_recommendation_provider_timeout"
        )
        assert result["classification"] == "transient"
        assert result["control_actions_executed"] == 0
        state = supervisor.store.episode_state(CYCLE)
        assert state is not None
        if index < 4:
            assert state["mode"] == "backing_off"
            observed_at = datetime.fromisoformat(
                state["episode"]["next_attempt_at"]
            )

    assert state["mode"] == "probing"
    assert state["alert_required"] is True
    assert state["episode"]["consecutive_transient_failures"] == 5
    assert state["events"][-1]["event_label"] == (
        "episode_short_budget_exhausted"
    )
    assert calls == ["refresh_recommendation"] * 5
    assert supervisor.store.current_state(CYCLE)["attempt_count"] == 0
    assert supervisor.execution.orders == []
    assert plane.active_plan(CYCLE) == {}


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(
            "strategy_recommendation_provider_timeout: untyped prose"
        ),
        RecommendationProviderError(
            "",
            "strategy_recommendation_provider_timeout",
        ),
    ],
    ids=["untyped-known-words", "malformed-typed-code"],
)
def test_untyped_or_malformed_provider_failure_stays_structural(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    supervisor, _control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.plan = {}
    plane.runtime.update(
        {
            "strategy_plan_id": None,
            "strategy_plan_version": None,
        }
    )
    calls: list[str] = []

    def fail(action: str, _payload: dict) -> dict:
        calls.append(action)
        raise failure

    supervisor.control = fail
    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(T0),
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == "unknown_blocker"
    assert result["control_actions_executed"] == 0
    assert calls == ["refresh_recommendation"]
    assert supervisor.store.current_state(CYCLE)["attempt_count"] == 0
    assert supervisor.execution.orders == []
    assert plane.active_plan(CYCLE) == {}


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        (
            {
                "schema_version": "strategy-ai-evaluation-v2",
                "status": "failed",
                "output": {
                    "machine_code": (
                        "strategy_recommendation_provider_timeout"
                    ),
                    "error": "completely unrelated human text",
                },
            },
            True,
        ),
        (
            {
                "schema_version": "strategy-ai-evaluation-v2",
                "status": "failed",
                "output": {
                    "error": (
                        "strategy_recommendation_provider_timeout: legacy prose"
                    )
                },
            },
            False,
        ),
        (
            {
                "schema_version": "strategy-ai-evaluation-v2",
                "status": "failed",
                "output": {
                    "machine_code": "not_on_the_whitelist",
                    "error": "strategy_recommendation_provider_timeout",
                },
            },
            False,
        ),
    ],
    ids=["typed", "legacy-prose-only", "unknown-code"],
)
def test_provider_recheck_consumes_only_typed_receipt_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt: dict,
    expected: bool,
) -> None:
    supervisor, _control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    evaluated_at = (T0 + timedelta(seconds=1)).isoformat()
    stored = {
        "evaluation_id": "ai-eval-provider-recheck",
        "cycle_id": CYCLE,
        "evaluated_at": evaluated_at,
        **deepcopy(receipt),
    }
    path = (
        tmp_path
        / "outputs"
        / "dualtrack"
        / "strategy_control"
        / "evaluations"
        / CYCLE
        / "provider-recheck.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([stored]), encoding="utf-8")
    monkeypatch.setattr(
        "services.paper_supervisor.CloudAIProviderReadiness.verify",
        lambda _self: {"ok": True},
    )

    assert supervisor._provider_failure_cleared(
        CYCLE,
        blocked_at=T0.isoformat(),
    ) is expected


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"event": "other"}, "heartbeat_incomplete"),
        ({"detail": {"ledger_refreshed": False}}, "heartbeat_incomplete"),
        ({"cycle_id": "2026-07-30_NIGHT"}, "heartbeat_cycle_mismatch"),
    ],
)
def test_partial_or_wrong_heartbeat_creates_zero_control_actions(
    tmp_path: Path,
    mutation: dict,
    reason: str,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    heartbeat = _heartbeat()
    heartbeat.update(mutation)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=heartbeat,
    )

    assert result["control_actions_executed"] == 0
    assert result["heartbeat"]["reason"] == reason
    assert control.calls == []


def test_complete_heartbeat_validator_rejects_future_and_stale() -> None:
    future = validate_complete_tick_heartbeat(
        _heartbeat(T0 + timedelta(seconds=1)),
        cycle_id=CYCLE,
        observed_at=T0,
    )
    stale = validate_complete_tick_heartbeat(
        _heartbeat(T0),
        cycle_id=CYCLE,
        observed_at=T0 + timedelta(seconds=181),
    )

    assert future["reason"] == "heartbeat_in_future"
    assert stale["reason"] == "heartbeat_stale"


def test_reconciliation_drift_is_structural_and_creates_zero_orders(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    supervisor.accounting_reconciliation = lambda: "drift"

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert result["machine_code"] == "ledger_reconciliation_drift"
    assert result["classification"] == "structural"
    assert result["control_actions_executed"] == 0
    assert control.calls == []


def test_unresolved_previous_cycle_runtime_is_structural(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    plane.runtime.update(
        {
            "cycle_id": "2026-07-29_NIGHT",
            "desired_state": "running",
            "actual_state": "running",
            "accepted_order_count": 3,
        }
    )

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert (
        result["machine_code"]
        == "previous_cycle_paper_state_unresolved"
    )
    assert control.calls == []


def test_cleared_reconciliation_blocker_resumes_without_old_command_replay(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    status = {"value": "drift"}
    supervisor.accounting_reconciliation = lambda: status["value"]
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    status["value"] = "pass"
    later = T0 + timedelta(seconds=61)

    second = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )
    third_at = later + timedelta(seconds=61)
    third = supervisor.converge_once(
        CYCLE,
        observed_at=third_at.isoformat(),
        heartbeat=_heartbeat(third_at),
    )

    assert first["machine_code"] == "ledger_reconciliation_drift"
    assert second["status"] == "structural_cleared"
    assert second["control_actions_executed"] == 0
    assert third["status"] == "executed"
    assert control.calls == ["prepare_start", "start"]


def test_pre_intent_deadline_is_transient_and_calls_no_control(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    ticks = iter([0.0, 46.0])
    supervisor.monotonic = lambda: next(ticks)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert (
        result["machine_code"]
        == "supervisor_attempt_deadline_before_intent"
    )
    assert result["classification"] == "transient"
    assert control.calls == []


def test_contending_loop_returns_lease_held_and_calls_no_control(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="test-holder",
    ) as lease:
        assert lease is not None
        result = supervisor.converge_once(
            CYCLE,
            observed_at=T0.isoformat(),
            heartbeat=_heartbeat(),
        )

    assert result["status"] == "lease_held"
    assert result["control_actions_executed"] == 0
    assert control.calls == []


def test_public_prepare_exposes_exact_frozen_start_intent_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: build_execution_engine_adapter(
            output
        ),
    )
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(CYCLE, "ai"))
    active = plane.lock_production_plan(
        CYCLE,
        selected_proposal_id=saved["proposal_id"],
    )

    prepared = plane.control(
        CYCLE,
        "prepare_start",
        adaptive_grid_payload(),
        market=market(close=4_137.44),
        account=account_context(),
        now=T0.isoformat(),
    )
    contract = prepared["start_intent_contract"]

    assert contract["pre_start_plan_identity"] == {
        "strategy_plan_id": active["strategy_plan_id"],
        "strategy_plan_version": active["version"],
        "strategy_type": "grid",
        "direction": active["direction"],
    }
    assert contract["plan_identity"]["strategy_plan_version"] == (
        active["version"] + 1
    )
    assert contract["expected_order_count"] == len(
        prepared["preview"]["orders"]
    )
    assert len(
        set(contract["expected_order_fingerprints"])
    ) == contract["expected_order_count"]


def test_runner_supervisor_mode_never_calls_legacy_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = deepcopy(TEST_CONFIG)
    config["cycle_decision"] = {"enabled": False}
    config["convergence"] = {
        "mode": "paper_supervisor",
        "provider_timeout_seconds": 25,
        "attempt_deadline_seconds": 45,
    }
    runner = DualTrackCycleRunner(
        output_root=tmp_path / "outputs",
        market_db=tmp_path / "market.db",
        config=config,
    )
    monkeypatch.setattr(runner, "_lifecycle_results", lambda _now: [])
    monkeypatch.setattr(
        runner,
        "_sweep_active_human_protective_exits",
        lambda *args, **kwargs: {"status": "skipped"},
    )
    monkeypatch.setattr(
        runner,
        "sync_obsidian_human_plans",
        lambda **kwargs: {"status": "skipped"},
    )
    monkeypatch.setattr(
        runner,
        "intraday_tick",
        lambda **kwargs: {"status": "skipped"},
    )
    monkeypatch.setattr(
        runner.scorer,
        "rebuild_ledgers",
        lambda: {"daily": []},
    )
    heartbeat = _heartbeat(
        datetime(2026, 7, 30, 1, 2, tzinfo=timezone.utc)
    )
    heartbeat["cycle_id"] = "2026-07-30_DAY"
    monkeypatch.setattr(
        runner,
        "_write_runner_state",
        lambda *args, **kwargs: heartbeat,
    )
    monkeypatch.setattr(
        runner,
        "_ensure_cycle_decision",
        lambda *args, **kwargs: pytest.fail(
            "legacy coordinator was called"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_ensure_paper_supervisor",
        lambda *args, **kwargs: {
            "status": "healthy",
            "control_actions_executed": 0,
        },
    )

    result = runner.live_tick(
        as_of="2026-07-30T01:02:00+00:00"
    )

    assert result["cycle_decision"]["status"] == "healthy"


def test_invalid_double_enabled_convergence_fails_closed(
    tmp_path: Path,
) -> None:
    config = deepcopy(TEST_CONFIG)
    config["cycle_decision"] = {"enabled": True}
    config["convergence"] = {
        "mode": "paper_supervisor",
        "provider_timeout_seconds": 25,
        "attempt_deadline_seconds": 45,
    }
    runner = DualTrackCycleRunner(
        output_root=tmp_path / "outputs",
        market_db=tmp_path / "market.db",
        config=config,
    )

    with pytest.raises(
        ValueError,
        match="supervisor_configuration_invalid",
    ):
        runner._convergence_mode()


def test_malformed_prepare_receipt_is_structural_and_not_retried(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )

    def malformed(action: str, payload: dict) -> dict:
        if action == "prepare_start":
            control.calls.append(action)
            return {
                "prepared_start_id": "prepared-malformed",
                "preview": {"preview_id": "preview-malformed"},
            }
        return control(action, payload)

    supervisor.control = malformed
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    later = T0 + timedelta(seconds=61)
    second = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert first["machine_code"] == "execution_receipt_identity_invalid"
    assert first["classification"] == "structural"
    assert second["status"] == "blocked_structural"
    assert control.calls == ["prepare_start"]


def test_missing_accepted_audit_is_dangerous_and_never_auto_clears(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    control._audit = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    later = T0 + timedelta(seconds=61)
    second = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert (
        first["machine_code"]
        == "partial_execution_or_cleanup_required"
    )
    assert first["status"] == "blocked_structural"
    assert second["status"] == "blocked_structural"
    assert control.calls.count("start") == 1


def test_running_adoption_allows_historical_rearm_but_requires_current_n_of_n(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"
    execution = supervisor.execution
    original = deepcopy(execution.orders[0])
    execution.orders[0]["state"] = "filled"
    execution.positions = [
        {
            "position_id": "position-order-0",
            "trade_id": "order-0",
            "status": "open",
            "side": "long",
            "remaining_units": 1.0,
            "entry_price": 100.0,
            "strategy_plan_id": plane.plan["strategy_plan_id"],
            "strategy_plan_version": plane.plan["version"],
        }
    ]
    execution.fills = [
        {
            "fill_id": "fill-order-0",
            "order_id": "order-0",
            "trade_id": "order-0",
            "event": "entry",
            "side": "buy",
            "price": 100.0,
            "quantity": 1.0,
            "strategy_plan_id": plane.plan["strategy_plan_id"],
            "strategy_plan_version": plane.plan["version"],
        }
    ]
    execution.orders.append(
        {
            **original,
            "source_fill_id": f"{original['source_fill_id']}:rearm:1",
            "order_id": "rearm-history-1",
            "state": "cancelled",
        }
    )
    # Runtime preserves the three logical slots accepted at start even though
    # one slot is now represented by an open position.
    plane.runtime["accepted_order_count"] = 3
    later = T0 + timedelta(seconds=61)

    adopted = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert adopted["terminal_status"] == "adopted_existing"
    assert adopted["status"] == "healthy"


def test_exact_filled_slot_clears_persisted_order_identity_blocker(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    clock = {"now": T0}
    supervisor.store.now = lambda: clock["now"]
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"

    execution = supervisor.execution
    execution.orders[0]["state"] = "filled"
    execution.positions = [{
        "position_id": "position-order-0",
        "trade_id": "order-0",
        "status": "open",
        "side": "long",
        "remaining_units": 1.0,
        "entry_price": 100.0,
        "strategy_plan_id": plane.plan["strategy_plan_id"],
        "strategy_plan_version": plane.plan["version"],
    }]
    execution.fills = [{
        "fill_id": "fill-order-0",
        "order_id": "order-0",
        "trade_id": "order-0",
        "event": "entry",
        "side": "buy",
        "price": 100.0,
        "quantity": 1.0,
        "strategy_plan_id": plane.plan["strategy_plan_id"],
        "strategy_plan_version": plane.plan["version"],
    }]
    # Start accepted three logical slots; one is now represented by the
    # exact open position and two remain accepted entry orders.
    plane.runtime["accepted_order_count"] = 3

    state = supervisor.store.episode_state(CYCLE)
    assert state is not None
    blocked_at = T0 + timedelta(seconds=30)
    blocked = supervisor.episodes.record_structural_blocker(
        state,
        classification=classify_blocker(
            control_code="order_identity_conflict"
        ),
        observed_at=blocked_at,
    )
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="persist-false-blocker",
    ) as lease:
        assert lease is not None
        supervisor.store.commit_episode_observation(
            lease,
            state=blocked,
            payload={
                "status": "blocked_structural",
                "machine_code": "order_identity_conflict",
                "classification": "structural",
                "control_actions_executed": 0,
            },
        )

    clearing_at = T0 + timedelta(seconds=61)
    clock["now"] = clearing_at
    cleared = supervisor.converge_once(
        CYCLE,
        observed_at=clearing_at.isoformat(),
        heartbeat=_heartbeat(clearing_at),
    )
    adopted_at = clearing_at + timedelta(seconds=61)
    clock["now"] = adopted_at
    adopted = supervisor.converge_once(
        CYCLE,
        observed_at=adopted_at.isoformat(),
        heartbeat=_heartbeat(adopted_at),
    )

    assert cleared["status"] == "structural_cleared"
    assert cleared["control_actions_executed"] == 0
    assert adopted["status"] == "healthy"
    assert adopted["terminal_status"] == "adopted_existing"
    assert adopted["control_actions_executed"] == 0
    assert control.calls == ["prepare_start", "start"]


@pytest.mark.parametrize("fill_trade_id", [None, "different-order"])
def test_running_adoption_requires_exact_open_position_entry_fill_lineage(
    tmp_path: Path,
    fill_trade_id: str | None,
) -> None:
    supervisor, _control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"
    execution = supervisor.execution
    execution.orders[0]["state"] = "filled"
    execution.positions = [
        {
            "position_id": "position-order-0",
            "trade_id": "order-0",
            "status": "open",
            "side": "long",
            "remaining_units": 1.0,
            "entry_price": 100.0,
            "strategy_plan_id": plane.plan["strategy_plan_id"],
            "strategy_plan_version": plane.plan["version"],
        }
    ]
    execution.fills = (
        []
        if fill_trade_id is None
        else [
            {
                "fill_id": "fill-order-0",
                "trade_id": fill_trade_id,
                "event": "entry",
                "side": "buy",
                "price": 100.0,
                "quantity": 1.0,
                "strategy_plan_id": plane.plan[
                    "strategy_plan_id"
                ],
                "strategy_plan_version": plane.plan["version"],
            }
        ]
    )
    plane.runtime["accepted_order_count"] = 2
    later = T0 + timedelta(seconds=61)

    blocked = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert blocked["status"] == "blocked_structural"
    assert blocked["machine_code"] == "order_identity_conflict"


@pytest.mark.parametrize(
    (
        "position_side",
        "fill_side",
        "position_entry_price",
        "fill_price",
        "fill_plan_id",
    ),
    [
        ("short", "buy", 100.0, 100.0, "exact"),
        ("short", "sell", 100.0, 100.0, "exact"),
        ("long", "buy", 99.0, 100.0, "exact"),
        ("long", "buy", 100.0, 100.0, None),
    ],
)
def test_running_adoption_rejects_position_economic_identity_drift(
    tmp_path: Path,
    position_side: str,
    fill_side: str,
    position_entry_price: float,
    fill_price: float,
    fill_plan_id: str | None,
) -> None:
    supervisor, _control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"
    execution = supervisor.execution
    execution.orders[0]["state"] = "filled"
    execution.positions = [
        {
            "position_id": "position-order-0",
            "trade_id": "order-0",
            "status": "open",
            "side": position_side,
            "remaining_units": 1.0,
            "entry_price": position_entry_price,
            "strategy_plan_id": plane.plan["strategy_plan_id"],
            "strategy_plan_version": plane.plan["version"],
        }
    ]
    execution.fills = [
        {
            "fill_id": "fill-order-0",
            "order_id": "order-0",
            "trade_id": "order-0",
            "event": "entry",
            "side": fill_side,
            "price": fill_price,
            "quantity": 1.0,
            "strategy_plan_id": (
                plane.plan["strategy_plan_id"]
                if fill_plan_id == "exact"
                else fill_plan_id
            ),
            "strategy_plan_version": plane.plan["version"],
        }
    ]
    plane.runtime["accepted_order_count"] = 2
    later = T0 + timedelta(seconds=61)

    blocked = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert blocked["status"] == "blocked_structural"
    assert blocked["machine_code"] == "order_identity_conflict"


def test_running_adoption_rejects_entry_quantity_above_authorized_order(
    tmp_path: Path,
) -> None:
    supervisor, _control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"
    execution = supervisor.execution
    execution.orders[0]["state"] = "filled"
    execution.positions = [
        {
            "position_id": "position-order-0",
            "trade_id": "order-0",
            "status": "open",
            "side": "long",
            "remaining_units": 3.0,
            "entry_price": 100.0,
            "strategy_plan_id": plane.plan["strategy_plan_id"],
            "strategy_plan_version": plane.plan["version"],
        }
    ]
    execution.fills = [
        {
            "fill_id": "fill-order-0",
            "order_id": "order-0",
            "trade_id": "order-0",
            "event": "entry",
            "side": "buy",
            "price": 100.0,
            "quantity": 3.0,
            "strategy_plan_id": plane.plan["strategy_plan_id"],
            "strategy_plan_version": plane.plan["version"],
        }
    ]
    plane.runtime["accepted_order_count"] = 2
    later = T0 + timedelta(seconds=61)

    blocked = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert blocked["status"] == "blocked_structural"
    assert blocked["machine_code"] == "order_identity_conflict"


def test_non_entry_commands_do_not_pollute_start_identity_authority(
    tmp_path: Path,
) -> None:
    supervisor, _control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"
    path = (
        supervisor.output_root
        / "dualtrack"
        / "nautilus_authoritative"
        / "commands"
        / f"{CYCLE}.json"
    )
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows.append(
        {
            "command_id": "order-0-TP",
            "cycle_id": CYCLE,
            "command": {
                "cycle_id": CYCLE,
                "event": "target",
                "side": "sell",
                "price": 110.0,
                "quantity": 1.0,
                "strategy_plan_id": plane.plan[
                    "strategy_plan_id"
                ],
                "strategy_plan_version": plane.plan["version"],
                "source_fill_id": "target:order-0",
            },
        }
    )
    path.write_text(json.dumps(rows), encoding="utf-8")
    later = T0 + timedelta(seconds=61)

    adopted = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert adopted["status"] == "healthy"
    assert adopted["terminal_status"] == "adopted_existing"


def test_running_adoption_rejects_unverified_replacement_orders(
    tmp_path: Path,
) -> None:
    supervisor, _control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"
    execution = supervisor.execution
    originals = deepcopy(execution.orders)
    for row in execution.orders:
        row["state"] = "cancelled"
    execution.orders.extend(
        {
            **row,
            "order_id": f"forged-{index}",
            "source_fill_id": f"forged-rearm-{index}",
            "price": float(row["price"]) + 9.0,
            "state": "accepted",
        }
        for index, row in enumerate(originals)
    )
    plane.runtime["accepted_order_count"] = len(originals)
    later = T0 + timedelta(seconds=61)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == "order_identity_conflict"


@pytest.mark.parametrize("replacement_id", [None, "order-0"])
def test_running_adoption_rejects_missing_or_duplicate_order_ids(
    tmp_path: Path,
    replacement_id: str | None,
) -> None:
    supervisor, _control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"
    if replacement_id is None:
        for row in supervisor.execution.orders:
            row.pop("order_id", None)
    else:
        supervisor.execution.orders[1]["order_id"] = replacement_id
    later = T0 + timedelta(seconds=61)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == "order_identity_conflict"


def test_running_adoption_rejects_conflicting_start_audits(
    tmp_path: Path,
) -> None:
    supervisor, control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    assert first["status"] == "executed"
    control._audit(
        {
            "expected_preview_id": first["preview_id"],
            "prepared_start_id": first["prepared_start_id"],
        },
        result="rejected",
        error="prepared_start_market_moved",
    )
    later = T0 + timedelta(seconds=61)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == "order_identity_conflict"


def test_running_adoption_rejects_cancelled_entry_without_open_position(
    tmp_path: Path,
) -> None:
    supervisor, _control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    supervisor.execution.orders[0]["state"] = "cancelled"
    plane.runtime["accepted_order_count"] = 2
    later = T0 + timedelta(seconds=61)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert result["machine_code"] == "order_identity_conflict"
    assert result["status"] == "blocked_structural"


def test_running_adoption_rejects_economic_order_drift(
    tmp_path: Path,
) -> None:
    supervisor, _control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    supervisor.execution.orders[0]["price"] += 0.5
    later = T0 + timedelta(seconds=61)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert result["machine_code"] == "order_identity_conflict"


def test_blocking_start_is_interrupted_and_recovers_as_unknown(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    original = supervisor.control

    def blocking(action: str, payload: dict) -> dict:
        if action == "start":
            control.calls.append(action)
            control.start_ids.append(str(payload["prepared_start_id"]))
            time.sleep(2)
            raise AssertionError("deadline did not interrupt start")
        return original(action, payload)

    supervisor.control = blocking
    supervisor.attempt_deadline_seconds = 1
    before = time.monotonic()
    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )
    elapsed = time.monotonic() - before

    assert elapsed < 1.5
    assert result["machine_code"] == "control_outcome_unknown"
    assert result["status"] == "blocked_structural"
    assert result["authority_recovery_pending"] is True
    assert control.calls.count("start") == 1
    later = T0 + timedelta(seconds=61)
    recovered = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )
    assert recovered["machine_code"] == "control_outcome_unknown"
    assert recovered.get("authority_recovery_pending") is not True
    projection = supervisor.store.current_state(CYCLE)
    assert projection["budget_floor"]["dangerous_start_attempts"] == 1


def test_fresh_authority_change_before_intent_creates_zero_orders(
    tmp_path: Path,
) -> None:
    supervisor, control, plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    original = supervisor.control

    def race(action: str, payload: dict) -> dict:
        response = original(action, payload)
        if action == "prepare_start":
            plane.runtime.update(
                {
                    "desired_state": "running",
                    "actual_state": "running",
                }
            )
        return response

    supervisor.control = race
    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert result["machine_code"] == "runtime_state_conflict"
    assert control.calls == ["prepare_start"]
    assert supervisor.execution.orders == []


def test_authoritative_rearm_topology_rejects_cross_slot_and_economic_drift() -> None:
    base = {
        "command_id": "command-1",
        "fingerprint": "a" * 64,
        "side": "buy",
        "quantity": "1",
        "price": "4000",
        "generation": 1,
        "slot_id": "slot-a",
        "rearm_of_order_id": None,
        "economics": {
            "event": "entry",
            "symbol": "GOLD",
            "order_type": "limit",
            "notional": "4000",
            "sl": "3900",
            "tp": "4100",
            "strategy_plan_id": "plan-1",
            "strategy_plan_version": 1,
        },
    }
    hidden_cross_slot = {
        **base,
        "command_id": "command-2",
        "fingerprint": "b" * 64,
        "generation": 2,
        "slot_id": "slot-b",
        "rearm_of_order_id": "command-1",
    }
    with pytest.raises(
        ValueError,
        match="execution_receipt_identity_invalid",
    ):
        PaperSupervisor._validate_authoritative_rearm_topology(
            [base, hidden_cross_slot]
        )

    changed_protection = {
        **base,
        "command_id": "command-2",
        "fingerprint": "c" * 64,
        "generation": 2,
        "rearm_of_order_id": "command-1",
        "economics": {
            **base["economics"],
            "tp": "9000",
        },
    }
    with pytest.raises(
        ValueError,
        match="execution_receipt_identity_invalid",
    ):
        PaperSupervisor._validate_authoritative_rearm_topology(
            [base, changed_protection]
        )


def test_legacy_config_without_convergence_uses_legacy_fallback(
    tmp_path: Path,
) -> None:
    config = deepcopy(TEST_CONFIG)
    config.pop("convergence", None)
    config["cycle_decision"] = {"enabled": True}
    runner = DualTrackCycleRunner(
        output_root=tmp_path / "outputs",
        market_db=tmp_path / "market.db",
        config=config,
    )

    assert runner._convergence_mode() == "legacy_cycle_decision"


def test_crash_after_clean_recovery_replays_budget_before_next_start(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["market_moved", "accepted"],
    )
    supervisor.store.now = lambda: T0
    real_write = supervisor.store.write_episode_state
    crash_once = {"armed": True}

    def crash_after_terminal(lease, state: dict) -> None:
        if (
            crash_once["armed"]
            and int(
                (state.get("budgets") or {}).get(
                    "clean_refusal_observations"
                )
                or 0
            )
            == 1
        ):
            crash_once["armed"] = False
            raise RuntimeError("injected episode projection crash")
        real_write(lease, state)

    supervisor.store.write_episode_state = crash_after_terminal
    with pytest.raises(
        RuntimeError,
        match="injected episode projection crash",
    ):
        supervisor.converge_once(
            CYCLE,
            observed_at=T0.isoformat(),
            heartbeat=_heartbeat(),
        )
    supervisor.store.write_episode_state = real_write
    later = T0 + timedelta(seconds=61)

    recovered = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert recovered["status"] == "executed"
    assert control.start_ids == ["prepared-1", "prepared-2"]
    episode = supervisor.store.episode_state(CYCLE)
    assert episode["budgets"]["clean_refusal_observations"] == 1


def test_crash_after_unknown_recovery_restores_dangerous_block(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    original = supervisor.control

    def response_lost(action: str, payload: dict) -> dict:
        if action == "start":
            control.calls.append(action)
            control.start_ids.append(str(payload["prepared_start_id"]))
            raise TimeoutError("lost response")
        return original(action, payload)

    supervisor.control = response_lost
    real_write = supervisor.store.write_episode_state
    crash_once = {"armed": True}

    def crash_after_terminal(lease, state: dict) -> None:
        if (
            crash_once["armed"]
            and int(
                (state.get("budgets") or {}).get(
                    "dangerous_start_attempts"
                )
                or 0
            )
            == 1
        ):
            crash_once["armed"] = False
            raise RuntimeError("injected dangerous projection crash")
        real_write(lease, state)

    supervisor.store.write_episode_state = crash_after_terminal
    with pytest.raises(
        RuntimeError,
        match="injected dangerous projection crash",
    ):
        supervisor.converge_once(
            CYCLE,
            observed_at=T0.isoformat(),
            heartbeat=_heartbeat(),
        )
    supervisor.store.write_episode_state = real_write
    later = T0 + timedelta(seconds=61)

    recovered = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert recovered["status"] == "blocked_structural"
    assert recovered["machine_code"] == "control_outcome_unknown"
    assert control.calls.count("start") == 1
    episode = supervisor.store.episode_state(CYCLE)
    assert episode["budgets"]["dangerous_start_attempts"] == 1


def test_deadline_after_partial_side_effect_runs_exception_cleanup(
    tmp_path: Path,
) -> None:
    supervisor, control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    real_call = control.__call__

    def cleanup_aware_control(action: str, payload: dict) -> dict:
        if action != "start":
            return real_call(action, payload)
        supervisor.execution.orders.append(
            {
                **_commands(control.prepared[
                    payload["prepared_start_id"]
                ]["future_plan"])[0],
                "order_id": "partial-order",
                "state": "accepted",
            }
        )
        try:
            time.sleep(2)
        except Exception:
            supervisor.execution.orders.clear()
            supervisor.execution.positions.clear()
            raise
        raise AssertionError("deadline did not interrupt start")

    supervisor.control = cleanup_aware_control
    supervisor.attempt_deadline_seconds = 1

    result = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=_heartbeat(),
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == "control_outcome_unknown"
    assert supervisor.execution.orders == []
    assert supervisor.execution.positions == []


def test_live_tick_hard_watchdog_exits_a_stuck_process_before_systemd(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "watchdog-entered"
    script = f"""
import time
from pathlib import Path
from pipelines.dualtrack_cycle_runner import live_tick_hard_watchdog

with live_tick_hard_watchdog(0.25):
    Path({str(marker)!r}).write_text("entered", encoding="utf-8")
    time.sleep(10)
"""
    started = time.monotonic()

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 124
    assert time.monotonic() - started < 2.0
    assert marker.read_text(encoding="utf-8") == "entered"


def test_hard_watchdog_survives_soft_deadline_cleanup_stall(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "soft-deadline-caught"
    script = f"""
import time
from pathlib import Path
from pipelines.dualtrack_cycle_runner import live_tick_hard_watchdog
from services.paper_supervisor import (
    PaperSupervisor,
    SupervisorAttemptDeadline,
)
import services.paper_supervisor as supervisor_module

supervisor = object.__new__(PaperSupervisor)
supervisor.monotonic = time.monotonic
supervisor.attempt_deadline_seconds = 0.1
supervisor_module.HARD_DEADLINE_RECOVERY_MARGIN_SECONDS = 0.2
with live_tick_hard_watchdog(0.5):
    with supervisor._attempt_deadline(time.monotonic()):
        try:
            time.sleep(10)
        except SupervisorAttemptDeadline:
            Path({str(marker)!r}).write_text("caught", encoding="utf-8")
            time.sleep(10)
"""
    started = time.monotonic()

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 124
    assert time.monotonic() - started < 2.0
    assert marker.read_text(encoding="utf-8") == "caught"


def test_crashed_pre_intent_reservation_counts_once_after_restarts(
    tmp_path: Path,
) -> None:
    supervisor, _control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="crashed-owner",
    ) as lease:
        assert lease is not None
        lease.record_pre_intent_started(
            attempt_id="supervisor-attempt-crashed",
            observed_at=T0.isoformat(),
            phase_scope="create_or_prepare",
        )
    later = T0 + timedelta(seconds=1)

    first = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )
    second_at = later + timedelta(seconds=1)
    second = supervisor.converge_once(
        CYCLE,
        observed_at=second_at.isoformat(),
        heartbeat=_heartbeat(second_at),
    )

    assert first["status"] == "backing_off"
    assert second["status"] == "backing_off"
    episode = supervisor.store.episode_state(CYCLE)
    assert (
        episode["episode"]["consecutive_transient_failures"]
        == 1
    )
    projection = supervisor.store.current_state(CYCLE)
    crashed = next(
        row
        for row in projection["pre_intent_attempts"]
        if row["attempt_id"] == "supervisor-attempt-crashed"
    )
    assert crashed["terminal_event_type"] == (
        "pre_intent_attempt_abandoned"
    )


def test_crashed_prepare_success_resets_exhausted_episode_before_abandonment(
    tmp_path: Path,
) -> None:
    supervisor, _control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    state = supervisor.episodes.new_cycle(
        CYCLE,
        observed_at=T0.isoformat(),
    )
    failure_times = (0, 60, 180, 480, 1080)
    for seconds in failure_times:
        state = supervisor.episodes.record_transient_failure(
            state,
            classification=classify_blocker(
                control_code="prepared_start_market_moved"
            ),
            observed_at=(
                T0 + timedelta(seconds=seconds)
            ).isoformat(),
        )
    assert state["mode"] == "probing"
    prepare_at = T0 + timedelta(seconds=2880)
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="crashed-owner",
    ) as lease:
        assert lease is not None
        supervisor.store.write_episode_state(lease, state)
        lease.record_pre_intent_started(
            attempt_id="supervisor-attempt-prepared",
            observed_at=prepare_at.isoformat(),
            phase_scope="create_or_prepare",
        )
        lease.record_pre_intent_prepare_succeeded(
            attempt_id="supervisor-attempt-prepared",
            observed_at=prepare_at.isoformat(),
        )
    restarted_at = prepare_at + timedelta(seconds=1)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=restarted_at.isoformat(),
        heartbeat=_heartbeat(restarted_at),
    )

    assert result["status"] == "backing_off"
    recovered = supervisor.store.episode_state(CYCLE)
    assert recovered["mode"] == "backing_off"
    assert recovered["alert_required"] is False
    assert recovered["episode"]["consecutive_transient_failures"] == 1
    assert recovered["episode"]["generation"] == 2


def test_crashed_missing_heartbeat_wal_preserves_ten_minute_continuity(
    tmp_path: Path,
) -> None:
    supervisor, _control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="crashed-owner",
    ) as lease:
        assert lease is not None
        lease.record_typed_heartbeat(
            observation_id="heartbeat-observation-crashed",
            observed_at=T0.isoformat(),
            status="missing",
            machine_code=(
                "execution_tick_heartbeat_temporarily_missing"
            ),
            reason="heartbeat_missing",
            heartbeat_recorded_at=None,
            heartbeat_digest="0" * 64,
        )
    later = T0 + timedelta(seconds=601)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=None,
    )

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == "execution_tick_scheduler_down"


def test_crashed_fresh_heartbeat_wal_clears_missing_continuity(
    tmp_path: Path,
) -> None:
    supervisor, _control, _plane = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="crashed-owner",
    ) as lease:
        assert lease is not None
        lease.record_typed_heartbeat(
            observation_id="heartbeat-observation-missing",
            observed_at=T0.isoformat(),
            status="missing",
            machine_code=(
                "execution_tick_heartbeat_temporarily_missing"
            ),
            reason="heartbeat_missing",
            heartbeat_recorded_at=None,
            heartbeat_digest="0" * 64,
        )
        fresh_at = T0 + timedelta(seconds=300)
        lease.record_typed_heartbeat(
            observation_id="heartbeat-observation-fresh",
            observed_at=fresh_at.isoformat(),
            status="fresh",
            machine_code="heartbeat_fresh",
            reason="complete",
            heartbeat_recorded_at=fresh_at.isoformat(),
            heartbeat_digest="1" * 64,
        )
    later = T0 + timedelta(seconds=601)

    result = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=None,
    )

    assert result["status"] == "backing_off"
    assert (
        result["machine_code"]
        == "execution_tick_heartbeat_temporarily_missing"
    )


def test_same_fresh_tick_returns_original_observation_without_second_start(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    supervisor.store.now = lambda: T0
    heartbeat = _heartbeat()

    first = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=heartbeat,
    )
    repeated = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=heartbeat,
    )

    assert first == repeated
    assert first["status"] == "executed"
    assert control.calls.count("start") == 1
    assert len(supervisor.store.observations(CYCLE)) == 1
    projection = supervisor.store.current_state(CYCLE)
    assert len(projection["tick_claims"]) == 1
    assert len(projection["attempts"]) == 1


def test_claim_without_observation_recovers_before_later_tick_can_start(
    tmp_path: Path,
) -> None:
    supervisor, control, _ = _supervisor(
        tmp_path,
        outcomes=["accepted"],
    )
    supervisor.store.now = lambda: T0
    heartbeat = _heartbeat()
    health = validate_complete_tick_heartbeat(
        heartbeat,
        cycle_id=CYCLE,
        observed_at=T0.isoformat(),
    )
    heartbeat_digest = _digest(dict(heartbeat))
    health = {**health, "heartbeat_digest": heartbeat_digest}
    source_tick_key, trust = supervisor._source_tick_identity(
        CYCLE,
        health=health,
    )
    with supervisor.store.try_lease(
        CYCLE,
        holder_id="crashed-after-claim",
    ) as lease:
        assert lease is not None
        lease.claim_tick(
            source_tick_key=source_tick_key,
            heartbeat_digest=heartbeat_digest,
            trust=trust,
            claimed_at=T0.isoformat(),
            heartbeat_recorded_at=T0.isoformat(),
        )

    recovered = supervisor.converge_once(
        CYCLE,
        observed_at=T0.isoformat(),
        heartbeat=heartbeat,
    )
    later = T0 + timedelta(seconds=61)
    executed = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert recovered["status"] == "recovered_no_action"
    assert recovered["control_actions_executed"] == 0
    assert executed["status"] == "executed"
    assert control.calls.count("start") == 1
    recovered_observation = supervisor.store.observations(CYCLE)[0]
    recovered_heartbeat = dict(
        (recovered_observation.get("payload") or {}).get(
            "heartbeat"
        )
        or {}
    )
    assert recovered_heartbeat["recorded_at"] == T0.isoformat()
