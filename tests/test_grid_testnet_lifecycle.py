from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from services.grid_testnet_lifecycle import GridTestnetLifecycle, GridTestnetLifecycleError


def _plan(*, direction: str = "long", lower: float = 63000.0, upper: float = 66000.0) -> dict:
    if direction == "neutral":
        rungs = [
            {"rung": 1, "price": 64000.0, "side": "buy", "take_profit": 65000.0, "hard_stop": lower, "quantity": 0.1},
            {"rung": 2, "price": 65000.0, "side": "sell", "take_profit": 64000.0, "hard_stop": upper, "quantity": 0.1},
        ]
    else:
        rungs = [
            {"rung": 1, "price": 65000.0, "side": "buy", "take_profit": 65500.0, "hard_stop": lower, "quantity": 0.1},
            {"rung": 2, "price": 64000.0, "side": "buy", "take_profit": 64500.0, "hard_stop": lower, "quantity": 0.1},
        ]
    return {
        "schema_version": "strategy-plan-v1",
        "strategy_type": "grid",
        "strategy_plan_id": f"grid-testnet-{direction}",
        "plan_digest": "sha256:" + "a" * 64,
        "version": 1,
        "cycle_id": "2026-08-22_DAY",
        "strategy_session_id": "session-grid-testnet",
        "strategy_revision_id": f"revision-grid-{direction}",
        "direction": direction,
        "instrument_id": "BTC-USD-PERP",
        "upper_price_boundary": upper,
        "lower_price_boundary": lower,
        "locked_at": "2026-08-22T01:00:00+00:00",
        "grid": {"rungs": rungs, "partial_entry_timeout_seconds": 300},
        "risk_budget": {
            "equity": 10000.0,
            "maximum_loss_at_full_depth": 1000.0,
            "leverage_limit": 10.0,
            "max_notional": 20000.0,
            "max_open_orders": 8,
            "max_open_positions": 2,
            "max_slippage": 50.0,
            "max_submit_retries": 3,
        },
    }


def _broker(tmp_path: Path, *, protection: bool = True):
    from tests.test_dca_testnet_lifecycle import _broker as build

    return build(tmp_path, protection=protection)


def _fill(order: dict, *, price: float, tid: int, quantity: float | None = None) -> dict:
    return {
        "coin": "BTC",
        "px": str(price),
        "sz": str(quantity if quantity is not None else order["quantity"]),
        "side": "B" if order["side"] == "buy" else "A",
        "time": 1787313661000 + tid,
        "oid": int(order["broker_order_id"]),
        "cloid": order["client_order_id"],
        "tid": tid,
    }


