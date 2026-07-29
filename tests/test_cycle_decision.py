from __future__ import annotations

from pathlib import Path

import pytest

from services.cycle_decision import CycleDecisionCoordinator, CycleDecisionLedger


CYCLE = "2026-07-29_DAY"
NOW = "2026-07-29T02:00:00+00:00"


class FakePlane:
    def __init__(self, *, plan=None, runtime=None) -> None:
        self.plan = plan
        self.runtime = runtime or {
            "desired_state": "stopped",
            "actual_state": "stopped",
        }
        self.locked = []

    def runtime_state(self, _cycle_id):
        return dict(self.runtime)

    def active_plan(self, _cycle_id):
        return dict(self.plan) if self.plan else None

    def lock_production_plan(self, cycle_id, *, selected_proposal_id, now):
        self.locked.append((cycle_id, selected_proposal_id, now))
        self.plan = {
            "strategy_plan_id": "plan-ai-1",
            "field_sources": {"direction": "ai"},
        }
        return dict(self.plan)


def _evaluation(direction="long", strategy_type="grid"):
    return {
        "recommendation": {
            "direction": direction,
            "style": "steady",
            "strategy_type": strategy_type,
            "evaluation_receipt": {"evaluation_id": "ai-eval-1"},
        },
        "proposal": {
            "proposal_id": "proposal-ai-1",
            "direction": direction,
            "style": "steady",
        },
        "preview": {"preview_id": "preview-1"},
    }


def test_cycle_decision_is_unique_and_immutable(tmp_path: Path) -> None:
    ledger = CycleDecisionLedger(tmp_path)
    first = ledger.record(
        {
            "cycle_id": CYCLE,
            "recorded_at": NOW,
            "source": "auto_ai",
            "outcome": "executed",
            "evaluation_id": "ai-eval-1",
        }
    )

    assert ledger.record(dict(first)) == first
    with pytest.raises(RuntimeError, match="immutable"):
        ledger.record(
            {
                "cycle_id": CYCLE,
                "recorded_at": NOW,
                "source": "auto_ai",
                "outcome": "not_executed",
                "reason_code": "different",
                "reason": "different",
                "next_action": "wait",
            }
        )


def test_automatic_decision_runs_prepare_then_complete_start(tmp_path: Path) -> None:
    plane = FakePlane()
    calls = []

    def control(action, payload):
        calls.append((action, dict(payload)))
        if action == "prepare_start":
            return {
                "prepared_start_id": "prepared-1",
                "preview": {
                    "preview_id": "preview-1",
                    "manual_confirmation": {"required": False},
                },
            }
        return {
            "created_orders": 30,
            "accepted_orders": 30,
            "plan": {"strategy_plan_id": "plan-ai-1"},
            "runtime": {
                "desired_state": "running",
                "actual_state": "running",
                "accepted_order_count": 30,
            },
        }

    result = CycleDecisionCoordinator(tmp_path).ensure(
        CYCLE,
        now=NOW,
        plane=plane,
        execution_snapshot={"orders": [], "positions": []},
        refresh_recommendation=_evaluation,
        control=control,
    )

    assert [row[0] for row in calls] == ["prepare_start", "start"]
    assert plane.locked == [(CYCLE, "proposal-ai-1", NOW)]
    assert result["decision"]["outcome"] == "executed"
    assert result["decision"]["terminal_status"] == "executed"
    assert result["decision"]["orders_created"] == 30
    assert result["decision"]["orders_accepted"] == 30


def test_risk_confirmation_records_not_executed_without_start(tmp_path: Path) -> None:
    calls = []

    def control(action, payload):
        calls.append(action)
        return {
            "prepared_start_id": "prepared-1",
            "preview": {
                "preview_id": "preview-1",
                "manual_confirmation": {
                    "required": True,
                    "required_acknowledgements": [{"code": "leverage_limit_exceeded"}],
                },
            },
        }

    result = CycleDecisionCoordinator(tmp_path).ensure(
        CYCLE,
        now=NOW,
        plane=FakePlane(),
        execution_snapshot={"orders": [], "positions": []},
        refresh_recommendation=_evaluation,
        control=control,
    )

    assert calls == ["prepare_start"]
    assert result["decision"]["outcome"] == "not_executed"
    assert result["decision"]["terminal_status"] == "blocked"
    assert result["decision"]["reason_code"] == "risk_confirmation_required"


def test_opposite_recommendation_preserves_position_and_creates_no_orders(
    tmp_path: Path,
) -> None:
    calls = []
    snapshot = {
        "orders": [
            {
                "order_id": "entry-long-2",
                "state": "accepted",
                "event": "entry",
            },
            {
                "order_id": "target-long-1",
                "state": "accepted",
                "event": "target",
            },
        ],
        "positions": [
            {
                "position_id": "position-long-1",
                "status": "open",
                "side": "long",
            }
        ],
    }

    result = CycleDecisionCoordinator(tmp_path).ensure(
        CYCLE,
        now=NOW,
        plane=FakePlane(),
        execution_snapshot=snapshot,
        refresh_recommendation=lambda: _evaluation(direction="short"),
        control=lambda action, payload: (
            calls.append((action, payload))
            or {
                "cancelled_entry_order_ids": list(payload["order_ids"]),
                "cancelled_orders": len(payload["order_ids"]),
            }
        ),
    )

    assert calls == [
        ("suspend_entries", {"order_ids": ["entry-long-2"]})
    ]
    assert result["decision"]["outcome"] == "position_conflict"
    assert result["decision"]["reason_code"] == "existing_position_direction_conflict"
    assert result["decision"]["open_position_ids"] == ["position-long-1"]
    assert result["decision"]["suspended_entry_order_ids"] == ["entry-long-2"]
    assert result["decision"]["orders_created"] == 0


