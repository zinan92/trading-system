"""Deterministic, non-authorizing previews for natural-language strategies."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def build_deterministic_risk_preview(
    candidate: Mapping[str, Any],
    *,
    market: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    strategy_type = str(candidate.get("strategy_type") or "")
    if strategy_type == "dca":
        return _build_dca_preview(candidate)
    if strategy_type == "grid":
        return _build_grid_preview(candidate, market=market)
    return None


def _build_dca_preview(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    """Derive explicit DCA exposure and price-risk facts without execution."""

    if str(candidate.get("strategy_type") or "") != "dca":
        return None
    entries = candidate.get("entry_prices")
    if not isinstance(entries, (list, tuple)) or not entries:
        return None
    try:
        entry_prices = [float(value) for value in entries]
        stop = float(candidate.get("stop_price"))
        take_profit = float(candidate.get("take_profit_price"))
    except (TypeError, ValueError):
        return None
    try:
        per_entry_aum = float(candidate.get("entry_notional_aum_multiple"))
        sizing_basis = "explicit_entry_notional"
    except (TypeError, ValueError):
        try:
            maximum_leverage = float(candidate.get("maximum_leverage"))
        except (TypeError, ValueError):
            return None
        per_entry_aum = maximum_leverage / len(entry_prices)
        sizing_basis = "equal_split_of_maximum_leverage"
    values = [*entry_prices, per_entry_aum, stop, take_profit]
    if any(not math.isfinite(value) or value <= 0 for value in values):
        return None
    total_exposure = per_entry_aum * len(entry_prices)
    gross_stop_loss = sum(per_entry_aum * abs(stop - entry) / entry * 100 for entry in entry_prices)
    gross_take_profit = sum(per_entry_aum * abs(entry - take_profit) / entry * 100 for entry in entry_prices)
    derived_fields = {
        "total_entry_exposure_aum": round(total_exposure, 8),
        "gross_stop_loss_pct_aum": round(gross_stop_loss, 8),
        "gross_take_profit_pct_aum": round(gross_take_profit, 8),
        "fees_and_slippage": "not_included",
    }
    entry_text = "、".join(_format_number(entry) for entry in entry_prices)
    assistant_reply = (
        f"我理解为：做空 DCA，在 {entry_text} 分 {len(entry_prices)} 笔入场；"
        f"每笔名义仓位约 {per_entry_aum:g}×AUM，合计 {total_exposure:g}×AUM；"
        f"止损 {stop:g}，止盈 {take_profit:g}。\n"
        f"按所有入场全部成交计算，止损的最大亏损（毛价格）约为 {gross_stop_loss:.2f}% AUM；"
        "手续费和滑点尚未计入。当前只是策略草稿，确认前不会创建计划或下单。"
    )
    return {
        "risk_preview": {
            "status": "derived",
            "basis": f"{sizing_basis}_and_price_levels",
            "derived_fields": derived_fields,
        },
        "derived_fields": derived_fields,
        "assistant_reply": assistant_reply,
    }


def _build_grid_preview(
    candidate: Mapping[str, Any],
    *,
    market: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    direction = str(candidate.get("direction") or "")
    if direction not in {"long", "short", "neutral"}:
        return None
    try:
        lower = float(candidate.get("lower_price_boundary"))
        upper = float(candidate.get("upper_price_boundary"))
        order_count = int(candidate.get("order_count"))
        maximum_leverage = float(candidate.get("maximum_leverage"))
    except (TypeError, ValueError):
        return None
    if not lower > 0 or not upper > lower or order_count <= 0 or not maximum_leverage > 0:
        return None
    try:
        spacing = float(candidate.get("grid_spacing"))
    except (TypeError, ValueError):
        spacing = (upper - lower) / (order_count + 1)
    if not math.isfinite(spacing) or spacing <= 0:
        return None
    prices = [lower + spacing * (index + 1) for index in range(order_count)]
    if any(price <= lower or price >= upper for price in prices):
        return None
    current_price = None
    if isinstance(market, Mapping):
        try:
            current_price = float(market.get("price"))
        except (TypeError, ValueError):
            current_price = None
    if direction == "neutral" and (current_price is None or not lower < current_price < upper):
        return None
    explicit_stop = candidate.get("stop_price")
    if direction == "long":
        stop = float(explicit_stop) if explicit_stop not in (None, "") else lower
        losses = [abs(stop - price) / price * 100 for price in prices]
    elif direction == "short":
        stop = float(explicit_stop) if explicit_stop not in (None, "") else upper
        losses = [abs(stop - price) / price * 100 for price in prices]
    else:
        losses = [
            abs((lower if price < current_price else upper) - price) / price * 100
            for price in prices
        ]
    per_rung_aum = maximum_leverage / order_count
    gross_loss = sum(per_rung_aum * loss for loss in losses)
    derived_fields = {
        "total_entry_exposure_aum": round(maximum_leverage, 8),
        "grid_order_count": order_count,
        "gross_hard_stop_loss_pct_aum": round(gross_loss, 8),
        "fees_and_slippage": "not_included",
    }
    hard_stop = {"lower": lower, "upper": upper} if direction == "neutral" else stop
    assistant_reply = (
        f"我理解为：{direction} Grid，边界 {lower:g}~{upper:g}，"
        f"共 {order_count} 个网格档位，最大总敞口 {maximum_leverage:g}×AUM；"
        f"Hard Stop={hard_stop}。\n"
        f"按所有档位全部成交计算，触及 Hard Stop 的最大亏损（毛价格）约为 {gross_loss:.2f}% AUM；"
        "手续费和滑点尚未计入。当前只是策略草稿，确认前不会创建计划或下单。"
    )
    return {
        "risk_preview": {
            "status": "derived",
            "basis": "grid_geometry_and_maximum_leverage",
            "derived_fields": derived_fields,
        },
        "derived_fields": derived_fields,
        "assistant_reply": assistant_reply,
    }


def _format_number(value: float) -> str:
    return f"{value:g}"


__all__ = ["build_deterministic_risk_preview"]
