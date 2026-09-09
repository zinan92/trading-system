from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.testnet_scheduler import (
    TESTNET_SCHEDULER_SCHEMA,
    TestnetScheduler,
    TestnetSchedulerGuard,
    TestnetSchedulerOwnershipStore,
)


NOW = "2026-08-26T01:00:00+00:00"
NOW_DT = datetime(2026, 8, 26, 1, tzinfo=UTC)


def _activation() -> dict[str, str]:
    return {
        "strategy_family": "dca",
        "strategy_session_id": "session-scheduler",
        "strategy_revision_id": "revision-scheduler-1",
        "plan_digest": "sha256:" + "a" * 64,
        "account_fingerprint": "sha256:" + "b" * 64,
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP",
        "runtime_id": "runtime-scheduler",
        "release_sha": "c" * 40,
        "capability_revision": "hyperliquid-testnet-runtime-v1",
    }


def _scheduler(tmp_path: Path) -> tuple[TestnetScheduler, TestnetAutomationCoordinator]:
    coordinator = TestnetAutomationCoordinator(
        tmp_path / "outputs",
        clock=lambda: NOW,
    )
    owner_store = TestnetSchedulerOwnershipStore(
        tmp_path / "outputs",
        clock=lambda: NOW_DT,
    )
    owner_store.initialize_local(owner_id="local-mac")
    return (
        TestnetScheduler(
            tmp_path / "outputs",
            coordinator,
            owner_id="local-mac",
            runtime_mode="local",
            clock=lambda: NOW,
        ),
        coordinator,
    )


def test_testnet_ownership_is_local_only_and_epoch_bound(tmp_path: Path) -> None:
    store = TestnetSchedulerOwnershipStore(tmp_path / "outputs", clock=lambda: NOW_DT)
    active = store.initialize_local(owner_id="local-mac")

    assert active["schema_version"] == TESTNET_SCHEDULER_SCHEMA
    assert active["scope"] == "testnet_only"
    assert active["status"] == "active"
    assert active["epoch"] == 1
    assert TestnetSchedulerGuard(tmp_path / "outputs", runtime_mode="local", owner_id="local-mac").verify()["ok"] is True
    assert TestnetSchedulerGuard(
        tmp_path / "outputs",
        runtime_mode="local",
        owner_id="local-mac",
    ).verify()["ok"] is True

    paused = store.pause(expected_owner_id="local-mac", expected_epoch=1)
    restored = store.activate(new_owner_id="local-secondary", expected_epoch=paused["epoch"])
    assert restored["epoch"] == 3
    assert restored["active_owner_id"] == "local-secondary"


