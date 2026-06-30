from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.journal_store import write_json
from services.position_map import GoldPositionMap


def _bar(timestamp: datetime, close: float, low: float = 100.0, high: float = 150.0) -> dict:
    return {
        "symbol": "GOLD",
        "timeframe": "4h",
        "timestamp": timestamp.isoformat(),
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1,
        "provider": "test",
        "quality_flags": [],
    }


def _write_bars(root: Path, run_date: str, closes: list[float]) -> None:
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    rows = [_bar(start + timedelta(hours=4 * index), close) for index, close in enumerate(closes)]
    write_json(root / "clean_bars" / run_date / "GOLD_4h.json", rows)


def _minute_bars(count: int) -> list[dict]:
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    rows = []
    price = 4200.0
    for index in range(count):
        close = round(price + ((index % 17) - 8) * 0.05 + 0.03, 4)
        rows.append(
            {
                "symbol": "GOLD",
                "timeframe": "1m",
                "timestamp": (start + timedelta(minutes=index)).isoformat(),
                "open": price,
                "high": max(price, close) + 0.2,
                "low": min(price, close) - 0.2,
                "close": close,
                "volume": 1,
                "provider": "test",
                "quality_flags": [],
            }
        )
        price = close
    return rows


def test_position_map_allows_trigger_near_fibonacci_key_level(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _write_bars(root, run_date, [124.0] * 79 + [125.0])

    position_map = GoldPositionMap(root).build(run_date, "gold_1m_chan")
    gate = GoldPositionMap(root).gate_signal({"direction": "long", "thesis": "fresh 2 buy"}, position_map)

    assert position_map["status"] == "ready"
    assert position_map["trade_zone"]["near_key_level"] is True
    assert position_map["nearest_levels"][0]["name"] == "fib_0.5"
    assert gate["allow_candidate"] is True
    assert gate["effective_direction"] == "long"


def test_position_map_downgrades_trigger_far_from_key_level(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _write_bars(root, run_date, [120.0] * 79 + [133.0])

    position_map = GoldPositionMap(root).build(run_date, "gold_1m_chan")
    gate = GoldPositionMap(root).gate_signal({"direction": "long", "thesis": "fresh 2 buy"}, position_map)

    assert position_map["status"] == "ready"
    assert position_map["trade_zone"]["near_key_level"] is False
    assert gate["allow_candidate"] is False
    assert gate["effective_direction"] == "watch"
    assert "not near" in gate["reason"]


def test_position_map_derives_higher_timeframes_from_1m_rows(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"

    position_map = GoldPositionMap(root).build_from_timeframes(run_date, "gold_1m_chan", {"1m": _minute_bars(2880)})

    assert position_map["status"] == "ready"
    assert position_map["primary_timeframe"] == "4h"
    assert position_map["frames"]["1h"]["source_path"] == "derived:1h:from:1m"
    assert position_map["frames"]["4h"]["source_path"] == "derived:4h:from:1m"
    assert position_map["frames"]["1h"]["bar_count"] == 48
    assert position_map["frames"]["4h"]["bar_count"] == 12
    assert position_map["nearest_trade_levels"]
    assert all(item["timeframe"] in {"1h", "4h"} for item in position_map["nearest_trade_levels"])
