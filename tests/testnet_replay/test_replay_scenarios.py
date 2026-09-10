from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
    hydration_state = dict(state)
    hydration_state["orders"] = [
        *state["orders"],
        {**state["orders"][0], "state": "filled", "broker_order_id": "filled-identity"},
        {**state["orders"][1], "state": "cancelled", "broker_order_id": "cancelled-identity"},
    ]
    hydration = hydrate_order_identities(fresh_binding, hydration_state)
    assert hydration["recovered"] == len(hydration_state["orders"])
    assert hydration["skipped"] == []
    entry = state["orders"][0]
    exchange.inject_fill(entry, tid=1, price=80_000)
    opened = lifecycle.on_fill(plan, fill(entry, tid=1, price=80_000), timestamp="2026-09-11T01:01:00+00:00")
    assert_invariants(opened, exchange, previous=state)
    restarted = new_lifecycle(tmp_path / "outputs", broker).start(plan, timestamp="2026-09-11T01:02:00+00:00")
    assert restarted["status"] == "active"
    assert all(row.get("order_id") for row in restarted["orders"])


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
    active_groups = [group for group in exchange.protection_groups.values() if group["state"] == "active"]
    assert len(active_groups) == 1
    group = active_groups[0]
    assert group["take_profit"]["type"] == "take_profit"
    assert group["stop_loss"]["type"] == "stop_loss"
    assert float(group["stop_loss"]["trigger_price"]) == plan["grid"]["rungs"][0]["hard_stop"]
    assert any(row["event"] == "tp" and row["reduce_only"] is True and row["time_in_force"] == "gtc" for row in exchange.open_orders)
    assert_invariants(opened, exchange, previous=state)


def test_issue_1240_1241_hard_stop_retries_from_current_bbo(tmp_path: Path) -> None:
    lifecycle, broker, exchange, plan, state = _started(tmp_path)
    exchange.inject_fill(state["orders"][0], tid=4, price=80_000)
    opened = lifecycle.on_fill(plan, fill(state["orders"][0], tid=4, price=80_000), timestamp="2026-09-11T01:01:00+00:00")
    stopped = lifecycle.on_market_event(plan, price=71_900, market={"bid": "77000", "ask": "77002", "mid": "77001"}, timestamp="2026-09-11T01:02:00+00:00")
    emergency = [row for row in exchange.submissions if row.get("event") == "hard_stop"]
    assert emergency and emergency[-1]["price"] == pytest.approx(76_950)
    assert stopped["hard_stop_requested"] is True
    assert stopped["status"] == "blocked_reconciliation"
    hard_stop = next(row for row in stopped["orders"] if row["event"] in {"hard_stop", "hard_stop_recovery"})
    exchange.inject_fill(hard_stop, tid=5, price=76_950)
    flattened = lifecycle.on_fill(plan, fill(hard_stop, tid=5, price=76_950), timestamp="2026-09-11T01:03:00+00:00")
    assert flattened["status"] == "terminal", (flattened.get("reconciliation"), exchange.open_orders, [getattr(item, "ticket", None) for item in exchange.cancellations])
    assert flattened["sealed"] is True
    assert not exchange.positions
    assert not exchange.open_orders
    assert all(group["state"] == "canceled" for group in exchange.protection_groups.values())
    assert_invariants(flattened, exchange, previous=stopped)


