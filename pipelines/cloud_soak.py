from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from services.cloud_soak import CloudPaperSoak
from services.config_loader import ROOT, load_pipeline_config


def _source_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return str(result.stdout or "").strip() if result.returncode == 0 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Start or check the Cloud Paper soak.")
    parser.add_argument("action", choices=("start", "check"))
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--mac-schedulers-disabled", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = load_pipeline_config()
    soak = CloudPaperSoak(
        output_root=args.output_root
        or Path(
            os.getenv(
                "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
                str(ROOT / config.get("output_root", "outputs")),
            )
        ),
        backup_root=args.backup_root
        or Path(os.getenv("GRIDMIND_BACKUP_ROOT", "/var/lib/gridmind/backups")),
        deployed_sha=_source_sha(),
        owner_id=str(os.getenv("GRIDMIND_SCHEDULER_OWNER_ID") or ""),
    )
    result = (
        soak.start(mac_schedulers_disabled=args.mac_schedulers_disabled)
        if args.action == "start"
        else soak.check()
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"cloud_soak: {result['status']} "
            f"ticks={result['metrics']['successful_tick_count']}"
        )
    return 0 if result["status"] in {"running", "pass"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
