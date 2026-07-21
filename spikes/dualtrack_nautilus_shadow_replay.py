"""Replay one immutable DualTrack input bundle in the isolated Nautilus runtime.

This module deliberately remains outside the application runtime. Invoke it
with the dedicated Nautilus Python environment; it never opens a venue client
or writes an authoritative legacy ledger.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
    settings = dict(bundle.get("execution_settings") or {})
    starting_cash = float(settings.get("starting_cash") or 10_000.0)
    max_leverage = float(settings.get("max_leverage") or 10.0)
    if starting_cash <= 0 or max_leverage <= 0:
        raise ValueError("shadow replay execution settings must be positive")
    engine = _run_market_replay(
        instrument,
        events,
        commands,
        starting_cash=starting_cash,
        max_leverage=max_leverage,
    )
    last_event = events[-1]
    command_count = len(commands)
    orders, fills, positions, realized, unrealized = _snapshot_reports(
        engine,
        commands,
        mark_price=float(last_event["price"]),
        price_precision=int(preflight["instrument"]["price_precision"]),
    )
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_shadow",
        "cycle_id": bundle["cycle_id"],
        "orders": orders,
        "fills": fills,
        "positions": positions,
        "account": _account(
            positions,
            fills,
            realized,
            unrealized,
            mark_price=float(last_event["price"]),
            starting_cash=starting_cash,
            max_leverage=max_leverage,
        ),
        "pnl": {"realized": realized, "unrealized": unrealized},
        "mark": {"price": last_event["price"], "fresh": True, "source": last_event["source"]},
        "capabilities": {
            "native_order_lifecycle": True,
            "paper_shadow": True,
            "market_replay": True,
            "comparison_normalization": {
                "mode": "venue_precision",
                "price_decimals": int(preflight["instrument"]["price_precision"]),
                "quantity_decimals": int(preflight["instrument"]["size_precision"]),
                "money_decimals": 8,
            },
        },
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


def _run_market_replay(
    instrument,
    events: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    *,
    starting_cash: float,
    max_leverage: float,
):
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, OrderType
    from nautilus_trader.model.identifiers import ClientOrderId, PositionId
    from nautilus_trader.model.objects import Money
    from nautilus_trader.trading.strategy import Strategy

    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    bars = []
    execution_levels = _execution_levels(commands)
    for event in events:
        timestamp = datetime.fromisoformat(str(event["ts_event"]).replace("Z", "+00:00"))
        timestamp_ns = int(timestamp.timestamp() * 1_000_000_000)
        active_levels = [
            price
            for command_at, price in execution_levels
            if command_at <= timestamp
        ]
        for price in _bar_execution_path(event, active_levels):
            bars.append(Bar(
                bar_type=bar_type,
                open=instrument.make_price(price),
                high=instrument.make_price(price),
                low=instrument.make_price(price),
                close=instrument.make_price(price),
                volume=instrument.make_qty(100),
                ts_event=timestamp_ns,
                ts_init=timestamp_ns,
            ))
    command_by_id = {str(row.get("command_id") or ""): dict(row.get("command") or {}) for row in commands}
    command_batches: list[tuple[datetime, list[str]]] = []
    for command_row in commands:
        command_at = _command_time(command_row)
        command_id = str(command_row["command_id"])
        if command_batches and command_batches[-1][0] == command_at:
            command_batches[-1][1].append(command_id)
        else:
            command_batches.append((command_at, [command_id]))
    command_batch_by_name = {
        f"dualtrack-command-batch:{index}": command_ids
        for index, (_command_at, command_ids) in enumerate(command_batches)
    }

    class ReplayConfig(StrategyConfig, frozen=True):
        instrument_id: object
        bar_type: BarType

    class ReplayStrategy(Strategy):
        def __init__(self, config: ReplayConfig) -> None:
            super().__init__(config)

        def on_start(self) -> None:
            self.instrument = self.cache.instrument(self.config.instrument_id)
            self.subscribe_bars(self.config.bar_type)
            for index, (command_at, _command_ids) in enumerate(command_batches):
                self.clock.set_time_alert(
                    name=f"dualtrack-command-batch:{index}",
                    alert_time=command_at,
                    callback=self.on_command_batch_time,
                )

        def on_bar(self, _bar: Bar) -> None:
            pass

        def on_command_batch_time(self, event) -> None:
            for command_id in command_batch_by_name[str(event.name)]:
                self.execute_command(command_id)

        def execute_command(self, command_id: str) -> None:
            command = command_by_id[command_id]
            event_name = str(command.get("event") or "entry").lower()
            if event_name == "cancel":
                target = self.cache.order(ClientOrderId(str(command.get("cancel_order_id") or "")))
                if target is not None:
                    self.cancel_order(target)
                return
            side = OrderSide.BUY if str(command["side"]).lower() == "buy" else OrderSide.SELL
            quantity = self.instrument.make_qty(_command_quantity(command))
            position_id = PositionId(f"POS-{command_id}")
            order_type = str(command.get("order_type") or "market").lower()
            if event_name == "entry" and command.get("sl") is not None and command.get("tp") is not None:
                order_list = self.order_factory.bracket(
                    instrument_id=self.config.instrument_id,
                    order_side=side,
                    quantity=quantity,
                    entry_order_type=OrderType.LIMIT if order_type == "limit" else OrderType.MARKET,
                    entry_price=self.instrument.make_price(command["price"]) if order_type == "limit" else None,
                    entry_client_order_id=ClientOrderId(command_id),
                    entry_tags=["ENTRY"],
                    sl_trigger_price=self.instrument.make_price(command["sl"]),
                    sl_client_order_id=ClientOrderId(f"{command_id}-SL"),
                    sl_tags=["STOP_LOSS"],
                    tp_price=self.instrument.make_price(command["tp"]),
                    tp_client_order_id=ClientOrderId(f"{command_id}-TP"),
                    tp_tags=["TAKE_PROFIT"],
                )
                self.submit_order_list(order_list, position_id=position_id)
            elif event_name == "entry" and order_type == "limit":
                order = self.order_factory.limit(
                    instrument_id=self.config.instrument_id, order_side=side, quantity=quantity,
                    price=self.instrument.make_price(command["price"]), post_only=False,
                    client_order_id=ClientOrderId(command_id), tags=["ENTRY"],
                )
                self.submit_order(order, position_id=position_id)
            else:
                if event_name in {"exit", "stop", "target", "flatten"}:
                    target_command_id = str(command.get("target_command_id") or "")
                    if float(command.get("target_remaining_after") or 0.0) <= 1e-9:
                        for suffix in ("-SL", "-TP"):
                            protective = self.cache.order(ClientOrderId(f"{target_command_id}{suffix}"))
                            if protective is not None:
                                self.cancel_order(protective)
                order = self.order_factory.market(
                    instrument_id=self.config.instrument_id, order_side=side, quantity=quantity,
                    reduce_only=event_name in {"exit", "stop", "target", "flatten"},
                    client_order_id=ClientOrderId(command_id), tags=[event_name.upper()],
                )
                target_position_id = str(command.get("target_position_id") or "")
                if event_name in {"exit", "stop", "target", "flatten"} and not target_position_id:
                    raise ValueError("Nautilus close command is missing target_position_id")
                self.submit_order(
                    order,
                    position_id=PositionId(target_position_id) if target_position_id else position_id,
                )

        def on_order_filled(self, event) -> None:
            command_id = str(event.client_order_id)
            command = command_by_id.get(command_id, {})
            if str(command.get("event") or "").lower() not in {"exit", "stop", "target", "flatten"}:
                return
            remaining = float(command.get("target_remaining_after") or 0.0)
            if remaining <= 1e-9:
                return
            target_command_id = str(command.get("target_command_id") or "")
            quantity = self.instrument.make_qty(remaining)
            for suffix in ("-SL", "-TP"):
                protective = self.cache.order(ClientOrderId(f"{target_command_id}{suffix}"))
                if protective is not None and protective.is_open:
                    self.modify_order(protective, quantity=quantity)

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(
        venue=instrument.id.venue,
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(starting_cash, USDT)],
        base_currency=USDT,
        default_leverage=Decimal(str(max_leverage)),
        reject_stop_orders=False,
        use_position_ids=True,
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


def _execution_levels(commands: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    """Return executable prices which need their own L1 bar matching update.

    Nautilus converts a LAST bar into only four synthetic trade ticks. A single
    tick advances one resting order at a crossed price, so a wide bar can leave
    later grid orders accepted even though its high/low crossed their limits.
    Expanding the same deterministic OHLC path at every entry/TP/SL level keeps
    Nautilus authoritative while giving each touched order one matching event.
    """

    levels: list[tuple[datetime, float]] = []
    for row in commands:
        command = dict(row.get("command") or {})
        command_at = _command_time(row)
        for key in ("price", "tp", "sl"):
            value = command.get(key)
            if value not in (None, "") and float(value) > 0:
                levels.append((command_at, float(value)))
    return levels


def _bar_execution_path(event: dict[str, Any], levels: list[float]) -> list[float]:
    """Expand one OHLC bar using Nautilus' adaptive high/low ordering.

    The returned point bars contain no invented prices: every intermediate
    point is an executable order level inside the original bar range.
    """

    open_price = float(event["open"])
    high_price = float(event["high"])
    low_price = float(event["low"])
    close_price = float(event["price"])
    high_first = abs(high_price - open_price) < abs(low_price - open_price)
    anchors = (
        [open_price, high_price, low_price, close_price]
        if high_first
        else [open_price, low_price, high_price, close_price]
    )
    bounded_levels = {
        float(price)
        for price in levels
        if low_price <= float(price) <= high_price
    }
    path = [anchors[0]]
    for target in anchors[1:]:
        cursor = path[-1]
        if target > cursor:
            points = sorted(price for price in bounded_levels if cursor < price <= target)
        elif target < cursor:
            points = sorted(
                (price for price in bounded_levels if target <= price < cursor),
                reverse=True,
            )
        else:
            points = []
        if not points or points[-1] != target:
            points.append(target)
        path.extend(points)
    return [price for index, price in enumerate(path) if index == 0 or price != path[index - 1]]


def _snapshot_reports(
    engine,
    commands: list[dict[str, Any]],
    *,
    mark_price: float,
    price_precision: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], float, float]:
    order_rows = engine.trader.generate_orders_report().reset_index().to_dict("records")
    fill_rows = engine.trader.generate_fills_report().reset_index().to_dict("records")
    position_rows = engine.trader.generate_positions_report().reset_index().to_dict("records")
    orders = _orders_from_reports(order_rows, commands)
    fills = _fills_from_reports(fill_rows, commands, price_precision=price_precision)
    positions = _positions_from_reports(position_rows, commands)
    realized = round(sum(_money_number(row.get("realized_pnl")) for row in position_rows), 8)
    unrealized = round(sum(
        (mark_price - float(row.get("entry_price") or 0.0))
        * float(row.get("remaining_units") or 0.0)
        * (1.0 if row.get("side") == "long" else -1.0)
        for row in positions
        if row.get("status") == "open"
    ), 8)
    return orders, fills, positions, realized, unrealized


def _orders_from_reports(
    reports: list[dict[str, Any]],
    commands: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(row.get("client_order_id") or ""): row for row in reports}
    entries: list[dict[str, Any]] = []
    for command_row in commands:
        command_id = str(command_row.get("command_id") or "")
        command = dict(command_row.get("command") or {})
        entry = by_id.get(command_id)
        if entry:
            entries.append(_normalized_order(entry, command_id=command_id, command=command, child=False))
    command_by_id = {str(row.get("command_id") or ""): dict(row.get("command") or {}) for row in commands}
    filled_children = [
        row
        for row in reports
        if str(row.get("status") or "").upper() == "FILLED"
        and str(row.get("client_order_id") or "").endswith(("-SL", "-TP"))
    ]
    filled_children.sort(key=lambda row: _timestamp_text(row.get("ts_last")))
    children = []
    for child in filled_children:
        order_id = str(child.get("client_order_id") or "")
        command_id = _parent_command_id(order_id)
        children.append(_normalized_order(
            child,
            command_id=command_id,
            command=command_by_id.get(command_id, {}),
            child=True,
        ))
    return [*entries, *children]


def _fills_from_reports(
    reports: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    *,
    price_precision: int | None = None,
) -> list[dict[str, Any]]:
    command_by_id = {str(row.get("command_id") or ""): dict(row.get("command") or {}) for row in commands}
    fills = []
    for row in reports:
        order_id = str(row.get("client_order_id") or "")
        command_id = _parent_command_id(order_id)
        command = command_by_id.get(command_id, {})
        event = _report_event(row, fallback=str(command.get("event") or "entry"))
        price = float(row.get("last_px") or row.get("avg_px") or row.get("price") or 0.0)
        quantity = float(row.get("last_qty") or row.get("filled_qty") or row.get("quantity") or 0.0)
        if event == "target":
            requested_price = float(command.get("tp") or price)
        elif event == "stop":
            requested_price = float(command.get("sl") or price)
        else:
            requested_price = float(command.get("price") or price)
        if price_precision is not None:
            requested_price = round(requested_price, price_precision)
        fill = {
            # Nautilus event IDs are regenerated on every replay. The client
            # order ID is our durable execution identity, so use it to keep a
            # completed fill stable across deterministic rebuilds.
            "fill_id": f"nautilus-{order_id}",
            "order_id": order_id,
            "trade_id": command_id,
            "cycle_id": str(command.get("cycle_id") or ""),
            "ts": _timestamp_text(row.get("ts_event") or row.get("ts_last")),
            "side": str(row.get("order_side") or row.get("side") or "").lower(),
            "event": event,
            "price": price,
            "quantity": quantity,
            "requested_price": requested_price,
            "slippage": round(abs(price - requested_price) * quantity, 8),
            "cost": _money_number(row.get("commission")),
            "liquidity": str(row.get("liquidity_side") or "").lower(),
            "strategy_plan_id": command.get("strategy_plan_id"),
            "strategy_plan_version": command.get("strategy_plan_version"),
        }
        raw_order_type = str(row.get("order_type") or row.get("type") or "").upper()
        if raw_order_type:
            fill["order_type"] = "market" if "MARKET" in raw_order_type else "limit"
        fills.append(fill)
    event_priority = {"entry": 0, "exit": 1, "target": 1, "stop": 1, "flatten": 1}
    return sorted(
        fills,
        key=lambda row: (
            str(row.get("ts") or ""),
            event_priority.get(str(row.get("event") or ""), 9),
            str(row.get("order_id") or ""),
        ),
    )


def _positions_from_reports(
    reports: list[dict[str, Any]],
    commands: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    command_by_id = {str(row.get("command_id") or ""): dict(row.get("command") or {}) for row in commands}
    positions = []
    for row in reports:
        command_id = _parent_command_id(str(row.get("opening_order_id") or ""))
        command = command_by_id.get(command_id, {})
        closed = row.get("ts_closed") not in (None, "", "<NA>") and str(row.get("ts_closed")) != "NaT"
        side = "long" if str(command.get("side") or "").lower() == "buy" else "short"
        quantity = float(str(row.get("quantity") or 0.0).split()[0])
        positions.append({
            "trade_id": command_id,
            "position_id": str(row.get("position_id") or command_id),
            "status": "closed" if closed else "open",
            "side": side,
            "remaining_units": 0.0 if closed else quantity,
            "entry_price": float(row.get("avg_px_open") or 0.0),
            "exit_price": float(row.get("avg_px_close") or 0.0) if closed else None,
            "entry_ts": _timestamp_text(row.get("ts_opened")),
            "exit_ts": _timestamp_text(row.get("ts_closed")) if closed else None,
            "realized_pnl": _money_number(row.get("realized_pnl")),
            "sl": command.get("sl"),
            "tp": command.get("tp"),
            "strategy_plan_id": command.get("strategy_plan_id"),
            "strategy_plan_version": command.get("strategy_plan_version"),
        })
    return positions


def _normalized_order(
    row: dict[str, Any],
    *,
    command_id: str,
    command: dict[str, Any],
    child: bool,
) -> dict[str, Any]:
    event = _report_event(row, fallback=str(command.get("event") or "entry"))
    order_id = str(row.get("client_order_id") or command_id)
    order_type = "market" if "MARKET" in str(row.get("type") or "").upper() else "limit"
    price = row.get("trigger_price") if event == "stop" else row.get("price")
    quantity = row.get("quantity")
    status = str(row.get("status") or "").lower()
    state = {"submitted": "accepted", "accepted": "accepted"}.get(status, status)
    result = {
        "order_id": order_id,
        "state": state,
        "side": str(row.get("side") or command.get("side") or "").lower(),
        "event": event,
        "order_type": order_type,
        "price": float(price or 0.0),
        "quantity": float(quantity or 0.0),
        "strategy_plan_id": command.get("strategy_plan_id"),
        "strategy_plan_version": command.get("strategy_plan_version"),
    }
    timestamp = _timestamp_text(row.get("ts_last") or row.get("ts_init"))
    if timestamp:
        result["ts"] = timestamp
    if not child:
        result["requested_price"] = float(command.get("price") or result["price"])
        result["requested_quantity"] = float(command.get("quantity") or result["quantity"])
    return result


def _report_event(row: dict[str, Any], *, fallback: str) -> str:
    order_id = str(row.get("client_order_id") or "")
    tags = str(row.get("tags") or "").upper()
    if order_id.endswith("-SL") or "STOP_LOSS" in tags:
        return "stop"
    if order_id.endswith("-TP") or "TAKE_PROFIT" in tags:
        return "target"
    return fallback.lower()


def _parent_command_id(order_id: str) -> str:
    for suffix in ("-SL", "-TP"):
        if order_id.endswith(suffix):
            return order_id[: -len(suffix)]
    return order_id


def _timestamp_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or str(value).strip().replace(".", "", 1).isdigit():
        number = float(value)
        magnitude = abs(number)
        if magnitude >= 1e17:
            number /= 1_000_000_000
        elif magnitude >= 1e14:
            number /= 1_000_000
        elif magnitude >= 1e11:
            number /= 1_000
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat() if callable(isoformat) else value)


def _account(
    positions: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    realized: float,
    unrealized: float,
    *,
    mark_price: float,
    starting_cash: float = 10_000.0,
    max_leverage: float = 10.0,
) -> dict[str, Any]:
    exposure = round(sum(
        float(position.get("remaining_units") or 0.0) * mark_price
        for position in positions
        if position.get("status") == "open"
    ), 8)
    fees = round(sum(float(fill.get("cost") or 0.0) for fill in fills), 8)
    slippage = round(sum(float(fill.get("slippage") or 0.0) for fill in fills), 8)
    ending_cash = round(starting_cash + realized, 8)
    return {
        "starting_cash": starting_cash,
        "realized_pnl": realized,
        "ending_cash": ending_cash,
        "equity": round(ending_cash + unrealized, 8),
        "margin": round(exposure / max_leverage, 8),
        "exposure": exposure,
        "slippage": slippage,
        "fees": fees,
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
