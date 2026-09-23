from __future__ import annotations

import pytest

from trading_strategy.grid_sizing import build_grid_preview, preview_id


CONFIG = {
    "capital_per_track_usd": 10_000,
    "max_leverage": 10,
    "cost_per_side_bp": 0.5,
    "execution_contract": {
        "schema_version": "dualtrack-execution-contract-v1",
        "execution_instrument_id": "XAUUSDT",
        "price_increment": "0.01",
        "quantity_increment": "0.001",
    },
    "strategy_grid": {
        "range_timeframe": "1d",
        "range_atr_period": 14,
        "spacing_timeframe": "4h",
        "spacing_atr_period": 14,
        "execution_timeframe": "1m",
        "min_grid_count": 30,
        "max_grid_count": 70,
        "cost_spacing_multiple": 5,
        "default_mode": "arithmetic",
        "capital_utilization_cap": 1,
        "required_leverage": 10,
        "min_net_profit_per_grid_usd": 10,
        "styles": {
            "steady": {
                "range_atr_multiple": 2,
                "spacing_atr_multiple": 0.25,
            },
            "aggressive": {
                "range_atr_multiple": 1,
                "spacing_atr_multiple": 0.125,
            },
        },
    },
}


def market(
    *,
    close: float = 4_137.44,
    fresh: bool = True,
    synthetic: bool = False,
) -> dict:
    bars = []
    for index in range(40):
        bar_close = close - 2.0 + index * 0.05
        bars.append(
            {
                "timestamp": f"2026-07-05T01:{index:02d}:00+00:00",
                "open": round(bar_close - 0.1, 4),
                "high": round(bar_close + 0.4, 4),
                "low": round(bar_close - 0.4, 4),
                "close": round(bar_close, 4),
            }
        )

    def context_bars(timeframe: str, span: float) -> list[dict]:
        rows = []
        for index in range(20):
            bar_close = close - 1.0 + index * 0.05
            rows.append(
                {
                    "timestamp": (
                        f"2026-06-{index + 1:02d}T00:00:00+00:00"
                        if timeframe == "1d"
                        else f"2026-07-02T{(index % 6) * 4:02d}:00:00+00:00"
                    ),
                    "open": round(bar_close - 0.1, 4),
                    "high": round(bar_close + span / 2.0, 4),
                    "low": round(bar_close - span / 2.0, 4),
                    "close": round(bar_close, 4),
                }
            )
        return rows

    return {
        "status": "ready" if fresh else "stale",
        "fresh": fresh,
        "is_synthetic": synthetic,
        "provider": "binance_usdm",
        "source_mode": "binance_usdm",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": close,
        "latest_timestamp": "2026-07-05T01:39:00+00:00",
        "bars": bars,
        "strategy_timeframes": {
            "1d": {
                "timeframe": "1d",
                "provider": "derived:binance_usdm",
                "is_synthetic": False,
                "bars": context_bars("1d", 10.0),
            },
            "4h": {
                "timeframe": "4h",
                "provider": "derived:binance_usdm",
                "is_synthetic": False,
                "bars": context_bars("4h", 4.0),
            },
        },
    }


def account() -> dict:
    return {"equity": 10_000.0}


def adaptive_payload(*, locks: list[str], **grid: float) -> dict:
    return {
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 4040.0, "high": 4200.0},
        "grid": {
            "mode": "arithmetic",
            "target_net_profit_per_grid_usd": 10.0,
            **grid,
        },
        "solver": {"mode": "manual_adaptive", "locked": locks},
    }


def adaptive_preview(payload: dict, *, market_data: dict | None = None) -> dict:
    return build_grid_preview(
        "2026-07-05_DAY",
        payload,
        market=market_data or market(),
        account=account(),
        config=CONFIG,
    )


def test_adaptive_solver_flags_narrow_locked_range_without_error() -> None:
    preview = adaptive_preview(adaptive_payload(locks=["range"]))

    assert 2 <= preview["grid"]["count"] < 30
    assert preview["grid"]["profit_target_met"] is True
    assert preview["risk"]["actual_leverage"] <= 10.0
    assert preview["solver"]["locked"] == ["range"]
    assert {row["code"] for row in preview["solver"]["risk_flags"]} == {
        "grid_count_outside_preferred_band"
    }


def test_adaptive_solver_preserves_locked_count_and_adapts_leverage() -> None:
    preview = adaptive_preview(
        adaptive_payload(locks=["range", "grid_count"], count=30)
    )

    assert preview["grid"]["count"] == 30
    assert preview["grid"]["profit_target_met"] is True
    assert 10.0 < preview["grid"]["leverage"] <= 20.0
    assert preview["risk"]["actual_leverage"] > 10.0
    assert "recommended_leverage_exceeded" in {
        row["code"] for row in preview["solver"]["risk_flags"]
    }


