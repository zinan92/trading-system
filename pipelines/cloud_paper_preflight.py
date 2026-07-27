from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.cloud_paper_preflight import CloudPaperPreflight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the read-only Linux Cloud Paper preflight.")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = CloudPaperPreflight(output_root=args.output_root).run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"cloud_paper_preflight: {result['status']}")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
