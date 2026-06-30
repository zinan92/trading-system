from __future__ import annotations

import json

from services.broker_adapter import broker_preflight


def main() -> None:
    print(json.dumps(broker_preflight(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