def test_scheduler_activation_and_duplicate_tick_are_idempotent(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    activation = _activation()

    activated = scheduler.activate(activation, command_id="activation-1", timestamp=NOW)
    assert activated["schema_version"] == TESTNET_SCHEDULER_SCHEMA
    assert activated["status"] == "active"
    assert activated["execution_enabled"] is False
    assert activated["coordinator"]["activation_id"] == coordinator.status()["activation_id"]

    advanced: list[dict] = []
    first = scheduler.tick(
        tick_id="tick-1",
        event={"kind": "market_heartbeat"},
        advance=lambda event: advanced.append(dict(event)) or {"status": "observed"},
        timestamp=NOW,
    )
    replay = scheduler.tick(
        tick_id="tick-1",
        event={"kind": "market_heartbeat"},
        advance=lambda _event: {"status": "must-not-run"},
        timestamp=NOW,
    )

    assert first["status"] == "active"
    assert first["next_action"] == "await_event_or_heartbeat"
    assert advanced == [{"kind": "market_heartbeat"}]
    assert replay == first


def test_scheduler_attaches_to_running_coordinator_without_reactivation(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    activation = _activation()
    coordinator.activate(activation, command_id="activation-1", now=NOW)
    current = coordinator.status()
    current["status"] = "grid_paused_range"
    current["execution_enabled"] = True
    coordinator._record(current)

    attached = scheduler.attach(current["activation_id"], timestamp=NOW)

    assert attached["event"] == "scheduler_attached"
    assert attached["status"] == "active"
    assert attached["activation_id"] == current["activation_id"]
    assert coordinator.status()["status"] == "grid_paused_range"

    tick = scheduler.tick(
        tick_id="paused-range-heartbeat",
        event={"kind": "market_heartbeat"},
        advance=lambda _event: {"status": "observed"},
        timestamp=NOW,
    )
    assert tick["status"] == "active"
    assert tick["coordinator_status"] == "grid_paused_range"
    assert tick["warning"] is None


def test_scheduler_attach_keeps_active_session_conflict_for_new_activation(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")

    class Coordinator:
        def status(self):
            return {"status": "grid_running", "activation_id": "activation-new"}

    scheduler = TestnetScheduler(output, Coordinator(), owner_id="local-mac", runtime_mode="local")
    scheduler._save_state({"status": "active", "activation_id": "activation-old"})

    result = scheduler.attach("activation-new", timestamp=NOW)

    assert result["status"] == "blocked"
    assert result["blocker"] == "testnet_scheduler_active_session_conflict"


def test_scheduler_only_replaces_blocked_session_after_terminal_or_idle(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")

    class Coordinator:
        def status(self):
            return {"status": "grid_running", "activation_id": "activation-new"}

    scheduler = TestnetScheduler(output, Coordinator(), owner_id="local-mac", runtime_mode="local")
    for replaceable_status in ("grid_terminal", "dca_terminal", "idle"):
        scheduler._save_state({
            "status": "blocked",
            "activation_id": "activation-old",
            "coordinator_status": replaceable_status,
        })
        assert scheduler.can_reattach("activation-new") is True

    scheduler._save_state({
        "status": "blocked",
        "activation_id": "activation-old",
        "coordinator_status": "grid_running",
    })
    assert scheduler.can_reattach("activation-new") is False


def test_scheduler_awaiting_operator_ends_when_coordinator_returns_idle(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")

    class Coordinator:
        def status(self):
            return {"status": "idle", "activation_id": None}

    scheduler = TestnetScheduler(output, Coordinator(), owner_id="local-mac", runtime_mode="local")
    scheduler._save_state({
        "status": "awaiting_operator",
        "activation_id": "activation-terminal",
        "execution_enabled": False,
        "next_action": "notify_park_and_wait",
    })

    result = scheduler.tick(tick_id="terminal-closed", timestamp=NOW)

    assert result["status"] == "idle"
    assert result["event"] == "scheduler_idle"
    assert result["activation_id"] is None
    assert result["next_action"] == "await_activation"


def test_scheduler_can_explicitly_resume_a_blocked_running_session(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    activation = scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current["status"] = "grid_running"
    current["execution_enabled"] = True
    coordinator._record(current)
    blocked = scheduler.tick(
        tick_id="tick-failed",
        event={"kind": "market_heartbeat"},
        advance=lambda _event: (_ for _ in ()).throw(RuntimeError("boom")),
        timestamp=NOW,
    )
    assert blocked["status"] == "active"
    assert blocked["event"] == "scheduler_advance_warning"

    resumed = scheduler.resume(activation["activation_id"], timestamp=NOW)

    assert resumed["event"] == "scheduler_resumed"
    assert resumed["status"] == "active"
    assert resumed["activation_id"] == activation["activation_id"]
    assert coordinator.status()["status"] == "grid_running"


def test_scheduler_failure_receipt_contains_redacted_exception_message(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    result = scheduler.tick(
        tick_id="tick-secret-error",
        event={"kind": "market_heartbeat"},
        advance=lambda _event: (_ for _ in ()).throw(RuntimeError("secret=super-secret token=abc")),
        timestamp=NOW,
    )

    assert result["warning"] == "scheduler_advance_failed:RuntimeError:secret=[REDACTED] token=[REDACTED]"


def test_scheduler_failure_receipt_preserves_redacted_typed_error_evidence(tmp_path: Path) -> None:
    from services.strategy_control_plane import StrategyControlMachineError

    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    def failed(_event):
        raise StrategyControlMachineError(
            "testnet_market_not_authoritative",
            {
                "reason": "market_stale",
                "age_seconds": -2.1,
                "api_key": "do-not-persist",
            },
        )

    result = scheduler.tick(
        tick_id="tick-typed-error",
        event={"kind": "market_heartbeat"},
        advance=failed,
        timestamp=NOW,
    )

    assert result["warning"] == (
        "scheduler_advance_failed:StrategyControlMachineError:"
        "testnet_market_not_authoritative"
    )
    assert result["advance_result"] == {
        "error": {
            "type": "StrategyControlMachineError",
            "code": "testnet_market_not_authoritative",
            "evidence": {
                "reason": "market_stale",
                "age_seconds": -2.1,
                "api_key": "[REDACTED]",
            },
        }
    }


def test_scheduler_blocks_only_after_three_consecutive_advance_failures(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    activation = scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    results = [scheduler.tick(tick_id=f"failed-{n}", event={"kind": "market_heartbeat"},
                             advance=lambda _event: {"status": "blocked", "reason": "temporary"},
                             timestamp=NOW) for n in range(1, 4)]

    assert [row["status"] for row in results] == ["active", "active", "blocked"]
    assert [row["advance_failure_count"] for row in results] == [1, 2, 3]


def test_scheduler_success_tick_clears_previous_warning(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    activation = scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    warning = scheduler.tick(
        tick_id="warning-1",
        event={"kind": "market_heartbeat"},
        advance=lambda _event: {"status": "blocked", "reason": "temporary"},
        timestamp=NOW,
    )
    recovered = scheduler.tick(
        tick_id="success-1",
        event={"kind": "market_heartbeat"},
        advance=lambda _event: {"status": "observed"},
        timestamp=NOW,
    )

    assert warning["warning"] == "temporary"
    assert recovered["warning"] is None


def test_scheduler_waits_for_operator_when_advance_reaches_terminal(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")

    class Coordinator:
        def __init__(self):
            self.state = {"status": "grid_running", "execution_enabled": True}

        def status(self):
            return dict(self.state)

    coordinator = Coordinator()
    scheduler = TestnetScheduler(output, coordinator, owner_id="local-mac", runtime_mode="local")
    scheduler._save_state({
        "status": "active",
        "activation_id": "activation-1",
        "execution_enabled": True,
        "restart_reconcile_required": False,
        "advance_failure_count": 0,
    })
    result = scheduler.tick(
        tick_id="terminal-1",
        event={"kind": "market_heartbeat"},
        advance=lambda _event: coordinator.state.update(status="grid_terminal", execution_enabled=False) or {"status": "grid_terminal"},
        timestamp=NOW,
    )

    assert result["status"] == "awaiting_operator"
    assert result["coordinator_status"] == "grid_terminal"
    assert result["next_action"] == "notify_park_and_wait"


def test_market_assembly_warning_preserves_reason_details_until_third_failure(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    def failed(_event):
        return {
            "status": "failed",
            "reason": "testnet_market_not_authoritative:market_identity_missing",
            "market_failure": {"reason": "market_identity_missing", "fields": ["cursor"]},
        }

    results = [scheduler.tick(tick_id=f"market-{n}", event={"kind": "market_heartbeat"}, advance=failed, timestamp=NOW) for n in range(1, 4)]

    assert [row["event"] for row in results] == ["scheduler_advance_warning", "scheduler_advance_warning", "scheduler_advance_blocked"]
    assert results[0]["warning"] == "testnet_market_not_authoritative:market_identity_missing"
    assert results[0]["advance_result"]["market_failure"] == {"reason": "market_identity_missing", "fields": ["cursor"]}


def test_scheduler_sampling_race_warning_does_not_consume_strike(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    def failed(_event):
        return {
            "status": "failed",
            "reason": "testnet_market_not_authoritative:market_price_mismatch",
            "market_failure": {
                "reason": "market_price_mismatch",
                "attempts": [{"attempt": 1, "binding_price": "1", "reader_price": "2"}],
            },
        }

    results = [scheduler.tick(tick_id=f"race-{n}", event={"kind": "market_heartbeat"},
                              advance=failed, timestamp=NOW) for n in range(1, 4)]

    assert [row["status"] for row in results] == ["active", "active", "active"]
    assert [row["advance_failure_count"] for row in results] == [0, 0, 0]
    assert all(row["event"] == "scheduler_advance_warning" for row in results)
    assert results[-1]["advance_result"]["market_failure"]["attempts"]


def test_facts_unavailable_warns_without_strike_then_blocks_after_ten_minutes(
    tmp_path: Path,
) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    def failed(_event):
        return {
            "status": "failed",
            "reason": "testnet_facts_unavailable:RuntimeError:provider unavailable",
        }

    first = scheduler.tick(
        tick_id="facts-1",
        event={"kind": "market_heartbeat"},
        advance=failed,
        timestamp="2026-08-26T01:00:00+00:00",
    )
    before_threshold = scheduler.tick(
        tick_id="facts-2",
        event={"kind": "market_heartbeat"},
        advance=failed,
        timestamp="2026-08-26T01:09:59+00:00",
    )
    blocked = scheduler.tick(
        tick_id="facts-3",
        event={"kind": "market_heartbeat"},
        advance=failed,
        timestamp="2026-08-26T01:10:00+00:00",
    )

    assert [first["status"], before_threshold["status"], blocked["status"]] == [
        "active",
        "active",
        "blocked",
    ]
    assert first["advance_failure_count"] == 0
    assert before_threshold["advance_failure_count"] == 0
    assert first["facts_unavailable_since"] == "2026-08-26T01:00:00+00:00"
    assert blocked["blocker"] == failed({})["reason"]
    assert blocked["next_action"] == "notify_park_and_wait"


def test_restart_reconcile_facts_unavailable_retries_without_strike(
    tmp_path: Path,
) -> None:
    scheduler, _coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    scheduler.mark_restart(timestamp=NOW)

    result = scheduler.tick(
        tick_id="restart-facts-1",
        reconcile=lambda: {
            "status": "failed",
            "reason": "testnet_facts_unavailable:TimeoutError:timed out",
        },
        timestamp=NOW,
    )

    assert result["status"] == "reconcile_required"
    assert result["warning"] == "testnet_facts_unavailable:TimeoutError:timed out"
    assert result["advance_failure_count"] == 0
    assert result["restart_reconcile_required"] is True
    assert result["next_action"] == "reconcile_before_resume"


def test_restart_reconciles_before_event_progression(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    activation = scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    scheduler.mark_restart(timestamp="2026-08-26T01:01:00+00:00")
    calls: list[str] = []

    result = scheduler.tick(
        tick_id="tick-restart",
        event={"kind": "fill"},
        reconcile=lambda: calls.append("reconcile") or {
            "status": "pass",
            "activation_id": activation["coordinator"]["activation_id"],
            "environment": "testnet",
            "account_fingerprint": _activation()["account_fingerprint"],
            "release_sha": _activation()["release_sha"],
        },
        advance=lambda _event: {"status": "advanced"},
        timestamp="2026-08-26T01:02:00+00:00",
    )

    assert calls == ["reconcile"]
    assert result["restart_reconciled"] is True
    assert result["status"] == "active"
    assert result["coordinator_status"] == coordinator.status()["status"]


def test_restart_reconciliation_failure_holds_new_entries_and_never_advances(tmp_path: Path) -> None:
    scheduler, _coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    scheduler.mark_restart(timestamp="2026-08-26T01:01:00+00:00")
    advanced: list[dict] = []

    result = scheduler.tick(
        tick_id="tick-bad-restart",
        event={"kind": "fill"},
        reconcile=lambda: {"status": "unknown", "reason": "account_cursor_unknown"},
        advance=lambda event: advanced.append(dict(event)) or {"status": "advanced"},
        timestamp="2026-08-26T01:02:00+00:00",
    )

    assert result["status"] == "blocked"
    assert result["blocker"] == "restart_reconciliation_failed"
    assert result["next_action"] == "notify_park_and_wait"
    assert advanced == []


def test_scheduler_rejects_wrong_owner_without_touching_coordinator(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    wrong = TestnetScheduler(
        tmp_path / "outputs",
        coordinator,
        owner_id="local-secondary",
        runtime_mode="local",
        clock=lambda: NOW,
    )

    result = wrong.tick(tick_id="wrong-owner", timestamp=NOW)

    assert result["status"] == "blocked"
    assert result["blocker"] == "testnet_scheduler_owner_mismatch"
    assert coordinator.status()["status"] == "activated"


def test_dead_man_marks_missing_heartbeat_without_authorizing_actions(tmp_path: Path) -> None:
    scheduler, _coordinator = _scheduler(tmp_path)
    result = scheduler.dead_man(timestamp="2026-08-26T01:06:00+00:00")

    assert result["dead_man"]["status"] == "execution_tick_scheduler_down"
    assert result["next_action"] == "notify_park_and_wait"
    assert result["alerts_authorize_actions"] is False
