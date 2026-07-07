from __future__ import annotations

import argparse
import json
import sys

from services.connector_onboarding import ConnectorOnboardingDryRun


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a read-only connector onboarding dry-run.")
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
    result = ConnectorOnboardingDryRun().evaluate(payload)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"connector_onboarding: {result.get('status')} connector={result.get('connector_id')}")
        for blocker in result.get("blockers", []):
            print(f"- {blocker.get('name')}: {blocker.get('summary')}")
    if result.get("status") != "ready_for_operator_setup":
        sys.exit(1)


if __name__ == "__main__":
    main()
