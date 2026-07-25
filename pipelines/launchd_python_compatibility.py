from __future__ import annotations

import argparse
import json

from services.python_runtime_compatibility import LaunchdPythonCompatibility


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the interpreters actually configured for local launchd Paper services.")
    parser.add_argument("--python", dest="interpreter", default="", help="Probe one explicit interpreter instead of discovering job plists.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = LaunchdPythonCompatibility(interpreter=args.interpreter or None).run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"launchd_python_compatibility: {result['status']} targets={len(result.get('targets', []))}")
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
