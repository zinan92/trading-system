"""Replay scenarios for the 2026-09-11 scheduler latch and the 2026-09-14 orphaned-ladder stop."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.test_dashboard_control_plane import DashboardControlPlane, _confirmable_preview, _eligible_catalog_loader
from tests.test_testnet_scheduler import NOW, _activation, _scheduler


def _market_gate_failure(_event):
    from services.strategy_control_plane import StrategyControlMachineError

    raise StrategyControlMachineError(
        "testnet_market_not_authoritative", {"reason": "market_quality_gate_failed"}
    )


def test_market_gate_failures_use_facts_window_not_three_strikes(tmp_path: Path) -> None:
    # 2026-09-11 14:00Z: three one-minute market-gate failures latched the grid for 2.5 days.
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    results = [
        scheduler.tick(tick_id=f"gate-{n}", event={"kind": "market_heartbeat"},
                       advance=_market_gate_failure, timestamp=f"2026-08-26T01:0{n}:00+00:00")
        for n in range(0, 4)
    ]

    assert [row["status"] for row in results] == ["active"] * 4
    assert [row["advance_failure_count"] for row in results] == [0] * 4


def test_market_gate_block_resumes_when_gate_clears(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    scheduler.tick(tick_id="gate-0", event={"kind": "market_heartbeat"},
                   advance=_market_gate_failure, timestamp="2026-08-26T01:00:00+00:00")
    blocked = scheduler.tick(tick_id="gate-1", event={"kind": "market_heartbeat"},
                             advance=_market_gate_failure, timestamp="2026-08-26T01:10:00+00:00")
    still = scheduler.tick(tick_id="gate-2", event={"kind": "market_heartbeat"},
                           advance=_market_gate_failure, timestamp="2026-08-26T01:11:00+00:00")
    resumed = scheduler.tick(tick_id="gate-3", event={"kind": "market_heartbeat"},
                             advance=lambda _event: {"status": "ok"}, timestamp="2026-08-26T01:12:00+00:00")

    assert blocked["status"] == "blocked"
    assert blocked["next_action"] == "notify_park_and_wait"
    assert still["status"] == "blocked"
    assert still["facts_unavailable_since"] == "2026-08-26T01:00:00+00:00"
    assert resumed["status"] == "active"
    assert resumed["blocker"] is None
    assert resumed["facts_unavailable_since"] is None


def test_read_side_block_does_not_resume_without_a_probe(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)
    scheduler.tick(tick_id="gate-0", event={"kind": "market_heartbeat"},
                   advance=_market_gate_failure, timestamp="2026-08-26T01:00:00+00:00")
    scheduler.tick(tick_id="gate-1", event={"kind": "market_heartbeat"},
                   advance=_market_gate_failure, timestamp="2026-08-26T01:10:00+00:00")

    unprobed = scheduler.tick(tick_id="gate-2", event=None, advance=None,
                              timestamp="2026-08-26T01:11:00+00:00")

    assert unprobed["status"] == "blocked"


def test_execution_failure_block_still_latches(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)
    for n in range(3):
        scheduler.tick(tick_id=f"failed-{n}", event={"kind": "market_heartbeat"},
                       advance=lambda _event: {"status": "blocked", "reason": "temporary"}, timestamp=NOW)

    after = scheduler.tick(tick_id="after", event={"kind": "market_heartbeat"},
                           advance=lambda _event: {"status": "ok"}, timestamp=NOW)

    assert after["status"] == "blocked"


def test_reconcile_stop_refuses_while_grid_lifecycle_orders_are_live(tmp_path: Path) -> None:
    # 2026-09-14: reconcile_stop reported idle while five f107 ladder orders were still resting.
    from services.testnet_automation_coordinator import TestnetAutomationCoordinator, TestnetCoordinatorError

    plane = DashboardControlPlane(tmp_path, catalog_loader=_eligible_catalog_loader)
    coordinator = TestnetAutomationCoordinator(tmp_path, clock=lambda: "2026-09-08T03:00:00+00:00")
    preview = _confirmable_preview()
    plane.persist_preview(preview)
    plane.confirm_and_run(
        preview,
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=coordinator,
    )
    digest = coordinator.status()["plan_digest"]
    lifecycle_dir = tmp_path / "dualtrack" / "grid_testnet_lifecycle"
    lifecycle_dir.mkdir(parents=True)
    (lifecycle_dir / "dashboard-plan:test.json").write_text(json.dumps([
        {"plan_digest": digest, "status": "paused_above_range",
         "orders": [{"state": "accepted", "broker_order_id": "59841174347"}]},
    ]))
    coordinator._record({**coordinator.status(), "status": "grid_blocked", "execution_enabled": False,
                         "execution_mutation": False, "network_operation_invoked": False,
                         "canonical_order_count": 0, "execution_receipts": []})

    with pytest.raises(TestnetCoordinatorError, match="submitted_orders_require_reconciliation"):
        coordinator.command("reconcile_stop", {"reason": "zero_orders"}, command_id="reconcile-orphan")

    (lifecycle_dir / "dashboard-plan:test.json").write_text(json.dumps([
        {"plan_digest": digest, "status": "stopped_by_operator",
         "orders": [{"state": "cancelled", "broker_order_id": "59841174347"}]},
    ]))
    assert coordinator.command("reconcile_stop", {"reason": "zero_orders"}, command_id="reconcile-clean")["status"] == "idle"


def test_control_pass_probes_and_resumes_a_market_gate_block(monkeypatch, tmp_path: Path) -> None:
    # Full production path: park_control must not skip a read-side block, or nothing ever resumes it.
    import pipelines.park_control as module
    from services.strategy_control_plane import StrategyControlMachineError
    from services.testnet_automation_coordinator import TestnetAutomationCoordinator
    from services.testnet_scheduler import TestnetScheduler, TestnetSchedulerOwnershipStore

    output = tmp_path / "outputs"
    TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")
    coordinator = TestnetAutomationCoordinator(output)
    activation = {**_activation(), "strategy_family": "grid"}
    coordinator.activate(activation, command_id="activate")
    TestnetScheduler(output, coordinator, owner_id="local-mac", runtime_mode="local").activate(
        activation, command_id="scheduler-activate"
    )
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)
    gate = {"ok": False}

    def advance(_event):
        if not gate["ok"]:
            raise StrategyControlMachineError("testnet_market_not_authoritative", {"reason": "market_quality_gate_failed"})
        return {"status": "ok"}

    monkeypatch.setattr(module, "_build_testnet_tick_callbacks", lambda *_args: (advance, None))
    scheduler = TestnetScheduler(output, coordinator, owner_id="local-mac", runtime_mode="local")
    state = scheduler.status()
    state.update(status="blocked", blocker="testnet_facts_unavailable:testnet_market_not_authoritative:market_quality_gate_failed",
                 facts_unavailable_since="2026-09-11T14:00:00+00:00", next_action="notify_park_and_wait")
    scheduler._save_state(state)

    still_blocked = module.run_testnet_control_tick(output)
    gate["ok"] = True
    time.sleep(1.1)  # control-pass tick ids have one-second resolution
    resumed = module.run_testnet_control_tick(output)

    assert still_blocked["status"] == "blocked"
    assert resumed["status"] == "active"
    assert resumed["blocker"] is None


def test_issue_1264_scheduler_block_and_resume_each_notify_park_once(tmp_path: Path) -> None:
    import pipelines.park_control as module
    from services.park_telegram_control import ParkTelegramLedger

    active = {"status": "active", "activation_id": "sha256:" + "8" * 64, "occurred_at": "2026-09-11T13:59:00+00:00"}
    blocked = {**active, "status": "blocked", "occurred_at": "2026-09-11T14:10:00+00:00",
               "blocker": "testnet_facts_unavailable:testnet_market_not_authoritative:market_quality_gate_failed"}
    still = {**blocked, "occurred_at": "2026-09-11T14:11:00+00:00"}
    resumed = {**active, "occurred_at": "2026-09-11T14:12:00+00:00"}
    binding = {"park_user_id": "741098667", "chat_id": "8831262827"}

    first = module.queue_testnet_scheduler_notice(tmp_path, active, blocked, **binding)
    module.queue_testnet_scheduler_notice(tmp_path, active, blocked, **binding)  # replayed pass
    silent = module.queue_testnet_scheduler_notice(tmp_path, blocked, still, **binding)
    back = module.queue_testnet_scheduler_notice(tmp_path, still, resumed, **binding)
    healthy = module.queue_testnet_scheduler_notice(tmp_path, resumed, {**resumed, "occurred_at": "x"}, **binding)

    rows = [row for row in ParkTelegramLedger(tmp_path, **binding).outbox_rows() if row.get("event") == "outbound_queued"]
    assert first is not None and back is not None
    assert silent is None and healthy is None
    assert len(rows) == 2
    assert "market_quality_gate_failed" in rows[0]["text"]
    assert "恢复" in rows[1]["text"]


def test_testnet_fill_notifies_park_once_per_fill(monkeypatch, tmp_path: Path) -> None:
    # 2026-09-14: Park was away from the desk and only blocks/resumes reached Telegram, never a fill.
    import pipelines.park_control as module
    from services.park_telegram_control import ParkTelegramLedger

    plan_id = "dashboard-plan:abcdb97b5a089ed7"
    status = {"status": "grid_running", "strategy_family": "grid", "selected_instrument_id": "BTC-USD-PERP",
              "grid_lifecycle": {"strategy_plan_id": plan_id}}

    class Coordinator:
        def __init__(self, _root):
            pass

        def status(self):
            return status

    monkeypatch.setattr(module, "TestnetAutomationCoordinator", Coordinator)
    lifecycle = tmp_path / "dualtrack" / "grid_testnet_lifecycle" / f"{plan_id}.json"
    lifecycle.parent.mkdir(parents=True)
    fills = [{"fill_id": "tid-1", "event": "entry", "side": "B", "quantity": 0.00024, "price": 76500.0}]
    lifecycle.write_text(json.dumps([{"instrument_id": "BTC-USD-PERP", "status": "active", "fills": fills}]))
    binding = {"park_user_id": "741098667", "chat_id": "8831262827"}

    module.queue_testnet_fill_notices(tmp_path, **binding)
    module.queue_testnet_fill_notices(tmp_path, **binding)  # next control pass, same fill
    fills.append({"fill_id": "tid-2", "event": "tp", "side": "A", "quantity": 0.00024, "price": 77200.0})
    lifecycle.write_text(json.dumps([{"instrument_id": "BTC-USD-PERP", "status": "active", "fills": fills}]))
    module.queue_testnet_fill_notices(tmp_path, **binding)

    rows = [row for row in ParkTelegramLedger(tmp_path, **binding).outbox_rows() if row.get("event") == "outbound_queued"]
    assert [row["message_type"] for row in rows] == ["park_fill", "park_fill"]
    assert "开仓" in rows[0]["text"] and "买入" in rows[0]["text"] and "76,500.0" in rows[0]["text"]
    assert "止盈" in rows[1]["text"] and "卖出" in rows[1]["text"]


def test_operator_stop_cancels_the_ladder_and_flattens(tmp_path: Path) -> None:
    # 2026-09-14: "stop" only recorded intent; the f107 ladder stayed live on the venue.
    from tests.test_testnet_grid_coordinator import NOW as GRID_NOW, _market_at, _setup

    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)
    started = coordinator.start_grid_session(plan, confirmation=confirmation, market=_market_at(market, GRID_NOW),
                                             broker=broker, timestamp=GRID_NOW)
    opened = coordinator.advance_grid_session(plan, broker=broker, fill=fill(started["lifecycle"]["orders"][0], price=65000.0, tid=5),
                                              market=_market_at(market, "2026-08-26T01:01:00+00:00"),
                                              timestamp="2026-08-26T01:01:00+00:00")
    assert any(row["state"] == "accepted" for row in opened["lifecycle"]["orders"])

    with pytest.raises(Exception, match="grid_stop_not_requested"):
        coordinator.stop_grid_session(plan, broker=broker, market=_market_at(market, "2026-08-26T01:02:00+00:00"),
                                      timestamp="2026-08-26T01:02:00+00:00")
    coordinator.command("stop", {"reason": "park_desk_stop"}, command_id="desk-stop")
    stopped = coordinator.stop_grid_session(plan, broker=broker, market=_market_at(market, "2026-08-26T01:02:00+00:00"),
                                            timestamp="2026-08-26T01:02:00+00:00")

    lifecycle = stopped["lifecycle"]
    assert lifecycle["hard_stop_reason"] == "operator_stop"
    assert not any(row["state"] == "accepted" and row["event"] in {"entry", "entry_rearm", "tp"} for row in lifecycle["orders"])
    assert any(row["event"] == "hard_stop" for row in lifecycle["orders"])
    assert stopped["status"] != "stop_requested"
    assert stopped["execution_enabled"] is False  # still flattening: entries must stay closed


def test_control_pass_builds_callbacks_for_a_grid_stop_request(monkeypatch, tmp_path: Path) -> None:
    # The stop is carried out by the 60s control pass; it must not skip a stop_requested coordinator.
    import pipelines.park_control as module
    from services.testnet_automation_coordinator import TestnetAutomationCoordinator
    from services.testnet_scheduler import TestnetScheduler, TestnetSchedulerOwnershipStore

    output = tmp_path / "outputs"
    TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")
    coordinator = TestnetAutomationCoordinator(output)
    activation = {**_activation(), "strategy_family": "grid"}
    coordinator.activate(activation, command_id="activate")
    TestnetScheduler(output, coordinator, owner_id="local-mac", runtime_mode="local").activate(activation, command_id="scheduler-activate")
    current = coordinator.status()
    current.update({"status": "stop_requested", "execution_enabled": False, "grid_lifecycle": {"strategy_plan_id": "dashboard-plan:x"}})
    coordinator._record(current)
    calls = []
    monkeypatch.setattr(module, "_build_testnet_tick_callbacks",
                        lambda *_args: (lambda _event: calls.append("advance") or {"status": "ok"}, None))

    module.run_testnet_control_tick(output)

    assert calls == ["advance"]


def test_operator_stop_on_an_unfilled_ladder_seals_and_closes_to_idle(tmp_path: Path) -> None:
    # The live 09-14 case: five resting entries, no fill. Stop must finish, not latch in hard_stop_triggered.
    from tests.test_testnet_grid_coordinator import NOW as GRID_NOW, _market_at, _setup

    coordinator, plan, confirmation, broker, _backend, market, _fill = _setup(tmp_path)
    coordinator.start_grid_session(plan, confirmation=confirmation, market=_market_at(market, GRID_NOW), broker=broker, timestamp=GRID_NOW)
    coordinator.command("stop", {"reason": "park_desk_stop"}, command_id="desk-stop")
    stopped = coordinator.stop_grid_session(plan, broker=broker, market=_market_at(market, "2026-08-26T01:02:00+00:00"),
                                            timestamp="2026-08-26T01:02:00+00:00")

    assert stopped["status"] == "grid_terminal"
    assert stopped["lifecycle"]["sealed"] is True
    assert all(row["state"] == "cancelled" for row in stopped["lifecycle"]["orders"])
    assert coordinator.command("close_terminal", {}, command_id="desk-close")["status"] == "idle"


def test_operator_stop_flattens_a_fill_seen_in_the_same_tick(tmp_path: Path) -> None:
    # A rung can fill between the stop press and the control pass; the flatten must cover it.
    from tests.test_testnet_grid_coordinator import NOW as GRID_NOW, _market_at, _setup

    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)
    started = coordinator.start_grid_session(plan, confirmation=confirmation, market=_market_at(market, GRID_NOW), broker=broker, timestamp=GRID_NOW)
    coordinator.command("stop", {"reason": "park_desk_stop"}, command_id="desk-stop")
    stopped = coordinator.stop_grid_session(plan, broker=broker, market=_market_at(market, "2026-08-26T01:02:00+00:00"),
                                            fills=[fill(started["lifecycle"]["orders"][0], price=65000.0, tid=7)],
                                            timestamp="2026-08-26T01:02:00+00:00")

    assert len(stopped["lifecycle"]["fills"]) == 1
    assert any(row["event"] == "hard_stop" for row in stopped["lifecycle"]["orders"])


# ---- Park's 暂停补单 (operator re-arm hold) -------------------------------------------------

def _hold(tmp_path: Path, paused: bool) -> None:
    path = tmp_path / "outputs" / "testnet_automation" / "operator_holds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"BTC-USD-PERP": {"rearm_paused": paused, "by": "park", "reason": "FOMC"}}))


def test_rearm_hold_skips_rearm_after_take_profit_and_releases_only_passive_rungs(tmp_path: Path) -> None:
    from tests.test_dca_testnet_lifecycle import _broker
    from tests.testnet_replay.harness import ReplayExchange, assert_invariants, fill, grid_plan, new_lifecycle

    broker, _backend = _broker(tmp_path)
    exchange = ReplayExchange(); exchange.observe(broker)
    lifecycle = new_lifecycle(tmp_path / "outputs", broker)
    plan = grid_plan()
    state = lifecycle.start(plan, timestamp="2026-09-11T01:00:00+00:00")
    entry = state["orders"][0]
    exchange.inject_fill(entry, tid=3, price=80_000)
    opened = lifecycle.on_fill(plan, fill(entry, tid=3, price=80_000), timestamp="2026-09-11T01:01:00+00:00")
    tp = next(row for row in opened["orders"] if row["event"] == "tp" and row["state"] == "accepted")
    _hold(tmp_path, True)
    exchange.inject_fill(tp, tid=4, price=80_500)
    closed = lifecycle.on_fill(plan, fill(tp, tid=4, price=80_500), timestamp="2026-09-11T01:02:00+00:00")
    assert closed["rearm_hold"] is True
    assert any(e["event"] == "operator_rearm_paused" and e.get("by") == "park" for e in closed["events"])
    assert not any(row["event"] == "entry_rearm" for row in closed["orders"])  # no new buy while held
    rung2_entry = next(row for row in closed["orders"] if row["event"] == "entry" and row["state"] == "accepted")
    assert rung2_entry["price"] == 78_000.0  # the resting ladder is left alone
    assert_invariants(closed, exchange, previous=opened)

    held_tick = lifecycle.on_market_event(plan, price=81_000, timestamp="2026-09-11T01:03:00+00:00")
    assert not any(row["event"] == "entry_rearm" for row in held_tick["orders"])

    # Released while price sits below the 80,000 rung: re-arming now would buy above market, so it waits.
    _hold(tmp_path, False)
    below = lifecycle.on_market_event(plan, price=79_500, timestamp="2026-09-11T01:04:00+00:00")
    assert any(e["event"] == "operator_rearm_resumed" for e in below["events"])
    assert below["rearm_pending_after_hold"] is True
    assert not any(row["event"] == "entry_rearm" for row in below["orders"])
    back = lifecycle.on_market_event(plan, price=80_600, timestamp="2026-09-11T01:05:00+00:00")
    rearms = [row for row in back["orders"] if row["event"] == "entry_rearm"]
    assert len(rearms) == 1 and rearms[0]["price"] == 80_000.0 and back["rearm_pending_after_hold"] is False
    assert_invariants(back, exchange, previous=below)


def test_rearm_hold_survives_range_reentry_and_unreadable_file_holds(tmp_path: Path) -> None:
    from tests.test_dca_testnet_lifecycle import _broker
    from tests.testnet_replay.harness import ReplayExchange, grid_plan, new_lifecycle

    broker, _backend = _broker(tmp_path)
    exchange = ReplayExchange(); exchange.observe(broker)
    lifecycle = new_lifecycle(tmp_path / "outputs", broker)
    plan = grid_plan()
    state = lifecycle.start(plan, timestamp="2026-09-11T01:00:00+00:00")
    assert state.get("rearm_hold") in (None, False)
    path = tmp_path / "outputs" / "testnet_automation" / "operator_holds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    paused = lifecycle.on_market_event(plan, price=84_100, timestamp="2026-09-11T01:01:00+00:00")
    assert paused["status"] == "paused_above_range"
    reentered = lifecycle.on_market_event(plan, price=83_900, timestamp="2026-09-11T01:02:00+00:00")
    assert reentered["rearm_hold"] is True
    assert any(e["event"] == "operator_rearm_paused" and e.get("reason") == "operator_holds_unreadable" for e in reentered["events"])
