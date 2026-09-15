"""Mainnet dry-run observer CLI (#1277).

    python -m pipelines.mainnet_observer observe --output-root ...   # launchd, every 60 s
    python -m pipelines.mainnet_observer status  --output-root ...
    python -m pipelines.mainnet_observer report  --output-root ...
    python -m pipelines.mainnet_observer unlock  --output-root ... --operator park
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from services import mainnet_observer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mainnet_observer")
    parser.add_argument("command", choices=["observe", "status", "report", "unlock"])
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--operator", default="")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.command == "observe":
        result = mainnet_observer.observe_once(args.output_root, now=now)
    elif args.command == "status":
        result = mainnet_observer.mainnet_status(args.output_root)
    elif args.command == "report":
        result = mainnet_observer.dry_run_report(args.output_root, now=now)
    else:
        result = mainnet_observer.unlock(args.output_root, operator=args.operator, now=now)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
