"""Per-strategy timeframe routing: the chan strategy consumes 1m candles while
gold_5m_v1 stays on 5m. Tests the two seams — KlineClient serving GOLD non-5m
from the local store (not mock), and run_daily_pipeline fetching + signalling on
the strategy's own timeframe."""

import json
from pathlib import Path

from schemas.asset import Asset
from schemas.market_data import Bar
from pipelines.daily import run_daily_pipeline
from services.kline_client import KlineClient
from services.market_store import MarketStore
from services.strategy_registry import StrategyRegistry


def _gold_1m_bars(count: int) -> list[Bar]:
    bars = []
    price = 4000.0
    for index in range(count):
        minute = index % 60
        hour = index // 60
        ts = f"2026-05-19T{hour:02d}:{minute:02d}:00+00:00"
        close = round(price + (index % 7) * 0.1, 2)
        bars.append(Bar("GOLD", "1m", ts, price, close + 1, price - 1, close, 5, "binance_usdm", ["crypto_perpetual"]))
        price = close
    return bars


def test_gold_1m_reads_real_store_bars_not_mock(tmp_path: Path):
    db = tmp_path / "market.db"
    store = MarketStore(db)
    store.upsert_bars(_gold_1m_bars(30))

    client = KlineClient("local_gold_api", local_db_path=db, fallback_to_mock=False)
    out = client.fetch(Asset("GOLD", "Gold", "commodity", "high", "UTC"), timeframe="1m", limit=50)

    # Real DB bars, NOT the mock/factor-proxy fabrication the old code path returned.
    assert len(out) == 30
    assert all(bar.provider == "binance_usdm" and bar.timeframe == "1m" for bar in out)
    assert out[0].timestamp == "2026-05-19T00:00:00+00:00"


def test_daily_pipeline_routes_strategy_timeframe_to_1m(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_DISABLE_DATA_QUALITY_GATE", "1")
    MarketStore(db).upsert_bars(_gold_1m_bars(300))

    strat = StrategyRegistry(
        {"gold_1m_ma": {"symbol": "GOLD", "timeframe": "1m", "signal": {}}}
    ).get("gold_1m_ma")
    run_daily_pipeline("2026-05-19", strategy=strat, output_root=root)

    # The strategy's timeframe drove the fetch: 1m clean bars written, no 5m.
    # 15m is a derived context series, not the signal timeframe.
    assert (root / "clean_bars" / "2026-05-19" / "GOLD_1m.json").exists()
    assert (root / "clean_bars" / "2026-05-19" / "GOLD_15m.json").exists()
    assert not (root / "clean_bars" / "2026-05-19" / "GOLD_5m.json").exists()
    fifteen = json.loads((root / "clean_bars" / "2026-05-19" / "GOLD_15m.json").read_text())
    assert fifteen[0]["timeframe"] == "15m"
    assert "source_timeframe:1m" in fifteen[0]["quality_flags"]
    signal = json.loads((root / "signals" / "2026-05-19.json").read_text())[0]
    assert any("_1m.json" in artifact for artifact in signal["source_artifacts"])


def test_daily_pipeline_default_strategy_stays_on_5m(monkeypatch, tmp_path: Path):
    """A strategy with no timeframe (default 5m) must keep writing 5m clean bars —
    gold_5m_v1's behaviour is unchanged."""
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_DISABLE_DATA_QUALITY_GATE", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4569.34")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-19T00:00:00+00:00")
    # Seed enough real 5m bars so the 5m path returns local history.
    five_min = [
        Bar("GOLD", "5m", f"2026-05-18T{i // 12:02d}:{(i % 12) * 5:02d}:00+00:00", 4560 + i, 4561 + i, 4559 + i, 4560.5 + i, 5, "binance_usdm", [])
        for i in range(30)
    ]
    MarketStore(db).upsert_bars(five_min)

    strat = StrategyRegistry({"gold_5m_v1": {"symbol": "GOLD", "signal": {}}}).get("gold_5m_v1")
    run_daily_pipeline("2026-05-19", strategy=strat, output_root=root)

    assert (root / "clean_bars" / "2026-05-19" / "GOLD_5m.json").exists()
    assert (root / "clean_bars" / "2026-05-19" / "GOLD_15m.json").exists()
    assert not (root / "clean_bars" / "2026-05-19" / "GOLD_1m.json").exists()
    fifteen = json.loads((root / "clean_bars" / "2026-05-19" / "GOLD_15m.json").read_text())
    assert "source_timeframe:5m" in fifteen[0]["quality_flags"]
    assert not (root / "position_maps" / "2026-05-19.json").exists()
