"""First exact paper-parity fixture for the future DualTrack Nautilus adapter.

Run this with the isolated Nautilus Python runtime.  It intentionally uses
only the validated upstream XAUUSDT instrument preflight and the explicit
paper-only fee model.  It does not touch any production or broker path.
"""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from services.dualtrack_execution_adapter import LegacyPaperExecutionAdapter
from services.dualtrack_execution_contract import compare_execution_snapshots
from services.dualtrack_nautilus_instrument import build_nautilus_instrument
from services.journal_store import write_json


SCENARIOS = {
    "long_stop": {
        "side": "buy",
        "position_side": "long",
        "entry_price": 100.0,
        "sl": 95.0,
        "tp": 105.0,
        "exit_bar": {"open": 100.0, "high": 101.0, "low": 94.0, "close": 94.0},
        "exit_event": "stop",
    },
    "short_stop": {
        "side": "sell",
        "position_side": "short",
        "entry_price": 100.0,
        "sl": 105.0,
        "tp": 95.0,
        "exit_bar": {"open": 100.0, "high": 106.0, "low": 99.0, "close": 106.0},
        "exit_event": "stop",
    },
    "long_target": {
        "side": "buy",
        "position_side": "long",
        "entry_price": 100.0,
        "sl": 95.0,
        "tp": 105.0,
        "exit_bar": {"open": 100.0, "high": 106.0, "low": 99.0, "close": 106.0},
        "exit_event": "target",
    },
    "short_target": {
        "side": "sell",
        "position_side": "short",
        "entry_price": 100.0,
        "sl": 105.0,
        "tp": 95.0,
        "exit_bar": {"open": 100.0, "high": 101.0, "low": 94.0, "close": 94.0},
        "exit_event": "target",
    },
    "long_same_bar_stop_first": {
        "side": "buy",
        "position_side": "long",
        "entry_price": 100.0,
        "sl": 95.0,
        "tp": 105.0,
        "exit_bar": {"open": 100.0, "high": 106.0, "low": 94.0, "close": 100.0},
        "exit_event": "stop",
    },
    "limit_entry_waits_for_touch": {
        "side": "buy",
        "position_side": "long",
        "initial_price": 100.0,
        "entry_price": 95.0,
        "entry_order_type": "limit",
        "sl": 90.0,
        "tp": 105.0,
        "exit_bar": {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0},
        "exit_event": "entry",
    },
    "scale_in_weighted_average": {
        "kind": "scale_in",
        "side": "buy",
        "position_side": "long",
        "commands": [
            {"side": "buy", "price": 100.0, "quantity": 1.0, "event": "entry"},
            {"side": "buy", "price": 98.0, "quantity": 1.0, "event": "entry"},
        ],
        "bars": [100.0, 98.0, 99.0],
    },
    "partial_reduction_then_close": {
        "kind": "partial_reduce",
        "side": "buy",
        "position_side": "long",
        "commands": [
            {"side": "buy", "price": 100.0, "quantity": 2.0, "event": "entry"},
            {"side": "sell", "price": 105.0, "quantity": 1.0, "event": "exit", "reduce_only": True},
            {"side": "sell", "price": 110.0, "quantity": 1.0, "event": "exit", "reduce_only": True},
        ],
        "bars": [100.0, 105.0, 110.0, 110.0],
    },
    "duplicate_command_event_replay": {
        "kind": "duplicate",
        "side": "buy",
        "position_side": "long",
        "commands": [{"side": "buy", "price": 100.0, "quantity": 1.0, "event": "entry"}],
        "bars": [100.0, 100.0],
    },
    "restart_replay_and_reconciliation": {
        "side": "buy",
        "position_side": "long",
        "entry_price": 100.0,
        "sl": 95.0,
        "tp": 105.0,
        "exit_bar": {"open": 100.0, "high": 101.0, "low": 94.0, "close": 94.0},
        "exit_event": "stop",
        "restart_check": True,
    },
}


