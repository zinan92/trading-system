"""Run the Linux Cloud runtime portability architecture check."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.cloud_linux_portability import audit_cloud_runtime
from services.config_loader import ROOT


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject macOS and personal absolute paths in the Linux Cloud runtime closure."
    )
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = audit_cloud_runtime(args.repo_root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"cloud_linux_portability: {result['status']}")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
