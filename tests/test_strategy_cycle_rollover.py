from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pipelines.dualtrack_cycle_runner as runner_module
from pipelines.dualtrack_cycle_runner import DualTrackCycleRunner
from schemas.market_data import Bar
from services.dualtrack_clock import parse_utc
from services.journal_store import load_json, write_json
from services.strategy_market_context import build_strategy_timeframes


def _runner(output: Path):
    runner = object.__new__(DualTrackCycleRunner)
    runner.output_root = output
    runner.config = {}
    runner.execution = None
    return runner


def _hold_process_mutation_lock(output: str, acquired, release) -> None:
    from services.strategy_control_plane import production_mutation_lock

    with production_mutation_lock(Path(output)):
        acquired.set()
        release.wait(5)


def _probe_process_mutation_lock(output: str, attempting, entered) -> None:
    from services.strategy_control_plane import production_mutation_lock

    attempting.set()
    with production_mutation_lock(Path(output)):
        entered.set()


def test_rollover_stops_packages_then_requires_explicit_next_cycle_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    calls: list[str] = []

    class Control:
        def __init__(self, _output):
            self.state = {
                "cycle_id": "2026-07-04_NIGHT",
                "desired_state": "running",
                "actual_state": "running",
                "updated_at": "2026-07-05T00:59:00+00:00",
            }

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return dict(self.state)

        def control(self, cycle_id, action, _payload, **_kwargs):
            calls.append(f"{action}:{cycle_id}")
            if action == "stop":
                self.state = {
                    **self.state,
                    "desired_state": "stopped",
                    "actual_state": "stopped",
                    "updated_at": "2026-07-05T01:00:00+00:00",
                }
                return {
                    "cancelled_orders": 12,
                    "flattened_positions": 2,
                    "reconciliation": {"status": "ok", "issues": []},
                    "runtime": dict(self.state),
                }
            raise AssertionError("rollover must not start the next cycle")

    class Packager:
        def __init__(self, *_args, **_kwargs):
            pass

        def package(self, cycle_id, *, now):
            calls.append(f"package:{cycle_id}")
            return {
                "status": "closed",
                "package_hash": "hash-1",
                "execution": {"account": {"equity": 10_005}},
            }

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    monkeypatch.setattr(runner_module, "StrategyCyclePackager", Packager)
    runner = _runner(output)
    runner.execution = object()
    runner._production_market_snapshot = lambda _now: {"status": "ready"}

    result = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:00:00+00:00"),
    )

    assert result["status"] == "awaiting_operator_start"
    assert calls == [
        "stop:2026-07-04_NIGHT",
        "package:2026-07-04_NIGHT",
    ]
    path = output / "dualtrack" / "strategy_control" / "rollovers" / "2026-07-04_NIGHT__2026-07-05_DAY.json"
    assert [row["status"] for row in load_json(path)] == [
        "intent_recorded",
        "previous_cycle_stopped",
        "previous_cycle_packaged",
        "awaiting_operator_start",
    ]


def test_rollover_gate_failure_writes_blocked_receipt_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"

    class Control:
        def __init__(self, _output):
            self.state = {
                "cycle_id": "2026-07-04_NIGHT",
                "desired_state": "running",
                "actual_state": "running",
                "updated_at": "2026-07-05T00:59:00+00:00",
            }

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return dict(self.state)

        def control(self, _cycle_id, action, _payload, *, market, **_kwargs):
            assert action == "stop"
            assert market["status"] == "blocked"
            assert market["fresh"] is False
            raise ValueError("paper safe action has no trusted execution-ledger price")

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    runner = _runner(output)
    runner._production_market_snapshot = lambda _now: (_ for _ in ()).throw(ValueError("market data is stale"))

    result = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:00:00+00:00"),
    )

    assert result["status"] == "blocked"
    assert result["stage"] == "stopping_previous_cycle"
    assert result["fail_closed"] is True
    assert result["reason"] == "paper safe action has no trusted execution-ledger price"


