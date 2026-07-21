from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.dualtrack_config import dualtrack_config
from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get(
    "TRADING_ORCHESTRATOR_NAUTILUS_TEST_PYTHON",
    "/Users/wendy/.local/share/trading-orchestrator/nautilus-1.230.0/bin/python",
))
PREFLIGHT = ROOT / "outputs" / "dualtrack" / "nautilus" / "instrument_preflight.json"


pytestmark = pytest.mark.skipif(
    not RUNTIME.exists() or not PREFLIGHT.exists(),
    reason="isolated Nautilus paper runtime evidence is not installed",
)


def _event(index: int, *, price: float, low: float, high: float) -> dict:
    minute = index + 1
    return {
        "event_id": f"runtime-event-{index}",
        "cycle_id": "2026-07-10_DAY",
        "ts_event": f"2026-07-10T01:{minute:02d}:00+00:00",
        "event_started_at": f"2026-07-10T01:{minute:02d}:00+00:00",
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
        "symbol": "GOLD",
        "timeframe": "1m",
        "open": price,
        "high": high,
        "low": low,
        "close": price,
        "price": price,
        "fresh": True,
        "is_synthetic": False,
    }


def _ohlc_event(
    index: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> dict:
    event = _event(index, price=close, low=low, high=high)
    event["open"] = open_price
    event["close"] = close
    return event


def test_real_runtime_fills_every_grid_level_crossed_by_completed_bar(tmp_path: Path) -> None:
    adapter = NautilusExecutionAdapter(
        tmp_path / "outputs",
        nautilus_python=RUNTIME,
        preflight_path=PREFLIGHT,
        defer_replay=True,
    )
    cycle_id = "2026-07-10_DAY"
    levels = [
        (3989.44, 3981.92),
        (3996.96, 3989.44),
        (4004.49, 3996.96),
    ]
    for index, (price, tp) in enumerate(levels):
        adapter.submit_order({
            "cycle_id": cycle_id,
            "ts": "2026-07-10T01:00:10+00:00",
            "side": "sell",
            "event": "entry",
            "order_type": "limit",
            "price": price,
            "quantity": 0.369,
            "sl": 4170.0,
            "tp": tp,
            "source_fill_id": f"runtime-grid-cross-{index}",
            "strategy_plan_id": "plan-runtime-grid-cross",
            "strategy_plan_version": 1,
        })
    adapter.process_market_event(_ohlc_event(
        0,
        open_price=3966.87,
        high=3966.87,
        low=3966.87,
        close=3966.87,
    ))
    adapter.process_market_event(_ohlc_event(
        1,
        open_price=3988.14,
        high=3996.65,
        low=3988.13,
        close=3996.65,
    ))
    adapter.process_market_event(_ohlc_event(
        2,
        open_price=3996.66,
        high=4008.24,
        low=3996.0,
        close=3999.05,
    ))
    adapter.flush(cycle_id)

    snapshot = adapter.snapshot(cycle_id)
    entry_fills = [row for row in snapshot["fills"] if row["event"] == "entry"]

    assert [row["price"] for row in entry_fills] == [3989.44, 3996.96, 4004.49]
    assert len([row for row in snapshot["positions"] if row["status"] == "open"]) == 3
    assert not [
        row
        for row in snapshot["orders"]
        if row["event"] == "entry" and row["state"] == "accepted" and row["price"] <= 4004.49
    ]


def test_real_runtime_fills_large_same_side_orders_at_one_crossed_level(tmp_path: Path) -> None:
    config = dualtrack_config()
    config["capital_per_track_usd"] = 1_000_000
    adapter = NautilusExecutionAdapter(
        tmp_path / "outputs",
        nautilus_python=RUNTIME,
        preflight_path=PREFLIGHT,
        defer_replay=True,
        config=config,
    )
    cycle_id = "2026-07-10_DAY"
    for index in range(2):
        adapter.submit_order({
            "cycle_id": cycle_id,
            "ts": "2026-07-10T01:00:10+00:00",
            "side": "sell",
            "event": "entry",
            "order_type": "limit",
            "price": 4000.0,
            "quantity": 150.0,
            "source_fill_id": f"runtime-same-level-{index}",
            "strategy_plan_id": "plan-runtime-same-level",
            "strategy_plan_version": 1,
        })
    adapter.process_market_event(_ohlc_event(
        0,
        open_price=3990.0,
        high=3990.0,
        low=3990.0,
        close=3990.0,
    ))
    adapter.process_market_event(_ohlc_event(
        1,
        open_price=3995.0,
        high=4005.0,
        low=3995.0,
        close=4001.0,
    ))
    adapter.flush(cycle_id)

    snapshot = adapter.snapshot(cycle_id)
    entry_fills = [row for row in snapshot["fills"] if row["event"] == "entry"]
    quantities_by_order: dict[str, float] = {}
    for fill in entry_fills:
        order_id = str(fill["order_id"])
        quantities_by_order[order_id] = quantities_by_order.get(order_id, 0.0) + float(fill["quantity"])

    assert len(quantities_by_order) == 2
    assert set(quantities_by_order.values()) == {150.0}
    assert {row["price"] for row in entry_fills} == {4000.0}
    assert not [row for row in snapshot["orders"] if row["state"] == "accepted"]


def test_real_runtime_cancels_pending_order_and_flattens_exact_hedged_position(tmp_path: Path) -> None:
    adapter = NautilusExecutionAdapter(
        tmp_path / "outputs",
        nautilus_python=RUNTIME,
        preflight_path=PREFLIGHT,
        defer_replay=True,
    )
    cycle_id = "2026-07-10_DAY"
    filled = adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-10T01:00:10+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "quantity": 1.0,
        "sl": 80.0,
        "tp": 110.0,
        "source_fill_id": "runtime-entry-filled",
        "authoritative_order_id": "legacy-filled",
        "strategy_plan_id": "plan-runtime",
        "strategy_plan_version": 1,
    })
    pending = adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-10T01:00:20+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 90.0,
        "quantity": 1.0,
        "sl": 85.0,
        "tp": 95.0,
        "source_fill_id": "runtime-entry-cancelled",
        "authoritative_order_id": "legacy-pending",
        "strategy_plan_id": "plan-runtime",
        "strategy_plan_version": 1,
    })
    adapter.cancel_orders(
        cycle_id,
        order_ids=["legacy-pending"],
        ts="2026-07-10T01:00:30+00:00",
        reason="runtime-test",
    )
    adapter.process_market_event(_event(0, price=101.0, low=99.0, high=102.0))
    adapter.process_market_event(_event(1, price=102.0, low=89.0, high=103.0))
    adapter.flush(cycle_id)

    first = adapter.snapshot(cycle_id)
    assert filled["state"] == pending["state"] == "accepted"
    assert {row["order_id"]: row["state"] for row in first["orders"]}[pending["order_id"]] == "canceled"
    assert len(first["fills"]) == 1
    assert len([row for row in first["positions"] if row["status"] == "open"]) == 1

    position = next(row for row in first["positions"] if row["status"] == "open")
    adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-10T01:03:00+00:00",
        "side": "sell",
        "event": "exit",
        "order_type": "market",
        "price": 102.0,
        "quantity": 0.4,
        "trade_id": position["trade_id"],
        "position_id": position["position_id"],
        "source_fill_id": "runtime-partial-reduction",
        "strategy_plan_id": "plan-runtime",
        "strategy_plan_version": 1,
    })
    adapter.process_market_event(_event(2, price=102.0, low=101.0, high=103.0))
    adapter.flush(cycle_id)

    reduced = adapter.snapshot(cycle_id)
    position = next(row for row in reduced["positions"] if row["status"] == "open")
    assert position["remaining_units"] == pytest.approx(0.6)
    assert reduced["account"]["starting_cash"] == 10_000.0
    assert reduced["account"]["margin"] == pytest.approx(reduced["account"]["exposure"] / 10.0)
    assert reduced["account"]["fees"] == pytest.approx(sum(row["cost"] for row in reduced["fills"]))
    assert reduced["account"]["equity"] == pytest.approx(
        reduced["account"]["ending_cash"] + reduced["pnl"]["unrealized"]
    )

    adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-10T01:04:00+00:00",
        "side": "sell",
        "event": "flatten",
        "order_type": "market",
        "price": 102.0,
        "trade_id": position["trade_id"],
        "position_id": position["position_id"],
        "source_fill_id": "runtime-flatten",
        "strategy_plan_id": "plan-runtime",
        "strategy_plan_version": 1,
    })
    adapter.process_market_event(_event(3, price=102.0, low=101.0, high=103.0))
    adapter.flush(cycle_id)

    final = adapter.snapshot(cycle_id)
    assert len(final["fills"]) == 3
    assert not [row for row in final["positions"] if row["status"] == "open"]
    assert final["account"]["margin"] == 0.0
    assert final["account"]["exposure"] == 0.0
    assert final["account"]["equity"] == pytest.approx(final["account"]["ending_cash"])
    assert not [
        row for row in final["orders"]
        if row["order_id"].endswith(("-SL", "-TP")) and row["state"] == "accepted"
    ]
    assert adapter.reconcile(cycle_id)["status"] == "ok"
    restarted = NautilusExecutionAdapter(
        tmp_path / "outputs",
        nautilus_python=RUNTIME,
        preflight_path=PREFLIGHT,
        defer_replay=True,
    )
    assert restarted.snapshot(cycle_id) == final
    assert restarted.flush(cycle_id)["status"] == "idempotent"
    assert restarted.reconcile(cycle_id)["status"] == "ok"


