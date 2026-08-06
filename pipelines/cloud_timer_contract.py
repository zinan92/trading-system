from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from services.cloud_timer_contract import CloudTimerContract
from services.config_loader import ROOT, load_pipeline_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect canonical Cloud Paper systemd timer invariants."
    )
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_pipeline_config()
    output_root = Path(
        args.output_root
        or os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT")
        or ROOT / str(config.get("output_root", "outputs"))
    )
    result = CloudTimerContract(output_root).run(
        persist=not args.no_persist
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"cloud_timer_contract: {result['status']}")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
