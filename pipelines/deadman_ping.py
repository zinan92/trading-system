from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.deadman_ping import ExternalDeadmanPing
from services.run_date import utc_run_date


def _output_root() -> Path:
    config = load_pipeline_config()
    return Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))


def _market_db() -> Path:
    config = load_pipeline_config()
    return Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))


def main() -> None:
    parser = argparse.ArgumentParser(description="Ping the external trading dead-man switch.")
    parser.add_argument("--date", default=utc_run_date(), help="UTC run date in YYYY-MM-DD format.")
    parser.add_argument("--url", default=None, help="Override TRADING_ORCHESTRATOR_DEADMAN_URL.")
    parser.add_argument("--position-url", default=None, help="Override TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL.")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--dry-run", action="store_true", help="Compute and persist the ping payload without sending.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = ExternalDeadmanPing(
        _output_root(),
        _market_db(),
        url=args.url,
        position_url=args.position_url,
        timeout_seconds=args.timeout_seconds,
    ).run(args.date, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            "deadman_ping "
            f"{result['status']} severity={result['severity']} configured={result['configured']} "
            f"position_open={result['exposure']['has_open_position']}"
        )


if __name__ == "__main__":
    main()
