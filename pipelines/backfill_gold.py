from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.market_data_access import uses_independent_datafeed
from services.yahoo_chart_client import YahooChartClient


def _paths() -> tuple[Path, Path, dict]:
    config = load_pipeline_config()
    env_local_db = os.getenv("TRADING_ORCHESTRATOR_MARKET_DB")
    local_db = Path(env_local_db or str(ROOT / config.get("local_market_db", "data/market_data.db")))
    if uses_independent_datafeed(local_db):
        raise RuntimeError("Production GOLD backfill belongs in datafeed; use its source adapter")
    output_root = Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    return local_db, output_root, config


def backfill_gold_5m(run_date: str | None = None, yahoo_symbol: str | None = None, range_value: str | None = None) -> dict:
    run_date = run_date or utc_run_date()
    local_db, output_root, config = _paths()
    backfill_config = config.get("gold_5m_backfill", {})
    symbol = yahoo_symbol or backfill_config.get("yahoo_symbol", "GC=F")
    range_arg = range_value or backfill_config.get("range", "5d")
    bars = YahooChartClient().fetch_5m_bars(yahoo_symbol=symbol, output_symbol="GOLD", range_value=range_arg, interval="5m")
    store = MarketStore(local_db)
    store.upsert_bars(bars)
    coverage = [item for item in store.coverage() if item["symbol"] == "GOLD" and item["timeframe"] == "5m"]
    result = {
        "backfilled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "symbol": "GOLD",
        "timeframe": "5m",
        "source_symbol": symbol,
        "provider": f"yahoo_chart:{symbol}",
        "range": range_arg,
        "imported_rows": len(bars),
        "first_timestamp": bars[0].timestamp if bars else None,
        "last_timestamp": bars[-1].timestamp if bars else None,
        "local_db": str(local_db),
        "coverage": coverage,
    }
    log_path = output_root / "backfills" / f"{run_date}.json"
    logs = load_json(log_path)
    logs.append(result)
    write_json(log_path, logs)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill GOLD 5m history into the local market database.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--yahoo-symbol", default=None, help="Yahoo chart symbol. Defaults to config gold_5m_backfill.yahoo_symbol.")
    parser.add_argument("--range", dest="range_value", default=None, help="Yahoo range, for example 5d or 1mo.")
    args = parser.parse_args()

    result = backfill_gold_5m(args.date, args.yahoo_symbol, args.range_value)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