def test_grid_initial_ladder_is_complete_and_geometry_is_locked(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    started = lifecycle.start(_plan(), timestamp="2026-08-22T01:00:00+00:00")

    assert started["status"] == "active"
    assert len(started["orders"]) == 2
    assert all(row["state"] == "accepted" for row in started["orders"])
    assert {row["side"] for row in started["rungs"]} == {"buy"}
    assert started["events"][-1]["event"] == "ladder_activated"


def test_grid_order_row_persists_native_client_order_id_when_receipt_exposes_it(
    tmp_path: Path,
) -> None:
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", object())
    receipt = SimpleNamespace(
        state="resting",
        order_id="canonical-order-1",
        client_order_id="canonical-cloid-1",
        native_client_order_id="0x" + "5" * 32,
        broker_order_id="1001",
        account_address="testnet-account",
        release_sha="a" * 40,
    )

    row = lifecycle._order_row({"ticket_id": "grid-entry-1"}, receipt)

    assert row["native_client_order_id"] == "0x" + "5" * 32


def test_grid_order_row_accepts_legacy_receipt_without_native_client_order_id(
    tmp_path: Path,
) -> None:
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", object())
    receipt = SimpleNamespace(
        state="resting",
        order_id="canonical-order-1",
        client_order_id="canonical-cloid-1",
        broker_order_id="1001",
        account_address="testnet-account",
        release_sha="a" * 40,
    )

    row = lifecycle._order_row({"ticket_id": "grid-entry-1"}, receipt)

    assert "native_client_order_id" not in row


def test_grid_advance_applies_fill_then_market_event(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    order = started["orders"][0]

    advanced = lifecycle.advance(
        plan,
        fills=[_fill(order, price=65000.0, tid=1)],
        timestamp="2026-08-22T01:01:00+00:00",
    )

    assert advanced["fills"][0]["order_id"] == order["order_id"]
    assert advanced["hard_stop_protection"]["status"] == "active"


def test_grid_five_rung_ladder_has_unique_canonical_and_client_identities(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    plan = _plan()
    plan["grid"]["rungs"] = [
        {"rung": index, "price": 64000.0 + index * 100, "side": "buy",
         "take_profit": 64500.0 + index * 100, "hard_stop": 63000.0, "quantity": 0.1}
        for index in range(5)
    ]
    plan["risk_budget"].update(max_open_orders=5, max_open_positions=5)
    plan["risk_budget"]["max_notional"] = 40000.0
    plan["risk_budget"]["maximum_loss_at_full_depth"] = 3000.0

    started = GridTestnetLifecycle(tmp_path / "outputs", broker).start(
        plan, timestamp="2026-08-22T01:00:00+00:00"
    )

    assert started["status"] == "active"
    assert len(started["orders"]) == 5
    for field in ("order_id", "idempotency_key", "client_order_id"):
        values = [row[field] for row in started["orders"]]
        assert len(values) == len(set(values)) == 5


def test_quantized_grid_spacing_tp_and_sl_allow_one_tick(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    plan = _plan(lower=75000.0, upper=78212.0)
    plan["execution_context"] = {"price_tick": "1", "price_max_decimal_places": 0}
    plan["hard_stop"] = 68000.0
    plan["grid"]["rungs"] = [
        {"rung": index, "price": price, "side": "buy", "take_profit": tp,
         "hard_stop": 74358, "quantity": 0.1}
        for index, (price, tp) in enumerate(
            [(75000, 75642), (75642, 76285), (76285, 76927),
             (76927, 77569), (77569, 78212)], start=1
        )
    ]
    plan["risk_budget"].update(max_open_orders=5, max_open_positions=5,
                                max_notional=40000, maximum_loss_at_full_depth=3000)

    started = GridTestnetLifecycle(tmp_path / "outputs", broker).start(
        plan, timestamp="2026-09-08T13:00:00+00:00"
    )
    assert started["status"] == "active"


def test_quantized_grid_spacing_two_tick_deviation_is_rejected(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    plan = _plan()
    plan["execution_context"] = {"price_tick": "1", "price_max_decimal_places": 0}
    plan["grid"]["rungs"] = [
        {"rung": 1, "price": 64000, "side": "buy", "take_profit": 64500, "hard_stop": 63000, "quantity": 0.1},
        {"rung": 2, "price": 64642, "side": "buy", "take_profit": 65142, "hard_stop": 63000, "quantity": 0.1},
        {"rung": 3, "price": 65286, "side": "buy", "take_profit": 65786, "hard_stop": 63000, "quantity": 0.1},
        {"rung": 4, "price": 65928, "side": "buy", "take_profit": 66428, "hard_stop": 63000, "quantity": 0.1},
    ]
    with pytest.raises(GridTestnetLifecycleError, match="grid_spacing_inconsistent"):
        GridTestnetLifecycle(tmp_path / "outputs", broker).start(
            plan, timestamp="2026-09-08T13:00:00+00:00"
        )


def test_grid_trailing_up_is_optional_boolean_only(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    plan = _plan()
    plan["grid"]["trailing_up"] = "true"

    with pytest.raises(GridTestnetLifecycleError, match="grid_trailing_up_must_be_boolean"):
        GridTestnetLifecycle(tmp_path / "outputs", broker).start(
            plan, timestamp="2026-09-08T13:00:00+00:00"
        )


def test_grid_canonical_boundary_and_external_hard_stop_are_accepted(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=False)
    plan = _plan(lower=75000.0, upper=78531.5)
    plan["hard_stop"] = 72000.0
    plan["grid"]["rungs"] = [
        {"rung": 1, "price": 75000.0, "side": "buy", "take_profit": 75353.15, "hard_stop": 74646.85, "quantity": 0.1},
        {"rung": 2, "price": 75353.15, "side": "buy", "take_profit": 75706.3, "hard_stop": 74646.85, "quantity": 0.1},
    ]

    started = GridTestnetLifecycle(tmp_path / "outputs", broker).start(
        plan, timestamp="2026-09-08T13:00:00+00:00"
    )

    assert started["status"] == "blocked_protection"
    assert started["rungs"][0]["price"] == 75000.0


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda plan: plan["grid"]["rungs"][0].update(price=75001.0, hard_stop=75001.0), "grid_buy_geometry_invalid"),
        (lambda plan: plan["grid"]["rungs"][0].update(price=74999.0), "grid_rung_outside_boundary"),
    ],
)
def test_grid_canonical_geometry_still_rejects_invalid_rungs(tmp_path: Path, mutation, error: str) -> None:
    broker, _ = _broker(tmp_path)
    plan = _plan(lower=75000.0, upper=78531.5)
    plan["hard_stop"] = 72000.0
    plan["grid"]["rungs"][0]["hard_stop"] = 74646.85
    mutation(plan)

    with pytest.raises(GridTestnetLifecycleError, match=error):
        GridTestnetLifecycle(tmp_path / "outputs", broker).start(
            plan, timestamp="2026-09-08T13:00:00+00:00"
        )


def test_grid_incomplete_initial_ladder_rolls_back_and_never_activates(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    original_submit = broker.submit_order

    def always_fail(request):
        raise TimeoutError("fixture submit unavailable")

    broker.submit_order = always_fail
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    started = lifecycle.start(_plan(), timestamp="2026-08-22T01:00:00+00:00")

    assert started["status"] == "blocked_reconciliation"
    assert started["orders"] == []
    assert any(event["event"] == "ladder_incomplete" for event in started["events"])
    broker.submit_order = original_submit


def test_grid_local_submit_validation_is_blocked_without_identity_query(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    calls = []

    def reject_locally(request):
        calls.append("submit")
        raise ValueError("quantity 0.00012 is below minimum notional")

    def query(_key):
        calls.append("query")
        raise AssertionError("local validation must not query identity")

    broker.submit_order = reject_locally
    broker.query_by_idempotency_key = query

    state = GridTestnetLifecycle(tmp_path / "outputs", broker).start(
        _plan(), timestamp="2026-08-22T01:00:00+00:00"
    )

    assert state["status"] == "blocked_local_validation"
    assert "blocked_local_validation:ValueError" in state["blocker"]
    assert "price=65000.0" in state["blocker"]
    assert "quantity=0.1" in state["blocker"]
    assert calls == ["submit"]


def test_grid_identity_query_key_error_keeps_missing_key_reason(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)

    def timeout(request):
        raise TimeoutError("transport timeout")

    def broken_query(_key):
        raise KeyError("state")

    broker.submit_order = timeout
    broker.query_by_idempotency_key = broken_query
    state = GridTestnetLifecycle(tmp_path / "outputs", broker).start(
        _plan(), timestamp="2026-08-22T01:00:00+00:00"
    )

    assert state["status"] == "blocked_reconciliation"
    assert "submit_unknown_query_failed:KeyError:missing_key=state" in state["blocker"]


def test_grid_entry_tp_and_original_price_rearm(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    entry = started["orders"][0]
    opened = lifecycle.on_fill(plan, _fill(entry, price=65000.0, tid=1), timestamp="2026-08-22T01:01:00+00:00")
    tp = next(row for row in opened["orders"] if row["event"] == "tp")
    assert tp["trigger_price"] == 65500.0
    closed = lifecycle.on_fill(plan, _fill(tp, price=65500.0, tid=2), timestamp="2026-08-22T01:02:00+00:00")

    assert closed["status"] == "active"
    assert closed["rungs"][0]["line"]["state"] == "rearmed"
    rearm = next(row for row in closed["orders"] if row["event"] == "entry_rearm")
    assert rearm["price"] == 65000.0
    assert rearm["idempotency_key"] != entry["idempotency_key"]


def test_blocked_local_fill_retries_without_cancelling_and_recovers_protection(tmp_path: Path) -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "issue_1232_fill_recovery.json").read_text())
    broker, backend = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    plan["lower_price_boundary"] = 73000.0
    plan["upper_price_boundary"] = 79000.0
    plan["grid"]["rungs"] = [
        {"rung": index, "price": price, "side": "buy",
         "take_profit": price + 500, "hard_stop": 73000.0,
         "quantity": 0.00024}
        for index, price in enumerate((74000.0, 74918.0, 75837.0, 76755.0, 77673.0), start=1)
    ]
    plan["risk_budget"].update(max_open_orders=8, max_open_positions=5, max_notional=40000.0)
    lifecycle.start(plan, timestamp="2026-09-10T11:00:00+00:00")
    state = lifecycle._state(plan)
    target = state["rungs"][4]
    target_order = state["orders"][4]
    for row in state["orders"]:
        row["state"] = "cancelled"
    target_order["state"] = "filled"
    state.update(status="blocked_reconciliation", blocker="grid_entry_fill_state_error:ValueError:grid line fill_id is required")
    lifecycle._save(state)
    backend.account_positions = [{"position": fixture["position"]}]
    original_request = broker.request
    broker.request = lambda port, operation, payload=None: (
        SimpleNamespace(positions=(fixture["position"],))
        if port == "account" and operation == "read"
        else original_request(port, operation, payload)
    )

    raw_fill = {
        "tid": fixture["fill"]["fill_id"], "hash": fixture["fill"]["hash"],
        "oid": int(target_order["broker_order_id"]), "cloid": target_order["client_order_id"],
        "px": fixture["fill"]["price"], "sz": fixture["fill"]["quantity"],
        "side": "B", "time": 1789038505000, "coin": "BTC",
        "order_id": target_order["order_id"],
    }
    retried = lifecycle.on_fill(plan, raw_fill, timestamp="2026-09-10T11:08:25+00:00")
    recovered = lifecycle.on_market_event(plan, price=fixture["market_price"], timestamp="2026-09-10T11:08:26+00:00")

    assert retried["rungs"][4]["line"]["state"] == "open"
    assert recovered["status"] == "active", recovered.get("events", [])[-1].get("reason")
    assert recovered["rungs"][4]["line"]["entry_filled_quantity"] == pytest.approx(0.00024)
    assert any(row["event"] == "tp" and row["rung_id"] == target["rung_id"] for row in recovered["orders"])
    assert recovered["hard_stop_protection"]["status"] == "active"
    assert sum(row["state"] == "accepted" and row["event"] == "entry_rearm" for row in recovered["orders"]) == 4
    assert not any(event["event"] == "order_cancel_attempt" for event in recovered["events"])

    fixture["position"]["szi"] = "0.00025"
    recovered_state = lifecycle._state(plan)
    recovered_state.update(status="blocked_reconciliation", blocker="grid_entry_fill_state_error:ValueError:grid line fill_id is required")
    blocked = lifecycle.on_market_event(plan, price=fixture["market_price"], timestamp="2026-09-10T11:08:27+00:00")
    assert blocked["status"] == "blocked_reconciliation"
    assert blocked["blocker"] == "position_open_unprotected"


def test_blocked_local_fill_recovers_from_public_facts_and_backfills_missing_fill_id(
    tmp_path: Path,
) -> None:
    facts_fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "issue_1234_facts.json").read_text()
    )
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    plan["lower_price_boundary"] = 73000.0
    plan["upper_price_boundary"] = 79000.0
    plan["grid"]["rungs"] = [
        {"rung": index, "price": price, "side": "buy", "take_profit": price + 500,
         "hard_stop": 73000.0, "quantity": 0.00024}
        for index, price in enumerate((74000.0, 74918.0, 75837.0, 76755.0, 77673.0), start=1)
    ]
    plan["risk_budget"].update(max_open_orders=8, max_open_positions=5, max_notional=40000.0)
    lifecycle.start(plan, timestamp="2026-09-10T11:00:00+00:00")
    state = lifecycle._state(plan)
    target = state["rungs"][4]
    target_order = state["orders"][4]
    target_order["broker_order_id"] = facts_fixture["fills"][0]["oid"]
    target_order["state"] = "filled"
    for row in state["orders"][:4]:
        row["state"] = "cancelled"
    state["fills"] = [{
        "fill_id": "",
        "fill_identities": [],
        "order_id": target_order["order_id"],
        "event": "entry",
        "side": "buy",
        "rung_id": target["rung_id"],
        "quantity": 0.00024,
    }]
    state.update(status="blocked_reconciliation", blocker="grid_entry_fill_state_error:ValueError:grid line fill_id is required")
    lifecycle._save(state)
    broker.read_public_facts = lambda **_kwargs: facts_fixture

    recovered = lifecycle.on_market_event(
        plan, price=facts_fixture["market_price"], timestamp="2026-09-10T11:08:26+00:00"
    )

    assert recovered["rungs"][4]["line"]["state"] == "open"
    assert recovered["fills"][0]["fill_id"] == "219949055209235"
    assert recovered["fills"][0]["fill_identities"] == ["219949055209235", "hash-1234-redacted"]
    assert recovered["status"] == "active"
    assert any(row["event"] == "tp" and row["rung_id"] == target["rung_id"] for row in recovered["orders"])
    assert recovered["hard_stop_protection"]["status"] == "active"
    assert sum(row["state"] == "accepted" and row["event"] == "entry_rearm" for row in recovered["orders"]) == 4
    assert any(event["event"] == "blocked_position_recovered" for event in recovered["events"])


def test_online_dashboard_state_shape_recovers_all_orders_and_backfills_fill_id(tmp_path: Path) -> None:
    state_fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "issue_1236_dashboard_state.json").read_text()
    )
    facts_fixture = {
        "status": "pass",
        "fills": [{"oid": 59671766069, "tid": "219949055209235", "hash": "hash-1236-redacted"}],
        "positions": [{"instrument_id": "BTC-USD-PERP", "signed_quantity": "0.00024"}],
        "open_orders": [],
    }
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    plan["lower_price_boundary"] = 73000.0
    plan["upper_price_boundary"] = 79000.0
    plan["grid"]["rungs"] = [
        {"rung": index, "price": price, "side": "buy", "take_profit": price + 500,
         "hard_stop": 73000.0, "quantity": quantity}
        for index, (price, quantity) in enumerate(
            ((74000.0, 0.00025), (74918.0, 0.00025), (75837.0, 0.00025),
             (76755.0, 0.00024), (77673.0, 0.00024)), start=1
        )
    ]
    plan["risk_budget"].update(max_open_orders=8, max_open_positions=5, max_notional=40000.0)
    lifecycle.start(plan, timestamp="2026-09-10T11:00:00+00:00")
    state = lifecycle._state(plan)
    original_orders = [dict(order) for order in state["orders"]]
    state["orders"] = [
        {**original, "broker_order_id": fixture_order["broker_order_id"],
         "client_order_id": original["client_order_id"], "state": fixture_order["state"]}
        for original, fixture_order in zip(original_orders, state_fixture["orders"])
    ]
    fixture_fill = dict(state_fixture["fills"][0])
    fixture_fill.update({"order_id": original_orders[4]["order_id"], "rung_id": state["rungs"][4]["rung_id"]})
    state["fills"] = [fixture_fill]
    state.update(status="blocked_reconciliation", blocker="position_open_unprotected")
    lifecycle._save(state)
    from pipelines.park_control import hydrate_order_identities
    recovered_orders = []
    broker.recover = lambda request, **kwargs: recovered_orders.append((request, kwargs))
    hydration = hydrate_order_identities(broker, state)
    assert hydration == {"recovered": 5, "skipped": []}
    assert [row[1]["state"] for row in recovered_orders] == ["canceled"] * 4 + ["filled"]
    lifecycle._backfill_fact_fills(state, facts_fixture["fills"])
    assert state["fills"][0]["fill_id"] == "219949055209235"
    broker.read_public_facts = lambda **_kwargs: facts_fixture

    recovered = lifecycle.on_market_event(plan, price=77673.0, timestamp="2026-09-10T11:08:26+00:00")

    assert recovered["fills"][0]["fill_id"] == "219949055209235"
    assert recovered["fills"][0]["fill_identities"] == ["219949055209235", "hash-1236-redacted"]
    assert recovered["status"] == "active"


