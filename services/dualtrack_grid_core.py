from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from schemas.market_data import Bar
from services.dualtrack_costs import dualtrack_order_cost


@dataclass(frozen=True)
class GridStop:
    side: str
    price: float
    confirm: str = "touch"


@dataclass(frozen=True)
class GridResult:
    armed: bool
    traded: bool
    fills: list[dict[str, Any]]
    gross_pnl: float
    net_pnl: float
    side_notional: float
    sides: int
    round_trips: int
    stop_hit: bool
    rearms: int
    max_inventory: int


def simulate_conditional_grid(
    *,
    cycle_id: str,
    bars: Iterable[Bar],
    direction: int,
    prev_range: float,
    spacing_bp: float,
    range_k: float,
    rung_notional: float,
    max_rungs: int,
    cost_per_side_bp: float,
    tp_mult: float = 1.0,
    re_arm_max: int = 0,
    budget_sizing: bool = False,
    layer: str = "grid",
    stop: GridStop | None = None,
    execution_cost_model: dict[str, Any] | None = None,
    cost_rules: dict[str, Any] | None = None,
) -> GridResult:
    rows = tuple(bars)
    if direction == 0 or not rows:
        return _idle()
    anchor = float(rows[0].open)
    spacing = anchor * float(spacing_bp) / 10_000.0
    sign = 1 if direction > 0 else -1
    plan_stop = _normalize_plan_stop(stop, sign)
    half_width = abs(anchor - plan_stop.price) if plan_stop else float(range_k) * float(prev_range)
    n_rungs = min(int(max_rungs), int(half_width / spacing)) if spacing > 0 else 0
    if n_rungs < 1:
        return _idle()
    effective_notional = (float(max_rungs) * float(rung_notional) / n_rungs) if budget_sizing else float(rung_notional)

    active_stop = plan_stop or GridStop(side="below" if sign > 0 else "above", price=anchor - sign * half_width)
    levels = (
        _levels_to_stop(anchor, sign, spacing, n_rungs, active_stop.price)
        if plan_stop
        else _levels(anchor, sign, spacing, n_rungs)
    )
    cost_config = {"cost_per_side_bp": cost_per_side_bp}
    if execution_cost_model:
        cost_config["execution_cost_model"] = execution_cost_model
    holding: dict[int, dict[str, Any]] = {}
    fills: list[dict[str, Any]] = []
    gross_pnl = 0.0
    total_cost = 0.0
    side_notional = 0.0
    sides = 0
    round_trips = 0
    stop_hit = False
    rearms = 0
    max_inventory = 0

    for i, bar in enumerate(rows):
        low, high = float(bar.low), float(bar.high)
        breached = _breached(bar, active_stop)
        # OHLC does not tell us the intrabar path.  When a bar touches a hard
        # stop and one or more entry levels, resolve the ambiguity
        # conservatively: the hard invalidation wins, so no new position may
        # be opened on an already-invalid bar.
        if breached and plan_stop is not None:
            exit_price = _stop_exit_price(bar, active_stop)
            for rung in list(holding):
                entry = holding[rung]
                exit_notional = _matched_exit_notional(entry, entry_price=levels[rung], exit_price=exit_price)
                exit_cost = dualtrack_order_cost(
                    config=cost_config,
                    price=exit_price,
                    notional=exit_notional,
                    contracts=entry.get("contracts"),
                    cost_rules=cost_rules,
                )
                pnl = sign * (exit_price - levels[rung]) * _units(levels[rung], float(entry["notional"]))
                gross_pnl += pnl
                sides += 1
                side_notional += exit_cost.notional
                total_cost += exit_cost.cost
                exit_fill = _fill(
                    cycle_id, fills, bar, side="sell" if sign > 0 else "buy",
                    price=exit_price, order_cost=exit_cost, layer=layer,
                    rung=rung, event="stop", realized_pnl=pnl - exit_cost.cost,
                    sl=active_stop.price, tp=None,
                )
                exit_fill["matched_entries"] = [_matched_entry(entry, levels[rung], pnl, exit_cost.cost)]
                fills.append(exit_fill)
            holding.clear()
            stop_hit = True
            # A plan invalidation is terminal for the cycle.  Only synthetic
            # range stops may re-arm, and only from the next bar.
            if plan_stop is not None:
                break
            if rearms < int(re_arm_max):
                rearms += 1
                re_anchor = float(bar.close)
                active_stop = GridStop(side="below" if sign > 0 else "above", price=re_anchor - sign * half_width)
                levels = _levels(re_anchor, sign, spacing, n_rungs)
                continue
            break
        for rung, level in enumerate(levels):
            if rung in holding:
                continue
            hits = low <= level if sign > 0 else high >= level
            if hits:
                entry_cost = dualtrack_order_cost(
                    config=cost_config,
                    price=level,
                    notional=effective_notional,
                    contracts=_contracts_per_rung(execution_cost_model),
                    cost_rules=cost_rules,
                )
                entry_fill = _fill(
                    cycle_id, fills, bar, side="buy" if sign > 0 else "sell",
                    price=level, order_cost=entry_cost, layer=layer,
                    rung=rung, event="entry", realized_pnl=-entry_cost.cost,
                    sl=active_stop.price, tp=level + sign * spacing * tp_mult,
                )
                holding[rung] = {
                    "bar_index": i,
                    "notional": entry_cost.notional,
                    "contracts": entry_cost.contracts,
                    "trade_id": entry_fill["fill_id"],
                }
                sides += 1
                side_notional += entry_cost.notional
                total_cost += entry_cost.cost
                max_inventory = max(max_inventory, len(holding))
                fills.append(entry_fill)
        if breached:
            exit_price = _stop_exit_price(bar, active_stop)
            for rung in list(holding):
                entry = holding[rung]
                exit_notional = _matched_exit_notional(entry, entry_price=levels[rung], exit_price=exit_price)
                exit_cost = dualtrack_order_cost(
                    config=cost_config,
                    price=exit_price,
                    notional=exit_notional,
                    contracts=entry.get("contracts"),
                    cost_rules=cost_rules,
                )
                pnl = sign * (exit_price - levels[rung]) * _units(levels[rung], float(entry["notional"]))
                gross_pnl += pnl
                sides += 1
                side_notional += exit_cost.notional
                total_cost += exit_cost.cost
                exit_fill = _fill(
                    cycle_id, fills, bar, side="sell" if sign > 0 else "buy",
                    price=exit_price, order_cost=exit_cost, layer=layer,
                    rung=rung, event="stop", realized_pnl=pnl - exit_cost.cost,
                    sl=active_stop.price, tp=None,
                )
                exit_fill["matched_entries"] = [_matched_entry(entry, levels[rung], pnl, exit_cost.cost)]
                fills.append(exit_fill)
            holding.clear()
            stop_hit = True
            if rearms < int(re_arm_max):
                rearms += 1
                re_anchor = float(bar.close)
                active_stop = GridStop(side="below" if sign > 0 else "above", price=re_anchor - sign * half_width)
                levels = _levels(re_anchor, sign, spacing, n_rungs)
                continue
            break
        for rung in list(holding):
            if int(holding[rung]["bar_index"]) >= i:
                continue
            target = levels[rung] + sign * spacing * float(tp_mult)
            done = high >= target if sign > 0 else low <= target
            if done:
                entry = holding[rung]
                exit_notional = _matched_exit_notional(entry, entry_price=levels[rung], exit_price=target)
                exit_cost = dualtrack_order_cost(
                    config=cost_config,
                    price=target,
                    notional=exit_notional,
                    contracts=entry.get("contracts"),
                    cost_rules=cost_rules,
                )
                pnl = sign * (target - levels[rung]) * _units(levels[rung], float(entry["notional"]))
                gross_pnl += pnl
                sides += 1
                side_notional += exit_cost.notional
                total_cost += exit_cost.cost
                round_trips += 1
                exit_fill = _fill(
                    cycle_id, fills, bar, side="sell" if sign > 0 else "buy",
                    price=target, order_cost=exit_cost, layer=layer,
                    rung=rung, event="target", realized_pnl=pnl - exit_cost.cost,
                    sl=active_stop.price, tp=target,
                )
                exit_fill["matched_entries"] = [_matched_entry(entry, levels[rung], pnl, exit_cost.cost)]
                fills.append(exit_fill)
                del holding[rung]

    if holding:
        last = rows[-1]
        last_close = float(last.close)
        for rung in list(holding):
            entry = holding[rung]
            exit_notional = _matched_exit_notional(entry, entry_price=levels[rung], exit_price=last_close)
            exit_cost = dualtrack_order_cost(
                config=cost_config,
                price=last_close,
                notional=exit_notional,
                contracts=entry.get("contracts"),
                cost_rules=cost_rules,
            )
            pnl = sign * (last_close - levels[rung]) * _units(levels[rung], float(entry["notional"]))
            gross_pnl += pnl
            sides += 1
            side_notional += exit_cost.notional
            total_cost += exit_cost.cost
            exit_fill = _fill(
                cycle_id, fills, last, side="sell" if sign > 0 else "buy",
                price=last_close, order_cost=exit_cost, layer=layer,
                rung=rung, event="flatten", realized_pnl=pnl - exit_cost.cost,
                sl=active_stop.price, tp=None,
            )
            exit_fill["matched_entries"] = [_matched_entry(entry, levels[rung], pnl, exit_cost.cost)]
            fills.append(exit_fill)
        holding.clear()

    return GridResult(
        armed=True,
        traded=bool(fills),
        fills=fills,
        gross_pnl=gross_pnl,
        net_pnl=gross_pnl - total_cost,
        side_notional=side_notional,
        sides=sides,
        round_trips=round_trips,
        stop_hit=stop_hit,
        rearms=rearms,
        max_inventory=max_inventory,
    )


