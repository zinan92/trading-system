from __future__ import annotations

import argparse
import json

from services.python_runtime_compatibility import LaunchdPythonCompatibility


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the Python 3.9 interpreter used by local launchd Paper services.")
    parser.add_argument("--python", dest="interpreter", default="", help="Explicit launchd interpreter path.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = LaunchdPythonCompatibility(interpreter=args.interpreter or None).run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"launchd_python_compatibility: {result['status']} interpreter={result['interpreter']}")
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
