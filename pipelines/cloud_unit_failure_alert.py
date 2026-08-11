from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from services.cloud_unit_failure_alert import CloudUnitFailureAlert
from services.config_loader import ROOT, load_pipeline_config
from services.live_env import apply_live_env


def _output_root() -> Path:
    config = load_pipeline_config()
    default = ROOT / config.get("output_root", "outputs")
    return Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(default)))


def _load_runtime_env() -> None:
    if os.getenv("GRIDMIND_RUNTIME_MODE") != "cloud":
        apply_live_env()


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist and signal a Cloud unit failure.")
    parser.add_argument("--failed-unit", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    _load_runtime_env()
    result = CloudUnitFailureAlert(
        _output_root(), timeout_seconds=args.timeout_seconds
    ).run(args.failed_unit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "cloud_unit_failure_alert "
            f"unit={result['failed_unit']} status={result['delivery']['status']}"
        )
    return 0 if result["delivery"]["delivered"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
