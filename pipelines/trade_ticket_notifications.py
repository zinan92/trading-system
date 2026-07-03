from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.run_date import utc_run_date
from services.trade_ticket_notifier import TradeTicketNotifier


def main() -> None:
    parser = argparse.ArgumentParser(description="Send trade-ticket review cards to the Feishu report channel.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--strategy-id", default="", help="Optional single strategy id under outputs/strategies/.")
    parser.add_argument("--force", action="store_true", help="Resend tickets even if a delivered notification already exists.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_pipeline_config()
    output_root = ROOT / str(config.get("output_root", "outputs"))
    notifier = TradeTicketNotifier(output_root)
    results = []

    strategy_root = output_root / "strategies"
    if args.strategy_id:
        namespaces = [strategy_root / args.strategy_id]
    else:
        namespaces = sorted(path for path in strategy_root.iterdir() if path.is_dir()) if strategy_root.exists() else []

    for namespace in namespaces:
        results.append(notifier.notify_namespace(args.date, namespace.name, namespace, force=args.force))

    payload = {
        "run_date": args.date,
        "strategy_count": len(results),
        "sent": sum(int(item.get("sent", 0) or 0) for item in results),
        "skipped": sum(int(item.get("skipped", 0) or 0) for item in results),
        "failed": sum(int(item.get("failed", 0) or 0) for item in results),
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(
        "trade_ticket_notifications: "
        f"sent={payload['sent']} skipped={payload['skipped']} failed={payload['failed']} strategies={payload['strategy_count']}"
    )


if __name__ == "__main__":
    main()
