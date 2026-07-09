from __future__ import annotations

import math

from services.dualtrack_scoring import apply_unrealized


def test_apply_unrealized_calculates_open_long_and_short_without_mutating_input() -> None:
    trades = [
        {"trade_id": "long-a", "status": "open", "side": "long", "entry_price": 100.0, "remaining_units": 2.0},
        {"trade_id": "short-a", "status": "open", "side": "short", "entry_price": 100.0, "remaining_units": 3.0},
    ]

    result = apply_unrealized(trades, 105.0, mark_fresh=True)

    assert result[0]["unrealized_pnl"] == 10.0
    assert result[1]["unrealized_pnl"] == -15.0
    assert "unrealized_pnl" not in trades[0]
    assert "unrealized_pnl" not in trades[1]


def test_apply_unrealized_fail_closes_missing_stale_or_nonfinite_mark() -> None:
    trades = [{"trade_id": "open-a", "status": "open", "side": "long", "entry_price": 100.0, "remaining_units": 2.0}]

    assert apply_unrealized(trades, None)[0]["unrealized_pnl"] is None
    assert apply_unrealized(trades, float("nan"))[0]["unrealized_pnl"] is None
    assert apply_unrealized(trades, math.inf)[0]["unrealized_pnl"] is None
    assert apply_unrealized(trades, 105.0, mark_fresh=False)[0]["unrealized_pnl"] is None


def test_apply_unrealized_does_not_fill_closed_trade_unrealized_or_overwrite_realized() -> None:
    trades = [
        {
            "trade_id": "closed-a",
            "status": "closed",
            "side": "long",
            "entry_price": 100.0,
            "remaining_units": 0.0,
            "realized_pnl": 7.5,
        }
    ]

    result = apply_unrealized(trades, 110.0)

    assert result[0]["realized_pnl"] == 7.5
    assert "unrealized_pnl" not in result[0]