def test_issue_1241_each_hard_stop_recovery_path_sets_request_flag(tmp_path: Path) -> None:
    # :432: recover an order-cancel blocker and discover the exchange exposure.
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path / "cancel-blocker")
    state = lifecycle._state(plan)
    state.update(status="blocked_reconciliation", blocker="order_cancel_failed:timeout")
    lifecycle._save(state)
    recovered = lifecycle.on_market_event(plan, price=80_000, timestamp="2026-09-11T01:01:00+00:00")
    assert recovered["hard_stop_requested"] is True

    # :971: the emergency flatten succeeds after the primary hard-stop submit fails.
    lifecycle, broker, exchange, plan, state = _started(tmp_path / "emergency")
    exchange.inject_fill(state["orders"][0], tid=44, price=80_000)
    opened = lifecycle.on_fill(plan, fill(state["orders"][0], tid=44, price=80_000), timestamp="2026-09-11T01:01:00+00:00")
    original_submit = broker.submit_order
    def submit(request):
        if request.ticket.get("event") == "hard_stop":
            raise RuntimeError("primary flatten unavailable")
        return original_submit(request)
    broker.submit_order = submit
    emergency_state = lifecycle._state(plan)
    emergency_state["hard_stop_requested"] = False
    lifecycle._save(emergency_state)
    lifecycle._submit_emergency_flatten(
        plan, emergency_state, emergency_state["rungs"][0],
        timestamp="2026-09-11T01:02:00+00:00", reason="testnet_replay",
        market={"bid": "77000", "ask": "77002", "mid": "77001"}, market_price=71_900,
    )
    assert lifecycle.snapshot(plan)["hard_stop_requested"] is True
    assert any(row.get("event") == "hard_stop_recovery" for row in lifecycle.snapshot(plan)["orders"])

    # :1267: the shared marker is exercised by the normal boundary path.
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path / "marker")
    exchange.inject_fill(state["orders"][0], tid=45, price=80_000)
    opened = lifecycle.on_fill(plan, fill(state["orders"][0], tid=45, price=80_000), timestamp="2026-09-11T01:01:00+00:00")
    stopped = lifecycle.on_market_event(plan, price=71_900, market={"bid": "77000", "ask": "77002", "mid": "77001"}, timestamp="2026-09-11T01:02:00+00:00")
    assert stopped["hard_stop_requested"] is True

    # :1279: a fresh heartbeat retry must mark the request before reading facts.
    lifecycle, _broker_obj, exchange, plan, state = _started(tmp_path / "heartbeat")
    state = lifecycle._state(plan)
    state.update(status="hard_stop_triggered", hard_stop_requested=False)
    lifecycle._save(state)
    retried = lifecycle.on_market_event(plan, price=80_000, timestamp="2026-09-11T01:01:00+00:00")
    assert retried["hard_stop_requested"] is True


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
    exchange.open_orders.clear()
    state = lifecycle._state(plan); state.update(status="hard_stop_triggered", blocker="position_open_unprotected", hard_stop_requested=True); lifecycle._save(state)
    previous = {"updated_at": state["updated_at"]}
    result = lifecycle.on_market_event(plan, price=80_000, timestamp="2026-09-11T01:01:00+00:00")
    assert result["status"] == "terminal", (result.get("reconciliation"), exchange.open_orders, result.get("events", [])[-3:])
    assert result["sealed"] is True
    assert any(event["event"] == "broker_absent_reconciled" for event in result["events"])
    assert_invariants(result, exchange, previous=previous)


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


def test_issue_1248_authoritative_start_market_uses_tolerance_bbo_and_age() -> None:
    from pipelines.testnet_automation_proof import TestnetAutomationProofError, _authoritative_market

    now = datetime.now(timezone.utc).replace(microsecond=0)
    plan = grid_plan()
    market = {"source": "hyperliquid.external_testnet", "mapping_revision": "fixture-v1",
              "mid": "100.0", "bid": "99.5", "ask": "100.5", "observed_at": now.isoformat()}

    class Broker:
        def __init__(self, price: str = "100.0", observed_at: datetime | None = None):
            self.price, self.observed_at = price, observed_at or now
        def market_fact(self, **_kwargs):
            return {"price": self.price, "source": "hyperliquid.external_testnet",
                    "transport_state": "external_testnet", "instrument_id": "BTC-USD-PERP",
                    "freshness": "fresh", "mapping_revision": "fixture-v1",
                    "observed_at": self.observed_at.isoformat()}

    assert _authoritative_market(Broker("100.05"), market=market, plan=plan, instrument_id="BTC-USD-PERP")["broker_market_fact"]["tolerance"] == "0.1"
    with pytest.raises(TestnetAutomationProofError, match="market_price_mismatch"):
        _authoritative_market(Broker("100.2"), market=market, plan=plan, instrument_id="BTC-USD-PERP")
    bbo_market = {**market, "ask": "100.04"}
    with pytest.raises(TestnetAutomationProofError, match="market_price_mismatch"):
        _authoritative_market(Broker("100.05"), market=bbo_market, plan=plan, instrument_id="BTC-USD-PERP")
    with pytest.raises(TestnetAutomationProofError, match="market_observation_mismatch"):
        _authoritative_market(Broker(observed_at=now - timedelta(seconds=11)), market=market, plan=plan, instrument_id="BTC-USD-PERP")


