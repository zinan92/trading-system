from __future__ import annotations

import argparse
import json
from datetime import date

from services.mt5_bridge_smoke import Mt5BridgeSmoke


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local-only MT5 file bridge smoke test with a mock receipt.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    print(json.dumps(Mt5BridgeSmoke().run(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
