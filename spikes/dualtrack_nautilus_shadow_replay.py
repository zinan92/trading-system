"""Replay one immutable DualTrack input bundle in the isolated Nautilus runtime.

This module deliberately remains outside the application runtime. Invoke it
with the dedicated Nautilus Python environment; it never opens a venue client
or writes an authoritative legacy ledger.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from services.dualtrack_nautilus_instrument import build_nautilus_instrument
from services.dualtrack_nautilus_parity_contract import platform_parity_code_hash
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
    native_commands = _native_replay_commands(commands)
    settings = dict(bundle.get("execution_settings") or {})
    starting_cash = float(settings.get("starting_cash") or 10_000.0)
    max_leverage = float(settings.get("max_leverage") or 10.0)
    if starting_cash <= 0 or max_leverage <= 0:
        raise ValueError("shadow replay execution settings must be positive")
    engine = _run_market_replay(
        instrument,
        events,
        native_commands,
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
    orders, fills, positions, realized, unrealized, safe_settlement_count = (
        _apply_paper_safe_action_settlements(
            orders,
            fills,
            positions,
            commands,
            mark_price=float(last_event["price"]),
            taker_fee_rate=float(fee.get("taker_fee_rate") or 0.0),
        )
    )
    result = {
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
            "paper_safe_action_settlement": True,
            "nautilus_version": _nautilus_version(),
            "platform_code_hash": platform_parity_code_hash(),
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
            "safe_action_settlement_count": safe_settlement_count,
            # A no-command replay is valuable ingestion evidence, but cannot
            # advance the seven-cycle execution parity cutover requirement.
            "qualifies_for_cutover": command_count > 0,
        },
    }
    _require_strict_json(result)
    return result


def _nautilus_version() -> str:
    import nautilus_trader

    return str(getattr(nautilus_trader, "__version__", ""))


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


def _native_replay_commands(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep post-replay safe settlements out of future native replays."""

    cancelled_targets = {
        str((row.get("command") or {}).get("cancel_order_id") or "")
        for row in rows
        if _valid_safe_action_gate(
            (row.get("command") or {}).get("safe_action_market_gate"),
            action_class="cancel",
        )
    } - {""}
    native = []
    for row in rows:
        command_id = str(row.get("command_id") or "")
        command = row.get("command") if isinstance(row.get("command"), dict) else {}
        event = str(command.get("event") or "entry").lower()
        gate = command.get("safe_action_market_gate")
        if command_id in cancelled_targets:
            continue
        if event == "cancel" and _valid_safe_action_gate(gate, action_class="cancel"):
            continue
        if event in {"exit", "stop", "target", "flatten"} and _valid_safe_action_gate(
            gate,
            action_class="reduce_only",
        ):
            continue
        native.append(row)
    return native


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


