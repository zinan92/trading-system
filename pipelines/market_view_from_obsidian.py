from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.live_env import apply_live_env
from services.market_view_obsidian import MarketViewObsidianSync


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate market_view runtime artifacts from the Obsidian daily trading note.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--obsidian-root", type=Path, default=None)
    parser.add_argument("--note-path", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    apply_live_env()
    obsidian_root = args.obsidian_root or _obsidian_root_from_env()
    if obsidian_root is None:
        raise SystemExit("--obsidian-root or TRADING_ORCHESTRATOR_OBSIDIAN_ROOT is required")

    cfg = load_pipeline_config()
    output_root = ROOT / str(cfg.get("output_root", "outputs"))
    try:
        result = MarketViewObsidianSync(output_root, obsidian_root).sync(args.date, note_path=args.note_path)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        "market_view_from_obsidian: "
        f"{result['run_date']} {result['direction_score']} {result['direction_bias']} "
        f"source={result.get('intake', {}).get('source_note', '')}"
    )


def _obsidian_root_from_env() -> Path | None:
    value = os.getenv("TRADING_ORCHESTRATOR_OBSIDIAN_ROOT", "").strip()
    return Path(value) if value else None


if __name__ == "__main__":
    main()
