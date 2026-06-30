from __future__ import annotations

import argparse

from services.data_trust_report import DataTrustReport


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the GOLD 5m data trust report for current display and trading gates.")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    result = DataTrustReport().run(args.date)
    print(f"data_trust: {result['status']} date={result['run_date']} mode={result['display_mode']} latest={result['summary']['latest_price']}")


if __name__ == "__main__":
    main()
