"""Replay one immutable DualTrack input bundle in the isolated Nautilus runtime.

This module deliberately remains outside the application runtime. Invoke it
with the dedicated Nautilus Python environment; it never opens a venue client
or writes an authoritative legacy ledger.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from services.dualtrack_nautilus_instrument import build_nautilus_instrument
from services.journal_store import write_json


def run_replay(preflight_path: str | Path, input_path: str | Path) -> dict[str, Any]:
    preflight = _latest(preflight_path, "preflight")
    bundle = _latest(input_path, "shadow input")
    if preflight.get("status") != "ready_for_paper_shadow":
        raise RuntimeError("preflight is not ready for paper shadow")
    if bundle.get("schema_version") != "dualtrack-shadow-input-v1":
        raise ValueError("unsupported shadow input schema")
    events = list(bundle.get("market_events") or [])
    if not events:
        raise ValueError("shadow input has no market events")
    instrument_symbol = str((preflight.get("instrument") or {}).get("symbol") or "")
    if not instrument_symbol or any(str(event.get("instrument_id") or "") != instrument_symbol for event in events):
        raise ValueError("shadow input instrument identity mismatch")
    fee = preflight.get("fee_model") or {}
    if fee.get("real_money_eligible") is not False:
        raise ValueError("shadow replay requires paper-only fee model")
    instrument = build_nautilus_instrument(
        preflight["instrument"],
        maker_fee_rate=str(fee.get("maker_fee_rate") or ""),
        taker_fee_rate=str(fee.get("taker_fee_rate") or ""),
    )
    commands = _replayable_commands(list(bundle.get("commands") or []))
    engine = _run_market_replay(instrument, events, commands)
    last_event = events[-1]
    command_count = len(commands)
    fills, positions, realized, unrealized = _snapshot_reports(engine, commands)
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_shadow",
        "cycle_id": bundle["cycle_id"],
        "orders": _orders_from_fills(fills),
        "fills": fills,
        "positions": positions,
        "account": _account(positions, realized, mark_price=float(last_event["price"])),
        "pnl": {"realized": realized, "unrealized": unrealized},
        "mark": {"price": last_event["price"], "fresh": True, "source": last_event["source"]},
        "capabilities": {"native_order_lifecycle": True, "paper_shadow": True, "market_replay": True},
        "reconciliation": {"status": "ok", "issues": []},
        "shadow_evidence": {
            "input_id": bundle["input_id"],
            "replayed_market_events": len(events),
            "authoritative_command_count": command_count,
            # A no-command replay is valuable ingestion evidence, but cannot
            # advance the seven-cycle execution parity cutover requirement.
            "qualifies_for_cutover": command_count > 0,
        },
    }


def _run_market_replay(instrument, events: list[dict[str, Any]], commands: list[dict[str, Any]]):
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, OrderType
    from nautilus_trader.model.objects import Money
    from nautilus_trader.trading.strategy import Strategy

    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    bars = []
    for event in events:
        timestamp = datetime.fromisoformat(str(event["ts_event"]).replace("Z", "+00:00"))
        timestamp_ns = int(timestamp.timestamp() * 1_000_000_000)
        bars.append(Bar(
            bar_type=bar_type,
            open=instrument.make_price(event["open"]),
            high=instrument.make_price(event["high"]),
            low=instrument.make_price(event["low"]),
            close=instrument.make_price(event["price"]),
            volume=instrument.make_qty(100),
            ts_event=timestamp_ns,
            ts_init=timestamp_ns,
        ))
    class ReplayConfig(StrategyConfig, frozen=True):
        instrument_id: object
        bar_type: BarType

    class ReplayStrategy(Strategy):
        def __init__(self, config: ReplayConfig) -> None:
            super().__init__(config)
            self.cursor = 0
            self.bar_index = 0

        def on_start(self) -> None:
            self.instrument = self.cache.instrument(self.config.instrument_id)
            self.subscribe_bars(self.config.bar_type)

        def on_bar(self, _bar: Bar) -> None:
            event = events[min(self.bar_index, len(events) - 1)]
            event_at = datetime.fromisoformat(str(event["ts_event"]).replace("Z", "+00:00"))
            while self.cursor < len(commands) and _command_time(commands[self.cursor]) <= event_at:
                command = commands[self.cursor]["command"]
                side = OrderSide.BUY if str(command["side"]).lower() == "buy" else OrderSide.SELL
                quantity = self.instrument.make_qty(_command_quantity(command))
                event_name = str(command.get("event") or "entry").lower()
                order_type = str(command.get("order_type") or "market").lower()
                if event_name == "entry" and command.get("sl") is not None and command.get("tp") is not None:
                    order_list = self.order_factory.bracket(
                        instrument_id=self.config.instrument_id,
                        order_side=side,
                        quantity=quantity,
                        entry_order_type=OrderType.LIMIT if order_type == "limit" else OrderType.MARKET,
                        entry_price=self.instrument.make_price(command["price"]) if order_type == "limit" else None,
                        sl_trigger_price=self.instrument.make_price(command["sl"]),
                        tp_price=self.instrument.make_price(command["tp"]),
                    )
                    self.submit_order_list(order_list)
                elif event_name == "entry" and order_type == "limit":
                    order = self.order_factory.limit(
                        instrument_id=self.config.instrument_id, order_side=side, quantity=quantity,
                        price=self.instrument.make_price(command["price"]), post_only=False,
                    )
                    self.submit_order(order)
                else:
                    order = self.order_factory.market(
                        instrument_id=self.config.instrument_id, order_side=side, quantity=quantity,
                        reduce_only=event_name in {"exit", "stop", "target", "flatten"},
                    )
                    self.submit_order(order)
                self.cursor += 1
            self.bar_index += 1

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
        bar_adaptive_high_low_ordering=True,
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)
    if commands:
        engine.add_strategy(ReplayStrategy(ReplayConfig(instrument_id=instrument.id, bar_type=bar_type)))
    engine.run()
    return engine


def _replayable_commands(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for row in rows:
        command = row.get("command") if isinstance(row, dict) else None
        if not isinstance(command, dict):
            raise ValueError("shadow command is missing command payload")
        if str(command.get("side") or "").lower() not in {"buy", "sell"}:
            raise ValueError("shadow command side is unsupported")
        if str(command.get("order_type") or "market").lower() not in {"market", "limit"}:
            raise ValueError("shadow command order type is unsupported")
        if not command.get("ts"):
            raise ValueError("shadow command timestamp is required")
        _command_quantity(command)
        accepted.append(dict(row))
    return sorted(accepted, key=_command_time)


def _command_time(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(row["command"]["ts"]).replace("Z", "+00:00"))


def _command_quantity(command: dict[str, Any]) -> float:
    quantity = command.get("quantity") or command.get("contracts")
    if quantity not in (None, ""):
        value = float(quantity)
    else:
        value = float(command.get("notional") or 0.0) / float(command.get("price") or 0.0)
    if value <= 0:
        raise ValueError("shadow command quantity is invalid")
    return value


def _snapshot_reports(engine, commands: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float, float]:
    fill_rows = engine.trader.generate_order_fills_report().to_dict("records")
    position_rows = engine.trader.generate_positions_report().to_dict("records")
    fills = []
    for index, row in enumerate(fill_rows):
        price = float(row["avg_px"])
        command_row = commands[min(index, len(commands) - 1)] if commands else {}
        command = command_row.get("command") or {}
        event = str(command.get("event") or "entry").lower()
        if index and command.get("sl") is not None and price == float(command["sl"]):
            event = "stop"
        elif index and command.get("tp") is not None and price == float(command["tp"]):
            event = "target"
        fills.append({
            "fill_id": f"nautilus-{index + 1}", "side": str(row["side"]).lower(), "price": price,
            "quantity": _command_quantity(command) if command else 0.0, "event": event,
            "command_id": str(command_row.get("command_id") or ""),
        })
    if not position_rows:
        return fills, [], 0.0, 0.0
    last = position_rows[-1]
    realized = _money_number(last.get("realized_pnl"))
    unrealized = _money_number(last.get("unrealized_pnl"))
    closed = bool(last.get("ts_closed"))
    quantity = float(str(last.get("quantity") or 0.0).split()[0])
    side = str(last.get("side") or "").lower()
    if side == "flat" and fills:
        side = "long" if fills[0]["side"] == "buy" else "short"
    return fills, [{
        "status": "closed" if closed else "open",
        "side": side,
        "remaining_units": 0.0 if closed else quantity,
        "entry_price": float(last.get("avg_px_open") or 0.0),
        "exit_price": float(last.get("avg_px_close") or 0.0) if closed else None,
    }], realized, unrealized


def _orders_from_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    orders = []
    for index, row in enumerate(fills):
        command_id = str(row.get("command_id") or row["fill_id"])
        event = str(row.get("event") or "entry")
        order_id = command_id if event == "entry" else f"{command_id}:{event}:{index + 1}"
        orders.append({
            "order_id": order_id,
            "state": "filled",
            "side": row["side"],
            "event": event,
            "order_type": "limit" if event == "target" else "market",
            "price": row["price"],
            "quantity": row["quantity"],
        })
    return orders


def _account(
    positions: list[dict[str, Any]],
    realized: float,
    *,
    mark_price: float,
) -> dict[str, Any]:
    exposure = round(sum(
        float(position.get("remaining_units") or 0.0) * mark_price
        for position in positions
        if position.get("status") == "open"
    ), 8)
    if not positions and realized == 0.0:
        return {"margin": 0.0, "exposure": 0.0, "slippage": 0.0}
    return {
        "starting_cash": 10_000.0,
        "realized_pnl": realized,
        "ending_cash": round(10_000.0 + realized, 8),
        "margin": round(exposure / 10.0, 8),
        "exposure": exposure,
        "slippage": 0.0,
    }


def _money_number(value: Any) -> float:
    return float(str(value or 0).split()[0])


def _latest(path_value: str | Path, label: str) -> dict[str, Any]:
    rows = json.loads(Path(path_value).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows or not isinstance(rows[-1], dict):
        raise ValueError(f"expected a non-empty {label} array")
    return rows[-1]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Replay an immutable DualTrack shadow input in Nautilus.")
    parser.add_argument("--preflight", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = run_replay(args.preflight, args.input)
    write_json(Path(args.output), [result])
    print(json.dumps(result, ensure_ascii=False, indent=2))
