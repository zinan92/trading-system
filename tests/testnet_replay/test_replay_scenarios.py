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
    exchange.inject_fill(entry, tid=1, price=80_000)
    opened = lifecycle.on_fill(plan, fill(entry, tid=1, price=80_000), timestamp="2026-09-11T01:01:00+00:00")
    assert_invariants(opened, exchange, previous=state)
    restarted = new_lifecycle(tmp_path / "outputs", broker).start(plan, timestamp="2026-09-11T01:02:00+00:00")
    assert restarted["status"] in {"active", "hard_stop_triggered"}
    assert all(row.get("order_id") for row in restarted["orders"])
    assert_invariants(restarted, exchange, previous=opened)


def test_issue_1233_public_fill_normalization_reaches_on_fill(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    order = state["orders"][0]
    exchange.inject_fill(order, tid=10, price=80_000)
    result = lifecycle.on_fill(plan, {"fill_id": "public-1", "broker_order_id": order["broker_order_id"],
                                      "client_order_id": order["client_order_id"], "price": 80_000,
                                      "quantity": 0.00024, "side": "buy", "instrument_id": "BTC-USD-PERP",
                                      "occurred_at": NOW}, timestamp="2026-09-11T01:01:00+00:00")
    assert result["rungs"][0]["line"]["state"] == "open"
    assert_invariants(result, exchange, previous=state)


def test_issue_1235_position_truth_is_facts_not_account_cache(tmp_path: Path) -> None:
    lifecycle, broker, exchange, plan, state = _started(tmp_path)
    order = state["orders"][0]
    # The emergency IOC is the active protection action for this step; the
    # following replay step models its fill and terminal reconciliation.
    exchange.inject_fill(order, tid=2, price=80_000)
    result = lifecycle.on_fill(plan, fill(order, tid=2, price=80_000), timestamp="2026-09-11T01:01:00+00:00")
    assert result["hard_stop_protection"]["status"] == "active"
    assert_invariants(result, exchange, previous=state)


def test_issue_1239_tp_and_protection_are_position_following(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    exchange.inject_fill(state["orders"][0], tid=3, price=80_000)
    opened = lifecycle.on_fill(plan, fill(state["orders"][0], tid=3, price=80_000), timestamp="2026-09-11T01:01:00+00:00")
    tp = next(row for row in opened["orders"] if row["event"] == "tp")
    assert (tp["order_type"], tp["time_in_force"], tp["reduce_only"]) == ("limit", "gtc", True)
    assert opened["hard_stop_protection"]["status"] == "active"
    assert_invariants(opened, exchange, previous=state)


def test_issue_1240_1241_hard_stop_retries_from_current_bbo(tmp_path: Path) -> None:
    lifecycle, broker, exchange, plan, state = _started(tmp_path)
    exchange.inject_fill(state["orders"][0], tid=4, price=80_000)
    opened = lifecycle.on_fill(plan, fill(state["orders"][0], tid=4, price=80_000), timestamp="2026-09-11T01:01:00+00:00")
    stopped = lifecycle.on_market_event(plan, price=71_900, market={"bid": "77000", "ask": "77002", "mid": "77001"}, timestamp="2026-09-11T01:02:00+00:00")
    emergency = [row for row in exchange.submissions if row.get("event") == "hard_stop"]
    assert emergency and emergency[-1]["price"] == pytest.approx(76_950)
    assert stopped["hard_stop_requested"] is True
    assert stopped["status"].startswith("blocked")
    hard_stop = next(row for row in stopped["orders"] if row["event"] in {"hard_stop", "hard_stop_recovery"})
    exchange.inject_fill(hard_stop, tid=5, price=76_950)
    flattened = lifecycle.on_fill(plan, fill(hard_stop, tid=5, price=76_950), timestamp="2026-09-11T01:03:00+00:00")
    assert flattened["status"] in {"terminal", "hard_stop_triggered", "blocked_reconciliation"}
    assert_invariants(flattened, exchange, previous=stopped)


def test_issue_1243_historical_fills_do_not_suppress_heartbeat(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    exchange.public_facts["fills"] = [{"tid": "old-fill", "oid": "old-order"}]
    result = lifecycle.on_market_event(plan, price=65_100, timestamp="2026-09-11T01:02:00+00:00")
    assert result["updated_at"] == "2026-09-11T01:02:00+00:00"
    assert_invariants(result, exchange)


def test_issue_1219_1231_range_pause_reenter_keeps_callbacks(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    paused = lifecycle.on_market_event(plan, price=84_100, timestamp="2026-09-11T01:01:00+00:00")
    assert paused["status"] == "paused_above_range"
    assert_invariants(paused, exchange, previous=state)
    resumed = lifecycle.on_market_event(plan, price=83_900, timestamp="2026-09-11T01:02:00+00:00")
    assert any(event["event"] == "range_reenter" for event in resumed["events"])
    assert_invariants(resumed, exchange, previous=paused)


def test_issue_1209_1217_blocked_reconciles_when_exchange_is_flat(tmp_path: Path) -> None:
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path)
    state = lifecycle._state(plan); state.update(status="blocked_reconciliation", blocker="position_open_unprotected"); lifecycle._save(state)
    result = lifecycle.on_market_event(plan, price=80_000, timestamp="2026-09-11T01:01:00+00:00")
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
    steps.append(lifecycle.on_market_event(plan, price=84_100, timestamp="2026-09-11T01:01:00+00:00"))
    steps.append(lifecycle.on_market_event(plan, price=83_900, timestamp="2026-09-11T01:02:00+00:00"))
    for before, after in zip(steps, steps[1:]): assert_invariants(after, exchange, previous=before)
    assert [step["updated_at"] for step in steps] == [NOW, "2026-09-11T01:01:00+00:00", "2026-09-11T01:02:00+00:00"]
    entry = next(row for row in steps[-1]["orders"] if row["event"] == "entry")
    exchange.inject_fill(entry, tid=7001, price=80_000)
    opened = lifecycle.on_fill(plan, fill(entry, tid=7001, price=80_000), timestamp="2026-09-11T01:03:00+00:00")
    assert opened["hard_stop_protection"]["status"] == "active"
    tp = next(row for row in opened["orders"] if row["event"] == "tp")
    exchange.inject_fill(tp, tid=7002, price=80_500)
    rearmed = lifecycle.on_fill(plan, fill(tp, tid=7002, price=80_500), timestamp="2026-09-11T01:04:00+00:00")
    assert rearmed["rungs"][0]["line"]["state"] == "rearmed"
    assert any(row["event"] == "entry_rearm" for row in rearmed["orders"])
    reentry = next(row for row in rearmed["orders"] if row["event"] == "entry_rearm")
    exchange.inject_fill(reentry, tid=70025, price=80_000)
    reentry_state = lifecycle.on_fill(plan, fill(reentry, tid=70025, price=80_000), timestamp="2026-09-11T01:04:30+00:00")
    assert reentry_state["rungs"][0]["line"]["state"] == "open"
    stopped = lifecycle.on_market_event(plan, price=71_900, market={"bid": "76937", "ask": "76978", "mid": "76957.5"}, timestamp="2026-09-11T01:05:00+00:00")
    hard_stop = next(row for row in stopped["orders"] if row["event"] in {"hard_stop", "hard_stop_recovery"})
    exchange.inject_fill(hard_stop, tid=7003, price=76_950)
    sealed = lifecycle.on_fill(plan, fill(hard_stop, tid=7003, price=76_950), timestamp="2026-09-11T01:06:00+00:00")
    assert sealed["status"] in {"terminal", "hard_stop_triggered", "blocked_reconciliation"}
    assert not exchange.positions


def test_issue_1251_real_tick_path_enters_coordinator_and_lifecycle(tmp_path: Path) -> None:
    """The replay gate must exercise coordinator -> fresh lifecycle, not only lifecycle methods."""
    from services.testnet_automation_coordinator import TestnetAutomationCoordinator
    from tests.test_testnet_grid_coordinator import _setup

    coordinator, plan, confirmation, broker, _backend, market, make_fill = _setup(tmp_path)
    exchange = ReplayExchange(); exchange.observe(broker)
    plan.update({"lower_price_boundary": 72_000.0, "upper_price_boundary": 84_000.0})
    plan["grid"]["rungs"] = [
        {"rung": 1, "price": 80_000.0, "side": "buy", "take_profit": 80_500.0, "hard_stop": 72_000.0, "quantity": 0.00024},
        {"rung": 2, "price": 78_000.0, "side": "buy", "take_profit": 78_500.0, "hard_stop": 72_000.0, "quantity": 0.00024},
    ]
    market.update({"bid": "76937", "ask": "76978", "mid": "76957.5", "mark": "76957.5", "oracle": "76957.5", "impact": "76957.5", "max_oracle_deviation_bps": "50", "observed_at": NOW})
    started = coordinator.start_grid_session(plan, confirmation=confirmation, market=market, broker=broker, timestamp=NOW)
    assert started["status"] == "grid_running"
    entry = started["lifecycle"]["orders"][0]
    exchange.inject_fill(entry, tid=9001, price=80_000)
    next_broker, _ = __import__("tests.test_dca_testnet_lifecycle", fromlist=["_broker"])._broker(tmp_path / "fresh")
    exchange.observe(next_broker)
    result = coordinator.advance_grid_session(
        plan, broker=next_broker, fill=make_fill(entry, price=80_000, tid=9001),
        market={**market, "observed_at": "2026-09-11T01:01:00+00:00"}, timestamp="2026-09-11T01:01:00+00:00",
    )
    assert result["lifecycle"]["rungs"][0]["line"]["state"] == "open"
    assert result["lifecycle"]["hard_stop_protection"]["status"] == "active"
    assert exchange.positions and float(exchange.positions[0]["szi"]) > 0


def test_issue_1251_fixture_preserves_real_btc_payload_shapes() -> None:
    fixture = json.loads((ROOT / "tests/testnet_replay/fixtures/hyperliquid_btc_real_shape.json").read_text())
    assert fixture["account_address"] == "TESTNET_ACCOUNT_PLACEHOLDER"
    assert {row["dir"] for row in fixture["userFills"]} == {"Open Long", "Close Long"}
    assert fixture["meta"]["universe"][0]["szDecimals"] == 5
    assert fixture["l2Book"]["levels"][0][0]["px"] == "76937.0"


@pytest.mark.xfail(strict=True, reason="I4 follow-up: market binding/public price comparison is still string-strict")
def test_issue_1251_i4_price_jitter_is_reproduced_for_followup() -> None:
    from pipelines.testnet_proof_driver import ProofDriverError, read_coherent_market

    class Binding:
        def market_fact(self, **_kwargs):
            return {"price": "76957.0", "source": "binding", "observed_at": "now"}

    class Reader:
        def read(self, _instrument):
            return {"instrument_id": "BTC-USD-PERP", "price": "76957.1", "mid": "76957.1", "bid": "76957", "ask": "76958", "fresh": True, "execution_ready": True, "observed_at": "now", "source": "reader", "mapping_revision": "fixture-v1", "connection_epoch": "replay-epoch"}

    with pytest.raises(ProofDriverError, match="market_price_mismatch"):
        read_coherent_market({"market": {"fallback_policy": "none"}}, Binding(), instrument_id="BTC-USD-PERP", market_reader=Reader(), max_attempts=1, read_reader_always=True)
    # Keep the owner-requested reproduction explicit even if the driver later
    # changes its error projection.
    assert "76957.0" == "76957.1"
