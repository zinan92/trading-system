from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from schemas.market_data import Bar


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
) -> GridResult:
    rows = tuple(bars)
    if direction == 0 or not rows:
        return _idle()
    anchor = float(rows[0].open)
    spacing = anchor * float(spacing_bp) / 10_000.0
    half_width = float(range_k) * float(prev_range)
    n_rungs = min(int(max_rungs), int(half_width / spacing)) if spacing > 0 else 0
    if n_rungs < 1:
        return _idle()
    effective_notional = (float(max_rungs) * float(rung_notional) / n_rungs) if budget_sizing else float(rung_notional)

    sign = 1 if direction > 0 else -1
    levels = _levels(anchor, sign, spacing, n_rungs)
    active_stop = stop or GridStop(side="below" if sign > 0 else "above", price=anchor - sign * half_width)
    holding: dict[int, int] = {}
    fills: list[dict[str, Any]] = []
    gross_pnl = 0.0
    side_notional = 0.0
    sides = 0
    round_trips = 0
    stop_hit = False
    rearms = 0
    max_inventory = 0

    for i, bar in enumerate(rows):
        low, high = float(bar.low), float(bar.high)
        breached = _breached(bar, active_stop)
        for rung, level in enumerate(levels):
            if rung in holding:
                continue
            hits = low <= level if sign > 0 else high >= level
            if hits:
                holding[rung] = i
                sides += 1
                side_notional += effective_notional
                max_inventory = max(max_inventory, len(holding))
                fills.append(_fill(
                    cycle_id, fills, bar, side="buy" if sign > 0 else "sell",
                    price=level, notional=effective_notional, layer=layer,
                    rung=rung, event="entry", realized_pnl=-_cost(effective_notional, cost_per_side_bp),
                    sl=active_stop.price, tp=level + sign * spacing * tp_mult,
                ))
        if breached:
            exit_price = _stop_exit_price(bar, active_stop)
            for rung in list(holding):
                pnl = sign * (exit_price - levels[rung]) * _units(levels[rung], effective_notional)
                gross_pnl += pnl
                sides += 1
                side_notional += effective_notional
                fills.append(_fill(
                    cycle_id, fills, bar, side="sell" if sign > 0 else "buy",
                    price=exit_price, notional=effective_notional, layer=layer,
                    rung=rung, event="stop", realized_pnl=pnl - _cost(effective_notional, cost_per_side_bp),
                    sl=active_stop.price, tp=None,
                ))
            holding.clear()
            stop_hit = True
            if rearms < int(re_arm_max):
                rearms += 1
                re_anchor = float(bar.close)
                levels = _levels(re_anchor, sign, spacing, n_rungs)
                active_stop = GridStop(side="below" if sign > 0 else "above", price=re_anchor - sign * half_width)
                continue
            break
        for rung in list(holding):
            if holding[rung] >= i:
                continue
            target = levels[rung] + sign * spacing * float(tp_mult)
            done = high >= target if sign > 0 else low <= target
            if done:
                pnl = sign * (target - levels[rung]) * _units(levels[rung], effective_notional)
                gross_pnl += pnl
                sides += 1
                side_notional += effective_notional
                round_trips += 1
                fills.append(_fill(
                    cycle_id, fills, bar, side="sell" if sign > 0 else "buy",
                    price=target, notional=effective_notional, layer=layer,
                    rung=rung, event="target", realized_pnl=pnl - _cost(effective_notional, cost_per_side_bp),
                    sl=active_stop.price, tp=target,
                ))
                del holding[rung]

    if holding:
        last = rows[-1]
        last_close = float(last.close)
        for rung in list(holding):
            pnl = sign * (last_close - levels[rung]) * _units(levels[rung], effective_notional)
            gross_pnl += pnl
            sides += 1
            side_notional += effective_notional
            fills.append(_fill(
                cycle_id, fills, last, side="sell" if sign > 0 else "buy",
                price=last_close, notional=effective_notional, layer=layer,
                rung=rung, event="flatten", realized_pnl=pnl - _cost(effective_notional, cost_per_side_bp),
                sl=active_stop.price, tp=None,
            ))
        holding.clear()

    return GridResult(
        armed=True,
        traded=bool(fills),
        fills=fills,
        gross_pnl=gross_pnl,
        net_pnl=gross_pnl - _cost(side_notional, cost_per_side_bp),
        side_notional=side_notional,
        sides=sides,
        round_trips=round_trips,
        stop_hit=stop_hit,
        rearms=rearms,
        max_inventory=max_inventory,
    )


def _fill(
    cycle_id: str,
    fills: list[dict[str, Any]],
    bar: Bar,
    *,
    side: str,
    price: float,
    notional: float,
    layer: str,
    rung: int,
    event: str,
    realized_pnl: float,
    sl: float | None,
    tp: float | None,
) -> dict[str, Any]:
    return {
        "fill_id": f"{cycle_id}_{layer}_{len(fills) + 1:04d}",
        "ts": bar.timestamp,
        "side": side,
        "price": round(float(price), 8),
        "notional": round(float(notional), 8),
        "sl": None if sl is None else round(float(sl), 8),
        "tp": None if tp is None else round(float(tp), 8),
        "layer": layer,
        "order_type": "limit",
        "out_of_plan": False,
        "realized_pnl": float(realized_pnl),
        "event": event,
        "rung": rung,
    }


def _levels(anchor: float, sign: int, spacing: float, n_rungs: int) -> list[float]:
    return [anchor - sign * spacing * (i + 1) for i in range(n_rungs)]


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
