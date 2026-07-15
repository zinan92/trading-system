from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.config_loader import ROOT, load_pipeline_config
from services.datafeed_market_client import DatafeedMarketClient
from services.journal_store import write_json


def run_tiger_futures_sessions(run_date: str, trading_date: str | None = None, config: dict | None = None) -> dict:
    pipeline = load_pipeline_config()
    datafeed = pipeline.get("datafeed", {}) or {}
    route = (datafeed.get("instrument_routes", {}) or {}).get("MGCmain", {})
    client = DatafeedMarketClient(base_url=str(datafeed.get("base_url") or "http://127.0.0.1:8100"))
    result = client.sessions(
        asset_class=str(route.get("asset_class") or "commodity"),
        ticker=str(route.get("ticker") or "MGCmain"),
        source=str(route.get("source") or "tiger_openapi_comex"),
        trading_date=trading_date or run_date,
    )
    result["run_date"] = run_date
    output_root = ROOT / str(pipeline.get("output_root", "outputs"))
    write_json(output_root / "tiger_futures_sessions" / "current.json", [result])
    write_json(output_root / "tiger_futures_sessions" / f"{run_date}.json", [result])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch read-only Tiger OpenAPI futures trading sessions.")
    parser.add_argument("--date", default=utc_run_date(), help="Run/artifact date.")
    parser.add_argument("--trading-date", help="Exchange trading date to query. Defaults to --date.")
    parser.add_argument("--contract", help="Tiger futures contract identifier, e.g. MGC2608 or 1OZ2608.")
    args = parser.parse_args()

    overrides = {}
    if args.contract:
        overrides["contract"] = args.contract
        overrides["output_symbol"] = args.contract
    result = run_tiger_futures_sessions(args.date, trading_date=args.trading_date, config=overrides)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
