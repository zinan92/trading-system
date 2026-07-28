from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from services.cloud_cutover_state import CloudCutoverStatePackage
from services.config_loader import ROOT


def _source_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or restore minimal Paper cutover state.")
    parser.add_argument("action", choices=("create", "restore"))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--package-id")
    args = parser.parse_args()
    package = CloudCutoverStatePackage(
        output_root=args.output_root,
        backup_root=args.backup_root,
        deployed_sha=_source_sha(),
    )
    if args.action == "create":
        result = package.create()
    else:
        if not args.package_id:
            raise SystemExit("restore requires --package-id")
        result = package.restore(
            package_id=args.package_id,
            destination_output_root=args.output_root,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