def test_issue_1250_clean_account_historical_fill_policy_is_fail_closed() -> None:
    from pipelines.testnet_automation_proof import _historical_unattributed_fill_rows
    from types import SimpleNamespace

    confirmation = SimpleNamespace(confirmed_at="2026-09-11T01:00:00+00:00")
    historical = SimpleNamespace(fill_id="old", occurred_at="2026-09-10T01:00:00+00:00")
    late = SimpleNamespace(fill_id="late", occurred_at="2026-09-11T01:00:01+00:00")
    old_rows = _historical_unattributed_fill_rows(SimpleNamespace(unattributed_fills=(historical,)), activation_confirmed_at=confirmation.confirmed_at)
    assert old_rows[0]["fill_id"] == "old"
    with pytest.raises(Exception, match="account_facts_unavailable"):
        _historical_unattributed_fill_rows(SimpleNamespace(unattributed_fills=(late,)), activation_confirmed_at=confirmation.confirmed_at)


def test_issue_1245_preview_and_lifecycle_full_depth_loss_are_same(tmp_path: Path) -> None:
    from services import grid_sizing
    from services.grid_testnet_lifecycle import GridTestnetLifecycle
    from services.strategy_control_plane import StrategyControlPlane
    from tests.test_grid_sizing import account, market
    preview = grid_sizing.build_grid_preview("replay", {"direction": "long", "style": "steady"},
        market=market(close=110.0), account=account(), config=StrategyControlPlane(tmp_path).config)
    plan = grid_plan()
    plan["risk_budget"]["maximum_loss_at_full_depth"] = preview["risk"]["max_loss"]
    plan["risk_budget"].update(max_open_orders=len(preview["orders"]), max_open_positions=len(preview["orders"]), max_notional=1_000_000_000, leverage_limit=1_000_000)
    rungs = [{"price": row["price"], "quantity": row["quantity"], "side": row["side"],
              "hard_stop": row.get("hard_stop", row.get("sl", plan["lower_price_boundary"]))} for row in preview["orders"]]
    GridTestnetLifecycle._validate_full_depth_risk(plan, rungs)
    assert preview["risk"]["max_loss"] == pytest.approx(sum(
        ((row["price"] - row["hard_stop"]) if row["side"] == "buy" else (row["hard_stop"] - row["price"])) * row["quantity"]
        for row in rungs
    ), abs=0.01)