def test_adaptive_solver_keeps_locked_leverage_and_flags_profit_gap() -> None:
    payload = adaptive_payload(
        locks=["range", "grid_count", "leverage"],
        count=30,
    )
    payload["risk_budget"] = {"leverage": 10.0}
    preview = adaptive_preview(payload)

    assert preview["grid"]["count"] == 30
    assert preview["grid"]["leverage"] == 10.0
    assert preview["risk"]["actual_leverage"] <= 10.0
    assert preview["grid"]["profit_target_met"] is False
    assert "grid_profit_target_not_met" in {
        row["code"] for row in preview["solver"]["risk_flags"]
    }


def test_adaptive_solver_ignores_unlocked_range_and_profit_values() -> None:
    payload = adaptive_payload(locks=[])
    payload["grid"]["target_net_profit_per_grid_usd"] = 99.0
    preview = adaptive_preview(payload)

    assert (preview["range"]["low"], preview["range"]["high"]) != (
        4040.0,
        4200.0,
    )
    assert preview["grid"]["target_net_profit_per_grid_usd"] == 10.0


def test_adaptive_solver_preserves_locked_notional() -> None:
    preview = adaptive_preview(
        adaptive_payload(
            locks=["range", "notional_per_grid"],
            notional_per_grid=5000.0,
        )
    )

    assert preview["grid"]["notional_per_grid"] == 5000.0
    assert preview["grid"]["profit_target_met"] is True
    assert preview["risk"]["actual_leverage"] <= 10.0


def test_adaptive_solver_exposes_stable_alternatives_after_precision_search() -> None:
    payload = adaptive_payload(locks=["range"])
    payload["range"] = {"low": 109.65, "high": 110.34}
    payload["solver"]["current_grid_count"] = 40
    preview = adaptive_preview(payload, market_data=market(close=110.0))

    assert 2 <= preview["grid"]["count"] < 70
    assert {row["id"] for row in preview["solver"]["alternatives"]} == {
        "preserve_grid_count",
        "preserve_profit_target",
        "preserve_recommended_leverage",
    }


def test_adaptive_solver_caps_auto_sizing_at_manual_paper_capacity() -> None:
    payload = adaptive_payload(
        locks=["range", "grid_count", "profit_target"],
        count=70,
    )
    preview = adaptive_preview(payload)

    assert preview["grid"]["count"] == 70
    assert preview["risk"]["actual_leverage"] <= 20.0
    assert preview["grid"]["profit_target_met"] is False
    assert "grid_profit_target_not_met" in {
        row["code"] for row in preview["solver"]["risk_flags"]
    }


def test_auto_density_skips_counts_below_venue_price_precision() -> None:
    preview = build_grid_preview(
        "2026-07-05_DAY",
        {
            "direction": "neutral",
            "style": "steady",
            "range": {"low": 109.65, "high": 110.34},
        },
        market=market(close=110.0),
        account={"equity": 2_000_000.0},
        config=CONFIG,
    )

    assert 30 <= preview["grid"]["count"] < 70
    assert preview["grid"]["profit_target_met"] is True
    assert all(order["price"] != order["tp"] for order in preview["orders"])


def test_adaptive_solver_rejects_fractional_locked_count() -> None:
    with pytest.raises(ValueError, match="locked grid count must be an integer"):
        adaptive_preview(
            adaptive_payload(locks=["range", "grid_count"], count=30.4)
        )


def test_preview_identity_binds_solver_locks_and_locked_values() -> None:
    economic = {
        "cycle_id": "cycle",
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 100.0, "high": 120.0},
        "grid": {"count": 30, "notional_per_grid": 5000.0},
        "orders": [{"side": "buy", "price": 100.0, "quantity": 1.0}],
    }
    automatic = {
        **economic,
        "solver": {"mode": "manual_adaptive", "locked": [], "locked_inputs": {}},
    }
    locked = {
        **economic,
        "solver": {
            "mode": "manual_adaptive",
            "locked": ["grid_count"],
            "locked_inputs": {"grid_count": 30},
        },
    }

    assert preview_id(automatic) != preview_id(locked)


def test_adaptive_solver_rejects_stale_market() -> None:
    with pytest.raises(ValueError, match="stale"):
        adaptive_preview(
            {"direction": "neutral", "style": "steady"},
            market_data=market(fresh=False),
        )


def test_adaptive_solver_rejects_synthetic_market() -> None:
    with pytest.raises(ValueError, match="synthetic"):
        adaptive_preview(
            {"direction": "neutral", "style": "steady"},
            market_data=market(synthetic=True),
        )