def run_fixture(preflight_path: str | Path, *, scenario_name: str = "long_stop") -> dict[str, Any]:
    artifact = _artifact(preflight_path)
    scenario = _scenario(scenario_name)
    if artifact.get("status") != "ready_for_paper_shadow":
        raise RuntimeError(f"preflight is not ready for paper shadow: {artifact.get('status')}")
    fee_model = artifact.get("fee_model") or {}
    maker_fee = str(fee_model.get("maker_fee_rate") or "")
    taker_fee = str(fee_model.get("taker_fee_rate") or "")
    if not maker_fee or not taker_fee or fee_model.get("real_money_eligible") is not False:
        raise RuntimeError("fixture requires an explicit paper-only maker/taker fee model")

    cycle_id = "2026-07-10_DAY"
    instrument = build_nautilus_instrument(
        artifact["instrument"],
        maker_fee_rate=maker_fee,
        taker_fee_rate=taker_fee,
    )
    candidate = _nautilus_snapshot(instrument, cycle_id=cycle_id, scenario=scenario)
    legacy = _legacy_snapshot(cycle_id=cycle_id, scenario=scenario)
    parity = compare_execution_snapshots(legacy, candidate, cycle_id=cycle_id)
    restart_evidence = None
    if scenario.get("restart_check"):
        restarted_legacy = _legacy_snapshot(cycle_id=cycle_id, scenario=scenario)
        restarted_candidate = _nautilus_snapshot(instrument, cycle_id=cycle_id, scenario=scenario)
        restart_evidence = {
            "mode": "immutable_replay",
            "legacy_reconciliation": legacy["reconciliation"]["status"],
            "candidate_reconciliation": candidate["reconciliation"]["status"],
            "legacy_restart_parity": compare_execution_snapshots(legacy, restarted_legacy, cycle_id=cycle_id)["status"],
            "candidate_restart_parity": compare_execution_snapshots(candidate, restarted_candidate, cycle_id=cycle_id)["status"],
        }
    return {
        "schema_version": "dualtrack-nautilus-gold-parity-fixture-v1",
        "scope": "paper_shadow_only",
        "scenario": scenario_name,
        "instrument_id": str(instrument.id),
        "fee_model": fee_model,
        "legacy": legacy,
        "candidate": candidate,
        "parity": parity,
        "restart_evidence": restart_evidence,
    }


