from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.market_view_intake import MarketViewIntake


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse Park's oral gold market view into a structured market-view artifact.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--raw-text", default="")
    parser.add_argument("--raw-file", type=Path, default=None)
    parser.add_argument("--obsidian-root", type=Path, default=None)
    parser.add_argument("--write-obsidian", action="store_true")
    parser.add_argument("--draft-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    raw_text = args.raw_file.read_text(encoding="utf-8") if args.raw_file else args.raw_text
    if not raw_text.strip():
        raise SystemExit("--raw-text or --raw-file is required")

    cfg = load_pipeline_config()
    output_root = ROOT / str(cfg.get("output_root", "outputs"))
    intake = MarketViewIntake(output_root, obsidian_root=args.obsidian_root)
    if args.draft_only:
        draft = intake.draft(args.date, raw_text)
        result = {
            "run_date": draft.run_date,
            "score": draft.score,
            "summary": draft.summary,
            "key_levels": draft.key_levels,
            "timeframes": draft.timeframes,
            "trade_plan": draft.trade_plan,
            "valid_for_hours": draft.valid_for_hours,
            "expires_at": draft.expires_at,
            "expires_if_price_moves_pct": draft.expires_if_price_moves_pct,
            "expiry_target_price": draft.expiry_target_price,
            "expire_above": draft.expire_above,
            "expire_below": draft.expire_below,
        }
    else:
        result = intake.record(args.date, raw_text, write_obsidian=args.write_obsidian)

    if args.json or args.draft_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        "market_view_intake: "
        f"{result['run_date']} {result['direction_score']} {result['direction_bias']} "
        f"target={result.get('expiry', {}).get('target_price') or 'n/a'}"
    )


if __name__ == "__main__":
    main()
