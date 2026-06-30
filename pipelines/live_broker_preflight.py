from __future__ import annotations

import argparse

from services.live_broker_preflight_report import LiveBrokerPreflightReport


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the live broker preflight matrix without submitting real orders.")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    result = LiveBrokerPreflightReport().run(args.date)
    print(f"live_broker_preflight: {result['status']} date={result['run_date']} provider={result['provider']}")


if __name__ == "__main__":
    main()
