from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.market_data_access import market_data_repository
from services.market_view import MarketViewStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Record Park's daily gold market view and direction bias.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--score", type=float, required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--raw-text", default="")
    parser.add_argument("--raw-file", type=Path, default=None)
    parser.add_argument("--key-level", action="append", default=[])
    parser.add_argument("--timeframe", action="append", default=[])
    parser.add_argument("--trade-plan", default="")
    parser.add_argument("--reference-price", type=float, default=None)
    parser.add_argument("--valid-for-hours", type=float, default=None)
    parser.add_argument("--expires-at", default="")
    parser.add_argument("--expire-if-price-moves-pct", type=float, default=None)
    parser.add_argument("--expiry-target-price", type=float, default=None)
    parser.add_argument("--expire-above", type=float, default=None)
    parser.add_argument("--expire-below", type=float, default=None)
    parser.add_argument("--obsidian-root", type=Path, default=None)
    parser.add_argument("--write-obsidian", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    raw_text = args.raw_text
    if args.raw_file:
        raw_text = args.raw_file.read_text(encoding="utf-8")
    cfg = load_pipeline_config()
    output_root = ROOT / str(cfg.get("output_root", "outputs"))
    reference_price = args.reference_price
    if reference_price is None:
        reference_price = _latest_gold_reference_price(ROOT / str(cfg.get("local_market_db", "data/market_data.db")))
    result = MarketViewStore(output_root, obsidian_root=args.obsidian_root).record(
        run_date=args.date,
        score=args.score,
        summary=args.summary,
        raw_text=raw_text,
        key_levels=args.key_level,
        timeframes=args.timeframe,
        trade_plan=args.trade_plan,
        reference_price=reference_price,
        valid_for_hours=args.valid_for_hours,
        expires_at=args.expires_at,
        expires_if_price_moves_pct=args.expire_if_price_moves_pct,
        expiry_target_price=args.expiry_target_price,
        expire_above=args.expire_above,
        expire_below=args.expire_below,
        write_obsidian=args.write_obsidian,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"market_view: {result['run_date']} {result['direction_score']} {result['direction_bias']}")


def _latest_gold_reference_price(db_path: Path) -> float | None:
    store = market_data_repository(db_path)
    latest = store.load_latest_quote("GOLD") or store.load_latest_bar("GOLD", "1m") or store.load_latest_bar("GOLD", "5m")
    if not latest:
        return None
    try:
        return float(latest.get("close") or latest.get("price"))
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