def test_missing_planning_timeframes_do_not_prevent_terminal_old_cycle_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    calls: list[str] = []

    class Control:
        def __init__(self, _output):
            self.state = {
                "cycle_id": "2026-07-04_NIGHT",
                "desired_state": "running",
                "actual_state": "running",
                "updated_at": "2026-07-05T00:59:00+00:00",
            }

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return dict(self.state)

        def control(self, cycle_id, action, _payload, **_kwargs):
            calls.append(f"{action}:{cycle_id}")
            assert action == "stop"
            self.state = {
                **self.state,
                "desired_state": "stopped",
                "actual_state": "stopped",
                "updated_at": "2026-07-05T01:00:00+00:00",
            }
            return {
                "cancelled_orders": 20,
                "flattened_positions": 1,
                "reconciliation": {"status": "ok", "issues": []},
                "runtime": dict(self.state),
            }

    class Packager:
        def __init__(self, *_args, **_kwargs):
            pass

        def package(self, cycle_id, *, now):
            del now
            calls.append(f"package:{cycle_id}")
            return {"status": "closed", "package_hash": "terminal", "execution": {"account": {}}}

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    monkeypatch.setattr(runner_module, "StrategyCyclePackager", Packager)
    runner = _runner(output)
    runner.execution = object()
    runner._production_market_snapshot = lambda _now: {"status": "blocked", "is_synthetic": False}

    result = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:00:00+00:00"),
    )

    assert calls == ["stop:2026-07-04_NIGHT", "package:2026-07-04_NIGHT"]
    assert result["status"] == "awaiting_operator_start"
    assert result["operator_action"] == "review and start the current cycle from the dashboard"
    rows = load_json(runner._rollover_path("2026-07-04_NIGHT", "2026-07-05_DAY"))
    assert [row["status"] for row in rows] == [
        "intent_recorded",
        "previous_cycle_stopped",
        "previous_cycle_packaged",
        "awaiting_operator_start",
    ]


@pytest.mark.parametrize("desired,actual", [("stopped", "stopped"), ("running", "error")])
def test_rollover_never_restarts_current_cycle_after_operator_stop_or_failed_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desired: str,
    actual: str,
) -> None:
    output = tmp_path / "outputs"

    class Control:
        def __init__(self, _output):
            pass

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return {"cycle_id": "2026-07-05_DAY", "desired_state": desired, "actual_state": actual}

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    runner = _runner(output)
    runner._production_market_snapshot = lambda _now: (_ for _ in ()).throw(AssertionError("must not retry"))
    write_json(runner._rollover_path("2026-07-04_NIGHT", "2026-07-05_DAY"), [{"status": "blocked", "should_continue": True}])

    result = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:10:00+00:00"),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "current_cycle_runtime_not_running"


def test_operator_stop_after_rollover_intent_cancels_automatic_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"

    class Control:
        def __init__(self, _output):
            pass

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return {
                "cycle_id": "2026-07-04_NIGHT",
                "desired_state": "stopped",
                "actual_state": "stopped",
                "updated_at": "2026-07-05T01:00:30+00:00",
            }

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    runner = _runner(output)
    write_json(runner._rollover_path("2026-07-04_NIGHT", "2026-07-05_DAY"), [{
        "status": "intent_recorded",
        "should_continue": True,
        "runtime_updated_at": "2026-07-05T00:59:00+00:00",
    }])

    result = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:01:00+00:00"),
    )

    assert result["status"] == "cancelled"
    assert result["stage"] == "operator_intent_check"
    assert result["should_continue"] is False
    assert runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:02:00+00:00"),
    ) == result


