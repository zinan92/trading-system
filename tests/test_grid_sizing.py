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


def test_auto_notional_is_min_of_capital_and_risk_caps(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    preview = grid_sizing.build_grid_preview(
        "2026-07-05_DAY", {"direction": "neutral", "style": "steady"},
        market=market(), account=account(), config=plane.config,
    )
    risk = preview["risk"]
    expected = min(risk["capital_notional_cap_per_grid"], risk["risk_notional_cap_per_grid"])
    assert preview["grid"]["notional_per_grid"] == pytest.approx(expected, abs=0.01)
    assert preview["grid"]["notional_mode"] == "auto"


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
