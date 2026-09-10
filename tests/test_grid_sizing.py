from pathlib import Path

import pytest

from services import grid_sizing
from services.grid_risk import full_depth_loss
from services.strategy_control_plane import StrategyControlPlane


def market(*, close: float = 110.0, fresh: bool = True) -> dict:
    bars = []
    for index in range(40):
        bar_close = close - 2.0 + index * 0.05
        bars.append({
            "timestamp": f"2026-07-05T01:{index:02d}:00+00:00",
            "open": round(bar_close - 0.1, 4),
            "high": round(bar_close + 0.4, 4),
            "low": round(bar_close - 0.4, 4),
            "close": round(bar_close, 4),
        })

    def context_bars(timeframe: str, span: float) -> list[dict]:
        rows = []
        for index in range(20):
            bar_close = close - 1.0 + index * 0.05
            rows.append({
                "timestamp": f"2026-06-{index + 1:02d}T00:00:00+00:00" if timeframe == "1d" else f"2026-07-02T{(index % 6) * 4:02d}:00:00+00:00",
                "open": round(bar_close - 0.1, 4),
                "high": round(bar_close + span / 2.0, 4),
                "low": round(bar_close - span / 2.0, 4),
                "close": round(bar_close, 4),
            })
        return rows

    return {
        "status": "ready" if fresh else "stale",
        "fresh": fresh,
        "is_synthetic": False,
        "provider": "binance_usdm",
        "source_mode": "binance_usdm",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": close,
        "latest_timestamp": "2026-07-05T01:39:00+00:00",
        "bars": bars,
        "strategy_timeframes": {
            "1d": {"timeframe": "1d", "provider": "derived:binance_usdm", "is_synthetic": False, "bars": context_bars("1d", 10.0)},
            "4h": {"timeframe": "4h", "provider": "derived:binance_usdm", "is_synthetic": False, "bars": context_bars("4h", 4.0)},
        },
    }


def account() -> dict:
    return {"equity": 10_000.0}


def frozen_production_grid_request() -> dict:
    return {
        "direction": "neutral",
        "style": "steady",
        "range": {
            "low": 3903.5986,
            "high": 4226.6814,
            "scope": "neutral_side",
            "split_price": 4065.14,
            "source_envelope": {
                "low": 3903.5986,
                "high": 4226.6814,
            },
        },
        "grid": {
            "count": 38,
            "mode": "arithmetic",
            "notional_per_grid": 5265.97,
            "notional_mode": "manual",
        },
        "risk_budget": {"leverage": 10},
    }


def test_average_true_range_known_value() -> None:
    bars = [
        {"high": 12.0, "low": 10.0, "close": 11.0},
        {"high": 13.0, "low": 11.0, "close": 12.0},
        {"high": 14.0, "low": 12.0, "close": 13.0},
    ]
    # TR for bar2 = max(2, |13-11|, |11-11|) = 2; bar3 = max(2, |14-12|, |12-12|) = 2
    assert grid_sizing.average_true_range(bars, period=2) == pytest.approx(2.0)


