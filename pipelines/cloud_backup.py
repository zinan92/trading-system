from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from services.cloud_backup import CloudPaperBackup
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
    parser = argparse.ArgumentParser(description="Create or verify a Cloud Paper backup.")
    parser.add_argument("action", choices=("create", "restore", "prune"))
    parser.add_argument("--backup-id")
    parser.add_argument("--destination-output-root", type=Path)
    parser.add_argument("--destination-datafeed-db", type=Path)
    parser.add_argument("--keep", type=int, default=7)
    args = parser.parse_args()
    config = load_pipeline_config()
    output_root = Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / config.get("output_root", "outputs")),
        )
    )
    backup = CloudPaperBackup(
        output_root=output_root,
        datafeed_db=Path(os.getenv("KLINE_DB_PATH", "/var/lib/gridmind/datafeed/kline.db")),
        backup_root=Path(os.getenv("GRIDMIND_BACKUP_ROOT", "/var/lib/gridmind/backups")),
        deployed_sha=_source_sha(),
        encryption_mode=os.getenv(
            "GRIDMIND_BACKUP_ENCRYPTION_MODE",
            "provider_volume_at_rest",
        ),
    )
    if args.action == "create":
        result = backup.create()
        result["retention"] = backup.prune(keep=args.keep)
    elif args.action == "prune":
        result = backup.prune(keep=args.keep)
    else:
        if not args.backup_id or not args.destination_output_root or not args.destination_datafeed_db:
            raise SystemExit(
                "restore requires --backup-id, --destination-output-root, "
                "and --destination-datafeed-db"
            )
        result = backup.restore(
            backup_id=args.backup_id,
            destination_output_root=args.destination_output_root,
            destination_datafeed_db=args.destination_datafeed_db,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