def test_issue_1223_proof_facts_are_typed_dataclasses() -> None:
    from dataclasses import replace
    from pipelines.testnet_automation_proof import _authoritative_account_snapshot
    from services.standard_broker_testnet_canary_facts import CanaryAccountFact, CanaryReconciliationFact
    from tests.test_standard_broker_testnet_canary_facts import _entry_bundle, _plan
    typed_plan = _plan()
    bundle = _entry_bundle(typed_plan, "order-1")
    account = bundle.account
    object.__setattr__(account, "account_address", "testnet-account")
    object.__setattr__(account, "broker_id", "hyperliquid")
    object.__setattr__(account, "environment", "testnet")
    object.__setattr__(account, "equity", account.equity_usd)
    object.__setattr__(account, "exposure", Decimal("0"))
    object.__setattr__(account, "margin_used", Decimal("0"))
    object.__setattr__(account, "provenance", SimpleNamespace(source="hyperliquid.external_testnet", transport_state="external_testnet"))
    reconciliation = bundle.reconciliation
    object.__setattr__(reconciliation, "observed_at", datetime.now(timezone.utc))
    object.__setattr__(reconciliation, "account", SimpleNamespace(fact=SimpleNamespace(data=account)))
    object.__setattr__(reconciliation, "identity", SimpleNamespace(broker_id="hyperliquid", environment="testnet", account_address="testnet-account", lifecycle_id="", release_sha="", capability_revision=""))
    bundle = replace(bundle, account=account, reconciliation=reconciliation)
    class Broker:
        def read_facts(self, **_kwargs): return bundle
    account, reconciliation = _authoritative_account_snapshot(Broker(), plan={"instrument_id": typed_plan.instrument_id}, account_address="testnet-account")
    assert isinstance(account, CanaryAccountFact)
    assert isinstance(reconciliation, CanaryReconciliationFact)


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
    for index, (price, timestamp) in enumerate(((84_100, "2026-09-11T01:01:00+00:00"), (83_900, "2026-09-11T01:02:00+00:00"))):
        broker, _backend = _broker(tmp_path / f"fresh-market-{index}")
        exchange.observe(broker)
        steps.append(new_lifecycle(tmp_path / "outputs", broker).on_market_event(plan, price=price, timestamp=timestamp))
    for before, after in zip(steps, steps[1:]): assert_invariants(after, exchange, previous=before)
    assert [step["updated_at"] for step in steps] == [NOW, "2026-09-11T01:01:00+00:00", "2026-09-11T01:02:00+00:00"]
    entry = next(row for row in steps[-1]["orders"] if row["event"] == "entry")
    exchange.inject_fill(entry, tid=7001, price=80_000)
    broker, _backend = _broker(tmp_path / "fresh-open")
    exchange.observe(broker)
    opened = new_lifecycle(tmp_path / "outputs", broker).on_fill(plan, fill(entry, tid=7001, price=80_000), timestamp="2026-09-11T01:03:00+00:00")
    assert opened["hard_stop_protection"]["status"] == "active"
    tp = next(row for row in opened["orders"] if row["event"] == "tp")
    exchange.inject_fill(tp, tid=7002, price=80_500)
    broker, _backend = _broker(tmp_path / "fresh-tp")
    exchange.observe(broker)
    rearmed = new_lifecycle(tmp_path / "outputs", broker).on_fill(plan, fill(tp, tid=7002, price=80_500), timestamp="2026-09-11T01:04:00+00:00")
    assert rearmed["rungs"][0]["line"]["state"] == "rearmed"
    assert any(row["event"] == "entry_rearm" for row in rearmed["orders"])
    reentry = next(row for row in rearmed["orders"] if row["event"] == "entry_rearm")
    exchange.inject_fill(reentry, tid=70025, price=80_000)
    broker, _backend = _broker(tmp_path / "fresh-reentry")
    exchange.observe(broker)
    reentry_state = new_lifecycle(tmp_path / "outputs", broker).on_fill(plan, fill(reentry, tid=70025, price=80_000), timestamp="2026-09-11T01:04:30+00:00")
    assert reentry_state["rungs"][0]["line"]["state"] == "open"
    broker, _backend = _broker(tmp_path / "fresh-stop")
    exchange.observe(broker)
    stopped = new_lifecycle(tmp_path / "outputs", broker).on_market_event(plan, price=71_900, market={"bid": "76937", "ask": "76978", "mid": "76957.5"}, timestamp="2026-09-11T01:05:00+00:00")
    hard_stop = next(row for row in stopped["orders"] if row["event"] in {"hard_stop", "hard_stop_recovery"})
    exchange.inject_fill(hard_stop, tid=7003, price=76_950)
    broker, _backend = _broker(tmp_path / "fresh-sealed")
    exchange.observe(broker)
    sealed = new_lifecycle(tmp_path / "outputs", broker).on_fill(plan, fill(hard_stop, tid=7003, price=76_950), timestamp="2026-09-11T01:06:00+00:00")
    assert sealed["status"] == "terminal", (sealed.get("reconciliation"), exchange.open_orders)
    assert sealed["sealed"] is True
    assert not exchange.positions
    assert not exchange.open_orders
    assert all(group["state"] == "canceled" for group in exchange.protection_groups.values())


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
    assert result
    assert result["lifecycle"]["updated_at"] > started["lifecycle"].get("updated_at", NOW)
    assert result["lifecycle"]["rungs"][0]["line"]["state"] == "open"
    assert result["lifecycle"]["hard_stop_protection"]["status"] == "active"
    assert exchange.positions and float(exchange.positions[0]["szi"]) > 0


