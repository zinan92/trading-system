"""回测工作台 (#1335): run up to three parameter sets of one strategy over one stored history.

Runs apart from every service and writes one JSON file. History is loaded once; the variants run
one after another in this process. A failure is a result too (``status: unavailable``), because the
page shows it. Nothing here reaches a ledger, a venue or a gate.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from services.datafeed_market_client import DatafeedMarketClient
from services.dca_backtest import run_backtest as run_dca
from services.dca_backtest import validate as validate_dca
from services.dualtrack_config import dualtrack_config
from services.grid_backtest import BacktestUnavailable, load_history

SCHEMA = "backtest-compare-v1"
MAX_VARIANTS = 3
# asset -> (datafeed source, instrument, asset class). One stored series each; no fallback between them.
ASSETS: dict[str, tuple[str, str, str]] = {
    "XAU": ("okx_swap_public", "XAU-USDT-SWAP", "commodity"),
    "BTC": ("okx_swap_crypto_public", "BTC-USDT-SWAP", "crypto"),
}
STRATEGIES = ("dca",)


def check(request: dict[str, Any]) -> dict[str, Any]:
    """The request the page may send, normalized. Raises ValueError naming what is wrong."""

    asset = str(request.get("asset") or "").upper()
    if asset not in ASSETS:
        raise ValueError(f"品种只能是 {' / '.join(ASSETS)}")
    strategy = str(request.get("strategy") or "")
    if strategy not in STRATEGIES:
        raise ValueError("策略目前只开通了定投（dca）")
    variants = request.get("variants")
    if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
        raise ValueError(f"参数要有 1–{MAX_VARIANTS} 组")
    return {"asset": asset, "strategy": strategy, "variants": [validate_dca(v if isinstance(v, dict) else {}) for v in variants]}


def write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def compare(request: dict[str, Any], *, client: Any, config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    checked = check(request)
    source, instrument, asset_class = ASSETS[checked["asset"]]
    fee_rate = float(config["paper_fee_model"]["taker_fee_rate"])
    history = load_history(client, source=source, instrument=instrument, asset_class=asset_class, now=now)
    bars = history["bars"]
    variants = [
        {key: value for key, value in run_dca(params, bars, fee_rate=fee_rate, funding=history["funding"], funding_from=history["funding_from"]).items() if key != "windows"}
        for params in checked["variants"]
    ]
    return {
        "schema_version": SCHEMA, "status": "ready", **checked, "variants": variants,
        "source": source, "instrument": instrument, "fee_rate": fee_rate,
        "history_start": bars[0].opened_at.isoformat(), "history_end": bars[-1].opened_at.isoformat(),
        "funding_from": history["funding_from"], "paper_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--datafeed-url", default="http://127.0.0.1:8100")
    args = parser.parse_args(argv)
    started = datetime.now(timezone.utc)
    write_result(args.output, {"schema_version": SCHEMA, "status": "running", "started_at": started.isoformat()})
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        result = compare(request, client=DatafeedMarketClient(base_url=args.datafeed_url, timeout_seconds=60.0), config=dualtrack_config())
    except (BacktestUnavailable, ValueError) as error:
        result = {"schema_version": SCHEMA, "status": "unavailable", "reason": str(error)}
    except Exception as error:  # noqa: BLE001 - the page says it failed; it must not take anything down.
        result = {"schema_version": SCHEMA, "status": "unavailable", "reason": f"回测出错：{type(error).__name__}", "detail": str(error)[-500:]}
    result["started_at"] = started.isoformat()
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["duration_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    write_result(args.output, result)
    print(json.dumps({key: result.get(key) for key in ("status", "reason", "duration_seconds")}, ensure_ascii=False))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
