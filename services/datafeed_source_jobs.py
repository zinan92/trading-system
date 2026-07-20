"""Named source jobs that keep scheduler compatibility while using datafeed only."""

from __future__ import annotations

from pathlib import Path
import os

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.market_data_refresh import refresh_market_data


def run_oanda_feed_import(run_date: str, output_root: Path | None = None) -> dict:
    config = load_pipeline_config()
    root = output_root or Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / str(config.get("output_root", "outputs"))),
        )
    )
    try:
        return refresh_market_data(
            run_date=run_date,
            symbol="GOLD",
            timeframe="5m",
            output_kind="oanda_feed",
            source="oanda_v20",
            output_root=root,
        )
    except Exception as error:
        result = {
            "run_date": run_date,
            "status": "skipped",
            "message": f"OANDA datafeed adapter is not available: {error}",
            "imported_rows": 0,
            "market_data_backend": "datafeed",
            "missing_env": ["configure_oanda_v20_in_datafeed"],
        }
        write_json(root / "oanda_feed" / "current.json", [result])
        write_json(root / "oanda_feed" / f"{run_date}.json", [result])
        return result
