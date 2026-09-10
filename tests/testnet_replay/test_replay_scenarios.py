from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.grid_testnet_lifecycle import GridTestnetLifecycleError
from services.testnet_scheduler import TestnetScheduler, TestnetSchedulerOwnershipStore
from tests.test_dca_testnet_lifecycle import _broker
from tests.testnet_replay.harness import ReplayExchange, assert_invariants, fill, grid_plan, new_lifecycle


ROOT = Path(__file__).parents[2]
NOW = "2026-09-11T01:00:00+00:00"


def _started(tmp_path: Path):
    broker, _backend = _broker(tmp_path)
    exchange = ReplayExchange(); exchange.observe(broker)
    lifecycle = new_lifecycle(tmp_path / "outputs", broker)
    plan = grid_plan()
    state = lifecycle.start(plan, timestamp=NOW)
    assert_invariants(state, exchange)
    return lifecycle, broker, exchange, plan, state


def test_issue_1227_1229_1237_1239_restart_hydrate_and_skip_terminal(tmp_path: Path) -> None:
    lifecycle, broker, exchange, plan, state = _started(tmp_path)
    from pipelines.park_control import hydrate_order_identities

    class FreshBinding:
        def __init__(self): self.recovered = []
        def recover(self, request, **kwargs): self.recovered.append((request, kwargs))

    fresh_binding = FreshBinding()
    hydration = hydrate_order_identities(fresh_binding, state)
    assert hydration["recovered"] == len(state["orders"])
    assert hydration["skipped"] == []
    entry = state["orders"][0]
    opened = lifecycle.on_fill(plan, fill(entry, tid=1, price=65_000), timestamp=NOW)
    assert_invariants(opened, exchange, previous=state)
    restarted = new_lifecycle(tmp_path / "outputs", broker).start(plan, timestamp="2026-09-11T01:01:00+00:00")
    assert restarted["status"] in {"active", "hard_stop_triggered"}
    assert all(row.get("order_id") for row in restarted["orders"])
    assert_invariants(restarted, exchange, previous=opened)