def simulate_explicit_grid(
    *,
    cycle_id: str,
    bars: Iterable[Bar],
    direction: int,
    orders: list[dict[str, Any]],
    stop: GridStop,
    rung_notional: float,
    cost_per_side_bp: float,
    finalize: bool,
    layer: str = "ai_grid",
    execution_cost_model: dict[str, Any] | None = None,
    cost_rules: dict[str, Any] | None = None,
) -> GridResult:
    """Replay exact AI-authored grid orders without deriving hidden levels."""
    rows = tuple(bars)
    sign = 1 if direction > 0 else -1 if direction < 0 else 0
    if not rows or sign == 0 or not orders:
        return _idle()
    active_stop = _normalize_plan_stop(stop, sign)
    if active_stop is None:
        raise ValueError("explicit grid requires a directionally valid stop")
    cost_config: dict[str, Any] = {"cost_per_side_bp": cost_per_side_bp}
    if execution_cost_model:
        cost_config["execution_cost_model"] = execution_cost_model

    holdings: dict[int, dict[str, Any]] = {}
    entered: set[int] = set()
    fills: list[dict[str, Any]] = []
    gross_pnl = 0.0
    total_cost = 0.0
    side_notional = 0.0
    sides = 0
    round_trips = 0
    stop_hit = False
    max_inventory = 0

    for bar_index, bar in enumerate(rows):
        if _breached(bar, active_stop):
            stop_hit = True
            exit_price = _stop_exit_price(bar, active_stop)
            for rung, holding in list(holdings.items()):
                exit_fill, pnl, exit_cost = _close_explicit_holding(
                    cycle_id,
                    fills,
                    bar,
                    sign=sign,
                    rung=rung,
                    order=orders[rung],
                    holding=holding,
                    price=exit_price,
                    event="stop",
                    stop=active_stop,
                    layer=layer,
                    cost_config=cost_config,
                    cost_rules=cost_rules,
                )
                fills.append(exit_fill)
                _mark_entry_closed(fills, holding, exit_fill)
                gross_pnl += pnl
                total_cost += exit_cost.cost
                side_notional += exit_cost.notional
                sides += 1
            holdings.clear()
            break

        low, high = float(bar.low), float(bar.high)
        for rung, order in enumerate(orders):
            if rung in entered:
                continue
            entry = float(order["entry"])
            touched = low <= entry if sign > 0 else high >= entry
            if not touched:
                continue
            weight = float(order.get("weight", 1.0))
            requested_notional = float(order.get("notional") or (float(rung_notional) * weight))
            contracts = _weighted_contracts(execution_cost_model, weight)
            entry_cost = dualtrack_order_cost(
                config=cost_config,
                price=entry,
                notional=requested_notional,
                contracts=contracts,
                cost_rules=cost_rules,
            )
            entry_fill = _explicit_fill(
                cycle_id,
                fills,
                bar,
                side="buy" if sign > 0 else "sell",
                price=entry,
                order_cost=entry_cost,
                layer=layer,
                rung=rung,
                event="entry",
                realized_pnl=-entry_cost.cost,
                sl=active_stop.price,
                tp=float(order["take_profit"]),
            )
            holdings[rung] = {
                "bar_index": bar_index,
                "notional": entry_cost.notional,
                "contracts": entry_cost.contracts,
                "trade_id": entry_fill["trade_id"],
                "fill_id": entry_fill["fill_id"],
                "units": _units(entry, entry_cost.notional) if entry_cost.contracts is None else None,
            }
            entered.add(rung)
            fills.append(entry_fill)
            total_cost += entry_cost.cost
            side_notional += entry_cost.notional
            sides += 1
            max_inventory = max(max_inventory, len(holdings))

        for rung, holding in list(holdings.items()):
            if int(holding["bar_index"]) >= bar_index:
                continue
            target = float(orders[rung]["take_profit"])
            touched = high >= target if sign > 0 else low <= target
            if not touched:
                continue
            exit_fill, pnl, exit_cost = _close_explicit_holding(
                cycle_id,
                fills,
                bar,
                sign=sign,
                rung=rung,
                order=orders[rung],
                holding=holding,
                price=target,
                event="target",
                stop=active_stop,
                layer=layer,
                cost_config=cost_config,
                cost_rules=cost_rules,
            )
            fills.append(exit_fill)
            _mark_entry_closed(fills, holding, exit_fill)
            gross_pnl += pnl
            total_cost += exit_cost.cost
            side_notional += exit_cost.notional
            sides += 1
            round_trips += 1
            del holdings[rung]

    if finalize and holdings:
        last = rows[-1]
        for rung, holding in list(holdings.items()):
            exit_fill, pnl, exit_cost = _close_explicit_holding(
                cycle_id,
                fills,
                last,
                sign=sign,
                rung=rung,
                order=orders[rung],
                holding=holding,
                price=float(last.close),
                event="flatten",
                stop=active_stop,
                layer=layer,
                cost_config=cost_config,
                cost_rules=cost_rules,
            )
            fills.append(exit_fill)
            _mark_entry_closed(fills, holding, exit_fill)
            gross_pnl += pnl
            total_cost += exit_cost.cost
            side_notional += exit_cost.notional
            sides += 1
        holdings.clear()

    return GridResult(
        armed=True,
        traded=bool(fills),
        fills=fills,
        gross_pnl=round(gross_pnl, 8),
        net_pnl=round(gross_pnl - total_cost, 8),
        side_notional=round(side_notional, 8),
        sides=sides,
        round_trips=round_trips,
        stop_hit=stop_hit,
        rearms=0,
        max_inventory=max_inventory,
    )