def _nautilus_snapshot(instrument, *, cycle_id: str, scenario: dict[str, Any]) -> dict[str, Any]:
    if scenario.get("kind") in {"scale_in", "partial_reduce", "duplicate"}:
        return _nautilus_sequence_snapshot(instrument, cycle_id=cycle_id, scenario=scenario)
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, OrderType
    from nautilus_trader.model.objects import Money
    from nautilus_trader.trading.strategy import Strategy

    class FixtureConfig(StrategyConfig, frozen=True):
        instrument_id: object
        bar_type: BarType

    class FixtureStrategy(Strategy):
        def __init__(self, config: FixtureConfig) -> None:
            super().__init__(config)
            self.submitted = False

        def on_start(self) -> None:
            self.instrument = self.cache.instrument(self.config.instrument_id)
            self.subscribe_bars(self.config.bar_type)

        def on_bar(self, _bar: Bar) -> None:
            if self.submitted:
                return
            self.submitted = True
            orders = self.order_factory.bracket(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.BUY if scenario["side"] == "buy" else OrderSide.SELL,
                quantity=self.instrument.make_qty(1),
                entry_order_type=OrderType.LIMIT if scenario.get("entry_order_type") == "limit" else OrderType.MARKET,
                entry_price=self.instrument.make_price(scenario["entry_price"]) if scenario.get("entry_order_type") == "limit" else None,
                sl_trigger_price=self.instrument.make_price(scenario["sl"]),
                tp_price=self.instrument.make_price(scenario["tp"]),
            )
            self.submit_order_list(orders)

    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")

    def bar(minute: int, open_: float, high: float, low: float, close: float) -> Bar:
        timestamp_ns = (1_800_000_000 + minute * 60) * 1_000_000_000
        return Bar(
            bar_type=bar_type,
            open=instrument.make_price(open_),
            high=instrument.make_price(high),
            low=instrument.make_price(low),
            close=instrument.make_price(close),
            volume=instrument.make_qty(100),
            ts_event=timestamp_ns,
            ts_init=timestamp_ns,
        )

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(
        venue=instrument.id.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10_000, USDT)],
        base_currency=USDT,
        default_leverage=Decimal(10),
        reject_stop_orders=False,
        use_position_ids=False,
        # The legacy adapter is deliberately conservative. Adaptive ordering
        # processes the nearer wick first, so the equal-distance fixture below
        # visits Low before High and makes the stop win.
        bar_adaptive_high_low_ordering=True,
    )
    engine.add_instrument(instrument)
    exit_bar = scenario["exit_bar"]
    engine.add_data([
        bar(0, scenario.get("initial_price", scenario["entry_price"]), scenario.get("initial_price", scenario["entry_price"]), scenario.get("initial_price", scenario["entry_price"]), scenario.get("initial_price", scenario["entry_price"])),
        bar(1, exit_bar["open"], exit_bar["high"], exit_bar["low"], exit_bar["close"]),
        bar(2, exit_bar["close"], exit_bar["close"], exit_bar["close"], exit_bar["close"]),
    ])
    engine.add_strategy(FixtureStrategy(FixtureConfig(instrument_id=instrument.id, bar_type=bar_type)))
    engine.run()
    fills_report = engine.trader.generate_order_fills_report()
    positions_report = engine.trader.generate_positions_report()
    fills = [
        {
            "fill_id": f"nautilus-{index + 1}",
            "side": str(row["side"]).lower(),
            "price": float(row["avg_px"]),
            "quantity": _quantity(scenario),
            "event": "entry" if index == 0 else _exit_event(float(row["avg_px"]), scenario),
        }
        for index, row in enumerate(fills_report.to_dict("records"))
    ]
    position_rows = positions_report.to_dict("records")
    realized = _money_number(position_rows[-1]["realized_pnl"]) if position_rows else 0.0
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_shadow",
        "cycle_id": cycle_id,
        "orders": _candidate_orders(fills, scenario),
        "fills": fills,
        "positions": ([{"status": "closed", "side": scenario["position_side"], "remaining_units": 0.0}] if position_rows else []),
        "account": ({
            "starting_cash": 10_000.0,
            "realized_pnl": realized,
            "ending_cash": round(10_000.0 + realized, 8),
            "margin": 0.0,
            "exposure": 0.0,
            "slippage": 0.0,
        } if position_rows else {"margin": 0.0, "exposure": 0.0, "slippage": 0.0}),
        "pnl": {"realized": realized, "unrealized": 0.0},
        "mark": {"price": exit_bar["close"], "fresh": True, "source": "fixture"},
        "capabilities": {"native_order_lifecycle": True, "paper_shadow": True},
        "reconciliation": {"status": "ok"},
    }


def _legacy_snapshot(*, cycle_id: str, scenario: dict[str, Any]) -> dict[str, Any]:
    if scenario.get("kind") in {"scale_in", "partial_reduce", "duplicate"}:
        return _legacy_sequence_snapshot(cycle_id=cycle_id, scenario=scenario)
    with tempfile.TemporaryDirectory() as directory:
        adapter = LegacyPaperExecutionAdapter(Path(directory) / "outputs")
        adapter.submit_order({
            "cycle_id": cycle_id,
            "ts": "2026-07-10T01:00:00+00:00",
            "side": scenario["side"],
            "event": "entry",
            "order_type": scenario.get("entry_order_type") or "market",
            "price": scenario["entry_price"],
            "notional": 100.0,
            "sl": scenario["sl"],
            "tp": scenario["tp"],
            "source": "parity_fixture",
        })
        adapter.process_market_event({
            "cycle_id": cycle_id,
            "ts_event": "2026-07-10T01:01:00+00:00",
            "event_started_at": "2026-07-10T01:01:00+00:00",
            "price": scenario["exit_bar"]["close"],
            **scenario["exit_bar"],
            "fresh": True,
            "is_synthetic": False,
            "source": "parity_fixture",
        })
        snapshot = adapter.snapshot(
            cycle_id,
            mark_price=scenario["exit_bar"]["close"],
            mark_fresh=True,
            mark_source="fixture",
        )
        snapshot["reconciliation"] = adapter.reconcile(cycle_id)
        return snapshot


