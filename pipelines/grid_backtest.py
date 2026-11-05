"""Backtest one Grid plan over the stored OKX history and write the result file.

Runs apart from every service: it writes one JSON file and nothing else. A failure is a
result too, ``status: unavailable`` with the reason, because the page shows it.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from services.datafeed_market_client import DatafeedMarketClient
from services.dualtrack_config import dualtrack_config
from services.grid_backtest import SCHEMA, STEP, BacktestUnavailable, load_history, run_backtest, shape_from_plan, subprocess_replay

SOURCE = "okx_swap_public"
INSTRUMENT = "XAU-USDT-SWAP"


def write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-path", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--preflight", required=True, type=Path)
    parser.add_argument("--nautilus-python", required=True)
    parser.add_argument("--datafeed-url", default="http://127.0.0.1:8100")
    parser.add_argument("--step-hours", type=float, default=STEP.total_seconds() / 3600)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = parser.parse_args(argv)
    started = datetime.now(timezone.utc)
    write_result(args.output, {"schema_version": SCHEMA, "status": "running", "started_at": started.isoformat()})
    try:
        shape = shape_from_plan(json.loads(args.plan_path.read_text(encoding="utf-8")))
        history = load_history(DatafeedMarketClient(base_url=args.datafeed_url, timeout_seconds=60.0), source=SOURCE, instrument=INSTRUMENT)
        result = run_backtest(
            shape, history["bars"], replay=subprocess_replay(args.nautilus_python, args.preflight), config=dualtrack_config(),
            instrument=INSTRUMENT, source=SOURCE, funding=history["funding"], funding_from=history["funding_from"],
            step=timedelta(hours=args.step_hours), workers=args.workers,
        )
    except BacktestUnavailable as error:
        result = {"schema_version": SCHEMA, "status": "unavailable", "reason": str(error), "generated_at": datetime.now(timezone.utc).isoformat()}
    except Exception as error:  # noqa: BLE001 - the page says it failed; it must not take anything down.
        result = {"schema_version": SCHEMA, "status": "unavailable", "reason": f"回测出错：{type(error).__name__}", "detail": str(error)[-500:], "generated_at": datetime.now(timezone.utc).isoformat()}
    result["duration_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    write_result(args.output, result)
    print(json.dumps({key: result.get(key) for key in ("status", "reason", "duration_seconds")}, ensure_ascii=False))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