def _close_explicit_holding(
    cycle_id: str,
    fills: list[dict[str, Any]],
    bar: Bar,
    *,
    sign: int,
    rung: int,
    order: dict[str, Any],
    holding: dict[str, Any],
    price: float,
    event: str,
    stop: GridStop,
    layer: str,
    cost_config: dict[str, Any],
    cost_rules: dict[str, Any] | None,
) -> tuple[dict[str, Any], float, Any]:
    entry = float(order["entry"])
    exit_notional = _matched_exit_notional(holding, entry_price=entry, exit_price=price)
    exit_cost = dualtrack_order_cost(
        config=cost_config,
        price=price,
        notional=exit_notional,
        contracts=holding.get("contracts"),
        cost_rules=cost_rules,
    )
    units = _units(entry, float(holding["notional"]))
    pnl = sign * (float(price) - entry) * units
    fill = _explicit_fill(
        cycle_id,
        fills,
        bar,
        side="sell" if sign > 0 else "buy",
        price=price,
        order_cost=exit_cost,
        layer=layer,
        rung=rung,
        event=event,
        realized_pnl=pnl - exit_cost.cost,
        sl=stop.price,
        tp=float(order["take_profit"]) if event != "stop" else None,
        gross_pnl=pnl,
    )
    fill["matched_entries"] = [{
        "fill_id": holding["fill_id"],
        "trade_id": holding["trade_id"],
        "units": units,
        "entry_price": entry,
        "gross_pnl": round(pnl, 8),
        "realized_pnl": round(pnl - exit_cost.cost, 8),
    }]
    return fill, pnl, exit_cost


