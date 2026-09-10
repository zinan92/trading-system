"""Shared risk calculations for the canonical Grid preview and lifecycle."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def full_depth_loss(
    rungs: Sequence[Mapping[str, Any]],
    hard_stop: float | Mapping[str, Any] | None = None,
) -> float:
    """Return modeled loss if every rung reaches its canonical hard stop.

    ``hard_stop`` may be one operator value or a side mapping.  When omitted,
    the already-normalized rung stop is authoritative.
    """

    total = 0.0
    for rung in rungs:
        side = str(rung.get("side") or "").lower()
        if isinstance(hard_stop, Mapping):
            stop = hard_stop.get("long" if side == "buy" else "short")
            if stop in (None, ""):
                stop = hard_stop.get(side)
        else:
            stop = hard_stop
        if stop in (None, ""):
            stop = rung.get("hard_stop", rung.get("sl"))
        if stop in (None, ""):
            raise ValueError("grid hard stop is required")
        price = float(rung["price"])
        quantity = float(rung.get("quantity") or rung.get("size") or 0.0)
        stop_price = float(stop)
        total += (
            (price - stop_price) if side == "buy" else (stop_price - price)
        ) * quantity
    return total
