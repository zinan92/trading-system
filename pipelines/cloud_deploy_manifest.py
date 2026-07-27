from __future__ import annotations

import argparse
import json

from services.cloud_cutover import CloudDeployManifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a source-bound, non-secret Cloud Paper deploy manifest."
    )
    parser.add_argument("--trading-system-sha", required=True)
    parser.add_argument("--datafeed-sha", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    result = CloudDeployManifest(
        trading_system_sha=args.trading_system_sha,
        datafeed_sha=args.datafeed_sha,
        hostname=args.hostname,
        region=args.region,
    ).to_dict()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