@pytest.mark.parametrize("actual", ["stopping", "error"])
def test_rollover_resumes_its_own_incomplete_previous_cycle_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actual: str,
) -> None:
    output = tmp_path / "outputs"
    owner = "rollover:2026-07-04_NIGHT->2026-07-05_DAY"

    class Control:
        def __init__(self, _output):
            self.state = {
                "cycle_id": "2026-07-04_NIGHT",
                "desired_state": "stopped",
                "actual_state": actual,
                "updated_at": "2026-07-05T01:00:30+00:00",
                "transition_owner": owner,
            }

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return dict(self.state)

        def control(self, cycle_id, action, payload, **_kwargs):
            assert cycle_id == "2026-07-04_NIGHT"
            assert action == "stop"
            assert payload == {
                "expected_runtime_updated_at": "2026-07-05T01:00:30+00:00",
                "transition_owner": owner,
            }
            self.state = {
                **self.state,
                "actual_state": "stopped",
                "updated_at": "2026-07-05T01:01:00+00:00",
            }
            return {
                "cancelled_orders": 3,
                "flattened_positions": 1,
                "reconciliation": {"status": "ok", "issues": []},
                "runtime": dict(self.state),
            }

    class Packager:
        def __init__(self, *_args, **_kwargs):
            pass

        def package(self, *_args, **_kwargs):
            raise ValueError("package unavailable")

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    monkeypatch.setattr(runner_module, "StrategyCyclePackager", Packager)
    runner = _runner(output)
    runner.execution = object()
    runner._production_market_snapshot = lambda _now: {"status": "blocked", "fresh": False}
    write_json(runner._rollover_path("2026-07-04_NIGHT", "2026-07-05_DAY"), [{
        "status": "intent_recorded",
        "should_continue": True,
        "runtime_updated_at": "2026-07-05T00:59:00+00:00",
    }])

    result = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:01:00+00:00"),
    )

    assert result["status"] == "blocked"
    assert result["stage"] == "packaging_previous_cycle"
    rows = load_json(runner._rollover_path("2026-07-04_NIGHT", "2026-07-05_DAY"))
    assert any(row.get("status") == "previous_cycle_stopped" for row in rows)