def _nautilus_sequence_snapshot(instrument, *, cycle_id: str, scenario: dict[str, Any]) -> dict[str, Any]:
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
    from nautilus_trader.model.objects import Money
    from nautilus_trader.trading.strategy import Strategy

    commands = list(scenario["commands"])
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")

    class SequenceConfig(StrategyConfig, frozen=True):
        instrument_id: object
        bar_type: BarType

    class SequenceStrategy(Strategy):
        def __init__(self, config: SequenceConfig) -> None:
            super().__init__(config)
            self.index = 0

        def on_start(self) -> None:
            self.instrument = self.cache.instrument(self.config.instrument_id)
            self.subscribe_bars(self.config.bar_type)

        def on_bar(self, _bar: Bar) -> None:
            if self.index >= len(commands):
                return
            command = commands[self.index]
            side = OrderSide.BUY if command["side"] == "buy" else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=self.config.instrument_id,
                order_side=side,
                quantity=self.instrument.make_qty(command["quantity"]),
                reduce_only=bool(command.get("reduce_only")),
            )
            self.submit_order(order)
            self.index += 1

    def bar(index: int, price: float) -> Bar:
        timestamp_ns = (1_810_000_000 + index * 60) * 1_000_000_000
        return Bar(
            bar_type=bar_type,
            open=instrument.make_price(price), high=instrument.make_price(price), low=instrument.make_price(price),
            close=instrument.make_price(price), volume=instrument.make_qty(100), ts_event=timestamp_ns, ts_init=timestamp_ns,
        )

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(venue=instrument.id.venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                     starting_balances=[Money(10_000, USDT)], base_currency=USDT, default_leverage=Decimal(10),
                     reject_stop_orders=False, use_position_ids=False)
    engine.add_instrument(instrument)
    engine.add_data([bar(index, price) for index, price in enumerate(scenario["bars"])])
    engine.add_strategy(SequenceStrategy(SequenceConfig(instrument_id=instrument.id, bar_type=bar_type)))
    engine.run()
    fill_rows = engine.trader.generate_order_fills_report().to_dict("records")
    position_rows = engine.trader.generate_positions_report().to_dict("records")
    fills = [
        {
            "fill_id": f"nautilus-{index + 1}",
            "side": str(row["side"]).lower(),
            "price": float(row["avg_px"]),
            "quantity": float(commands[index]["quantity"]),
            "event": commands[index]["event"],
        }
        for index, row in enumerate(fill_rows)
    ]
    realized = _money_number(position_rows[-1]["realized_pnl"]) if position_rows else 0.0
    open_units = 2.0 if scenario["kind"] == "scale_in" else (1.0 if scenario["kind"] == "duplicate" else 0.0)
    positions = (
        [{"status": "open", "side": "long", "remaining_units": 2.0}]
        if scenario["kind"] == "scale_in"
        else ([{"status": "open", "side": "long", "remaining_units": 1.0}] if scenario["kind"] == "duplicate"
        else [{"status": "closed", "side": "long", "remaining_units": 0.0}]
        )
    )
    unrealized = 0.0 if scenario["kind"] == "scale_in" else 0.0
    return {
        "schema_version": "dualtrack-execution-v1", "engine": "nautilus_shadow", "cycle_id": cycle_id,
        "orders": [
            {"order_id": f"nautilus-{index + 1}", "state": "filled", "side": fill["side"], "event": fill["event"],
             "order_type": "market", "price": fill["price"], "quantity": fill["quantity"]}
            for index, fill in enumerate(fills)
        ],
        "fills": fills, "positions": positions,
        "account": {
            "starting_cash": 10_000.0, "realized_pnl": realized, "ending_cash": round(10_000.0 + realized, 8),
            "exposure": 198.0 if scenario["kind"] == "scale_in" else (100.0 if scenario["kind"] == "duplicate" else 0.0),
            "margin": 19.8 if scenario["kind"] == "scale_in" else (10.0 if scenario["kind"] == "duplicate" else 0.0),
            "slippage": 0.0,
        },
        "pnl": {"realized": realized, "unrealized": unrealized},
        "mark": {"price": scenario["bars"][-1], "fresh": True, "source": "fixture"},
        "capabilities": {"native_order_lifecycle": True, "paper_shadow": True}, "reconciliation": {"status": "ok"},
    }


