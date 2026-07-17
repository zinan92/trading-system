from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import pytest

from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter
from services.dualtrack_nautilus_parity_contract import platform_parity_code_hash
from services.dualtrack_config import dualtrack_config
from services.journal_store import load_json, write_json
from services.strategy_shadow import StrategyShadowRunner
from services.strategy_shadow_nautilus import NautilusStrategyShadowReplay


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get(
    "TRADING_ORCHESTRATOR_NAUTILUS_TEST_PYTHON",
    "/Users/wendy/.local/share/trading-orchestrator/nautilus-1.230.0/bin/python",
))
PREFLIGHT = Path(os.environ.get(
    "TRADING_ORCHESTRATOR_NAUTILUS_PREFLIGHT",
    str(ROOT / "outputs" / "dualtrack" / "nautilus" / "instrument_preflight.json"),
))


pytestmark = pytest.mark.skipif(
    not RUNTIME.exists() or not PREFLIGHT.exists(),
    reason="isolated Nautilus paper runtime evidence is not installed",
)


def test_real_runtime_strategy_shadow_has_one_content_bound_execution_truth(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    settings = deepcopy(dualtrack_config())
    preflight = deepcopy(load_json(PREFLIGHT)[-1])
    preflight["fee_model"] = {
        **dict(preflight.get("fee_model") or {}),
        "mode": "account_observed",
        "maker_fee_rate": "0.0002",
        "taker_fee_rate": "0.0004",
        "funding_rate": "0",
        "real_money_eligible": False,
    }
    settings["paper_fee_model"] = dict(preflight["fee_model"])
    golden_preflight = tmp_path / "golden-instrument-preflight.json"
    write_json(golden_preflight, [preflight])
    plan = {
        "schema_version": "strategy-plan-v1",
        "strategy_plan_id": "plan-runtime-strategy-shadow",
        "cycle_id": "2026-07-10_DAY",
        "version": 1,
        "locked_at": "2026-07-10T01:00:00+00:00",
        "status": "active",
        "direction": "long",
        "range": {"low": 95.0, "high": 101.0},
        "execution_context": {
            "market": {
                "price": 100.0,
                "symbol": "GOLD",
                "provider": "binance_usdm_futures",
            },
        },
        "grid": {
            "count": 1,
            "notional_per_grid": 99.0,
            "orders": [{
                "preview_order_id": "preview-01-buy",
                "side": "buy",
                "price": 99.0,
                "quantity": 1.0,
                "notional": 99.0,
                "sl": 95.0,
                "tp": 101.0,
            }],
        },
    }
    events = [
        _event(0, price=100.0, low=100.0, high=100.0),
        _event(1, price=100.0, low=98.5, high=100.5),
        _event(2, price=101.0, low=100.0, high=101.5),
    ]

    def run() -> dict:
        port = NautilusStrategyShadowReplay(
                output,
                nautilus_python=RUNTIME,
                preflight_path=golden_preflight,
            config=settings,
        )
        return StrategyShadowRunner(
            output,
            replay_port=port,
            config=settings,
        ).run(
            cycle_id="2026-07-10_DAY",
            variant_id="runtime-golden-grid",
            plan=plan,
            market_events=events,
        )

    first = run()
    second = run()

    assert first == second
    assert first["status"] == "pass"
    assert first["execution_receipt"]["status"] == "pass"
    assert first["execution_receipt"]["nautilus_version"] == "1.230.0"
    assert first["execution_receipt"]["platform_code_hash"] == platform_parity_code_hash()
    assert first["execution_receipt"]["accounting_snapshot_id"] == first["accounting_snapshot"]["snapshot_id"]
    assert first["accounting_snapshot"]["counts"] == {
        "order_count": 2,
        "open_order_count": 0,
        "fill_count": 2,
        "entry_fill_count": 1,
        "exit_fill_count": 1,
        "trade_count": 1,
        "open_trade_count": 0,
        "completed_trade_count": 1,
        "position_count": 1,
        "open_position_count": 0,
    }
    assert first["metrics"]["trade_count"] == 1
    assert first["metrics"]["cost"] == pytest.approx(0.04)
    pnl = first["accounting_snapshot"]["pnl"]
    assert pnl["net_realized_pnl"] == pytest.approx(
        pnl["gross_realized_pnl"] - pnl["fees"] + pnl["funding"]
    )
    assert first["accounting_snapshot"]["account"]["margin"] == 0.0
    assert first["accounting_snapshot"]["account"]["exposure"] == 0.0
    assert first["review"]["available_at"] == plan["locked_at"]
    assert first["safety"]["writes_authority_gate_evidence"] is False
    for relative in (
        "dualtrack/reconciliation",
        "dualtrack/nautilus/parity",
        "dualtrack/cutover",
    ):
        assert not (output / relative).exists()


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
    closed = next(row for row in final["positions"] if row["status"] == "closed")
    entry_fill = next(row for row in final["fills"] if row["event"] == "entry")
    exit_fills = [row for row in final["fills"] if row["event"] in {"exit", "flatten"}]
    gross_realized = sum(
        (float(exit_fill["price"]) - float(entry_fill["price"]))
        * float(exit_fill["quantity"])
        for exit_fill in exit_fills
    )
    assert closed["realized_pnl"] == pytest.approx(
        gross_realized - final["account"]["fees"] + final["account"]["funding"]
    )
    assert final["account"]["realized_pnl"] == pytest.approx(closed["realized_pnl"])
    assert final["pnl"]["realized"] == pytest.approx(closed["realized_pnl"])
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
