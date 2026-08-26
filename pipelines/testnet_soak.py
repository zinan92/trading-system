"""Attended source-bound Testnet soak window collector.

The command only reads already-produced DCA/Grid/broker evidence files and
records a package. It never submits, cancels, flattens, or changes strategy.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.testnet_soak_readiness import TestnetSoakError, TestnetSoakReadiness
from services.testnet_automation_readiness import TestnetAutomationReadiness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record one attended Testnet soak window from evidence artifacts")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--observation", required=True, help="JSON observation containing identity/window/gates")
    parser.add_argument("--artifact", action="append", default=[], metavar="CATEGORY=PATH")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--automation", action="store_true", help="Use the two-window BTC automation readiness contract")
    parser.add_argument("--strategy-family", choices=("dca", "grid"))
    parser.add_argument("--instrument-id", default="BTC-USD-PERP")
    args = parser.parse_args(argv)
    try:
        observation = json.loads(Path(args.observation).read_text(encoding="utf-8"))
        artifacts = {}
        for item in args.artifact:
            category, separator, path = str(item).partition("=")
            if not separator or not category or not path:
                raise TestnetSoakError("artifact_argument_invalid")
            artifacts[category] = path
        if args.automation:
            if not args.strategy_family:
                raise TestnetSoakError("strategy_family_required_for_automation_readiness")
            soak = TestnetAutomationReadiness(
                Path(args.output_root),
                strategy_family=args.strategy_family,
                instrument_id=args.instrument_id,
            )
        else:
            soak = TestnetSoakReadiness(Path(args.output_root))
        row = soak.record_window_from_artifacts(observation, artifact_paths=artifacts)
        result = {"status": row["status"], "window": row}
        if args.finalize:
            result["readiness"] = soak.finalize()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if row["status"] == "pass" and (not args.finalize or result["readiness"].get("status") == "ready") else 2
    except (AttributeError, OSError, ValueError, TestnetSoakError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "code": getattr(exc, "code", type(exc).__name__)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
