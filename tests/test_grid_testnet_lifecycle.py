from pathlib import Path

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