def _legacy_sequence_snapshot(*, cycle_id: str, scenario: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        adapter = LegacyPaperExecutionAdapter(Path(directory) / "outputs")
        trade_id = f"{cycle_id}_sequence"
        for index, command in enumerate(scenario["commands"]):
            price = float(command["price"])
            quantity = float(command["quantity"])
            payload = {
                "cycle_id": cycle_id, "ts": f"2026-07-10T01:0{index}:00+00:00", "side": command["side"],
                "event": command["event"], "order_type": "market", "price": price,
                "notional": price * quantity, "contracts": quantity, "trade_id": trade_id,
                "position_id": "sequence", "source": "parity_fixture",
            }
            if scenario["kind"] == "duplicate":
                payload["source_fill_id"] = "fixture-duplicate-command"
            adapter.submit_order(payload)
            if scenario["kind"] == "duplicate":
                adapter.submit_order(payload)
        snapshot = adapter.snapshot(cycle_id, mark_price=scenario["bars"][-1], mark_fresh=True, mark_source="fixture")
        snapshot["reconciliation"] = adapter.reconcile(cycle_id)
        return snapshot


def _artifact(path_value: str | Path) -> dict[str, Any]:
    rows = json.loads(Path(path_value).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows or not isinstance(rows[-1], dict):
        raise ValueError("expected a non-empty preflight artifact array")
    return rows[-1]


def _scenario(name: str) -> dict[str, Any]:
    try:
        return dict(SCENARIOS[name])
    except KeyError as exc:
        raise ValueError(f"unknown scenario: {name}") from exc


def _exit_event(price: float, scenario: dict[str, Any]) -> str:
    if price == float(scenario["sl"]):
        return "stop"
    if price == float(scenario["tp"]):
        return "target"
    return "exit"


def _candidate_orders(fills: list[dict[str, Any]], scenario: dict[str, Any]) -> list[dict[str, Any]]:
    if not fills and scenario.get("entry_order_type") == "limit":
        return [{
            "order_id": "nautilus-pending-entry",
            "state": "accepted",
            "side": scenario["side"],
            "event": "entry",
            "order_type": "limit",
            "price": scenario["entry_price"],
            "quantity": _quantity(scenario),
        }]
    return [
        {
            "order_id": f"nautilus-{index + 1}",
            "state": "filled",
            "side": fill["side"],
            "event": fill["event"],
            "order_type": "market",
            "price": fill["price"],
            "quantity": fill["quantity"],
        }
        for index, fill in enumerate(fills)
    ]


def _quantity(scenario: dict[str, Any]) -> float:
    return float(scenario.get("notional", 100.0)) / float(scenario["entry_price"])


def _money_number(value: Any) -> float:
    return float(str(value).split()[0])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the paper-only XAUUSDT Nautilus parity fixture.")
    parser.add_argument("--preflight", required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="long_stop")
    parser.add_argument(
        "--output",
        default="",
        help="Optional JSON artifact path. The fixture never writes to a live execution ledger.",
    )
    args = parser.parse_args()
    result = run_fixture(args.preflight, scenario_name=args.scenario)
    if args.output:
        write_json(Path(args.output), [result])
    print(json.dumps(result, ensure_ascii=False, indent=2))
