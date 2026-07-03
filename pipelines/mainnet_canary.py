from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.mainnet_canary import MainnetCanary
from services.run_date import utc_run_date


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Attended mainnet canary. Default action is a READ-ONLY go/no-go check. "
            "Submitting requires --submit-mainnet-canary AND --confirm-mainnet-canary plus explicit order parameters."
        )
    )
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--submit-mainnet-canary", action="store_true", help="Request submission (still requires --confirm-mainnet-canary).")
    parser.add_argument("--confirm-mainnet-canary", action="store_true", help="Second explicit confirmation for a REAL mainnet order.")
    parser.add_argument("--side", choices=["buy", "sell"], help="Canary direction.")
    parser.add_argument("--quantity", type=float, help="Minimum order size, e.g. 0.001.")
    parser.add_argument("--entry-price", type=float, help="Reference entry price for the market order.")
    parser.add_argument("--stop-loss", type=float, help="Protective stop price (required).")
    parser.add_argument("--take-profit", type=float, help="Protective target price (required).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_pipeline_config()
    output_root = ROOT / config.get("output_root", "outputs")
    canary = MainnetCanary(Path(output_root))

    if not args.submit_mainnet_canary:
        report = canary.check(args.date)
        _emit(report, args.json)
        sys.exit(0 if report["go"] else 1)

    missing = [name for name, value in (
        ("--side", args.side),
        ("--quantity", args.quantity),
        ("--entry-price", args.entry_price),
        ("--stop-loss", args.stop_loss),
        ("--take-profit", args.take_profit),
    ) if value is None]
    if missing:
        parser.error("submission requires explicit order parameters: " + ", ".join(missing))

    result = canary.submit(
        args.date,
        side=args.side,
        quantity=args.quantity,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
        entry_price=args.entry_price,
        confirm=args.confirm_mainnet_canary,
    )
    _emit(result, args.json)


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if "blockers" in payload:
        print(f"mainnet canary check: {'GO' if payload['go'] else 'NO-GO'} ({payload['run_date']})")
        for reason in payload["blockers"]:
            print(f"  - {reason}")
        if payload["go"]:
            print("  all gates green; submit with --submit-mainnet-canary --confirm-mainnet-canary and explicit order parameters")
        return
    print(f"mainnet canary: {payload.get('status')} ticket={payload.get('ticket_id')}")


if __name__ == "__main__":
    main()