def test_rollover_retry_after_packaging_failure_preserves_closeout_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package crash must retry packaging, never repeat the prior stop."""

    output = tmp_path / "outputs"
    owner = "rollover:2026-07-04_NIGHT->2026-07-05_DAY"
    calls: list[str] = []

    class Control:
        state = {
            "cycle_id": "2026-07-04_NIGHT",
            "desired_state": "running",
            "actual_state": "running",
            "updated_at": "2026-07-05T00:59:00+00:00",
        }

        def __init__(self, _output):
            pass

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return dict(self.state)

        def control(self, cycle_id, action, payload, **_kwargs):
            assert cycle_id == "2026-07-04_NIGHT"
            assert action == "stop"
            assert payload["transition_owner"] == owner
            calls.append("stop")
            self.__class__.state = {
                "cycle_id": cycle_id,
                "desired_state": "stopped",
                "actual_state": "stopped",
                "updated_at": "2026-07-05T01:00:00+00:00",
                "transition_owner": owner,
            }
            return {
                "cancelled_orders": 2,
                "cancelled_order_ids": ["order-entry-1", "order-entry-2"],
                "flattened_positions": 1,
                "flattened_position_ids": ["position-1"],
                "reconciliation": {"status": "ok", "issues": []},
                "runtime": dict(self.state),
            }

    class Packager:
        attempts = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def package(self, cycle_id, *, now):
            assert cycle_id == "2026-07-04_NIGHT"
            self.__class__.attempts += 1
            calls.append("package")
            if self.__class__.attempts == 1:
                raise RuntimeError("fault injected after closeout before terminal package")
            return {
                "status": "closed",
                "package_hash": "terminal-package-hash",
                "execution": {
                    "orders": [
                        {"order_id": "order-entry-1", "state": "cancelled"},
                        {"order_id": "order-entry-2", "state": "cancelled"},
                    ],
                    "positions": [{"position_id": "position-1", "status": "closed"}],
                    "reconciliation": {"status": "ok", "issues": []},
                },
            }

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    monkeypatch.setattr(runner_module, "StrategyCyclePackager", Packager)
    runner = _runner(output)
    runner.execution = object()
    runner._production_market_snapshot = lambda _now: {"status": "ready"}

    first = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:01:00+00:00"),
    )
    assert first["status"] == "blocked"
    assert first["stage"] == "packaging_previous_cycle"

    second = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:02:00+00:00"),
    )

    assert second["status"] == "awaiting_operator_start"
    assert calls == ["stop", "package", "package"]
    rows = load_json(runner._rollover_path("2026-07-04_NIGHT", "2026-07-05_DAY"))
    stopped = next(row for row in rows if row.get("status") == "previous_cycle_stopped")
    assert stopped["cancelled_order_ids"] == ["order-entry-1", "order-entry-2"]
    assert stopped["flattened_position_ids"] == ["position-1"]
    assert sum(row.get("status") == "awaiting_operator_start" for row in rows) == 1


def test_rollover_recovers_owned_failed_current_cycle_without_restarting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    owner = "rollover:2026-07-04_NIGHT->2026-07-05_DAY"

    class Control:
        def __init__(self, _output):
            pass

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return {
                "cycle_id": "2026-07-05_DAY",
                "desired_state": "stopped",
                "actual_state": "error",
                "updated_at": "2026-07-05T01:00:30+00:00",
                "transition_owner": owner,
            }

        def control(self, cycle_id, action, payload, **_kwargs):
            assert cycle_id == "2026-07-05_DAY"
            assert action == "stop"
            assert payload["transition_owner"] == owner
            return {
                "cancelled_orders": 4,
                "flattened_positions": 1,
                "reconciliation": {"status": "ok", "issues": []},
            }

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    runner = _runner(output)
    runner._production_market_snapshot = lambda _now: {"status": "blocked", "fresh": False}

    result = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:01:00+00:00"),
    )

    assert result["status"] == "blocked"
    assert result["stage"] == "current_cycle_recovered_stopped"
    assert result["should_continue"] is False


def test_rollover_repairs_completed_receipt_after_crash_following_successful_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"

    class Control:
        def __init__(self, _output):
            pass

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return {
                "cycle_id": "2026-07-05_DAY",
                "desired_state": "running",
                "actual_state": "running",
                "strategy_plan_id": "plan-running",
                "strategy_plan_version": 4,
                "accepted_order_count": 24,
            }

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    runner = _runner(output)
    write_json(runner._rollover_path("2026-07-04_NIGHT", "2026-07-05_DAY"), [{
        "status": "previous_cycle_packaged",
        "should_continue": True,
        "package_hash": "terminal-hash",
    }])

    result = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:01:00+00:00"),
    )

    assert result["status"] == "completed"
    assert result["recovered_after_restart"] is True
    assert result["package_hash"] == "terminal-hash"
    assert result["strategy_plan_id"] == "plan-running"
    assert result["accepted_orders"] == 24


def test_rollover_fails_closed_on_incomplete_current_cycle_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"

    class Control:
        def __init__(self, _output):
            pass

        def runtime_configured(self):
            return True

        def persisted_runtime_state(self):
            return {
                "cycle_id": "2026-07-05_DAY",
                "desired_state": "running",
                "actual_state": "starting",
                "updated_at": "2026-07-05T01:00:30+00:00",
            }

        def control(self, cycle_id, action, payload, **_kwargs):
            assert cycle_id == "2026-07-05_DAY"
            assert action == "stop"
            assert payload["expected_runtime_updated_at"] == "2026-07-05T01:00:30+00:00"
            return {
                "cancelled_orders": 7,
                "flattened_positions": 1,
                "reconciliation": {"status": "ok", "issues": []},
            }

    monkeypatch.setattr(runner_module, "StrategyControlPlane", Control)
    runner = _runner(output)
    runner._production_market_snapshot = lambda _now: {"status": "blocked", "is_synthetic": False}

    result = runner._rollover_production(
        "2026-07-04_NIGHT",
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:01:00+00:00"),
    )

    assert result["status"] == "blocked"
    assert result["stage"] == "current_cycle_recovered_stopped"
    assert result["fail_closed"] is True
    assert result["cancelled_orders"] == 7
    assert result["flattened_positions"] == 1


def test_persisted_runtime_state_exposes_previous_cycle_without_masking(tmp_path: Path) -> None:
    from services.strategy_control_plane import StrategyControlPlane

    output = tmp_path / "outputs"
    write_json(output / "dualtrack" / "strategy_control" / "runtime.json", [{
        "cycle_id": "2026-07-04_NIGHT",
        "desired_state": "running",
        "actual_state": "running",
        "accepted_order_count": "25",
    }])

    state = StrategyControlPlane(output).persisted_runtime_state()

    assert state["cycle_id"] == "2026-07-04_NIGHT"
    assert state["desired_state"] == "running"
    assert state["accepted_order_count"] == 25


def test_current_cycle_runtime_exposes_unresolved_prior_paper_orders(tmp_path: Path) -> None:
    from services.strategy_control_plane import StrategyControlPlane

    output = tmp_path / "outputs"
    write_json(output / "dualtrack" / "strategy_control" / "runtime.json", [{
        "cycle_id": "2026-07-04_NIGHT",
        "desired_state": "running",
        "actual_state": "running",
        "accepted_order_count": 25,
        "strategy_plan_id": "prior-plan",
        "strategy_plan_version": 3,
    }])

    state = StrategyControlPlane(output).runtime_state("2026-07-05_DAY")

    assert state["stale_cycle"] is True
    assert state["previous_runtime_unresolved"] is True
    assert state["previous_accepted_order_count"] == 25
    assert state["previous_strategy_plan_id"] == "prior-plan"


def test_new_cycle_start_is_rejected_while_prior_paper_runtime_is_unresolved(tmp_path: Path) -> None:
    from services.strategy_control_plane import StrategyControlPlane

    output = tmp_path / "outputs"
    write_json(output / "dualtrack" / "strategy_control" / "runtime.json", [{
        "cycle_id": "2026-07-04_NIGHT",
        "desired_state": "running",
        "actual_state": "running",
        "accepted_order_count": 15,
    }])

    with pytest.raises(ValueError, match="previous_cycle_paper_state_unresolved:2026-07-04_NIGHT:running:15"):
        StrategyControlPlane(output)._assert_no_unresolved_prior_cycle_runtime("2026-07-05_DAY")


def test_stop_runtime_compare_and_swap_rejects_changed_operator_state(tmp_path: Path) -> None:
    from services.strategy_control_plane import StrategyControlPlane

    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    write_json(output / "dualtrack" / "strategy_control" / "runtime.json", [{
        "cycle_id": cycle_id,
        "desired_state": "stopped",
        "actual_state": "stopped",
        "updated_at": "2026-07-05T01:00:30+00:00",
    }])

    with pytest.raises(ValueError, match="runtime changed after rollover intent"):
        StrategyControlPlane(output).control(
            cycle_id,
            "stop",
            {"expected_runtime_updated_at": "2026-07-05T00:59:00+00:00"},
            now="2026-07-05T01:01:00+00:00",
        )


def test_stop_persists_rollover_owner_for_crash_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.strategy_control_plane import StrategyControlPlane
    import services.strategy_control_plane as control_module

    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    owner = "rollover:2026-07-04_NIGHT->2026-07-05_DAY"
    monkeypatch.setattr(control_module, "dualtrack_config", lambda: {
        "execution_engine": {
            "authoritative": "legacy_paper",
            "shadow": "none",
            "real_money_eligible": False,
        },
    })
    write_json(output / "dualtrack" / "strategy_control" / "runtime.json", [{
        "cycle_id": cycle_id,
        "desired_state": "running",
        "actual_state": "running",
        "updated_at": "2026-07-05T01:00:30+00:00",
    }])

    result = StrategyControlPlane(output).control(
        cycle_id,
        "stop",
        {
            "expected_runtime_updated_at": "2026-07-05T01:00:30+00:00",
            "transition_owner": owner,
        },
        market={"status": "blocked", "fresh": False, "is_synthetic": False},
        now="2026-07-05T01:01:00+00:00",
    )

    assert result["runtime"]["actual_state"] == "stopped"
    assert result["runtime"]["transition_owner"] == owner
    assert StrategyControlPlane(output).persisted_runtime_state()["transition_owner"] == owner


def test_production_mutation_lock_serializes_independent_processes(tmp_path: Path) -> None:
    import multiprocessing

    context = multiprocessing.get_context("fork")
    output = tmp_path / "outputs"
    acquired = context.Event()
    release = context.Event()
    attempting = context.Event()
    entered = context.Event()
    holder = context.Process(
        target=_hold_process_mutation_lock,
        args=(str(output), acquired, release),
    )
    contender = context.Process(
        target=_probe_process_mutation_lock,
        args=(str(output), attempting, entered),
    )
    holder.start()
    assert acquired.wait(5)
    contender.start()
    assert attempting.wait(5)
    assert entered.wait(0.2) is False
    release.set()
    assert entered.wait(5)
    holder.join(5)
    contender.join(5)
    assert holder.exitcode == 0
    assert contender.exitcode == 0


def test_protective_execution_sweep_holds_production_mutation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    output = tmp_path / "outputs"
    held = False

    @contextmanager
    def fake_lock(lock_output: Path):
        nonlocal held
        assert lock_output == output
        held = True
        try:
            yield
        finally:
            held = False

    class Execution:
        def snapshot(self, _cycle_id):
            assert held is True
            return {
                "positions": [],
                "orders": [{
                    "state": "accepted",
                    "event": "entry",
                    "order_type": "limit",
                    "ts": "2026-07-05T01:00:00+00:00",
                }],
            }

        def process_market_event(self, _event):
            assert held is True
            return {"status": "ok", "triggered": [], "accepted_limit_fills": []}

        def flush_shadow(self, _cycle_id):
            assert held is True
            return {"status": "ok"}

    class Market:
        def load_bars_between(self, *_args):
            assert held is True
            return [Bar(
                symbol="GOLD",
                timeframe="1m",
                timestamp="2026-07-05T01:00:00+00:00",
                open=4000.0,
                high=4001.0,
                low=3999.0,
                close=4000.0,
                volume=1.0,
                provider="trusted",
                quality_flags=["execution_venue"],
            )]

    monkeypatch.setattr(runner_module, "production_mutation_lock", fake_lock)
    runner = _runner(output)
    runner.execution = Execution()
    runner.market = Market()
    runner.symbol = "GOLD"
    runner.timeframe = "1m"
    runner.config = {"market_data": {"provider": "trusted"}}
    runner._latest_market_record = lambda: {
        "provider": "trusted",
        "timestamp": "2026-07-05T01:00:30+00:00",
        "close": 4000.0,
        "quality_flags": ["execution_venue"],
    }
    runner._market_max_age_seconds = lambda: 120
    runner._filter_market_session_bars = lambda bars: bars
    runner._execution_instrument_id = lambda: "GOLD-PERP"

    result = runner._sweep_human_protective_exits(
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:01:00+00:00"),
    )

    assert result["status"] == "ok"
    assert result["processed_events"] == 1
    assert held is False


def test_protective_execution_sweep_does_not_process_a_forming_bar(
    tmp_path: Path,
) -> None:
    """The current minute is retained until it becomes a completed bar."""

    output = tmp_path / "outputs"

    class Execution:
        def snapshot(self, _cycle_id):
            return {
                "positions": [],
                "orders": [{
                    "state": "accepted",
                    "event": "entry",
                    "order_type": "limit",
                    "ts": "2026-07-05T01:00:00+00:00",
                }],
            }

        def process_market_event(self, _event):
            raise AssertionError("a forming bar must not reach execution")

    class Market:
        def load_bars_between(self, *_args):
            return [Bar(
                symbol="GOLD",
                timeframe="1m",
                timestamp="2026-07-05T01:01:00+00:00",
                open=4000.0,
                high=4001.0,
                low=3999.0,
                close=4000.0,
                volume=1.0,
                provider="trusted",
                quality_flags=["execution_venue"],
            )]

    runner = _runner(output)
    runner.execution = Execution()
    runner.market = Market()
    runner.symbol = "GOLD"
    runner.timeframe = "1m"
    runner.config = {"market_data": {"provider": "trusted"}}
    runner._latest_market_record = lambda: {
        "provider": "trusted",
        "timestamp": "2026-07-05T01:01:00+00:00",
        "close": 4000.0,
        "quality_flags": ["execution_venue"],
    }
    runner._market_max_age_seconds = lambda: 120
    runner._filter_market_session_bars = lambda bars: bars
    runner._execution_instrument_id = lambda: "GOLD-PERP"

    result = runner._sweep_human_protective_exits(
        "2026-07-05_DAY",
        now=parse_utc("2026-07-05T01:01:00+00:00"),
    )

    assert result["status"] == "ok"
    assert result["reason"] == "waiting_for_completed_market_bar"
    assert result["processed_events"] == 0


def test_rollover_preserves_explicit_plan_geometry_and_notional() -> None:
    payload = DualTrackCycleRunner._rollover_start_payload({
        "direction": "long",
        "style": "steady",
        "range": {"low": 3900, "high": 4100},
        "grid": {
            "mode": "arithmetic",
            "count": 50,
            "notional_per_grid": 2800,
            "out_of_range": "exit_only",
        },
        "risk_budget": {"leverage": 3},
    })

    assert payload["range"] == {"low": 3900, "high": 4100}
    assert payload["grid"] == {
        "mode": "arithmetic",
        "count": 50,
        "notional_per_grid": 2800,
        "notional_mode": "manual",
    }
    assert payload["risk_budget"]["leverage"] == 3


def test_strategy_market_context_excludes_forming_bars() -> None:
    checked_at = datetime(2026, 7, 21, 8, 30, tzinfo=timezone.utc)

    class Feed:
        def __init__(self, **_kwargs):
            pass

        def snapshot(self, *, symbol, timeframe, limit, as_of):
            del limit
            duration = timedelta(days=1) if timeframe == "1d" else timedelta(hours=4)
            start = datetime(2026, 6, 1, tzinfo=timezone.utc)
            bars = []
            cursor = start
            while len(bars) < 30:
                if timeframe != "1d" or cursor.weekday() < 5:
                    bars.append({
                        "timestamp": cursor.isoformat(),
                        "open": 100,
                        "high": 102,
                        "low": 99,
                        "close": 101,
                    })
                cursor += duration
            bars.append({
                "timestamp": (parse_utc(as_of) - duration / 2).isoformat(),
                "open": 101,
                "high": 103,
                "low": 100,
                "close": 102,
            })
            return {
                "status": "ready",
                "fresh": True,
                "is_synthetic": False,
                "provider": "binance_usdm_futures",
                "symbol": symbol,
                "bars": bars,
            }

    contexts = build_strategy_timeframes(
        as_of=checked_at.isoformat(),
        timeframes=("1d", "4h"),
        feed_cls=Feed,
    )

    assert all(context["completed_only"] is True for context in contexts.values())
    assert parse_utc(contexts["1d"]["latest_timestamp"]) + timedelta(days=1) <= checked_at
    assert parse_utc(contexts["4h"]["latest_timestamp"]) + timedelta(hours=4) <= checked_at