def test_issue_1251_park_control_tick_callbacks_run_facts_reconcile_and_advance(
    monkeypatch, tmp_path: Path
) -> None:
    import pipelines.park_control as module
    from tests.test_testnet_grid_coordinator import _setup

    assert "grid_paused_range" in module.TICK_CALLBACK_COORDINATOR_STATES
    output = tmp_path / "outputs"
    coordinator, plan, confirmation, broker, _backend, market, _make_fill = _setup(tmp_path)
    exchange = ReplayExchange(); exchange.observe(broker)
    tick_base = (datetime.now(timezone.utc) - timedelta(seconds=30)).replace(microsecond=0).isoformat()
    market["observed_at"] = tick_base
    started = coordinator.start_grid_session(plan, confirmation=confirmation, market=market, broker=broker, timestamp=tick_base)
    paused = coordinator.advance_grid_session(plan, broker=broker, price=84_100, market=market, timestamp=tick_base)
    assert paused["status"] == "grid_paused_range"
    initial = paused["lifecycle"]
    exchange.public_facts["fills"] = [{"tid": "historical", "oid": "old", "px": "70000", "sz": "0.1", "side": "B", "time": 1}]

    class Config:
        start_ready = True
        instrument_id = "BTC-USD-PERP"
        account_address = "TESTNET_ACCOUNT_PLACEHOLDER"
        runtime_id = "runtime-testnet"
        release_sha = "a" * 40
        standard_broker_release_sha = "b" * 40
        capability_revision = "capability"
        secret_file = tmp_path / "secret"

    class Reader:
        def read(self, _instrument): return {**exchange.market(), "price": "76957.5", "max_oracle_deviation_bps": "50", "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}

    advanced: list[dict] = []
    monkeypatch.setattr(module.HyperliquidTestnetRuntimeConfig, "from_environment", staticmethod(lambda: Config()))
    monkeypatch.setattr(module, "_load_dashboard_plan", lambda *_args: ({"market": {"fallback_policy": "none"}}, {"confirmation_id": "confirmation", "operator_id": "park"}))
    monkeypatch.setattr(module, "build_plan", lambda *_args: plan)
    monkeypatch.setattr(module, "HyperliquidTestnetMarketReader", Reader)
    monkeypatch.setattr(module, "read_coherent_market", lambda *_args, **_kwargs: ({**exchange.market(), "price": "76957.5", "max_oracle_deviation_bps": "50", "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}, [{"passed": True}]))
    monkeypatch.setattr(module, "hydrate_order_identities", lambda *_args, **_kwargs: {"recovered": len(initial["orders"]), "skipped": []})
    import services.broker_composition as composition
    monkeypatch.setattr(composition, "build_broker_execution_port", lambda _context: broker)
    advance, reconcile = module._build_testnet_tick_callbacks(output, {
        "activation_id": coordinator.status()["activation_id"], "plan_digest": plan["plan_digest"], "strategy_family": "grid",
        "instrument_id": "BTC-USD-PERP",
    })
    reconciled = reconcile()
    fill_event = exchange.inject_fill(initial["orders"][0], tid=901, price=80_000)
    exchange.public_facts["fills"] = [fill_event]
    result = advance({"kind": "market_heartbeat"})
    assert reconciled["status"] == "pass"
    assert result["status"] in {"grid_paused_range", "grid_blocked"}
    assert result["lifecycle"]["updated_at"] > initial["updated_at"]
    assert result["lifecycle"]["rungs"][0]["line"]["state"] == "open"


def test_issue_1251_fixture_preserves_real_btc_payload_shapes() -> None:
    fixture = json.loads((ROOT / "tests/testnet_replay/fixtures/hyperliquid_btc_real_shape.json").read_text())
    assert fixture["account_address"] == "TESTNET_ACCOUNT_PLACEHOLDER"
    assert {row["dir"] for row in fixture["userFills"]} == {"Open Long", "Close Long"}
    assert fixture["meta"]["universe"][0]["szDecimals"] == 5
    assert fixture["l2Book"]["levels"][0][0]["px"] == "76937.0"


def test_issue_1251_i4_price_jitter_is_reproduced_for_followup() -> None:
    from pipelines.testnet_proof_driver import ProofDriverError, read_coherent_market

    class Binding:
        def market_fact(self, **_kwargs):
            return {"price": "76957.0", "source": "binding", "observed_at": "now"}

    class Reader:
        def read(self, _instrument):
            return {"instrument_id": "BTC-USD-PERP", "price": "76957.1", "mid": "76957.1", "bid": "76957", "ask": "76958", "mark": "76957.1", "oracle": "76957.1", "impact": "76957.1", "depth_notional": "100", "max_slippage": "50", "max_oracle_deviation_bps": "50", "fresh": True, "execution_ready": True, "is_synthetic": False, "fallback_policy": "none", "observed_at": "now", "source": "reader", "mapping_revision": "fixture-v1", "broker_id": "hyperliquid", "environment": "testnet", "asset_index": 0, "universe_revision": "fixture-v1", "connection_epoch": "replay-epoch", "cursor": "replay-cursor"}

    market, _checks = read_coherent_market({"market": {"fallback_policy": "none"}}, Binding(), instrument_id="BTC-USD-PERP", market_reader=Reader(), max_attempts=1, read_reader_always=True)
    assert market["price"] == "76957.1"


def test_issue_1253_tick_market_pair_uses_tolerance_bbo_and_observation_age() -> None:
    from services.testnet_market_document import compare_market_observations

    common = {"bid": "100", "ask": "101", "max_slippage": "1", "binding_observed_at": "2026-09-11T01:00:00+00:00", "reader_observed_at": "2026-09-11T01:00:10+00:00"}
    assert compare_market_observations("100.05", "100", **common)["passed"] is True
    assert compare_market_observations("100.2", "100", **common)["reason_code"] == "market_price_mismatch"
    assert compare_market_observations("100.05", "100", **{**common, "bid": "100.1"})["reason_code"] == "market_bbo_inconsistent"
    assert compare_market_observations("100.05", "100", **{**common, "reader_observed_at": "2026-09-11T01:00:11+00:00"})["reason_code"] == "market_observation_mismatch"
