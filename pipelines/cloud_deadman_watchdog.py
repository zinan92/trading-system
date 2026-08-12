from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from services.cloud_deadman_watchdog import CloudDeadmanWatchdog
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
    parser = argparse.ArgumentParser(
        description="Watch the Cloud dead-man process without masking its heartbeat."
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    _load_runtime_env()
    result = CloudDeadmanWatchdog(_output_root()).run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "cloud_deadman_watchdog "
            f"status={result['status']} code={result['machine_code']}"
        )
    if result["status"] == "healthy":
        return 0
    return 0 if dict(result.get("delivery") or {}).get("delivered") else 1


if __name__ == "__main__":
    raise SystemExit(main())
