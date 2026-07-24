from __future__ import annotations

import argparse
import json

from services.paper_predeploy_gate import PaperPredeployGate


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the non-actuating Paper pre-deploy compatibility gate.")
    parser.add_argument("--python", dest="interpreter", default="", help="Explicit launchd interpreter path.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = PaperPredeployGate(interpreter=args.interpreter or None).run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"paper_predeploy_gate: {result['status']}")
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
