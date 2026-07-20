"""Historical backtest pipeline — runs every enabled strategy's engine over the
local market history and writes per-strategy reports + a backtest leaderboard
(separate from the forward-paper leaderboard).

Each engine enumerates its own signals (`historical_signals`); the MA legacy
engine has no such method, so we approximate it with a price-only SMA cross and
FLAG it (`faithful_signals: false`) — its live signal also uses macro factors,
which a price-only backtest can't reproduce.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from services.backtest_plugin_composition import compose_historical_strategy_backtest
from services.backtest_plugin_registry import BacktestPluginRegistry
from services.backtest_port import HistoricalStrategyBacktestRequest
from services.backtest_service import HistoricalStrategyBacktestService
from services.config_loader import ROOT, load_pipeline_config, load_risk_rules
from services.journal_store import write_json
from services.market_data_access import market_data_repository
from services.run_date import utc_run_date
from services.strategy_registry import StrategyRegistry


def _sma(values: list[float], period: int) -> list[float]:
    out: list[float] = []
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        out.append(running / min(i + 1, period))
    return out


def _ma_cross_signals(strategy, bars: list) -> list[dict]:
    """Price-only SMA cross — a flagged approximation of the legacy MA engine."""
    cfg = strategy.params.get("signal", {}) or {}
    short = int(cfg.get("ma_short_bars", 5))
    long = int(cfg.get("ma_long_bars", 20))
    closes = [float(b.close) for b in bars]
    if len(closes) <= long:
        return []
    ss, ls = _sma(closes, short), _sma(closes, long)
    out = []
    for i in range(long, len(closes)):
        prev, cur = ss[i - 1] - ls[i - 1], ss[i] - ls[i]
        if prev <= 0 < cur:
            out.append({"index": i, "direction": "long"})
        elif prev >= 0 > cur:
            out.append({"index": i, "direction": "short"})
    return out


def enumerate_signals(strategy, bars: list) -> tuple[list[dict], bool]:
    """(signals, faithful). faithful=False means it's an approximation of the
    live engine (the MA price-only fallback)."""
    engine = strategy.signal_engine()
    historical = getattr(engine, "historical_signals", None)
    if historical is not None:
        return historical(bars), True
    return _ma_cross_signals(strategy, bars), False


def backtest_strategy(
    strategy,
    bars: list,
    *,
    backtest_service: HistoricalStrategyBacktestService,
    cost_rules: dict,
) -> dict:
    signals, faithful = enumerate_signals(strategy, bars)
    request = HistoricalStrategyBacktestRequest(
        strategy={
            "strategy_id": strategy.strategy_id,
            "symbol": strategy.symbol,
            "timeframe": strategy.timeframe,
            "engine": str(strategy.params.get("engine", "ma")),
        },
        bars=tuple(bar.to_dict() for bar in bars),
        signals=tuple(dict(signal) for signal in signals),
        backtest_config={
            "strategy_backtest": dict(strategy.params.get("backtest") or {}),
            "starting_equity": float(strategy.starting_equity),
        },
        cost_rules=cost_rules,
    )
    metrics = backtest_service.run(request)
    return {
        "strategy_id": strategy.strategy_id,
        "symbol": strategy.symbol,
        "timeframe": strategy.timeframe,
        "engine": str(strategy.params.get("engine", "ma")),
        "bars": len(bars),
        "first_timestamp": bars[0].timestamp if bars else "",
        "last_timestamp": bars[-1].timestamp if bars else "",
        "signal_count": len(signals),
        "faithful_signals": faithful,
        "backtest_plugin": backtest_service.runtime.audit_dict(),
        "backtest_input_hash": request.input_hash,
        **metrics,
    }


def backtest_all(
    run_date: str | None = None,
    output_root: Path | None = None,
    market_db: Path | None = None,
    registry: StrategyRegistry | None = None,
    max_bars: int | None = None,
    backtest_plugin_registry: BacktestPluginRegistry | None = None,
) -> dict:
    run_date = run_date or utc_run_date()
    config = load_pipeline_config()
    backtest_service = HistoricalStrategyBacktestService(
        compose_historical_strategy_backtest(
            config,
            registry=backtest_plugin_registry,
        )
    )
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
    registry = registry or StrategyRegistry()
    store = market_data_repository(market_db)
    cost_rules = dict(
        load_risk_rules().get("default", {}).get("paper_execution_costs", {})
    )

    results = []
    for strategy in registry.enabled():
        bars = store.load_bars(strategy.symbol, strategy.timeframe, max_bars or 1_000_000)
        report = backtest_strategy(
            strategy,
            bars,
            backtest_service=backtest_service,
            cost_rules=cost_rules,
        )
        write_json(output_root / "backtests" / f"{strategy.strategy_id}.json", [report])
        results.append(report)

    ranked = sorted(results, key=lambda r: r["return_pct"], reverse=True)
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    payload = {
        "run_date": run_date,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "exit_model": "fixed stop/target/timeout",
        "backtest_plugin": backtest_service.runtime.audit_dict(),
        "strategy_count": len(results),
        "strategies": ranked,
    }
    write_json(output_root / "backtests" / "leaderboard.json", [payload])
    write_json(output_root / "backtests" / f"leaderboard_{run_date}.json", [payload])
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest every enabled strategy over local market history.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--max-bars", type=int, default=None, help="Cap bars per strategy (default: all available). Use to bound chan runtime.")
    args = parser.parse_args()

    payload = backtest_all(args.date, max_bars=args.max_bars)
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