def _explicit_fill(
    cycle_id: str,
    fills: list[dict[str, Any]],
    bar: Bar,
    *,
    side: str,
    price: float,
    order_cost: Any,
    layer: str,
    rung: int,
    event: str,
    realized_pnl: float,
    sl: float | None,
    tp: float | None,
    gross_pnl: float = 0.0,
) -> dict[str, Any]:
    trade_id = f"{cycle_id}_{layer}_trade_{rung + 1:04d}"
    units = _units(float(price), float(order_cost.notional))
    return {
        "fill_id": f"{cycle_id}_{layer}_{len(fills) + 1:04d}",
        "trade_id": trade_id,
        "ts": bar.timestamp,
        "side": side,
        "price": round(float(price), 8),
        "sl": None if sl is None else round(float(sl), 8),
        "tp": None if tp is None else round(float(tp), 8),
        "layer": layer,
        "position_id": f"{layer}_{rung}",
        "order_type": "limit" if event in {"entry", "target"} else "market",
        "out_of_plan": False,
        "gross_pnl": round(float(gross_pnl), 8),
        "realized_pnl": round(float(realized_pnl), 8),
        "event": event,
        "rung": rung,
        "pnl_units": round(units, 10),
        "remaining_units": round(units, 10) if event == "entry" else 0.0,
        "position_status": "open" if event == "entry" else "closed",
        **order_cost.fill_fields(),
    }


