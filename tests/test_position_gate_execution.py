import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipelines.daily import _position_gate_enabled, run_daily_pipeline
from schemas.asset import Asset
from schemas.market_data import Bar
from services.market_store import MarketStore
from services.strategy_registry import StrategyRegistry


def _rising_gold_5m_bars(count: int = 180) -> list[Bar]:
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    rows = []
    price = 4200.0
    for index in range(count):
        close = round(price + 0.8, 2)
        rows.append(
            Bar(
                "GOLD",
                "5m",
                (start + timedelta(minutes=5 * index)).isoformat(),
                price,
                close + 0.3,
                price - 0.3,
                close,
                10,
                "binance_usdm",
                [],
            )
        )
        price = close
    return rows


def test_position_gate_blocks_ticket_before_execution_path(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    run_date = "2026-05-26"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4344.00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-26T15:00:00+00:00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_DISABLE_DATA_QUALITY_GATE", "1")
    MarketStore(db).upsert_bars(_rising_gold_5m_bars())

    strategy = StrategyRegistry(
        {
            "gold_position_gate_test": {
                "symbol": "GOLD",
                "timeframe": "5m",
                "position_gate": {"enabled": True},
                "signal": {
                    "long_strength_min": 0,
                    "long_confidence_min": 0,
                    "short_strength_max": -1,
                    "event_block_below": 0,
                },
            }
        }
    ).get("gold_position_gate_test")

    run_daily_pipeline(run_date, strategy=strategy, output_root=root)

    signal = json.loads((root / "signals" / f"{run_date}.json").read_text())[0]
    tickets = json.loads((root / "trade_tickets" / f"{run_date}.json").read_text())
    risk_blocks = json.loads((root / "risk_blocks" / f"{run_date}.json").read_text())
    position_map = json.loads((root / "position_maps" / f"{run_date}.json").read_text())[0]

    assert signal["direction"] == "watch"
    assert signal["regime"] == "position_map_block"
    assert "Position gate blocked long signal" in signal["thesis"]
    assert tickets == []
    assert any(item["reason"] == "position map gate blocked trading" for item in risk_blocks)
    assert position_map["status"] == "ready"
    assert position_map["location"] == "high"


def test_position_gate_is_explicit_opt_in_for_gold_strategies():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    registry = StrategyRegistry(
        {
            "legacy_default": {"symbol": "GOLD", "signal": {}},
            "gated": {"symbol": "GOLD", "position_gate": {"enabled": True}, "signal": {}},
            "ungated": {"symbol": "GOLD", "position_gate": {"enabled": False}, "signal": {}},
        }
    )

    assert _position_gate_enabled(registry.get("legacy_default"), asset) is False
    assert _position_gate_enabled(registry.get("gated"), asset) is True
    assert _position_gate_enabled(registry.get("ungated"), asset) is False
