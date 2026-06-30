import json
from pathlib import Path

from schemas.market_data import Bar
from services.market_store import MarketStore
from services.strategy_registry import StrategyRegistry
from pipelines.backtest_strategies import backtest_all, enumerate_signals


def _seed(db: Path, symbol: str, timeframe: str, closes: list[float]) -> None:
    bars = []
    for i, c in enumerate(closes):
        ts = f"2026-05-{1 + i // 1440:02d}T{(i // 60) % 24:02d}:{i % 60:02d}:00+00:00"
        bars.append(Bar(symbol, timeframe, ts, c, c + 1, c - 1, c, 10, "binance_usdm", []))
    MarketStore(db).upsert_bars(bars)


def test_macd_signals_are_faithful_ma_is_approximated():
    rising_then_falling = [100 + i for i in range(50)] + [150 - i for i in range(50)]
    bars = [Bar("GOLD", "1m", f"2026-05-01T00:{i % 60:02d}:00+00:00", c, c + 1, c - 1, c, 10, "binance_usdm", []) for i, c in enumerate(rising_then_falling)]

    macd_strat = StrategyRegistry({"m": {"engine": "macd", "signal": {}}}).get("m")
    ma_strat = StrategyRegistry({"a": {"engine": "ma", "signal": {"ma_short_bars": 5, "ma_long_bars": 20}}}).get("a")

    macd_sigs, macd_faithful = enumerate_signals(macd_strat, bars)
    ma_sigs, ma_faithful = enumerate_signals(ma_strat, bars)

    assert macd_faithful is True and len(macd_sigs) >= 1
    assert ma_faithful is False and len(ma_sigs) >= 1  # price-only SMA cross fallback


def test_backtest_all_writes_reports_and_ranked_leaderboard(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    # Oscillating prices so both engines generate several round-trip trades.
    wave = []
    for cycle in range(6):
        wave += [100 + i * 0.5 for i in range(30)] + [115 - i * 0.5 for i in range(30)]
    _seed(db, "GOLD", "1m", wave)

    registry = StrategyRegistry(
        {
            "gold_1m_macd": {"symbol": "GOLD", "timeframe": "1m", "engine": "macd", "signal": {}},
            "gold_1m_ma": {"symbol": "GOLD", "timeframe": "1m", "engine": "ma", "signal": {"ma_short_bars": 5, "ma_long_bars": 20}},
        }
    )
    payload = backtest_all("2026-05-20", registry=registry)

    assert payload["strategy_count"] == 2
    assert payload["exit_model"] == "fixed stop/target/timeout"
    ids = {s["strategy_id"] for s in payload["strategies"]}
    assert ids == {"gold_1m_macd", "gold_1m_ma"}
    # ranked by return_pct descending
    rets = [s["return_pct"] for s in payload["strategies"]]
    assert rets == sorted(rets, reverse=True)
    assert all(s["rank"] in (1, 2) for s in payload["strategies"])
    # per-strategy report written + leaderboard persisted
    assert (root / "backtests" / "gold_1m_macd.json").exists()
    saved = json.loads((root / "backtests" / "leaderboard.json").read_text())[0]
    assert saved["strategy_count"] == 2
    macd_report = next(s for s in payload["strategies"] if s["strategy_id"] == "gold_1m_macd")
    assert macd_report["trades"] >= 1
    assert "max_drawdown_pct" in macd_report and "profit_factor" in macd_report