def test_grid_facts_failure_keeps_blocked_without_cancellation_or_terminal_close(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-09-10T11:00:00+00:00")
    state = lifecycle._state(plan)
    state.update(status="blocked_reconciliation", blocker="grid_entry_fill_state_error:ValueError:grid line fill_id is required")
    lifecycle._save(state)
    broker.read_public_facts = lambda **_kwargs: {"open_orders": [], "fills": []}

    blocked = lifecycle.on_market_event(plan, price=65000.0, timestamp="2026-09-10T11:01:00+00:00")

    assert blocked["status"] == "blocked_reconciliation"
    assert blocked["blocker"] == "position_open_unprotected"
    assert not any(event["event"] == "order_cancel_attempt" for event in blocked["events"])


def test_grid_terminal_reconciliation_blocks_when_facts_show_position(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    broker.read_public_facts = lambda **_kwargs: {
        "open_orders": [],
        "positions": [{"instrument_id": "BTC-USD-PERP", "signed_quantity": "0.00024"}],
    }

    result = lifecycle._terminal_reconciliation({"instrument_id": "BTC-USD-PERP", "orders": [], "fills": [], "rungs": []}, "2026-09-10T11:01:00+00:00")

    assert result["status"] == "blocked"
    assert result["broker_position_count"] == 1


def test_grid_partial_entry_deadline_sets_tp_to_authoritative_fill_quantity(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    entry = started["orders"][0]
    lifecycle.on_fill(plan, _fill(entry, price=65000.0, tid=3, quantity=0.04), timestamp="2026-08-22T01:01:00+00:00")
    deadline = lifecycle.on_market_event(plan, price=64900.0, timestamp="2026-08-22T01:06:00+00:00")

    tp = next(row for row in deadline["orders"] if row["event"] == "tp")
    assert tp["quantity"] == pytest.approx(0.04)
    assert deadline["rungs"][0]["line"]["state"] == "open"
    assert any(event["event"] == "partial_entry_deadline" for event in deadline["events"])


def test_grid_late_entry_fill_after_deadline_is_reconciled_into_tp(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    entry = started["orders"][0]
    lifecycle.on_fill(plan, _fill(entry, price=65000.0, tid=8, quantity=0.04), timestamp="2026-08-22T01:01:00+00:00")
    lifecycle.on_market_event(plan, price=64900.0, timestamp="2026-08-22T01:06:00+00:00")
    late = lifecycle.on_fill(plan, _fill(entry, price=65000.0, tid=9, quantity=0.02), timestamp="2026-08-22T01:06:01+00:00")

    tp = [row for row in late["orders"] if row["event"] == "tp"][-1]
    assert tp["quantity"] == pytest.approx(0.06)
    assert any(event["event"] == "late_entry_fill_reconciled" for event in late["rungs"][0]["line"]["transitions"])


def test_grid_crossed_unfilled_rung_is_cancelled_and_skipped(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")

    skipped = lifecycle.on_market_event(plan, price=63900.0, timestamp="2026-08-22T01:01:00+00:00")

    rung = skipped["rungs"][1]
    assert rung["missed"] is True
    assert rung["line"]["state"] == "cancelled"
    assert any(event["event"] == "rung_missed_skipped" for event in skipped["events"])


def test_long_grid_upper_boundary_pauses_and_reenters_without_changing_orders(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    before = [(row["order_id"], row["state"]) for row in started["orders"]]

    paused = lifecycle.on_market_event(plan, price=66001.0, timestamp="2026-08-22T01:01:00+00:00")
    assert paused["status"] == "paused_above_range"
    assert [(row["order_id"], row["state"]) for row in paused["orders"]] == before
    assert paused["events"][-1]["event"] == "range_exit_upper"

    reentered = lifecycle.on_market_event(plan, price=65999.0, timestamp="2026-08-22T01:02:00+00:00")
    assert reentered["status"] == "active"
    assert reentered["events"][-1]["event"] == "range_reenter"


def test_paused_long_grid_records_tp_and_defers_rearm_until_reentry(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(plan, _fill(started["orders"][0], price=65000.0, tid=101), timestamp="2026-08-22T01:01:00+00:00")
    paused = lifecycle.on_market_event(plan, price=66001.0, timestamp="2026-08-22T01:02:00+00:00")
    tp = next(row for row in paused["orders"] if row["event"] == "tp")

    closed = lifecycle.on_fill(plan, _fill(tp, price=65500.0, tid=102), timestamp="2026-08-22T01:03:00+00:00")
    assert closed["status"] == "paused_above_range"
    assert closed["rungs"][0]["line"]["state"] == "rearmed"
    assert not any(row["event"] == "entry_rearm" for row in closed["orders"])

    resumed = lifecycle.on_market_event(plan, price=65999.0, timestamp="2026-08-22T01:04:00+00:00")
    assert resumed["status"] == "active"
    assert any(row["event"] == "entry_rearm" for row in resumed["orders"])


def test_grid_hard_stop_cancels_tp_and_flattens_before_sealing(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(plan, _fill(started["orders"][0], price=65000.0, tid=4), timestamp="2026-08-22T01:01:00+00:00")
    triggered = lifecycle.on_market_event(plan, price=63000.0, timestamp="2026-08-22T01:02:00+00:00")

    assert triggered["status"] == "hard_stop_triggered"
    assert any(row["event"] == "hard_stop" and row["reduce_only"] for row in triggered["orders"])
    assert all(row["state"] != "accepted" for row in triggered["orders"] if row["event"] == "tp")
    hard_stop = next(row for row in triggered["orders"] if row["event"] == "hard_stop")
    terminal = lifecycle.on_fill(plan, _fill(hard_stop, price=63000.0, tid=5), timestamp="2026-08-22T01:03:00+00:00")

    assert terminal["status"] == "terminal"
    assert terminal["sealed"] is True
    assert terminal["reconciliation"]["status"] == "ok"
    assert terminal["park_notification"]["status"] == "queued"


def test_grid_hard_stop_retries_failed_cancels_on_next_tick_and_closes_exposure_fact(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    original_cancel = broker.cancel_order
    failures = {"enabled": True}

    def cancel(request):
        if failures["enabled"]:
            raise KeyError(f"unknown Hyperliquid order identity: {request.broker_order_id}")
        return original_cancel(request)

    broker.cancel_order = cancel
    blocked = lifecycle.on_market_event(plan, price=63000.0, timestamp="2026-08-22T01:01:00+00:00")

    assert blocked["status"] == "blocked_reconciliation"
    assert blocked["exchange_exposure_open"]["status"] == "open"
    assert len(blocked["exchange_exposure_open"]["order_ids"]) == 2
    assert sum(event["event"] == "order_cancel_attempt" for event in blocked["events"]) == 2

    failures["enabled"] = False
    recovered = lifecycle.on_market_event(plan, price=63100.0, timestamp="2026-08-22T01:02:00+00:00")

    assert recovered["status"] == "terminal"
    assert "exchange_exposure_open" not in recovered
    assert any(event["event"] == "exchange_exposure_closed" for event in recovered["events"])
    assert sum(event["event"] == "order_cancel_attempt" for event in recovered["events"]) == 4


def test_blocked_dashboard_fixture_discovers_and_retries_all_plan_orders(tmp_path: Path) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "grid_blocked_exposure_retry.json").read_text()
    )
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    plan["grid"]["rungs"] = [
        {"rung": index, "price": 64000.0 + index * 100, "side": "buy",
         "take_profit": 64500.0 + index * 100, "hard_stop": 63000.0, "quantity": 0.1}
        for index in range(5)
    ]
    plan["risk_budget"].update(max_open_orders=5, max_open_positions=5,
                                max_notional=40000.0, maximum_loss_at_full_depth=3000.0)
    lifecycle.start(plan, timestamp="2026-09-08T13:47:21+00:00")
    state = lifecycle._state(plan)
    for row, fixture_order in zip(state["orders"], fixture["orders"]):
        row["broker_order_id"] = fixture_order["broker_order_id"]
        row["client_order_id"] = fixture_order["client_order_id"]
    state["status"] = fixture["status"]
    state["blocker"] = fixture["blocker"]
    state.pop("exchange_exposure_open", None)
    lifecycle._save(state)

    exchange_open = True
    original_request = broker.request
    broker.request = lambda port, operation, payload=None: (
        fixture["open_orders"] if operation == "open_orders" and exchange_open
        else [] if operation == "open_orders"
        else original_request(port, operation, payload)
    )

    def cancel(_request):
        nonlocal exchange_open
        exchange_open = False
        return SimpleNamespace(state="cancelled")

    broker.cancel_order = cancel
    recovered = lifecycle.on_market_event(plan, price=78488.5, timestamp="2026-09-08T14:51:00+00:00")

    assert recovered["status"] == "terminal"
    assert recovered["sealed"] is True
    assert recovered["park_notification_required"] is True
    assert all(row["state"] == "cancelled" for row in recovered["orders"])
    assert recovered["reconciliation"]["broker_open_order_count"] == 0


def test_blocked_dashboard_state_reconciles_broker_absent_unfilled_rows(tmp_path: Path) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "grid_broker_absent_reconcile.json").read_text()
    )
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    plan["grid"]["rungs"] = [
        {"rung": index, "price": 64000.0 + index * 100, "side": "buy",
         "take_profit": 64500.0 + index * 100, "hard_stop": 63000.0, "quantity": 0.1}
        for index in range(5)
    ]
    plan["risk_budget"].update(max_open_orders=5, max_open_positions=5,
                                max_notional=40000.0, maximum_loss_at_full_depth=3000.0)
    lifecycle.start(plan, timestamp="2026-09-08T13:47:21+00:00")
    state = lifecycle._state(plan)
    for row, fixture_order in zip(state["orders"], fixture["orders"]):
        row["broker_order_id"] = fixture_order["broker_order_id"]
        row["client_order_id"] = fixture_order["client_order_id"]
        row["state"] = fixture_order["state"]
    state.update(status=fixture["status"], blocker=fixture["blocker"], hard_stop_requested=True)
    lifecycle._save(state)

    recovered = lifecycle.on_market_event(plan, price=78488.5, timestamp="2026-09-08T16:05:00+00:00")

    assert all(row["state"] == "cancelled" for row in recovered["orders"])
    assert all(row["cancel_reason"] == "broker_absent_reconciled" for row in recovered["orders"])
    assert recovered["status"] == "terminal"
    assert recovered["sealed"] is True
    assert recovered["park_notification_required"] is True
    assert recovered["reconciliation"]["local_open_order_count"] == 0
    assert recovered["reconciliation"]["broker_absent_reconciled_count"] == 5
    assert sum(event["event"] == "broker_absent_reconciled" for event in recovered["events"]) == 5


@pytest.mark.parametrize("account_positions, add_fill", [([{"signed_quantity": "0.1"}], False), ([], True)])
def test_broker_absent_row_with_position_or_fill_remains_blocked(
    tmp_path: Path, account_positions: list[dict], add_fill: bool
) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-09-08T13:47:21+00:00")
    state = lifecycle._state(plan)
    state.update(status="blocked_reconciliation", blocker="hard_stop_reconciliation_blocked", hard_stop_requested=True)
    if add_fill:
        state["fills"] = [{"order_id": state["orders"][0]["order_id"], "fill_identities": ["fill-1"]}]
    original_request = broker.request
    broker.request = lambda port, operation, payload=None: (
        [] if port == "order_execution" and operation == "open_orders"
        else SimpleNamespace(
            positions=tuple(SimpleNamespace(signed_quantity=row["signed_quantity"]) for row in account_positions)
        ) if port == "account" and operation == "read"
        else original_request(port, operation, payload)
    )
    lifecycle._save(state)

    blocked = lifecycle.on_market_event(plan, price=78488.5, timestamp="2026-09-08T16:05:00+00:00")

    assert blocked["status"] == "blocked_reconciliation"
    assert blocked["blocker"] == "hard_stop_reconciliation_blocked"
    assert blocked["reconciliation"]["reason"] == "local_row_missing_but_position_open"
    assert any(row["state"] == "accepted" for row in blocked["orders"])


def test_broker_truth_query_failure_remains_blocked(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-09-08T13:47:21+00:00")
    state = lifecycle._state(plan)
    state.update(status="blocked_reconciliation", blocker="hard_stop_reconciliation_blocked", hard_stop_requested=True)
    lifecycle._save(state)

    def unavailable(*_args, **_kwargs):
        raise TimeoutError("broker unavailable")

    broker.request = unavailable
    blocked = lifecycle.on_market_event(plan, price=78488.5, timestamp="2026-09-08T16:05:00+00:00")

    assert blocked["status"] == "blocked_reconciliation"
    assert blocked["blocker"] == "hard_stop_reconciliation_blocked"
    assert blocked["reconciliation"]["reason"] == "broker_truth_query_failed:TimeoutError:broker unavailable"
    assert all(row["state"] == "accepted" for row in blocked["orders"])


def test_blocked_interrupt_still_cancels_resting_entries(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-09-08T13:47:21+00:00")
    state = lifecycle._state(plan)
    state["status"] = "blocked_reconciliation"
    state["blocker"] = "order_cancel_failed:previous_failure"
    lifecycle._save(state)

    cancelled = []
    broker.cancel_order = lambda request: cancelled.append(request) or SimpleNamespace(state="cancelled")
    recovered = lifecycle.interrupt(plan, timestamp="2026-09-08T14:51:00+00:00")

    assert len(cancelled) == len(started["orders"])
    assert recovered["status"] == "blocked_reconciliation"


def test_grid_hard_stop_recovery_fill_is_consumable_after_primary_submit_failure(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    original_submit = broker.submit_order
    failed = {"value": 0}

    def fail_primary(request):
        if request.ticket.get("event") == "hard_stop" and failed["value"] < 3:
            failed["value"] += 1
            raise TimeoutError("primary hard stop unavailable")
        return original_submit(request)

    broker.submit_order = fail_primary
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(plan, _fill(started["orders"][0], price=65000.0, tid=74), timestamp="2026-08-22T01:01:00+00:00")
    triggered = lifecycle.on_market_event(plan, price=63000.0, timestamp="2026-08-22T01:02:00+00:00")
    recovery = next(row for row in triggered["orders"] if row["event"] == "hard_stop_recovery")
    terminal = lifecycle.on_fill(plan, _fill(recovery, price=63000.0, tid=75), timestamp="2026-08-22T01:03:00+00:00")

    assert terminal["status"] == "terminal"
    assert terminal["sealed"] is True


def test_grid_late_cancelled_unfilled_rung_after_hard_stop_is_flattened(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(plan, _fill(started["orders"][0], price=65000.0, tid=77), timestamp="2026-08-22T01:00:30+00:00")
    lifecycle.on_market_event(plan, price=63000.0, timestamp="2026-08-22T01:01:00+00:00")
    late_entry = started["orders"][1]
    late = lifecycle.on_fill(plan, _fill(late_entry, price=64000.0, tid=76), timestamp="2026-08-22T01:02:00+00:00")

    assert late["status"] == "hard_stop_triggered"
    assert any(row["event"] == "hard_stop" for row in late["orders"])
    assert not any(row["event"] == "tp" and row["state"] == "accepted" for row in late["orders"])


def test_grid_post_terminal_late_entry_reopens_only_for_hard_stop_recovery(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    sealed = lifecycle.on_market_event(plan, price=63000.0, timestamp="2026-08-22T01:01:00+00:00")
    late = lifecycle.on_fill(plan, _fill(started["orders"][1], price=64000.0, tid=78), timestamp="2026-08-22T01:02:00+00:00")

    assert sealed["sealed"] is True
    assert late["status"] == "hard_stop_triggered"
    assert late["post_terminal_late_fill"] is True
    assert any(row["event"] == "hard_stop" for row in late["orders"])


def test_grid_neutral_keeps_both_entry_legs_and_hard_stop_is_net_covered(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan(direction="neutral")
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")

    assert {row["side"] for row in started["rungs"]} == {"buy", "sell"}
    buy = next(row for row in started["orders"] if row["side"] == "buy")
    opened = lifecycle.on_fill(plan, _fill(buy, price=64000.0, tid=6), timestamp="2026-08-22T01:01:00+00:00")
    assert opened["hard_stop_protection"]["quantity"] == pytest.approx(0.1)
    assert opened["hard_stop_protection"]["reduce_only"] is True


def test_grid_replay_alias_does_not_double_position(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    entry = started["orders"][0]
    raw = _fill(entry, price=65000.0, tid=7)
    raw["hash"] = "grid-fill-7"
    first = lifecycle.on_fill(plan, {key: value for key, value in raw.items() if key != "tid"}, timestamp="2026-08-22T01:01:00+00:00")
    replay = lifecycle.on_fill(plan, raw, timestamp="2026-08-22T01:02:00+00:00")

    assert len(replay["fills"]) == 1
    assert replay["rungs"][0]["line"]["entry_filled_quantity"] == first["rungs"][0]["line"]["entry_filled_quantity"]


def test_grid_revision_change_is_rejected(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    lifecycle = GridTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    changed = {**plan, "plan_digest": "sha256:" + "h" * 64}

    with pytest.raises(GridTestnetLifecycleError, match="strategy_revision_mismatch"):
        lifecycle.on_market_event(changed, price=64000.0, timestamp="2026-08-22T01:01:00+00:00")