def test_frozen_grid_capacity_shift_is_typed_without_becoming_transient(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    payload = frozen_production_grid_request()

    split = grid_sizing.build_grid_preview(
        "2026-08-03_DAY",
        payload,
        market=market(close=4065.14),
        account={"equity": 10005.35},
        config=plane.config,
    )
    assert split["risk"]["max_simultaneous_same_side_levels"] == 19
    assert split["risk"]["profit_target_met"] is True

    with pytest.raises(
        grid_sizing.GridPreviewInfeasibleError
    ) as captured:
        grid_sizing.build_grid_preview(
            "2026-08-03_DAY",
            payload,
            market=market(close=4071.19),
            account={"equity": 10005.35},
            config=plane.config,
        )
    assert captured.value.code == "grid_preview_infeasible"
    assert captured.value.evidence["reasons"] == [
        "capital_capacity_exceeded"
    ]
    assert captured.value.evidence["side_counts"] == {
        "buy": 20,
        "sell": 18,
    }
    assert captured.value.evidence["max_side_notional"] > (
        captured.value.evidence["capital_budget"]
    )


def test_average_true_range_requires_history() -> None:
    with pytest.raises(ValueError):
        grid_sizing.average_true_range([{"high": 1, "low": 1, "close": 1}], period=14)


@pytest.mark.parametrize(
    ("price", "max_decimal_places", "expected"),
    [
        (75688.6, 1, 75689.0),  # BTC: 5 significant digits, one decimal place allowed
        (3123.45, 2, 3123.5),   # ETH-like precision: 5 significant digits
        (0.00123456, 6, 0.001235),  # low-priced asset: decimal cap is not the only rule
    ],
)
def test_hyperliquid_grid_prices_quantize_to_venue_precision(
    price: float, max_decimal_places: int, expected: float,
) -> None:
    increment = grid_sizing.venue_price_increment(price, max_decimal_places=max_decimal_places)
    assert float(increment) > 0
    config = {
        "execution_contract": {
            "price_increment": str(increment),
            "quantity_increment": "0.00001",
            "price_max_decimal_places": max_decimal_places,
            "price_max_significant_digits": 5,
        }
    }
    command = grid_sizing._quantize_grid_command(
        {"side": "buy", "price": price, "tp": price * 2, "sl": price / 2},
        config,
        low=price - 1000,
        high=price + 1000,
    )
    assert command["price"] == pytest.approx(expected)


def test_build_grid_preview_matches_control_plane_preview(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    body = {"direction": "neutral", "style": "steady"}
    via_plane = plane.preview("2026-07-05_DAY", dict(body), market=market(), account=account())
    via_module = grid_sizing.build_grid_preview(
        "2026-07-05_DAY", dict(body), market=market(), account=account(), config=plane.config,
    )
    assert via_plane == via_module
    assert via_plane["preview_id"] == via_module["preview_id"]


def test_direction_uses_only_its_executable_half_range_and_count(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    previews = {
        direction: grid_sizing.build_grid_preview(
            "2026-07-05_DAY", {"direction": direction, "style": "steady"},
            market=market(), account=account(), config=plane.config,
        )
        for direction in ("neutral", "long", "short")
    }
    neutral = previews["neutral"]
    long = previews["long"]
    short = previews["short"]
    assert long["range"]["low"] == neutral["range"]["low"]
    assert long["range"]["high"] == pytest.approx(market()["latest_close"])
    assert short["range"]["low"] == pytest.approx(market()["latest_close"])
    assert short["range"]["high"] == neutral["range"]["high"]
    assert long["grid"]["count"] == (neutral["grid"]["count"] + 1) // 2
    assert short["grid"]["count"] == (neutral["grid"]["count"] + 1) // 2
    assert len(long["orders"]) == long["grid"]["count"]
    assert len(short["orders"]) == short["grid"]["count"]
    assert {o["side"] for o in previews["long"]["orders"]} == {"buy"}
    assert {o["side"] for o in previews["short"]["orders"]} == {"sell"}
    assert {o["side"] for o in previews["neutral"]["orders"]} == {"buy", "sell"}


def test_directional_odd_current_count_maps_39_to_20_in_adaptive_preview(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    payload = {
        "direction": "long",
        "style": "steady",
        "grid": {"mode": "arithmetic"},
        "solver": {
            "mode": "manual_adaptive",
            "locked": [],
            "current_grid_count": 39,
            "current_direction": "neutral",
        },
    }
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        payload,
        market=market(),
        account=account(),
        config=plane.config,
    )

    assert preview["grid"]["count"] == 20
    assert len(preview["orders"]) == 20
    assert preview["range"]["high"] == pytest.approx(market()["latest_close"])
    assert {order["side"] for order in preview["orders"]} == {"buy"}


def test_scoped_running_range_is_not_retrimmed_by_a_later_market_tick(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        {
            "direction": "long",
            "style": "steady",
            "range": {
                "low": 100.0,
                "high": 110.0,
                "scope": "long_side",
                "split_price": 110.0,
                "source_envelope": {"low": 100.0, "high": 120.0},
            },
            "grid": {
                "count": 20,
                "mode": "arithmetic",
                "notional_per_grid": 2_000.0,
                "notional_mode": "manual",
            },
        },
        market=market(close=109.5),
        account=account(),
        config=plane.config,
        allow_unsafe_manual_preview=True,
    )

    assert preview["range"]["low"] == 100.0
    assert preview["range"]["high"] == 110.0
    assert preview["range"]["scope"] == "long_side"
    assert preview["range"]["split_price"] == 110.0
    assert preview["grid"]["count"] == 20
    assert {order["side"] for order in preview["orders"]} == {"buy"}


def test_auto_notional_uses_the_full_leverage_capacity_on_the_worst_side(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY", {"direction": "neutral", "style": "steady"},
        market=market(), account=account(), config=plane.config,
    )
    risk = preview["risk"]
    assert preview["grid"]["notional_per_grid"] == pytest.approx(
        risk["capital_notional_cap_per_grid"], abs=0.01,
    )
    assert risk["capital_budget"] == 100_000.0
    assert (
        preview["grid"]["notional_per_grid"]
        * risk["max_simultaneous_same_side_levels"]
    ) == pytest.approx(risk["absolute_notional_ceiling"], abs=0.25)
    assert preview["grid"]["notional_mode"] == "auto"
    assert preview["grid"]["leverage"] == 10.0
    assert 30 <= preview["grid"]["count"] <= 70
    assert preview["grid"]["min_net_profit_per_grid_usd"] >= 10.0
    assert preview["grid"]["profit_target_met"] is True


def test_style_changes_geometry_but_not_the_capital_utilization_policy(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    previews = {
        style: grid_sizing.build_grid_preview(
            "2026-07-05_DAY", {"direction": "neutral", "style": style},
            market=market(), account=account(), config=plane.config,
        )
        for style in ("steady", "aggressive")
    }

    steady = previews["steady"]
    aggressive = previews["aggressive"]
    assert steady["range"] != aggressive["range"]
    assert steady["grid"]["spacing"] != aggressive["grid"]["spacing"]
    assert steady["risk"]["capital_budget"] == aggressive["risk"]["capital_budget"] == 100_000.0
    assert steady["grid"]["margin_utilization_cap"] == aggressive["grid"]["margin_utilization_cap"] == 1.0
    assert steady["grid"]["count"] > aggressive["grid"]["count"]
    assert steady["grid"]["min_net_profit_per_grid_usd"] >= 10.0
    assert aggressive["grid"]["min_net_profit_per_grid_usd"] >= 10.0
    assert steady["risk"]["actual_leverage"] <= 10.0
    assert aggressive["risk"]["actual_leverage"] <= 10.0


def test_arithmetic_and_geometric_modes_generate_their_declared_geometry(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    base = {
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 90.0, "high": 130.0},
        "grid": {"count": 30},
    }
    arithmetic = grid_sizing.build_grid_preview(
        "2026-07-05_DAY", {**base, "grid": {**base["grid"], "mode": "arithmetic"}},
        market=market(), account=account(), config=plane.config,
    )
    geometric = grid_sizing.build_grid_preview(
        "2026-07-05_DAY", {**base, "grid": {**base["grid"], "mode": "geometric"}},
        market=market(), account=account(), config=plane.config,
    )

    arithmetic_levels = arithmetic["grid"]["levels"]
    geometric_levels = geometric["grid"]["levels"]
    arithmetic_steps = [right - left for left, right in zip(arithmetic_levels, arithmetic_levels[1:])]
    geometric_ratios = [right / left for left, right in zip(geometric_levels, geometric_levels[1:])]
    assert arithmetic["grid"]["mode"] == "arithmetic"
    assert geometric["grid"]["mode"] == "geometric"
    assert max(arithmetic_steps) - min(arithmetic_steps) < 1e-6
    assert max(geometric_ratios) - min(geometric_ratios) < 1e-6
    assert geometric["grid"]["spacing_ratio"] > 1.0
    assert geometric["preview_id"] == grid_sizing.build_grid_preview(
        "2026-07-05_DAY", {**base, "grid": {**base["grid"], "mode": "geometric"}},
        market=market(), account=account(), config=plane.config,
    )["preview_id"]


def test_unknown_grid_mode_is_rejected(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    with pytest.raises(ValueError, match="grid mode"):
        grid_sizing.build_grid_preview(
            "2026-07-05_DAY",
            {"direction": "neutral", "style": "steady", "grid": {"mode": "log-ish"}},
            market=market(), account=account(), config=plane.config,
        )


def test_manual_notional_above_safe_cap_is_rejected(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    with pytest.raises(ValueError, match="within 10x capacity"):
        grid_sizing.build_grid_preview(
            "2026-07-05_DAY",
            {"direction": "neutral", "style": "steady", "grid": {"notional_per_grid": 10_000_000.0, "notional_mode": "manual"}},
            market=market(), account=account(), config=plane.config,
        )


def test_read_only_preview_can_explain_unsafe_manual_notional_without_resizing_it(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        {
            "direction": "neutral",
            "style": "steady",
            "grid": {
                "notional_per_grid": 10_000_000.0,
                "notional_mode": "manual",
            },
        },
        market=market(),
        account=account(),
        config=plane.config,
        allow_unsafe_manual_preview=True,
    )

    assert preview["grid"]["notional_per_grid"] == 10_000_000.0
    assert preview["risk"]["capital_budget_exceeded"] is True
    assert preview["risk"]["max_loss_role"] == "risk_gate_enforced"
    assert 0 < preview["risk"]["safe_notional_cap_per_grid"] < 10_000_000.0


def test_operator_hard_stop_is_the_same_full_depth_loss_as_lifecycle_model() -> None:
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        {
            "direction": "long",
            "style": "steady",
            "range": {"low": 80, "high": 120},
            "grid": {"count": 15, "notional_per_grid": 1_000, "notional_mode": "manual"},
            "hard_stop": 70,
        },
        market=market(),
        account=account(),
        config=StrategyControlPlane(Path("/tmp/grid-risk-test")).config,
    )

    assert {order["sl"] for order in preview["orders"]} == {70.0}
    assert preview["risk"]["max_loss"] == round(full_depth_loss(preview["orders"]), 2)


def test_auto_density_searches_from_70_down_to_30_for_ten_dollar_target(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        {
            "direction": "neutral",
            "style": "steady",
            "range": {"low": 3_900.0, "high": 4_100.0},
        },
        market=market(close=4_000.0),
        account=account(),
        config=plane.config,
    )

    assert preview["grid"]["count"] == 30
    assert preview["grid"]["min_net_profit_per_grid_usd"] >= 10.0
    assert min(order["planned_net_profit_usd"] for order in preview["orders"]) >= 10.0
    assert preview["risk"]["actual_leverage"] <= 10.0


def test_auto_density_selects_the_highest_feasible_count_not_the_atr_count(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        {
            "direction": "neutral",
            "style": "steady",
            "range": {"low": 90.0, "high": 130.0},
        },
        market=market(),
        account={"equity": 2_000_000.0},
        config=plane.config,
    )

    assert preview["grid"]["count"] == 70
    assert preview["grid"]["profit_target_met"] is True


def test_auto_density_skips_counts_below_venue_price_precision(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        {
            "direction": "neutral",
            "style": "steady",
            "range": {"low": 109.65, "high": 110.34},
        },
        market=market(),
        account={"equity": 2_000_000.0},
        config=plane.config,
    )

    assert 30 <= preview["grid"]["count"] < 70
    assert preview["grid"]["profit_target_met"] is True


def adaptive_payload(*, locks: list[str], **grid: float) -> dict:
    return {
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 4_040.0, "high": 4_200.0},
        "grid": {
            "mode": "arithmetic",
            "target_net_profit_per_grid_usd": 10.0,
            **grid,
        },
        "solver": {"mode": "manual_adaptive", "locked": locks},
    }


def test_adaptive_solver_narrow_range_returns_a_flagged_preview_instead_of_error(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        adaptive_payload(locks=["range"]),
        market=market(close=4_137.44),
        account=account(),
        config=plane.config,
    )

    assert 2 <= preview["grid"]["count"] < 30
    assert preview["grid"]["profit_target_met"] is True
    assert preview["risk"]["actual_leverage"] <= 10.0
    assert preview["solver"]["locked"] == ["range"]
    assert {row["code"] for row in preview["solver"]["risk_flags"]} == {
        "grid_count_outside_preferred_band"
    }


def test_adaptive_solver_preserves_locked_count_and_adapts_leverage(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        adaptive_payload(locks=["range", "grid_count"], count=30),
        market=market(close=4_137.44),
        account=account(),
        config=plane.config,
    )

    assert preview["grid"]["count"] == 30
    assert preview["grid"]["profit_target_met"] is True
    assert 10.0 < preview["grid"]["leverage"] <= 20.0
    assert preview["risk"]["actual_leverage"] > 10.0
    assert "recommended_leverage_exceeded" in {
        row["code"] for row in preview["solver"]["risk_flags"]
    }


def test_adaptive_solver_preserves_locked_count_and_leverage_then_flags_profit(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    payload = adaptive_payload(
        locks=["range", "grid_count", "leverage"],
        count=30,
    )
    payload["risk_budget"] = {"leverage": 10.0}
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        payload,
        market=market(close=4_137.44),
        account=account(),
        config=plane.config,
    )

    assert preview["grid"]["count"] == 30
    assert preview["grid"]["leverage"] == 10.0
    assert preview["risk"]["actual_leverage"] <= 10.0
    assert preview["grid"]["profit_target_met"] is False
    assert "grid_profit_target_not_met" in {
        row["code"] for row in preview["solver"]["risk_flags"]
    }


def test_adaptive_solver_keeps_invalid_geometry_as_a_hard_stop(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    payload = adaptive_payload(locks=["range"])
    payload["range"] = {"low": 4_200.0, "high": 4_040.0}
    with pytest.raises(ValueError, match="positive low below high"):
        grid_sizing.build_grid_preview(
            "2026-07-05_DAY",
            payload,
            market=market(close=4_137.44),
            account=account(),
            config=plane.config,
        )


def test_adaptive_solver_ignores_unlocked_range_and_profit_values(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    payload = adaptive_payload(locks=[])
    payload["grid"]["target_net_profit_per_grid_usd"] = 99.0
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        payload,
        market=market(close=4_137.44),
        account=account(),
        config=plane.config,
    )

    assert (preview["range"]["low"], preview["range"]["high"]) != (
        4_040.0,
        4_200.0,
    )
    assert preview["grid"]["target_net_profit_per_grid_usd"] == 10.0


def test_adaptive_solver_preserves_locked_notional_and_adapts_density(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        adaptive_payload(
            locks=["range", "notional_per_grid"],
            notional_per_grid=5_000.0,
        ),
        market=market(close=4_137.44),
        account=account(),
        config=plane.config,
    )

    assert preview["grid"]["notional_per_grid"] == 5_000.0
    assert preview["grid"]["profit_target_met"] is True
    assert preview["risk"]["actual_leverage"] <= 10.0


def test_adaptive_solver_skips_unexecutable_high_density_and_returns_alternatives(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    payload = adaptive_payload(locks=["range"])
    payload["range"] = {"low": 109.65, "high": 110.34}
    payload["solver"]["current_grid_count"] = 40
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        payload,
        market=market(close=110.0),
        account={"equity": 2_000_000.0},
        config=plane.config,
    )

    assert 2 <= preview["grid"]["count"] < 70
    assert {row["id"] for row in preview["solver"]["alternatives"]} == {
        "preserve_grid_count",
        "preserve_profit_target",
        "preserve_recommended_leverage",
    }
    assert {row["label"] for row in preview["solver"]["alternatives"]} == {
        "保格数",
        "保收益",
        "保杠杆",
    }


def test_adaptive_solver_caps_auto_sizing_at_manual_paper_leverage_capacity(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    payload = adaptive_payload(
        locks=["range", "grid_count", "profit_target"],
        count=70,
        target_net_profit_per_grid_usd=10.0,
    )
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY",
        payload,
        market=market(close=4_137.44),
        account=account(),
        config=plane.config,
    )

    assert preview["grid"]["count"] == 70
    assert preview["risk"]["actual_leverage"] <= 20.0
    assert preview["grid"]["profit_target_met"] is False
    assert "grid_profit_target_not_met" in {
        row["code"] for row in preview["solver"]["risk_flags"]
    }


def test_adaptive_solver_rejects_fractional_locked_count_at_backend(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    with pytest.raises(ValueError, match="locked grid count must be an integer"):
        grid_sizing.build_grid_preview(
            "2026-07-05_DAY",
            adaptive_payload(
                locks=["range", "grid_count"],
                count=30.4,
            ),
            market=market(close=4_137.44),
            account=account(),
            config=plane.config,
        )


def test_preview_identity_binds_solver_locks_and_locked_values() -> None:
    economic = {
        "cycle_id": "cycle",
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 100.0, "high": 120.0},
        "grid": {"count": 30, "notional_per_grid": 5_000.0},
        "orders": [{"side": "buy", "price": 100.0, "quantity": 1.0}],
    }
    automatic = {
        **economic,
        "solver": {
            "mode": "manual_adaptive",
            "locked": [],
            "locked_inputs": {},
        },
    }
    locked = {
        **economic,
        "solver": {
            "mode": "manual_adaptive",
            "locked": ["grid_count"],
            "locked_inputs": {"grid_count": 30},
        },
    }

    assert grid_sizing.preview_id(automatic) != grid_sizing.preview_id(locked)


def test_manual_grid_below_ten_dollar_target_is_rejected(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    with pytest.raises(ValueError, match="planned net profit of 10.00 USD"):
        grid_sizing.build_grid_preview(
            "2026-07-05_DAY",
            {
                "direction": "neutral",
                "style": "steady",
                "range": {"low": 3_900.0, "high": 4_100.0},
                "grid": {"count": 70},
            },
            market=market(close=4_000.0),
            account=account(),
            config=plane.config,
        )


def test_leverage_above_limit_is_rejected(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    limit = float(plane.config.get("max_leverage") or 10.0)
    with pytest.raises(ValueError, match="leverage"):
        grid_sizing.build_grid_preview(
            "2026-07-05_DAY",
            {"direction": "neutral", "style": "steady", "risk_budget": {"leverage": limit + 1.0}},
            market=market(), account=account(), config=plane.config,
        )


def test_stale_market_is_rejected(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    with pytest.raises(ValueError, match="stale"):
        grid_sizing.build_grid_preview(
            "2026-07-05_DAY", {"direction": "neutral", "style": "steady"},
            market=market(fresh=False), account=account(), config=plane.config,
        )
