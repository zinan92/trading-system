"""CLI: audit the local market_data.db bars table for data-quality issues.

Examples:
  python3 -m pipelines.data_health --date 2026-05-29
  python3 -m pipelines.data_health --date 2026-05-29 --json
  python3 -m pipelines.data_health --date 2026-05-29 --clean       # drop degenerate snapshot rows
  python3 -m pipelines.data_health --date 2026-05-29 --clean --dry-run
  python3 -m pipelines.data_health --date 2026-05-29 --clean-synthetic           # drop cold-start synthetic seed rows
  python3 -m pipelines.data_health --date 2026-05-29 --clean-synthetic --dry-run
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from services.data_health import DataHealthAuditor, run_data_health


def _format_human(result: dict) -> str:
    lines: list[str] = []
    s = result.get("summary", {})
    status = result.get("status", "?")
    badge = {"ok": "✅", "info": "ℹ", "warn": "⚠️", "error": "❌"}.get(status, "?")
    lines.append(f"{badge} data_health {status.upper()}")
    lines.append(f"  symbol={result['symbol']} timeframe={result['timeframe']} db={result.get('db_path','')}")
    lines.append(f"  total_bars={s.get('total_bars')}  first={s.get('first_timestamp')}  last={s.get('last_timestamp')}")
    lines.append(f"  latest_age={s.get('latest_age_minutes')} min  provider={s.get('latest_provider')}  close={s.get('latest_close')}")
    lines.append("")
    lines.append("providers:")
    for p in result.get("providers", []):
        lines.append(
            f"  {p['provider']:<22} rows={p['rows']:>5} "
            f"first={p['first_timestamp']} last={p['last_timestamp']} "
            f"degenerate={p['degenerate']} misaligned={p['misaligned']}"
        )
    issues = result.get("issues", [])
    if issues:
        lines.append("")
        lines.append("issues:")
        for issue in issues:
            level = issue.get("level", "?").upper()
            lines.append(f"  [{level:<5}] {issue['code']}: {issue['message']}")
            if issue.get("action"):
                lines.append(f"          → {issue['action']}")
    recs = result.get("recommendations", [])
    if recs:
        lines.append("")
        lines.append("recommendations:")
        for r in recs:
            lines.append(f"  • {r}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit the local market_data.db bars table.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Run date for the audit artifact (YYYY-MM-DD).")
    parser.add_argument("--symbol", default="GOLD")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--json", action="store_true", help="Emit the raw JSON payload instead of the human summary.")
    parser.add_argument("--clean", action="store_true", help="Delete degenerate snapshot rows (V=0, H=L=O=C) from the bars table.")
    parser.add_argument("--clean-synthetic", action="store_true", help="Delete cold-start synthetic seed rows (provider=local_synthetic_seed) from the bars table.")
    parser.add_argument("--dry-run", action="store_true", help="With --clean/--clean-synthetic: print what would be deleted but do not modify the DB.")
    args = parser.parse_args()

    auditor = DataHealthAuditor(symbol=args.symbol, timeframe=args.timeframe)

    if args.clean or args.clean_synthetic:
        if args.clean:
            print(json.dumps(auditor.clean_degenerate_snapshots(dry_run=args.dry_run), indent=2))
        if args.clean_synthetic:
            print(json.dumps(auditor.clean_synthetic_seed(dry_run=args.dry_run), indent=2))
        if not args.dry_run:
            # Re-audit so the persisted artifact reflects the cleaned state.
            run_data_health(args.date)
        return

    result = run_data_health(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_format_human(result))


if __name__ == "__main__":
    main()
