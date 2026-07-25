from __future__ import annotations

import argparse
import json

from services.paper_service_rebootstrap import (
    PAPER_REBOOTSTRAP_LABELS,
    PaperServiceRebootstrap,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebootstrap one allowlisted local Paper launchd service."
    )
    parser.add_argument("--label", required=True, choices=PAPER_REBOOTSTRAP_LABELS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = PaperServiceRebootstrap().run(args.label)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"paper_service_rebootstrap: {result['status']}")
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