def _mark_entry_closed(fills: list[dict[str, Any]], holding: dict[str, Any], exit_fill: dict[str, Any]) -> None:
    units = float((exit_fill.get("matched_entries") or [{}])[0].get("units") or 0.0)
    for fill in fills:
        if fill.get("fill_id") != holding.get("fill_id"):
            continue
        fill["remaining_units"] = 0.0
        fill["closed_units"] = round(units, 10)
        fill["position_status"] = "closed"
        return


def _weighted_contracts(execution_cost_model: dict[str, Any] | None, weight: float) -> float | None:
    base = _contracts_per_rung(execution_cost_model)
    if base is None:
        return None
    contracts = float(base) * float(weight)
    if contracts <= 0 or not contracts.is_integer():
        raise ValueError("explicit grid contract weights must produce a positive whole contract count")
    return contracts


def _fill(
    cycle_id: str,
    fills: list[dict[str, Any]],
    bar: Bar,
    *,
    side: str,
    price: float,
    order_cost: Any,
    layer: str,
    rung: int,
    event: str,
    realized_pnl: float,
    sl: float | None,
    tp: float | None,
) -> dict[str, Any]:
    fill = {
        "fill_id": f"{cycle_id}_{layer}_{len(fills) + 1:04d}",
        "ts": bar.timestamp,
        "side": side,
        "price": round(float(price), 8),
        "sl": None if sl is None else round(float(sl), 8),
        "tp": None if tp is None else round(float(tp), 8),
        "layer": layer,
        "order_type": "limit",
        "out_of_plan": False,
        "realized_pnl": float(realized_pnl),
        "event": event,
        "rung": rung,
    }
    fill.update(order_cost.fill_fields())
    return fill