def test_real_runtime_cancels_full_grid_batch_at_one_timestamp(tmp_path: Path) -> None:
    adapter = NautilusExecutionAdapter(
        tmp_path / "outputs",
        nautilus_python=RUNTIME,
        preflight_path=PREFLIGHT,
        defer_replay=True,
    )
    cycle_id = "2026-07-10_DAY"
    receipts = []
    for index in range(40):
        price = 80.0 + index if index < 20 else 81.0 + index
        receipts.append(adapter.submit_order({
            "cycle_id": cycle_id,
            "ts": "2026-07-10T01:00:10+00:00",
            "side": "buy" if index < 20 else "sell",
            "event": "entry",
            "order_type": "limit",
            "price": price,
            "quantity": 0.1,
            "sl": 60.0 if index < 20 else 140.0,
            "tp": price + 1.0 if index < 20 else price - 1.0,
            "source_fill_id": f"runtime-grid-{index}",
            "strategy_plan_id": "plan-runtime",
            "strategy_plan_version": 1,
        }))
    adapter.cancel_orders(
        cycle_id,
        order_ids=[row["order_id"] for row in receipts],
        ts="2026-07-10T01:00:30+00:00",
        reason="runtime-full-grid-cancel",
    )
    adapter.process_market_event(_event(0, price=100.0, low=100.0, high=100.0))
    adapter.flush(cycle_id)

    snapshot = adapter.snapshot(cycle_id)

    assert len(snapshot["orders"]) == 40
    assert {row["state"] for row in snapshot["orders"]} == {"canceled"}
    assert snapshot["fills"] == []
    assert snapshot["positions"] == []
    assert adapter.reconcile(cycle_id)["status"] == "ok"


