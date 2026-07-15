from __future__ import annotations

import argparse
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_assets, load_pipeline_config
from services.broker_feed_bridge import BrokerFeedBridge
from services.journal_store import load_json, write_json
from services.kline_client import KlineClient
from services.market_data_access import uses_independent_datafeed

# Treat a 5m bar as fresh while it is at most one bar plus a small grace
# window old. Anything beyond that is "stale" — surfacing this in the
# collector log lets the dashboard distinguish "I really refreshed" from
# "I silently fell back to the last cached bar".
_FRESH_BAR_AGE_SECONDS = 7 * 60
_MOCK_PROVIDERS = {"mock", "factor_proxy", "local_synthetic_seed"}


def _classify_fetch(timestamp: str, provider: str) -> tuple[str, float | None]:
    """Return (fetch_status, bar_age_seconds) for a collector record."""

    if provider in _MOCK_PROVIDERS:
        return "fallback_mock", None
    try:
        normalized = re.sub(r"(\.\d{6})\d+", r"\1", str(timestamp).replace("Z", "+00:00"))
        ts = datetime.fromisoformat(normalized)
    except ValueError:
        return "unknown", None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 0:
        return "fresh", round(age, 1)
    return ("fresh" if age <= _FRESH_BAR_AGE_SECONDS else "stale"), round(age, 1)


def _paths(config: dict) -> tuple[Path | None, Path]:
    env_local_db = os.getenv("TRADING_ORCHESTRATOR_MARKET_DB")
    local_db = env_local_db or config.get("local_market_db")
    local_db_path = Path(local_db) if local_db else None
    if local_db_path and not local_db_path.is_absolute() and not env_local_db:
        local_db_path = ROOT / local_db_path
    output_root = Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    return local_db_path, output_root


def collect_once(run_date: str | None = None) -> list[dict]:
    run_date = run_date or utc_run_date()
    config = load_pipeline_config()
    local_db_path, output_root = _paths(config)
    if not uses_independent_datafeed(local_db_path):
        BrokerFeedBridge(output_root=output_root, market_db=local_db_path).import_pending(run_date)
    kline = KlineClient(
        base_url=config["kline_base_url"],
        fallback_to_mock=bool(config.get("fallback_to_mock", True)),
        local_db_path=local_db_path,
        allow_synthetic_seed=bool(config.get("allow_synthetic_seed", False)),
        gold_backfill=config.get("gold_5m_backfill", {}),
    )
    timeframes = list(config.get("timeframes", ["5m"]))
    factor_timeframes = list(config.get("factor_timeframes", ["1d"]))
    records = []
    for asset in load_assets():
        asset_timeframes = timeframes if asset.tradable else factor_timeframes
        for timeframe in asset_timeframes:
            bars = kline.fetch(asset, timeframe=timeframe, limit=60)
            latest = bars[-1]
            quote = kline.store.load_latest_quote(asset.symbol) if asset.symbol == "GOLD" and kline.store else {}
            display = quote or latest.to_dict()
            fetch_status, bar_age_seconds = _classify_fetch(display["timestamp"], display["provider"])
            records.append(
                {
                    "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "symbol": asset.symbol,
                    "timeframe": timeframe,
                    "timestamp": display["timestamp"],
                    "close": display["close"],
                    "provider": display["provider"],
                    "quality_flags": display.get("quality_flags", []),
                    "record_type": display.get("record_type", "bar"),
                    "market_data_backend": "datafeed" if uses_independent_datafeed(local_db_path) else "legacy_test_store",
                    "stored_rows_seen": len(bars),
                    "fetch_status": fetch_status,
                    "bar_age_seconds": bar_age_seconds,
                }
            )
    path = output_root / "collector_runs" / f"{run_date}.json"
    existing = load_json(path)
    seen = {(item["symbol"], item["timeframe"], item["timestamp"]) for item in existing}
    merged = existing + [item for item in records if (item["symbol"], item["timeframe"], item["timestamp"]) not in seen]
    write_json(path, merged)
    _write_raw_quote_snapshots(output_root, run_date, records)
    return records


def _write_raw_quote_snapshots(output_root: Path, run_date: str, records: list[dict]) -> None:
    path = output_root / "raw_snapshots" / run_date / "quote_snapshots.json"
    existing = load_json(path)
    seen = {(item.get("symbol"), item.get("timeframe"), item.get("timestamp"), item.get("record_type")) for item in existing}
    additions = [
        item
        for item in records
        if (item.get("symbol"), item.get("timeframe"), item.get("timestamp"), item.get("record_type")) not in seen
    ]
    write_json(path, existing + additions)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect live market snapshots into the local market database.")
    parser.add_argument("--date", default=utc_run_date(), help="Run date for collector log output.")
    parser.add_argument("--interval-seconds", type=int, default=300, help="Loop interval. Defaults to one 5m bar.")
    parser.add_argument("--iterations", type=int, default=1, help="Number of collection iterations. Use 1 for a single run.")
    args = parser.parse_args()

    for index in range(args.iterations):
        records = collect_once(args.date)
        for item in records:
            print(f"{item['symbol']} {item['timeframe']} {item['close']} {item['provider']} {item['timestamp']}")
        if index < args.iterations - 1:
            time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
