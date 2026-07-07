from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_openapi_kill_switch import run_tiger_openapi_paper_kill_switch


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan Tiger OpenAPI paper kill-switch actions. Defaults to dry-run/read-only.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--confirm-tiger-kill", action="store_true", help="Activate HALT; Tiger cancel/close runs only if explicitly enabled in broker config.")
    parser.add_argument("--clear-halt", action="store_true")
    parser.add_argument("--confirm-clear", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run_tiger_openapi_paper_kill_switch(
        args.date,
        confirm_tiger_kill=args.confirm_tiger_kill,
        clear_halt=args.clear_halt,
        confirm_clear=args.confirm_clear,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            "tiger_openapi_kill_switch: "
            f"{result['status']} date={result['run_date']} "
            f"network_order_created={result.get('network_order_created', False)} "
            f"network_cancel_created={result.get('network_cancel_created', False)}"
        )


if __name__ == "__main__":
    main()
