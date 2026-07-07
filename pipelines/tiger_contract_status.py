from __future__ import annotations

import argparse
import json

from services.config_loader import ROOT, load_pipeline_config
from services.run_date import utc_run_date
from services.tiger_contracts import TigerContractResolver


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve Tiger execution contract and rollover status without Tiger network calls.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--symbol", default="MGCmain")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = load_pipeline_config()
    output_root = ROOT / str(config.get("output_root", "outputs"))
    profile = (config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper", {})
    result = TigerContractResolver(profile if isinstance(profile, dict) else {}).write_status(
        output_root,
        args.date,
        args.symbol,
        as_of=args.as_of or f"{args.date}T00:00:00+00:00",
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        resolution = result["resolution"]
        print(
            "tiger_contract_status: "
            f"{resolution['status']} requested={resolution['requested_symbol']} "
            f"execution={resolution['execution_symbol']} days_to_contract_month={resolution['days_to_contract_month']}"
        )


if __name__ == "__main__":
    main()