def _apply_paper_safe_action_settlements(
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    *,
    mark_price: float,
    taker_fee_rate: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], float, float, int]:
    """Settle paper-only safe actions after the final real market event.

    No market event is fabricated. Cancellation changes only accepted order
    lifecycle state; reduce-only fills use the server-bound price evidence on
    the command and can never increase position size.
    """

    order_by_id = {str(row.get("order_id") or ""): row for row in orders}
    fill_order_ids = {str(row.get("order_id") or "") for row in fills}
    command_rows = [
        (str(row.get("command_id") or ""), dict(row.get("command") or {}))
        for row in commands
    ]
    command_by_id = dict(command_rows)
    settlement_count = 0

    for _command_id, command in command_rows:
        gate = command.get("safe_action_market_gate")
        if not _valid_safe_action_gate(gate, action_class="cancel"):
            continue
        target_id = str(command.get("cancel_order_id") or "")
        target = order_by_id.get(target_id)
        if target is None and target_id in command_by_id:
            target = _paper_order_from_command(target_id, command_by_id[target_id])
            orders.append(target)
            order_by_id[target_id] = target
        if target is None or str(target.get("state") or "") != "accepted":
            continue
        target["state"] = "canceled"
        target["cancelled_at"] = str(command.get("ts") or "")
        target["safe_action_market_gate"] = dict(gate)
        settlement_count += 1

    for command_id, command in command_rows:
        event = str(command.get("event") or "").lower()
        gate = command.get("safe_action_market_gate")
        if event not in {"exit", "stop", "target", "flatten"}:
            continue
        if not _valid_safe_action_gate(gate, action_class="reduce_only"):
            continue
        if command.get("market_fresh") is not False:
            raise ValueError("paper safe-action settlement freshness evidence mismatch")
        if str(gate.get("pricing_source") or "") == "fresh_server_mark":
            continue
        if command_id in fill_order_ids:
            continue
        price = float(command.get("price") or 0.0)
        quantity = float(command.get("quantity") or command.get("contracts") or 0.0)
        requested_at = str(command.get("requested_at") or "")
        if price <= 0 or quantity <= 0 or not requested_at:
            raise ValueError("paper safe-action settlement command is incomplete")
        if float(gate.get("pricing_price") or 0.0) != price:
            raise ValueError("paper safe-action settlement price evidence mismatch")
        if command.get("market_price") not in (None, "") and float(command["market_price"]) != price:
            raise ValueError("paper safe-action settlement market price mismatch")
        if str(gate.get("pricing_timestamp") or "") != str(command.get("market_timestamp") or ""):
            raise ValueError("paper safe-action settlement timestamp evidence mismatch")
        if str(gate.get("pricing_provider") or "") != str(command.get("market_source") or ""):
            raise ValueError("paper safe-action settlement provider evidence mismatch")
        target = _safe_action_target_position(positions, command)
        remaining_before = float(target.get("remaining_units") or 0.0)
        target_side = str(target.get("side") or "").lower()
        if target_side not in {"long", "short"}:
            raise ValueError("paper safe-action target position side is invalid")
        expected_side = "sell" if target_side == "long" else "buy"
        if str(command.get("side") or "").lower() != expected_side:
            raise ValueError("paper safe-action settlement side does not reduce the target position")
        if quantity > remaining_before + 1e-9:
            raise ValueError("paper safe-action settlement exceeds the open position")
        entry_at = datetime.fromisoformat(str(target.get("entry_ts") or "").replace("Z", "+00:00"))
        settled_at = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        priced_at = datetime.fromisoformat(
            str(gate.get("pricing_timestamp") or "").replace("Z", "+00:00")
        )
        if settled_at <= entry_at:
            raise ValueError("paper safe-action settlement must follow the position entry")
        if priced_at > settled_at:
            raise ValueError("paper safe-action pricing evidence cannot be in the future")
        direction = 1.0 if str(target.get("side") or "") == "long" else -1.0
        gross = (price - float(target.get("entry_price") or 0.0)) * quantity * direction
        cost = price * quantity * taker_fee_rate
        remaining_after = max(0.0, remaining_before - quantity)
        if command.get("target_remaining_before") not in (None, "") and abs(
            float(command["target_remaining_before"]) - remaining_before
        ) > 1e-9:
            raise ValueError("paper safe-action settlement position evidence is stale")
        if command.get("target_remaining_after") not in (None, "") and abs(
            float(command["target_remaining_after"]) - remaining_after
        ) > 1e-9:
            raise ValueError("paper safe-action settlement remaining evidence mismatch")
        target["remaining_units"] = round(remaining_after, 10)
        target["realized_pnl"] = round(float(target.get("realized_pnl") or 0.0) + gross - cost, 8)
        target["status"] = "closed" if remaining_after <= 1e-9 else "open"
        if target["status"] == "closed":
            target["exit_price"] = price
            target["exit_ts"] = requested_at
        order = order_by_id.get(command_id)
        if order is None:
            order = {
                "order_id": command_id,
                "side": str(command.get("side") or ""),
                "event": event,
                "order_type": "market",
                "strategy_plan_id": command.get("strategy_plan_id"),
                "strategy_plan_version": command.get("strategy_plan_version"),
            }
            orders.append(order)
            order_by_id[command_id] = order
        order.update({
            "state": "filled",
            "price": price,
            "quantity": quantity,
            "ts": requested_at,
            "safe_action_market_gate": dict(gate),
        })
        fills.append({
            "fill_id": f"nautilus-{command_id}",
            "order_id": command_id,
            "trade_id": str(command.get("target_command_id") or target.get("trade_id") or ""),
            "cycle_id": str(command.get("cycle_id") or ""),
            "ts": requested_at,
            "side": str(command.get("side") or ""),
            "event": event,
            "price": price,
            "quantity": quantity,
            "requested_price": price,
            "slippage": 0.0,
            "cost": round(cost, 8),
            "liquidity": "taker",
            "order_type": "market",
            "strategy_plan_id": command.get("strategy_plan_id"),
            "strategy_plan_version": command.get("strategy_plan_version"),
            "safe_action_market_gate": dict(gate),
        })
        fill_order_ids.add(command_id)
        settlement_count += 1

    fills.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("order_id") or "")))
    realized = round(sum(float(row.get("realized_pnl") or 0.0) for row in positions), 8)
    unrealized = round(sum(
        (mark_price - float(row.get("entry_price") or 0.0))
        * float(row.get("remaining_units") or 0.0)
        * (1.0 if row.get("side") == "long" else -1.0)
        for row in positions
        if row.get("status") == "open"
    ), 8)
    return orders, fills, positions, realized, unrealized, settlement_count


