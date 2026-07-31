"""Build the fail-closed Cloud Paper live-tick latency report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.live_tick_latency_report import (
    DEFAULT_MINIMUM_SAMPLES,
    build_live_tick_latency_report,
)
from services.live_tick_timing import read_timing_receipts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize one homogeneous natural Cloud live-tick window.",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--minimum-samples",
        type=int,
        default=DEFAULT_MINIMUM_SAMPLES,
    )
    args = parser.parse_args()
    rows = read_timing_receipts(args.output_root)
    report = build_live_tick_latency_report(
        rows,
        minimum_samples=args.minimum_samples,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