def _levels(anchor: float, sign: int, spacing: float, n_rungs: int) -> list[float]:
    return [anchor - sign * spacing * (i + 1) for i in range(n_rungs)]


def _levels_to_stop(anchor: float, sign: int, spacing: float, n_rungs: int, stop_price: float) -> list[float]:
    levels = [anchor - sign * spacing * (i + 1) for i in range(max(0, n_rungs - 1))]
    levels.append(float(stop_price))
    if sign > 0:
        return [level for level in levels if level >= float(stop_price)]
    return [level for level in levels if level <= float(stop_price)]


def _normalize_plan_stop(stop: GridStop | None, sign: int) -> GridStop | None:
    if stop is None:
        return None
    expected_side = "below" if sign > 0 else "above"
    if stop.side != expected_side:
        return None
    return stop


def _breached(bar: Bar, stop: GridStop) -> bool:
    price = float(stop.price)
    if stop.side == "below":
        return float(bar.close if stop.confirm == "close_1m" else bar.low) <= price
    if stop.side == "above":
        return float(bar.close if stop.confirm == "close_1m" else bar.high) >= price
    raise ValueError("stop.side must be below or above")


def _stop_exit_price(bar: Bar, stop: GridStop) -> float:
    price = float(stop.price)
    return min(float(bar.open), price) if stop.side == "below" else max(float(bar.open), price)


def _units(level: float, notional: float) -> float:
    return notional / level if level > 0 else 0.0


def _matched_exit_notional(entry: dict[str, Any], *, entry_price: float, exit_price: float) -> float:
    if entry.get("contracts") is not None:
        return float(entry["notional"])
    return _units(entry_price, float(entry["notional"])) * float(exit_price)


def _matched_entry(entry: dict[str, Any], entry_price: float, gross_pnl: float, exit_cost: float) -> dict[str, Any]:
    return {
        "trade_id": str(entry["trade_id"]),
        "units": _units(entry_price, float(entry["notional"])),
        "gross_pnl": round(float(gross_pnl), 8),
        "realized_pnl": round(float(gross_pnl) - float(exit_cost), 8),
    }


def _contracts_per_rung(execution_cost_model: dict[str, Any] | None) -> float | None:
    if not execution_cost_model or not execution_cost_model.get("venue"):
        return None
    value = execution_cost_model.get("contracts_per_rung", execution_cost_model.get("min_contracts", 1))
    contracts = float(value)
    if contracts <= 0:
        raise ValueError("contracts_per_rung must be positive")
    if not contracts.is_integer():
        raise ValueError("contracts_per_rung must be a whole number")
    return contracts


def _cost(notional: float, cost_per_side_bp: float) -> float:
    return float(notional) * float(cost_per_side_bp) / 10_000.0


def _idle() -> GridResult:
    return GridResult(
        armed=False,
        traded=False,
        fills=[],
        gross_pnl=0.0,
        net_pnl=0.0,
        side_notional=0.0,
        sides=0,
        round_trips=0,
        stop_hit=False,
        rearms=0,
        max_inventory=0,
    )
