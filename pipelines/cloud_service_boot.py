from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from services.cloud_service_boot import CloudPaperServiceBootGate
from services.config_loader import ROOT, load_pipeline_config
from services.paper_release_receipt import PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one Cloud Paper service boot.")
    parser.add_argument("--service", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = load_pipeline_config()
    output_root = Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / config.get("output_root", "outputs")),
        )
    )
    result = CloudPaperServiceBootGate(output_root).verify(args.service)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"cloud_service_boot: {args.service} {result['status']}")
    return 0 if result["ok"] else PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
