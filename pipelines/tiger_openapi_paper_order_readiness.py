from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_openapi_paper_order_readiness import TigerOpenApiPaperOrderReadiness


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate artifact-only Tiger paper order readiness.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--refresh-runbook", action="store_true", help="Write an artifact-only evidence refresh runbook from the latest readiness receipt.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    service = TigerOpenApiPaperOrderReadiness()
    result = service.refresh_runbook(args.date) if args.refresh_runbook else service.run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if args.refresh_runbook:
            print(
                "tiger_openapi_paper_order_readiness_refresh_runbook: "
                f"{result['status']} date={result['run_date']} "
                f"steps={len(result['command_sequence'])} "
                f"opens_trade_client_read_only={result['command_sequence_safety']['opens_trade_client_read_only']}"
            )
        else:
            print(
                "tiger_openapi_paper_order_readiness: "
                f"{result['status']} date={result['run_date']} "
                f"blockers={len(result['blockers'])} "
                f"real_tiger_network_call_attempted={result['real_tiger_network_call_attempted']}"
            )


if __name__ == "__main__":
    main()
