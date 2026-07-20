from __future__ import annotations

import argparse
import json
from typing import Sequence

from services.market_data_refresh import refresh_market_data
from services.run_date import utc_run_date


def run_binance_usdm_1m_feed_import(run_date: str) -> dict:
    return refresh_market_data(
        run_date=run_date,
        symbol="GOLD",
        timeframe="1m",
        output_kind="binance_usdm_1m_feed",
    )


def run(run_date: str) -> dict:
    try:
        return run_binance_usdm_1m_feed_import(run_date)
    except Exception as exc:  # noqa: BLE001 - this heartbeat must fail visibly.
        return {
            "run_date": run_date,
            "status": "error",
            "message": f"{type(exc).__name__}: {exc}",
        }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Refresh GOLD 1m bars for DualTrack focus mode.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args(argv)

    result = run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"gold_1m_feed_heartbeat: {result.get('status')} imported_rows={result.get('imported_rows', 0)}")
    if result.get("status") == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
