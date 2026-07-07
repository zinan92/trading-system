from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.connector_catalog import ConnectorCatalog
from services.journal_store import write_json
from services.run_date import utc_run_date


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the read-only connector catalog artifact.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = build_connector_catalog_response()
    config = load_pipeline_config()
    output_root = ROOT / str(config.get("output_root", "outputs"))
    write_json(output_root / "connector_catalog" / "current.json", [payload])
    write_json(output_root / "connector_catalog" / f"{args.date}.json", [payload])

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        summary = payload.get("summary", {})
        print(
            "connector_catalog: "
            f"{summary.get('connector_count', 0)} connectors, "
            f"{summary.get('ready_price_feed_count', 0)} price feeds ready, "
            f"{summary.get('ready_broker_count', 0)} brokers ready"
        )


def build_connector_catalog_response() -> dict:
    return ConnectorCatalog().snapshot()


if __name__ == "__main__":
    main()
