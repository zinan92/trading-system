from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.mac_paper_scheduler_isolation import (
    RESTORE_ACKNOWLEDGEMENT,
    MacPaperSchedulerIsolation,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Persistently isolate or explicitly restore Mac Paper jobs."
    )
    parser.add_argument("action", choices=("isolate", "verify", "restore"))
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--acknowledgement", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = load_pipeline_config()
    output_root = args.output_root or Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / config.get("output_root", "outputs")),
        )
    )
    service = MacPaperSchedulerIsolation(output_root)
    if args.action == "isolate":
        result = service.isolate()
    elif args.action == "verify":
        result = service.verify()
    else:
        result = service.restore(acknowledgement=args.acknowledgement)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"mac_paper_scheduler_{args.action}: {result['status']} "
            f"blocker={result.get('blocker') or '-'}"
        )
        if args.action == "restore" and result["status"] != "pass":
            print(f"required acknowledgement: {RESTORE_ACKNOWLEDGEMENT}")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

