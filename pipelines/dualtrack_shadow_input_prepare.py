"""Prepare a no-write, immutable event bundle for one Nautilus shadow cycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipelines.dualtrack_cycle_runner import DualTrackCycleRunner
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window_from_id, parse_utc
from services.execution_plugin_composition import build_execution_engine_adapter
from services.dualtrack_shadow_input import build_shadow_input
from services.dualtrack_config import dualtrack_config
from services.journal_store import load_json, write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare immutable DualTrack input for a Nautilus shadow replay.")
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--output-root", default="")
    parser.add_argument("--market-db", default="")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    as_of = parse_utc(args.as_of) if args.as_of else cycle_window_from_id(args.cycle_id).end
    runner = DualTrackCycleRunner(output_root=output_root, market_db=Path(args.market_db) if args.market_db else None)
    shadow = ((dualtrack_config().get("execution_shadow") or {}).get("nautilus") or {})
    source_symbol = str(shadow.get("source_symbol") or "")
    execution_instrument_id = str(shadow.get("execution_instrument_id") or "")
    if not source_symbol or source_symbol != runner.symbol or not execution_instrument_id:
        return _write_blocked(output_root, args.cycle_id, "shadow_symbol_mapping_invalid", args.json)
    preflight_rows = load_json(output_root / "dualtrack" / "nautilus" / "instrument_preflight.json")
    preflight = preflight_rows[-1] if preflight_rows else {}
    if preflight.get("status") != "ready_for_paper_shadow":
        return _write_blocked(output_root, args.cycle_id, "instrument_preflight_missing_or_not_ready", args.json)
    preflight_instrument = str((preflight.get("instrument") or {}).get("symbol") or "")
    if preflight_instrument != execution_instrument_id:
        return _write_blocked(output_root, args.cycle_id, "instrument_preflight_symbol_mismatch", args.json)
    bars = runner._cycle_bars(args.cycle_id, as_of=as_of)
    reason = runner._market_bar_rejection_reason(bars)
    if reason:
        return _write_blocked(output_root, args.cycle_id, reason, args.json)
    provider = str((runner.config.get("market_data") or {}).get("provider") or "")
    events = [
        {
            **runner._protective_bar_event(args.cycle_id, bar, now=as_of),
            "provider": provider,
            "instrument_id": execution_instrument_id,
            "source_symbol": source_symbol,
        }
        for bar in bars
    ]
    snapshot = build_execution_engine_adapter(output_root, config=runner.config).snapshot(args.cycle_id)
    commands = load_json(output_root / "dualtrack" / "shadow_commands" / f"{args.cycle_id}.json")
    try:
        artifact = build_shadow_input(
            cycle_id=args.cycle_id,
            authoritative_snapshot=snapshot,
            market_events=events,
            commands=commands,
        )
    except ValueError as exc:
        return _write_blocked(output_root, args.cycle_id, str(exc), args.json)
    write_json(output_root / "dualtrack" / "shadow_inputs" / f"{args.cycle_id}.json", [artifact])
    if args.json:
        print(json.dumps(artifact, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_shadow_input_prepare: cycle_id={args.cycle_id} input_id={artifact['input_id']}")
    return 0


def _write_blocked(output_root: Path, cycle_id: str, blocker: str, as_json: bool) -> int:
    artifact = {
        "schema_version": "dualtrack-shadow-input-v1",
        "cycle_id": cycle_id,
        "status": "blocked",
        "blocker": blocker,
    }
    write_json(output_root / "dualtrack" / "shadow_inputs" / f"{cycle_id}.json", [artifact])
    if as_json:
        print(json.dumps(artifact, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_shadow_input_prepare: cycle_id={cycle_id} status=blocked blocker={blocker}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