def _paper_order_from_command(command_id: str, command: dict[str, Any]) -> dict[str, Any]:
    """Project a canceled-before-fill command that native replay intentionally omits."""

    return {
        "order_id": command_id,
        "state": "accepted",
        "side": str(command.get("side") or "").lower(),
        "event": str(command.get("event") or "entry").lower(),
        "order_type": str(command.get("order_type") or "market").lower(),
        "price": float(command.get("price") or 0.0),
        "quantity": float(command.get("quantity") or command.get("contracts") or 0.0),
        "ts": str(command.get("ts") or ""),
        "requested_price": float(command.get("requested_price") or command.get("price") or 0.0),
        "requested_quantity": float(
            command.get("requested_quantity")
            or command.get("quantity")
            or command.get("contracts")
            or 0.0
        ),
        "strategy_plan_id": command.get("strategy_plan_id"),
        "strategy_plan_version": command.get("strategy_plan_version"),
    }


def _valid_safe_action_gate(value: Any, *, action_class: str) -> bool:
    base = (
        isinstance(value, dict)
        and value.get("schema_version") == "paper-safe-action-market-gate-v1"
        and value.get("scope") == "paper_only"
        and value.get("action_class") == action_class
        and value.get("entry_market_gate_applies") is False
    )
    if not base:
        return False
    if action_class == "cancel":
        return value.get("pricing_required") is False and value.get("pricing_source") == "not_required"
    try:
        price = float(value.get("pricing_price") or 0.0)
    except (TypeError, ValueError):
        return False
    return (
        value.get("pricing_required") is True
        and value.get("pricing_source") in {
            "last_known_execution_event",
            "last_known_server_mark",
            "last_known_execution_fill",
            "position_cost_basis",
        }
        and price > 0
        and bool(str(value.get("pricing_timestamp") or ""))
        and bool(str(value.get("pricing_provider") or ""))
    )


def _safe_action_target_position(
    positions: list[dict[str, Any]],
    command: dict[str, Any],
) -> dict[str, Any]:
    trade_ids = {
        str(command.get("target_command_id") or ""),
        str(command.get("trade_id") or ""),
    } - {""}
    position_ids = {
        str(command.get("target_position_id") or ""),
        str(command.get("position_id") or ""),
    } - {""}
    open_positions = [row for row in positions if str(row.get("status") or "") == "open"]
    candidates = []
    if trade_ids:
        candidates = [row for row in open_positions if str(row.get("trade_id") or "") in trade_ids]
    if not candidates and position_ids:
        candidates = [
            row for row in open_positions if str(row.get("position_id") or "") in position_ids
        ]
    if len(candidates) != 1:
        raise ValueError("paper safe-action settlement could not resolve one open position")
    return candidates[0]


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
            "trade_id": str(command.get("trade_id") or command_id),
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
    raw_price = row.get("trigger_price") if event == "stop" else row.get("price")
    reported_price = _finite_float(raw_price)
    requested_price = _finite_float(command.get("price"))
    if reported_price is None and order_type != "market":
        raise ValueError("Nautilus order report price must be finite")
    quantity = _finite_float(row.get("quantity"))
    if quantity is None:
        raise ValueError("Nautilus order report quantity must be finite")
    status = str(row.get("status") or "").lower()
    state = {"submitted": "accepted", "accepted": "accepted"}.get(status, status)
    result = {
        "order_id": order_id,
        "state": state,
        "side": str(row.get("side") or command.get("side") or "").lower(),
        "event": event,
        "order_type": order_type,
        "quantity": quantity,
        "strategy_plan_id": command.get("strategy_plan_id"),
        "strategy_plan_version": command.get("strategy_plan_version"),
    }
    timestamp = _timestamp_text(row.get("ts_last") or row.get("ts_init"))
    if timestamp:
        result["ts"] = timestamp
    if reported_price is not None:
        result["price"] = reported_price
    if not child:
        if requested_price is not None:
            result["requested_price"] = requested_price
        requested_quantity = _finite_float(command.get("quantity"))
        result["requested_quantity"] = requested_quantity if requested_quantity is not None else quantity
    return result


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _require_strict_json(value: Any) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Nautilus replay result must contain only strict JSON values") from exc


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
        "funding": 0.0,
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
