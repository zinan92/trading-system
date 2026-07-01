from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.live_env import LiveEnvStatus, initialize_live_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Check local live broker/OANDA env file without printing secret values.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--init", action="store_true", help="Create configs/live.env from the template with 600 permissions if it is missing.")
    parser.add_argument("--force", action="store_true", help="With --init, overwrite configs/live.env from the template.")
    args = parser.parse_args()

    init_result = initialize_live_env(force=args.force) if args.init else None
    result = LiveEnvStatus().run(args.date)
    if args.json:
        payload = {**result, "init": init_result} if init_result else result
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if init_result:
        print(f"init: path={init_result['path']} created={init_result['created']} overwritten={init_result['overwritten']} mode={init_result['permissions']['mode']}")
    print(f"live_env: {result['status']} date={result['run_date']}")
    print(f"env_file: {result['env_path']} exists={result['env_file_exists']}")
    print(f"present={','.join(result['present_keys']) or '-'}")
    print(f"missing={','.join(result['missing_keys']) or '-'}")


if __name__ == "__main__":
    main()
