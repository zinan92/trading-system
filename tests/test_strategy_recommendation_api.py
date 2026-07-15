from datetime import datetime, timedelta, timezone
from pathlib import Path

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
