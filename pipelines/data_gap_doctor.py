from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.data_gap_doctor import DataGapDoctor


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect GOLD 5m clean bars and report exact missing-bar gaps.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()
    config = load_pipeline_config()
    output_root = Path(config.get("output_root", "outputs"))
    if not output_root.is_absolute():
        output_root = ROOT / output_root
    print(json.dumps(DataGapDoctor(output_root).run(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
