from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from services.bar_importer import BarCsvImporter
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.market_store import MarketStore


def _paths() -> tuple[Path, Path]:
    config = load_pipeline_config()
    env_local_db = os.getenv("TRADING_ORCHESTRATOR_MARKET_DB")
    local_db = Path(env_local_db or str(ROOT / config.get("local_market_db", "data/market_data.db")))
    output_root = Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    return local_db, output_root


def import_bars(path: Path, symbol: str, timeframe: str, provider: str, run_date: str | None = None) -> dict:
    run_date = run_date or date.today().isoformat()
    local_db, output_root = _paths()
    result = BarCsvImporter(MarketStore(local_db)).import_csv(path, symbol=symbol, timeframe=timeframe, provider=provider)
    result["imported_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result["local_db"] = str(local_db)
    log_path = output_root / "imports" / f"{run_date}.json"
    logs = load_json(log_path)
    logs.append(result)
    write_json(log_path, logs)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Import OHLCV bars from CSV into the local market database.")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--symbol", default="GOLD")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--provider", default="csv_import")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    result = import_bars(args.csv_path, args.symbol, args.timeframe, args.provider, args.date)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
