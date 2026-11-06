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

from services import backtest_evidence as evidence
from services.config_loader import ROOT
from services.datafeed_market_client import DatafeedMarketClient
from services.dualtrack_config import dualtrack_config
from services.dualtrack_execution_contract import execution_contract_evidence
from services.grid_backtest import (MAX_DATA_AGE, SCHEMA, STEP, WINDOW, BacktestUnavailable, load_history, run_backtest, shape_from_plan,
                                    subprocess_replay)

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
    code = evidence.code_identity(ROOT)
    inputs = {"preflight_sha256": evidence.file_sha256(args.preflight)}  # hashed, never copied: it names a runtime
    params: dict[str, Any] = {"plan_sha256": evidence.file_sha256(args.plan_path), "step_hours": args.step_hours,
                              "window_days": WINDOW.days, "max_data_age_days": MAX_DATA_AGE.days}
    try:
        config = dualtrack_config()
        shape = shape_from_plan(json.loads(args.plan_path.read_text(encoding="utf-8")))
        contract = execution_contract_evidence(dict(config))
        params |= {"shape": shape, "fee_rate": float(config["paper_fee_model"]["taker_fee_rate"]),
                   "starting_cash": float(config["capital_per_track_usd"]), "max_leverage": float(config["max_leverage"]),
                   "price_increment": str(contract["price_increment"]), "quantity_increment": str(contract["quantity_increment"])}
        history = load_history(DatafeedMarketClient(base_url=args.datafeed_url, timeout_seconds=60.0), source=SOURCE, instrument=INSTRUMENT)
        result = run_backtest(
            shape, history["bars"], replay=subprocess_replay(args.nautilus_python, args.preflight), config=config,
            instrument=INSTRUMENT, source=SOURCE, funding=history["funding"], funding_from=history["funding_from"],
            step=timedelta(hours=args.step_hours), workers=args.workers,
        )
        bars = history["bars"]
        result["evidence"] = evidence.pack(
            code=code, inputs=inputs, params=params,
            data=evidence.data_evidence(source=SOURCE, instrument=INSTRUMENT, bars=bars, funding=history["funding"],
                                        funding_from=history["funding_from"], report=history.get("report")),
            checks=[evidence.history_check(last_bar=bars[-1].opened_at, run_started=started),
                    *evidence.window_checks(result["windows"], length=WINDOW)],
        )
    except BacktestUnavailable as error:
        result = {"schema_version": SCHEMA, "status": "unavailable", "reason": str(error), "generated_at": datetime.now(timezone.utc).isoformat()}
    except Exception as error:  # noqa: BLE001 - the page says it failed; it must not take anything down.
        result = {"schema_version": SCHEMA, "status": "unavailable", "reason": f"回测出错：{type(error).__name__}", "detail": str(error)[-500:], "generated_at": datetime.now(timezone.utc).isoformat()}
    if "evidence" not in result:
        result["evidence"] = evidence.pack(code=code, inputs=inputs, params=params, data=None, reason=result.get("reason"))
    result["duration_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    write_result(args.output, result)
    print(json.dumps({key: result.get(key) for key in ("status", "reason", "duration_seconds")}, ensure_ascii=False))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
