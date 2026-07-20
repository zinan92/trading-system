"""Idempotently import pre-shadow production commands into Nautilus paper shadow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter
from services.dualtrack_config import dualtrack_config
from services.journal_store import load_json, write_json


def backfill_shadow_commands(
    adapter: Any,
    *,
    source_path: Path,
    cycle_id: str,
) -> dict[str, Any]:
    rows = load_json(source_path)
    if not rows:
        raise ValueError("shadow command source is empty")
    receipts = []
    for row in rows:
        if str(row.get("cycle_id") or "") != cycle_id:
            raise ValueError("shadow command cycle_id mismatch")
        command = row.get("command") if isinstance(row, dict) else None
        if not isinstance(command, dict):
            raise ValueError("shadow command is missing command payload")
        if str(command.get("cycle_id") or "") != cycle_id:
            raise ValueError("shadow command payload cycle_id mismatch")
        authoritative_order_id = str(row.get("accepted_order_id") or row.get("accepted_fill_id") or "")
        mapped = dict(command)
        if authoritative_order_id:
            mapped["authoritative_order_id"] = authoritative_order_id
        receipts.append(adapter.submit_order(mapped))
    flushed = adapter.flush(cycle_id)
    return {
        "schema_version": "dualtrack-nautilus-command-backfill-v1",
        "cycle_id": cycle_id,
        "status": str(flushed.get("status") or "blocked"),
        "source_path": str(source_path),
        "source_command_count": len(rows),
        "accepted_receipt_count": len(receipts),
        "processed_command_count": int(flushed.get("processed_command_count") or 0),
        "processed_event_count": int(flushed.get("processed_event_count") or 0),
        "real_money_eligible": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill one cycle's immutable production commands into Nautilus shadow.")
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--nautilus-python", required=True)
    parser.add_argument("--output-root", default="")
    parser.add_argument("--source-path", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    source_path = Path(args.source_path) if args.source_path else (
        output_root / "dualtrack" / "shadow_commands" / f"{args.cycle_id}.json"
    )
    adapter = NautilusExecutionAdapter(
        output_root,
        nautilus_python=args.nautilus_python,
        defer_replay=True,
        config=dualtrack_config(),
    )
    try:
        result = backfill_shadow_commands(adapter, source_path=source_path, cycle_id=args.cycle_id)
    except Exception as exc:
        result = {
            "schema_version": "dualtrack-nautilus-command-backfill-v1",
            "cycle_id": args.cycle_id,
            "status": "blocked",
            "blocker": str(exc)[-1000:],
            "source_path": str(source_path),
            "real_money_eligible": False,
        }
    root = output_root / "dualtrack" / "nautilus_shadow_runtime" / "backfills"
    write_json(root / f"{args.cycle_id}.json", [result])
    write_json(root / "current.json", [result])
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_nautilus_command_backfill: cycle_id={args.cycle_id} status={result['status']}")
    return 0 if result["status"] in {"replayed", "idempotent"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
