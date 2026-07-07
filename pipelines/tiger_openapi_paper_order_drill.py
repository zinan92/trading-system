from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_openapi_paper_order_drill import DEFAULT_SCENARIOS, TigerOpenApiPaperOrderDrill


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local fake-client Tiger OpenAPI paper order-path drill.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--scenario", action="append", choices=DEFAULT_SCENARIOS, help="Scenario to run; can be passed more than once.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = TigerOpenApiPaperOrderDrill(run_id=args.run_id).run(args.date, scenarios=args.scenario)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            "tiger_openapi_paper_order_drill: "
            f"{result['status']} run_id={result['run_id']} "
            f"scenarios={','.join(item['status'] for item in result['scenarios'])} "
            f"real_tiger_network_call_attempted={result['real_tiger_network_call_attempted']}"
        )


if __name__ == "__main__":
    main()
