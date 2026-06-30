from __future__ import annotations

import argparse
import json
from datetime import date

from services.data_archive_manifest import run_data_archive_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local data archive manifest for a Trading Bot run date.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    print(json.dumps(run_data_archive_manifest(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
