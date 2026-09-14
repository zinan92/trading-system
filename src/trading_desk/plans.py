"""Indicative plans from Park's direction. The desk never submits orders; approval is handed to the operator."""
from __future__ import annotations

from typing import Any

TEMPLATE = {
    # BTC matches Park's approved f107 shape: 5 rungs x 19 USDC, range ~4.5% under price, stop ~2.7% beyond.
    "BTC": {"rungs": 5, "notional": 19.0, "range_pct": 0.045, "stop_pct": 0.027, "tick": 1.0, "unit": "USDC"},
    # XAU matches the running paper grid: 10 rungs x 0.2 oz, 200-point range, stop 50 beyond.
    "XAU": {"rungs": 10, "units": 0.2, "range_pct": 0.046, "stop_pct": 0.0116, "tick": 0.01, "unit": "USDT"},
}


def max_loss(asset: str, rungs: list[float], stop: float, *, notional: float | None = None, units: float | None = None) -> float:
    total = 0.0
    for price in rungs:
        qty = units if units is not None else (notional or 0.0) / price
        total += qty * abs(price - stop)
    return round(total, 2)


def build_plan(asset: str, direction: str, price: float | None, grid: dict[str, Any] | None) -> dict[str, Any]:
    if direction not in {"long", "short", "flat"}:
        raise ValueError("direction_invalid")
    grid = grid or {}
    running = bool(grid.get("ok")) and grid.get("status") not in {None, "terminal", "stopped_by_operator", "TERMINAL"}
    if direction == "flat":
        return {
            "kind": "hold", "title": "观望 · 不开新单", "direction": "flat",
            "lines": [
                ["现有网格", "保持不动" if running else "当前没有运行中的网格"],
                ["挂单", f"{grid.get('open_orders', 0)} 张继续挂着" if running else "无"],
            ],
            "note": "想更保守，可以在批准时写明「撤掉现有网格」，由执行员按流程停止。",
        }
    if price is None:
        return {"kind": "unavailable", "title": "现价读不到，暂时生成不了计划", "direction": direction, "lines": [], "note": "行情恢复后重新点一次方向。"}
    if running and grid.get("direction") == direction and grid.get("rungs"):
        rungs = [float(r) for r in grid["rungs"]]
        stop = float(grid["hard_stop"])
        tpl = TEMPLATE[asset]
        loss = grid.get("max_loss") or max_loss(asset, rungs, stop, notional=grid.get("notional_per_rung") or tpl.get("notional"), units=grid.get("units_per_rung") if asset == "XAU" else None)
        return {
            "kind": "keep", "title": f"{'做多' if direction == 'long' else '做空'}网格 · 沿用正在跑的这一套", "direction": direction,
            "range": [grid.get("lower"), grid.get("upper")], "rungs": rungs, "hard_stop": stop, "max_loss": round(float(loss), 2),
            "lines": [["区间", f"{_fmt(grid.get('lower'))} – {_fmt(grid.get('upper'))}"], ["硬止损", _fmt(stop)], ["最多亏", f"{float(loss):,.2f} {tpl['unit']}"]],
            "note": "方向和正在跑的网格一致，不需要改动。" + ("现价在区间上方，买单挂着等回落。" if direction == "long" and grid.get("upper") and price > float(grid["upper"]) else ""),
        }
    tpl = TEMPLATE[asset]
    width = price * tpl["range_pct"]
    if direction == "long":
        lower, upper = price - width, price
        stop = lower * (1 - tpl["stop_pct"])
    else:
        lower, upper = price, price + width
        stop = upper * (1 + tpl["stop_pct"])
    step = (upper - lower) / tpl["rungs"]
    rungs = [round(_tick(lower + step * i if direction == "long" else upper - step * i, tpl["tick"]), 2) for i in range(tpl["rungs"])]
    rungs.sort()
    stop = round(_tick(stop, tpl["tick"]), 2)
    loss = max_loss(asset, rungs, stop, notional=tpl.get("notional"), units=tpl.get("units"))
    size = f"{tpl['notional']:.0f} USDC" if "notional" in tpl else f"{tpl['units']} 盎司"
    replace_note = "会先停掉正在跑的反方向网格，两个方向不同时开。" if running else ""
    return {
        "kind": "new", "title": f"{'做多' if direction == 'long' else '做空'}网格 · 新参数，需要你批准", "direction": direction,
        "range": [round(lower, 2), round(upper, 2)], "rungs": rungs, "hard_stop": stop, "max_loss": loss,
        "lines": [["区间", f"{_fmt(lower)} – {_fmt(upper)}"], [f"{'买单' if direction == 'long' else '卖单'}", f"{len(rungs)} 张 × {size}"],
                  ["硬止损", _fmt(stop)], ["最多亏", f"{loss:,.2f} {tpl['unit']}"]],
        "note": replace_note + "批准后交给执行员走预览和确认流程，本页不直接下单。",
    }


def _tick(value: float, tick: float) -> float:
    return round(round(value / tick) * tick, 2)


def _fmt(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:,.0f}" if number >= 1000 and number == int(number) else f"{number:,.2f}".rstrip("0").rstrip(".")