@pytest.mark.parametrize(
    ("side", "sl", "tp", "exit_price", "exit_low", "exit_high", "expected_event"),
    [
        ("buy", 95.0, 105.0, 105.0, 100.0, 106.0, "target"),
        ("sell", 105.0, 95.0, 106.0, 99.0, 106.0, "stop"),
        ("buy", 95.0, 105.0, 100.0, 94.0, 106.0, "stop"),
    ],
)
def test_real_runtime_bracket_terminal_events_are_deterministic(
    tmp_path: Path,
    side: str,
    sl: float,
    tp: float,
    exit_price: float,
    exit_low: float,
    exit_high: float,
    expected_event: str,
) -> None:
    adapter = NautilusExecutionAdapter(
        tmp_path / "outputs",
        nautilus_python=RUNTIME,
        preflight_path=PREFLIGHT,
        defer_replay=True,
    )
    cycle_id = "2026-07-10_DAY"
    adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-10T01:00:10+00:00",
        "side": side,
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "quantity": 1.0,
        "sl": sl,
        "tp": tp,
        "source_fill_id": f"runtime-bracket-{side}-{expected_event}",
        "strategy_plan_id": "plan-runtime",
        "strategy_plan_version": 1,
    })
    adapter.process_market_event(_event(0, price=100.0, low=99.0, high=101.0))
    adapter.process_market_event(_event(1, price=100.0, low=99.0, high=101.0))
    adapter.process_market_event(_event(2, price=exit_price, low=exit_low, high=exit_high))
    adapter.flush(cycle_id)

    snapshot = adapter.snapshot(cycle_id)
    assert [row["event"] for row in snapshot["fills"]] == ["entry", expected_event]
    assert snapshot["positions"][0]["status"] == "closed"
    assert adapter.reconcile(cycle_id)["status"] == "ok"
