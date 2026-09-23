"""Pure direction-aware marketability rules for grid entry geometry."""

from __future__ import annotations


def market_is_on_non_entry_side(
    direction: str,
    market_price: float,
    range_low: float,
    range_high: float,
) -> bool:
    """Return true when an out-of-range mark cannot cross an entry order.

    Long grids contain buy entries, so a mark above the Range leaves every
    entry below market.  Short grids contain sell entries, so a mark below the
    Range leaves every entry above market.  The opposite sides remain unsafe.
    """

    normalized = str(direction or "").lower()
    return (normalized == "long" and market_price > range_high) or (
        normalized == "short" and market_price < range_low
    )


def market_outside_range_requires_blocker(
    direction: str,
    market_price: float,
    range_low: float,
    range_high: float,
) -> bool:
    """Return true only when outside-Range geometry can market an entry."""

    if range_low <= market_price <= range_high:
        return False
    return not market_is_on_non_entry_side(
        direction,
        market_price,
        range_low,
        range_high,
    )
