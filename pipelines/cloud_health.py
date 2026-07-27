from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from services.cloud_health import CloudPaperHealth
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


def build_cloud_health(*, persist: bool = True) -> dict:
    config = load_pipeline_config()
    output_root = Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / config.get("output_root", "outputs")),
        )
    )
    return CloudPaperHealth(
        output_root=output_root,
        backup_root=Path(
            os.getenv("GRIDMIND_BACKUP_ROOT", "/var/lib/gridmind/backups")
        ),
        deployed_sha=_source_sha(),
    ).run(persist=persist)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build layered Cloud Paper health.")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = build_cloud_health(persist=not args.no_persist)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"cloud_health: {result['status']} "
            f"incidents={len(result['incidents'])}"
        )
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