def test_gate_failure_records_reason_and_is_not_retried(tmp_path: Path) -> None:
    evaluations = []

    def refresh():
        evaluations.append("called")
        raise ValueError("paper_execution_tick_unavailable:stale")

    coordinator = CycleDecisionCoordinator(tmp_path)
    first = coordinator.ensure(
        CYCLE,
        now=NOW,
        plane=FakePlane(),
        execution_snapshot={"orders": [], "positions": []},
        refresh_recommendation=refresh,
        control=lambda action, payload: {},
    )
    second = coordinator.ensure(
        CYCLE,
        now=NOW,
        plane=FakePlane(),
        execution_snapshot={"orders": [], "positions": []},
        refresh_recommendation=refresh,
        control=lambda action, payload: {},
    )

    assert evaluations == ["called"]
    assert first["decision"]["reason_code"] == "paper_execution_tick_unavailable"
    assert second["status"] == "existing"


def test_manual_running_plan_is_recorded_without_ai(tmp_path: Path) -> None:
    plane = FakePlane(
        plan={
            "strategy_plan_id": "manual-plan",
            "field_sources": {"direction": "human", "grid": "human"},
        },
        runtime={"desired_state": "running", "actual_state": "running"},
    )

    result = CycleDecisionCoordinator(tmp_path).ensure(
        CYCLE,
        now=NOW,
        plane=plane,
        execution_snapshot={},
        refresh_recommendation=lambda: pytest.fail("AI must not run"),
        control=lambda action, payload: pytest.fail("control must not run"),
    )

    assert result["decision"]["source"] == "manual"
    assert result["decision"]["outcome"] == "executed"
    assert result["decision"]["terminal_status"] == "adopted_existing"


def test_active_ai_plan_is_started_without_repeating_ai_evaluation(
    tmp_path: Path,
) -> None:
    plane = FakePlane(
        plan={
            "strategy_plan_id": "active-ai-plan",
            "status": "active",
            "direction": "neutral",
            "style": "steady",
            "strategy_type": "grid",
            "field_sources": {"direction": "ai", "style": "ai"},
            "source_proposal_ids": ["proposal-ai-existing"],
            "evaluation_receipt": {"evaluation_id": "ai-eval-existing"},
        }
    )
    calls = []

    def control(action, payload):
        calls.append((action, dict(payload)))
        if action == "prepare_start":
            return {
                "prepared_start_id": "prepared-existing",
                "preview": {
                    "preview_id": "preview-existing",
                    "manual_confirmation": {"required": False},
                },
            }
        return {
            "created_orders": 38,
            "accepted_orders": 38,
            "plan": {"strategy_plan_id": "active-ai-plan"},
            "runtime": {
                "desired_state": "running",
                "actual_state": "running",
                "accepted_order_count": 38,
            },
        }

    result = CycleDecisionCoordinator(tmp_path).ensure(
        CYCLE,
        now=NOW,
        plane=plane,
        execution_snapshot={"orders": [], "positions": []},
        refresh_recommendation=lambda: pytest.fail("AI must not be repeated"),
        control=control,
    )

    assert [action for action, _ in calls] == ["prepare_start", "start"]
    assert plane.locked == []
    assert result["decision"]["terminal_status"] == "executed"
    assert result["decision"]["evaluation_id"] == "ai-eval-existing"
    assert result["decision"]["orders_created"] == 38
    assert result["decision"]["orders_accepted"] == 38


def test_existing_running_ai_plan_is_adopted_without_duplicate_orders(
    tmp_path: Path,
) -> None:
    plane = FakePlane(
        plan={
            "strategy_plan_id": "running-ai-plan",
            "field_sources": {"direction": "ai"},
        },
        runtime={"desired_state": "running", "actual_state": "running"},
    )

    result = CycleDecisionCoordinator(tmp_path).ensure(
        CYCLE,
        now=NOW,
        plane=plane,
        execution_snapshot={},
        refresh_recommendation=lambda: pytest.fail("AI must not run"),
        control=lambda action, payload: pytest.fail("control must not run"),
    )

    assert result["decision"]["terminal_status"] == "adopted_existing"
    assert result["decision"]["strategy_plan_id"] == "running-ai-plan"


def test_zero_order_start_is_recorded_as_blocked_and_never_retried(
    tmp_path: Path,
) -> None:
    calls = []

    def control(action, payload):
        calls.append(action)
        if action == "prepare_start":
            return {
                "prepared_start_id": "prepared-1",
                "preview": {
                    "preview_id": "preview-1",
                    "manual_confirmation": {"required": False},
                },
            }
        return {
            "created_orders": 0,
            "accepted_orders": 0,
            "runtime": {
                "desired_state": "stopped",
                "actual_state": "stopped",
                "accepted_order_count": 0,
            },
        }

    coordinator = CycleDecisionCoordinator(tmp_path)
    first = coordinator.ensure(
        CYCLE,
        now=NOW,
        plane=FakePlane(),
        execution_snapshot={"orders": [], "positions": []},
        refresh_recommendation=_evaluation,
        control=control,
    )
    second = coordinator.ensure(
        CYCLE,
        now=NOW,
        plane=FakePlane(),
        execution_snapshot={"orders": [], "positions": []},
        refresh_recommendation=_evaluation,
        control=control,
    )

    assert calls == ["prepare_start", "start"]
    assert first["decision"]["terminal_status"] == "blocked"
    assert first["decision"]["reason_code"] == "paper_start_incomplete"
    assert second["status"] == "existing"
