from __future__ import annotations

import argparse
import json
import sys

from services.run_date import utc_run_date
from services.tiger_openapi_paper_order_canary import ACKNOWLEDGEMENT, TigerOpenApiPaperOrderCanary


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Attended Tiger paper order canary. Default action is an artifact-only check. "
            "Submitting requires --submit-tiger-paper-canary, --confirm-tiger-paper-canary, "
            "and the exact acknowledgement phrase."
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
    parser.add_argument("--notes", default="attended Tiger paper order canary")
    parser.add_argument(
        "--use-attended-canary-risk-limits",
        action="store_true",
        help="Use the broker-profile attended paper canary risk package for this explicit canary only.",
    )
    parser.add_argument("--submit-tiger-paper-canary", action="store_true")
    parser.add_argument("--confirm-tiger-paper-canary", action="store_true")
    parser.add_argument("--acknowledge-tiger-paper-network-submission", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    canary = TigerOpenApiPaperOrderCanary()
    if args.submit_tiger_paper_canary:
        payload = canary.submit(
            args.date,
            ticket_id=args.ticket_id,
            asset=args.asset,
            side=args.side,
            quantity=args.quantity,
            entry_price=args.entry_price,
            stop_loss=args.stop_loss,
            take_profit=args.take_profit,
            confirm=args.confirm_tiger_paper_canary,
            acknowledgement=args.acknowledge_tiger_paper_network_submission,
            operator=args.operator,
            notes=args.notes,
            use_attended_canary_risk_limits=args.use_attended_canary_risk_limits,
        )
    else:
        payload = canary.check(
            args.date,
            ticket_id=args.ticket_id,
            asset=args.asset,
            side=args.side,
            quantity=args.quantity,
            entry_price=args.entry_price,
            stop_loss=args.stop_loss,
            take_profit=args.take_profit,
            operator=args.operator,
            notes=args.notes,
            use_attended_canary_risk_limits=args.use_attended_canary_risk_limits,
        )
    _emit(payload, args.json)
    if payload.get("status") not in {"ready_for_operator_authorization", "submitted_to_tiger_paper"}:
        sys.exit(1)


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"tiger_openapi_paper_order_canary: {payload.get('status')} date={payload.get('run_date')}")
    if payload.get("blockers"):
        for item in payload["blockers"]:
            print(f"- {item.get('name')}: {item.get('summary')}")
    elif payload.get("status") == "ready_for_operator_authorization":
        print("- artifact-only check passed")
        print(f"- submit requires acknowledgement: {ACKNOWLEDGEMENT}")


if __name__ == "__main__":
    main()
