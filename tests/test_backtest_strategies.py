import json
from pathlib import Path

import pytest

import pipelines.backtest_strategies as backtest_pipeline
from pipelines.backtest_strategies import backtest_all, enumerate_signals
from schemas.market_data import Bar
from services.backtest_plugin_registry import BacktestPluginRegistry, UnknownBacktestPlugin
from services.backtest_port import HISTORICAL_STRATEGY_KIND, HistoricalStrategyBacktestRequest
from services.market_store import MarketStore
from services.strategy_registry import StrategyRegistry


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


def test_historical_pipeline_uses_custom_plugin_and_persists_provenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, HistoricalStrategyBacktestRequest] = {}

    class CustomHistoricalPort:
        def run(self, request: HistoricalStrategyBacktestRequest) -> dict:
            captured["request"] = request
            return {
                "trades": 2,
                "wins": 1,
                "losses": 1,
                "win_rate": 0.5,
                "profit_factor": 1.25,
                "net_pnl": 25.0,
                "return_pct": 0.25,
                "max_drawdown_pct": 0.1,
                "avg_r": 0.2,
                "sharpe_per_trade": 0.4,
                "avg_bars_held": 3.0,
                "final_equity": 10_025.0,
                "strategy_id": "forged",
                "backtest_plugin": {"forged": True},
            }

    plugin_registry = BacktestPluginRegistry()
    plugin_registry.register(
        "custom_history",
        lambda _context: CustomHistoricalPort(),
        kind=HISTORICAL_STRATEGY_KIND,
        evidence_tier="custom_historical",
        promotion_evidence_capable=True,
    )
    monkeypatch.setattr(
        backtest_pipeline,
        "load_pipeline_config",
        lambda: {"backtest_plugins": {"historical_strategy": "custom_history"}},
    )
    db = tmp_path / "market.db"
    root = tmp_path / "outputs"
    _seed(db, "GOLD", "1m", [100 + index * 0.1 for index in range(60)])
    strategy_registry = StrategyRegistry(
        {
            "custom": {
                "symbol": "GOLD",
                "timeframe": "1m",
                "engine": "ma",
                "signal": {"ma_short_bars": 5, "ma_long_bars": 20},
            }
        },
        default_starting_equity=10_000.0,
    )

    payload = backtest_all(
        "2026-07-18",
        output_root=root,
        market_db=db,
        registry=strategy_registry,
        backtest_plugin_registry=plugin_registry,
    )

    report = payload["strategies"][0]
    assert report["strategy_id"] == "custom"
    assert report["net_pnl"] == 25.0
    assert report["backtest_plugin"]["plugin"]["name"] == "custom_history"
    assert report["backtest_plugin"]["plugin"]["kind"] == HISTORICAL_STRATEGY_KIND
    assert report["backtest_input_hash"] == captured["request"].input_hash
    assert payload["backtest_plugin"]["registry_fingerprint"] == plugin_registry.fingerprint
    saved = json.loads((root / "backtests" / "custom.json").read_text())[0]
    assert saved["backtest_plugin"] == report["backtest_plugin"]


def test_unknown_historical_plugin_fails_before_output_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        backtest_pipeline,
        "load_pipeline_config",
        lambda: {"backtest_plugins": {"historical_strategy": "typo"}},
    )
    strategy_registry = StrategyRegistry(
        {},
        default_starting_equity=10_000.0,
    )
    output = tmp_path / "outputs"

    with pytest.raises(UnknownBacktestPlugin, match="typo"):
        backtest_all(
            "2026-07-18",
            output_root=output,
            market_db=tmp_path / "market.db",
            registry=strategy_registry,
        )

    assert not output.exists()
