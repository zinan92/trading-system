from pathlib import Path

import pytest

from services import grid_sizing
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


def test_average_true_range_known_value() -> None:
    bars = [
        {"high": 12.0, "low": 10.0, "close": 11.0},
        {"high": 13.0, "low": 11.0, "close": 12.0},
        {"high": 14.0, "low": 12.0, "close": 13.0},
    ]
    # TR for bar2 = max(2, |13-11|, |11-11|) = 2; bar3 = max(2, |14-12|, |12-12|) = 2
    assert grid_sizing.average_true_range(bars, period=2) == pytest.approx(2.0)


def test_average_true_range_requires_history() -> None:
    with pytest.raises(ValueError):
        grid_sizing.average_true_range([{"high": 1, "low": 1, "close": 1}], period=14)


def test_build_grid_preview_matches_control_plane_preview(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    body = {"direction": "neutral", "style": "steady"}
    via_plane = plane.preview("2026-07-05_DAY", dict(body), market=market(), account=account())
    via_module = grid_sizing.build_grid_preview(
        "2026-07-05_DAY", dict(body), market=market(), account=account(), config=plane.config,
    )
    assert via_plane == via_module
    assert via_plane["preview_id"] == via_module["preview_id"]


def test_direction_arms_sides_without_moving_range(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    previews = {
        direction: grid_sizing.build_grid_preview(
            "2026-07-05_DAY", {"direction": direction, "style": "steady"},
            market=market(), account=account(), config=plane.config,
        )
        for direction in ("neutral", "long", "short")
    }
    ranges = {d: (p["range"]["low"], p["range"]["high"]) for d, p in previews.items()}
    assert len(set(ranges.values())) == 1, "direction must not move the market range"
    assert {o["side"] for o in previews["long"]["orders"]} == {"buy"}
    assert {o["side"] for o in previews["short"]["orders"]} == {"sell"}
    assert {o["side"] for o in previews["neutral"]["orders"]} == {"buy", "sell"}


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
    assert steady["grid"]["notional_per_grid"] == aggressive["grid"]["notional_per_grid"]


def test_arithmetic_and_geometric_modes_generate_their_declared_geometry(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    base = {
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 90.0, "high": 130.0},
        "grid": {"count": 8},
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
    with pytest.raises(ValueError, match="exceeds safe cap"):
        grid_sizing.build_grid_preview(
            "2026-07-05_DAY",
            {"direction": "neutral", "style": "steady", "grid": {"notional_per_grid": 10_000_000.0, "notional_mode": "manual"}},
            market=market(), account=account(), config=plane.config,
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
