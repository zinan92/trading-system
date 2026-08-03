from __future__ import annotations

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
from services.paper_supervisor import PaperSupervisor, _digest
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


class FakePlane:
    def __init__(self) -> None:
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
    ) -> dict:
        assert cycle_id == CYCLE
        assert proposal["proposal_id"] == "proposal-1"
        assert preview["preview_id"] == "recommendation-preview-1"
        return {"envelope_authorization_id": "envelope-1"}

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
        if action == "refresh_recommendation":
            return {
                "recommendation": {
                    "direction": "neutral",
                    "style": "steady",
                    "strategy_type": "grid",
                },
                "proposal": {
                    "proposal_id": "proposal-1",
                    "direction": "neutral",
                    "style": "steady",
                },
                "preview": {
                    "preview_id": "recommendation-preview-1",
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
            self.prepare_ids.append(prepared_id)
            self.prepared[prepared_id] = row
            return deepcopy(row)
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
        ),
        control,
        plane,
    )


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
    plane.runtime["accepted_order_count"] = 2
    later = T0 + timedelta(seconds=61)

    adopted = supervisor.converge_once(
        CYCLE,
        observed_at=later.isoformat(),
        heartbeat=_heartbeat(later),
    )

    assert adopted["terminal_status"] == "adopted_existing"
    assert adopted["status"] == "healthy"


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
