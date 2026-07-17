"""Verify that raw historical machine fills rebuild without phantom units."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_nautilus_parity_contract import platform_parity_code_hash
from services.dualtrack_scoring import _trades_from_fills
from services.journal_store import load_json, write_json


def build_residual_check(output_root: Path, *, cycle_id: str) -> dict:
    fills = load_json(Path(output_root) / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
    evidence = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "platform_code_hash": platform_parity_code_hash(),
    }
    if not fills:
        return {
            "schema_version": "dualtrack-machine-residual-remediation-v1",
            **evidence,
            "cycle_id": cycle_id,
            "status": "blocked",
            "blocker": "historical_machine_fills_missing",
        }
    trades = _trades_from_fills(fills, track="machine")
    residuals = [
        {"trade_id": trade.get("trade_id"), "remaining_units": trade.get("remaining_units")}
        for trade in trades
        if float(trade.get("remaining_units") or 0.0) > 1e-9
    ]
    return {
        "schema_version": "dualtrack-machine-residual-remediation-v1",
        **evidence,
        "cycle_id": cycle_id,
        "status": "pass" if not residuals else "drift",
        "raw_fill_count": len(fills),
        "rebuilt_trade_count": len(trades),
        "residuals": residuals,
        "raw_fills_immutable": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check historical DualTrack machine residual units without rewriting raw fills.")
    parser.add_argument("--cycle-id", default="2026-07-09_DAY")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    result = build_residual_check(output_root, cycle_id=args.cycle_id)
    write_json(output_root / "dualtrack" / "nautilus" / "parity" / "historical_machine_residual_units.json", [result])
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_machine_residual_check: cycle_id={args.cycle_id} status={result['status']}")
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