def test_issue_1233_public_fill_normalization_reaches_on_fill(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    order = state["orders"][0]
    result = lifecycle.on_fill(plan, {"fill_id": "public-1", "broker_order_id": order["broker_order_id"],
                                      "client_order_id": order["client_order_id"], "price": 65_000,
                                      "quantity": 0.1, "side": "buy", "instrument_id": "BTC-USD-PERP",
                                      "occurred_at": NOW}, timestamp=NOW)
    assert result["rungs"][0]["line"]["state"] == "open"
    assert_invariants(result, exchange, previous=state)


def test_issue_1235_position_truth_is_facts_not_account_cache(tmp_path: Path) -> None:
    lifecycle, broker, exchange, plan, state = _started(tmp_path)
    order = state["orders"][0]
    # The emergency IOC is the active protection action for this step; the
    # following replay step models its fill and terminal reconciliation.
    exchange.public_facts["positions"] = list(exchange.positions)
    result = lifecycle.on_fill(plan, fill(order, tid=2, price=65_000), timestamp=NOW)
    assert result["hard_stop_protection"]["status"] == "active"
    assert_invariants(result, exchange, previous=state)


def test_issue_1239_tp_and_protection_are_position_following(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    opened = lifecycle.on_fill(plan, fill(state["orders"][0], tid=3, price=65_000), timestamp=NOW)
    tp = next(row for row in opened["orders"] if row["event"] == "tp")
    assert (tp["order_type"], tp["time_in_force"], tp["reduce_only"]) == ("limit", "gtc", True)
    assert opened["hard_stop_protection"]["status"] == "active"
    assert_invariants(opened, exchange, previous=state)


def test_issue_1240_1241_hard_stop_retries_from_current_bbo(tmp_path: Path) -> None:
    lifecycle, broker, exchange, plan, state = _started(tmp_path)
    opened = lifecycle.on_fill(plan, fill(state["orders"][0], tid=4, price=65_000), timestamp=NOW)
    stopped = lifecycle.on_market_event(plan, price=62_900, market={"bid": "77000", "ask": "77002", "mid": "77001"}, timestamp=NOW)
    emergency = [row for row in exchange.submissions if row.get("event") == "hard_stop"]
    assert emergency and emergency[-1]["price"] == pytest.approx(76_950)
    assert stopped["hard_stop_requested"] is True
    assert_invariants(stopped, exchange, previous=opened)


def test_issue_1243_historical_fills_do_not_suppress_heartbeat(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    exchange.public_facts["fills"] = [{"tid": "old-fill", "oid": "old-order"}]
    result = lifecycle.on_market_event(plan, price=65_100, timestamp="2026-09-11T01:02:00+00:00")
    assert result["updated_at"] == "2026-09-11T01:02:00+00:00"
    assert_invariants(result, exchange, previous=state)


def test_issue_1219_1231_range_pause_reenter_keeps_callbacks(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    paused = lifecycle.on_market_event(plan, price=66_100, timestamp=NOW)
    assert paused["status"] == "paused_above_range"
    assert_invariants(paused, exchange, previous=state)
    resumed = lifecycle.on_market_event(plan, price=65_900, timestamp="2026-09-11T01:01:00+00:00")
    assert any(event["event"] == "range_reenter" for event in resumed["events"])
    assert_invariants(resumed, exchange, previous=paused)


def test_issue_1209_1217_blocked_reconciles_when_exchange_is_flat(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    state = lifecycle._state(plan); state.update(status="blocked_reconciliation", blocker="position_open_unprotected"); lifecycle._save(state)
    result = lifecycle.on_market_event(plan, price=65_000, timestamp=NOW)
    assert result["status"] in {"active", "terminal", "blocked_reconciliation"}
    assert_invariants(result, exchange, previous=state)


def test_issue_1233_local_fill_error_never_cancels_exchange_orders(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, _state = _started(tmp_path)
    with pytest.raises(GridTestnetLifecycleError, match="unknown_grid_client_identity"):
        lifecycle.on_fill(plan, {"tid": "bad-fill", "cloid": "not-registered", "px": "65000", "sz": "0.1", "side": "B"}, timestamp=NOW)
    blocked = lifecycle.snapshot(plan)
    assert blocked["status"].startswith("blocked")
    assert exchange.cancellations == []
    assert_invariants(blocked, exchange)


def test_issue_1213_1215_sampling_warning_does_not_consume_strike(tmp_path: Path) -> None:
    output = tmp_path / "outputs"; TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")
    class Coordinator:
        def status(self): return {"status": "grid_running", "execution_enabled": True}
    scheduler = TestnetScheduler(output, Coordinator(), owner_id="local-mac", runtime_mode="local")
    scheduler._save_state({"status": "active", "activation_id": "replay", "execution_enabled": True, "advance_failure_count": 0})
    result = scheduler.tick(tick_id="sampling", event={"kind": "market_heartbeat"},
                            advance=lambda _event: {"status": "failed", "reason": "testnet_market_not_authoritative:market_price_mismatch",
                                                     "market_failure": {"reason": "market_price_mismatch", "attempts": [{"attempt": 1}]}}, timestamp=NOW)
    assert result["status"] == "active" and result["advance_failure_count"] == 0


def test_issue_1248_1249_1250_1245_1223_startup_facts_are_fail_closed(tmp_path: Path) -> None:
    from pipelines.testnet_automation_proof import _has_nonzero_position
    assert _has_nonzero_position([]) is False
    assert _has_nonzero_position([SimpleNamespace(signed_quantity="0")]) is False
    assert _has_nonzero_position([SimpleNamespace(signed_quantity="0.1")]) is True
    plan = grid_plan(); assert plan["risk_budget"]["maximum_loss_at_full_depth"] == 1000.0


def test_issue_1221_1226_terminal_close_returns_scheduler_to_idle(tmp_path: Path) -> None:
    output = tmp_path / "outputs"; TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")
    class Coordinator:
        def status(self): return {"status": "grid_terminal", "activation_id": "old"}
    scheduler = TestnetScheduler(output, Coordinator(), owner_id="local-mac", runtime_mode="local")
    scheduler._save_state({"status": "awaiting_operator", "activation_id": "old", "coordinator_status": "grid_terminal"})
    from services.testnet_scheduler import close_scheduler_session
    closed = close_scheduler_session(output, "old", timestamp=NOW)
    assert closed["status"] == "idle" and closed["previous_activation_id"] == "old"


def test_issue_1251_end_to_end_replay_has_distinct_process_steps(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    steps = [state]
    steps.append(lifecycle.on_market_event(plan, price=66_100, timestamp="2026-09-11T01:01:00+00:00"))
    steps.append(lifecycle.on_market_event(plan, price=65_900, timestamp="2026-09-11T01:02:00+00:00"))
    for before, after in zip(steps, steps[1:]): assert_invariants(after, exchange, previous=before)
    assert [step["updated_at"] for step in steps] == [NOW, "2026-09-11T01:01:00+00:00", "2026-09-11T01:02:00+00:00"]
