from __future__ import annotations

import argparse
import json
import sys

from services.connector_activation_plan import ConnectorActivationPlan


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a connector activation config preview without writing config.")
    parser.add_argument("--connector-id", required=True)
    parser.add_argument("--role", action="append", dest="roles", default=[])
    parser.add_argument("--environment", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = {
        "connector_id": args.connector_id,
        "requested_roles": args.roles or None,
        "environment": args.environment,
        "credential_confirmations": {},
    }
    result = ConnectorActivationPlan().evaluate(payload)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            "connector_activation_plan: "
            f"{result.get('status')} connector={result.get('connector_id')} "
            f"changes={result.get('change_count', 0)}"
        )
        for blocker in result.get("blockers", []):
            print(f"- blocker {blocker.get('name')}: {blocker.get('summary')}")
        for warning in result.get("warnings", []):
            print(f"- warning {warning.get('name')}: {warning.get('summary')}")
    if result.get("status") != "preview_ready":
        sys.exit(1)


if __name__ == "__main__":
    main()
