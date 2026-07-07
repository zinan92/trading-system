from __future__ import annotations

import argparse
import json
import sys

from services.run_date import utc_run_date
from services.tiger_openapi_paper_order_approval import TigerOpenApiPaperOrderApproval


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build an artifact-only operator approval package for an attended Tiger paper canary. "
            "This command never submits, previews, cancels, modifies, or closes Tiger orders."
        )
    )
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--ticket-id", required=True)
    parser.add_argument("--asset", required=True, help="Dated Tiger futures contract, e.g. MGC2608.")
    parser.add_argument("--side", choices=["buy", "sell"], required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--entry-price", type=float, required=True)
    parser.add_argument("--stop-loss", type=float, required=True)
    parser.add_argument("--take-profit", type=float, required=True)
    parser.add_argument("--operator", default="manual")
    parser.add_argument("--props-path", default="", help="Owner-only Tiger OpenAPI properties path for generated commands.")
    parser.add_argument("--notes", default="attended Tiger paper order canary approval package")
    parser.add_argument("--use-attended-canary-risk-limits", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = TigerOpenApiPaperOrderApproval().build(
        args.date,
        ticket_id=args.ticket_id,
        asset=args.asset,
        side=args.side,
        quantity=args.quantity,
        entry_price=args.entry_price,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
        operator=args.operator,
        props_path=args.props_path,
        use_attended_canary_risk_limits=args.use_attended_canary_risk_limits,
        notes=args.notes,
    )
    _emit(payload, args.json)
    if payload.get("status") != "ready_for_operator_approval":
        sys.exit(1)


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"tiger_openapi_paper_order_approval: {payload.get('status')} date={payload.get('run_date')}")
    for item in payload.get("blockers", []):
        print(f"- {item.get('name')}: {item.get('summary')}")
    if payload.get("status") == "ready_for_operator_approval":
        print("- approval package ready; no Tiger network call was attempted")
        print(f"- submit command: {payload.get('submit_command')}")


if __name__ == "__main__":
    main()
