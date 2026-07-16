from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pipelines.dashboard_server as dashboard_server
from pipelines.dashboard_server import build_strategy_console_control_response
from services.strategy_control_plane import StrategyControlPlane


def _bars(timeframe: str, count: int, close: float, span: float) -> list[dict]:
    rows = []
    step = {"1d": timedelta(days=1), "4h": timedelta(hours=4), "1h": timedelta(hours=1), "15m": timedelta(minutes=15)}[timeframe]
    started = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for index in range(count):
        value = close - 3 + index * 0.15
        rows.append({
            "timestamp": (started + step * index).isoformat(),
            "open": value - 0.1,
            "high": value + span / 2,
            "low": value - span / 2,
            "close": value,
        })
    return rows


def _market() -> dict:
    execution = _bars("1h", 20, 4050, 2)
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "binance_usdm",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": execution[-1]["close"],
        "latest_timestamp": execution[-1]["timestamp"],
        "bars": execution,
        "strategy_timeframes": {
            "1d": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1d", 20, 4050, 20)},
            "4h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("4h", 60, 4050, 8)},
            "1h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1h", 60, 4050, 3)},
            "15m": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("15m", 80, 4050, 2)},
        },
    }


def test_refresh_recommendation_saves_ai_proposal_without_mutating_production(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    original = plane.upsert_proposal({
        "cycle_id": cycle_id,
        "source": "human",
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 4000, "high": 4100},
        "grid": {"count": 30},
    })
    active = plane.lock_production_plan(cycle_id, selected_proposal_id=original["proposal_id"])

    result = build_strategy_console_control_response(
        {"cycle_id": cycle_id, "action": "refresh_recommendation", "as_of": "2026-07-05T02:00:00+00:00"},
        output_root=output,
        market=_market(),
        account={"equity": 10_000},
        recommendation_provider=lambda prompt: {
            "direction": "short",
            "style": "aggressive",
            "rationale": "D1 与 4H 走弱，1H 反弹不足。",
            "key_levels": [4040, 4080],
            "ai_self_assessment": 6,
            "evidence_used": ["D1", "4H", "1H"],
        },
    )

    assert result["production_plan_unchanged"] is True
    assert result["proposal"]["source"] == "ai"
    assert result["proposal"]["signal"]["calibration_status"] == "uncalibrated"
    assert result["proposal"]["evaluation_receipt"]["status"] == "success"
    assert result["proposal"]["evaluation_receipt"]["input"]["contexts"]["15m"]["indicators"]["ema20"] is not None
    assert (output / result["proposal"]["evaluation_receipt"]["archive"]["relative_path"]).exists()
    assert result["preview"]["strategy_timeframes"] == {"range": "1d", "spacing": "4h", "execution": "1m"}
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == active["strategy_plan_id"]
    assert not (output / "dualtrack" / "orders" / f"{cycle_id}_human.json").exists()


def test_grid_preview_requires_only_the_d1_and_4h_planning_timeframes(tmp_path: Path, monkeypatch) -> None:
    market = _market()
    contexts = market.pop("strategy_timeframes")
    requested = []

    def partial_contexts(*, timeframes=None, **_kwargs):
        requested.append(tuple(timeframes or ()))
        return {timeframe: contexts[timeframe] for timeframe in timeframes}

    monkeypatch.setattr(dashboard_server, "build_strategy_timeframes_response", partial_contexts)
    result = build_strategy_console_control_response(
        {
            "cycle_id": "2026-07-05_DAY",
            "action": "preview",
            "direction": "neutral",
            "style": "steady",
        },
        output_root=tmp_path / "outputs",
        market=market,
        account={"equity": 10_000},
    )

    assert requested == [("1d", "4h")]
    assert result["preview"]["strategy_timeframes"] == {"range": "1d", "spacing": "4h", "execution": "1m"}


def test_strategy_timeframes_retry_one_transient_same_source_failure(monkeypatch) -> None:
    calls = []

    class FlakyFeed:
        def snapshot(self, *, timeframe, **_kwargs):
            calls.append(timeframe)
            if timeframe == "1d" and calls.count("1d") == 1:
                return {
                    "status": "blocked",
                    "is_synthetic": False,
                    "access_issues": ["datafeed unavailable: temporary connection reset"],
                    "bars": [],
                }
            return {
                "status": "ready",
                "fresh": True,
                "is_synthetic": False,
                "provider": "binance_usdm_futures",
                "bars": _bars(timeframe, 32 if timeframe == "1d" else 64, 4050, 20 if timeframe == "1d" else 8),
            }

    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", lambda **_kwargs: FlakyFeed())
    result = dashboard_server.build_strategy_timeframes_response(
        as_of="2026-07-05T12:00:00+00:00",
        timeframes=("1d", "4h"),
    )

    assert calls == ["1d", "1d", "4h"]
    assert result["1d"]["provider"] == "binance_usdm_futures"
    assert result["4h"]["provider"] == "binance_usdm_futures"


def test_strategy_timeframes_never_retry_or_accept_synthetic_data(monkeypatch) -> None:
    calls = []

    class SyntheticFeed:
        def snapshot(self, *, timeframe, **_kwargs):
            calls.append(timeframe)
            return {
                "status": "ready",
                "fresh": True,
                "is_synthetic": True,
                "provider": "synthetic_fixture",
                "bars": _bars(timeframe, 32, 4050, 20),
            }

    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", lambda **_kwargs: SyntheticFeed())
    with pytest.raises(ValueError, match="strategy timeframe 1d is unavailable or untrusted"):
        dashboard_server.build_strategy_timeframes_response(
            as_of="2026-07-05T12:00:00+00:00",
            timeframes=("1d",),
        )

    assert calls == ["1d"]
