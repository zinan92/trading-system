from __future__ import annotations

import argparse
import json
from datetime import date

from services.config_loader import ROOT, load_pipeline_config
from services.paper_equity_curve import PaperEquityCurve


def main() -> None:
    parser = argparse.ArgumentParser(description="Build paper trading equity curve.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    output_root = ROOT / load_pipeline_config().get("output_root", "outputs")
    result = PaperEquityCurve(output_root).build(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(
        f"equity_curve: date={args.date} equity={result['current_equity']} "
        f"drawdown={result['current_drawdown_pct']} max_drawdown={result['max_drawdown_pct']}"
    )


if __name__ == "__main__":
    main()
