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
from services.grid_backtest import BacktestUnavailable, Replay, load_history, subprocess_replay
from services.grid_backtest import run_backtest as run_grid

SCHEMA = "backtest-compare-v1"
MAX_VARIANTS = 3
# asset -> (datafeed source, instrument, asset class). One stored series each; no fallback between them.
ASSETS: dict[str, tuple[str, str, str]] = {
    "XAU": ("okx_swap_public", "XAU-USDT-SWAP", "commodity"),
    "BTC": ("okx_swap_crypto_public", "BTC-USDT-SWAP", "crypto"),
}
STRATEGIES = ("dca", "grid")
# Grids replay through Paper's Nautilus replay, which needs that instrument's contract. Only gold
# has one today (the Paper preflight); BTC grids wait for #1337 rather than borrow gold's contract.
GRID_ASSETS = ("XAU",)
GRID_WORKERS = 3  # of 10 cores; Paper runs on the same machine


def validate_grid(params: dict[str, Any]) -> dict[str, Any]:
    """A grid laid around the current price, in percent. Raises ValueError naming the field."""

    try:
        direction = str(params["direction"])
        below, above = float(params["below_pct"]), float(params["above_pct"])
        levels = int(params["levels"])
        notional = float(params["notional_per_level"])
        stop = params.get("stop_pct")
        stop = None if stop in (None, "", 0, 0.0) else float(stop)
    except (KeyError, TypeError, ValueError):
        raise ValueError("网格参数要有方向、下沿 %、上沿 %、格数、每格金额") from None
    if direction not in {"long", "short", "neutral"}:
        raise ValueError("方向只能是做多、做空或双向")
    if not (0.5 <= below <= 30 and 0.5 <= above <= 30):
        raise ValueError("上下沿要在现价 0.5%–30% 之内")
    if not 2 <= levels <= 30 or float(params["levels"]) != levels:
        raise ValueError("格数要是 2–30 的整数")
    if not 10 <= notional <= 100_000:
        raise ValueError("每格金额要在 10–100000 USDT 之间")
    if stop is not None:
        if direction == "neutral":
            raise ValueError("双向网格用上下沿止损，不另设止损")
        edge = below if direction == "long" else above
        if not 0 < stop < edge:
            raise ValueError("止损要在现价和区间边沿之间")
    return {"direction": direction, "below_pct": below, "above_pct": above, "levels": levels, "notional_per_level": notional, "stop_pct": stop}


def grid_shape(params: dict[str, Any], *, equity: float) -> dict[str, Any]:
    """The same geometry park_strategy_plan builds, laid at price 1.0; grid_backtest.scaled moves it."""

    lower, upper = 1 - params["below_pct"] / 100, 1 + params["above_pct"] / 100
    spacing = (upper - lower) / (params["levels"] + 1)
    prices = [lower + spacing * (i + 1) for i in range(params["levels"])]
    direction = params["direction"]
    rungs = []
    for i, price in enumerate(prices):
        side = ("buy" if price < 1 else "sell") if direction == "neutral" else ("buy" if direction == "long" else "sell")
        take_profit = (prices[i + 1] if i + 1 < len(prices) else upper) if side == "buy" else (prices[i - 1] if i > 0 else lower)
        rungs.append({"rung": i + 1, "side": side, "price": price, "take_profit": take_profit})
    stop = params["stop_pct"]
    return {
        "direction": direction, "market_price": 1.0, "lower": lower, "upper": upper,
        "stop": None if stop is None else (1 - stop / 100 if direction == "long" else 1 + stop / 100),
        "target": None, "quantity": params["notional_per_level"], "account_equity": equity, "rungs": rungs,
    }


def check(request: dict[str, Any]) -> dict[str, Any]:
    """The request the page may send, normalized. Raises ValueError naming what is wrong."""

    asset = str(request.get("asset") or "").upper()
    if asset not in ASSETS:
        raise ValueError(f"品种只能是 {' / '.join(ASSETS)}")
    strategy = str(request.get("strategy") or "")
    if strategy not in STRATEGIES:
        raise ValueError("策略只能是定投（dca）或网格（grid）")
    if strategy == "grid" and asset not in GRID_ASSETS:
        raise ValueError("BTC 网格回测还没开通（#1337）：回放需要 BTC 的合约说明")
    variants = request.get("variants")
    if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
        raise ValueError(f"参数要有 1–{MAX_VARIANTS} 组")
    validate = validate_dca if strategy == "dca" else validate_grid
    return {"asset": asset, "strategy": strategy, "variants": [validate(v if isinstance(v, dict) else {}) for v in variants]}


def write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def preflight_instrument(path: Path) -> str:
    """The contract the replay will build: the newest preflight's instrument symbol ("" if unreadable)."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    latest = payload[-1] if isinstance(payload, list) and payload else payload
    return str(((latest or {}).get("instrument") or {}).get("symbol") or "") if isinstance(latest, dict) else ""


def compare(request: dict[str, Any], *, client: Any, config: dict[str, Any], replay: Replay | None = None,
            replay_instrument: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    checked = check(request)
    source, instrument, asset_class = ASSETS[checked["asset"]]
    if checked["strategy"] == "grid":
        if replay is None:
            raise BacktestUnavailable("网格回测需要 Paper 的回放环境")
        # The replay builds its contract from the Paper preflight; a different venue's contract would
        # replay gold history against the wrong instrument, so say so before reading any history.
        if replay_instrument is not None and replay_instrument != instrument:
            raise BacktestUnavailable(f"Paper 预检的合约是 {replay_instrument or '（读不到）'}，回测历史是 {instrument}，两者不一致")
    fee_rate = float(config["paper_fee_model"]["taker_fee_rate"])
    history = load_history(client, source=source, instrument=instrument, asset_class=asset_class, now=now)
    bars, funding, funding_from = history["bars"], history["funding"], history["funding_from"]
    if checked["strategy"] == "dca":
        variants = [
            {key: value for key, value in run_dca(params, bars, fee_rate=fee_rate, funding=funding, funding_from=funding_from).items() if key != "windows"}
            for params in checked["variants"]
        ]
    else:
        assert replay is not None
        equity = float(config["capital_per_track_usd"])
        variants = []
        for params in checked["variants"]:
            result = run_grid(grid_shape(params, equity=equity), bars, replay=replay, config=config, instrument=instrument, source=source,
                              funding=funding, funding_from=funding_from, workers=GRID_WORKERS, now=now)
            variants.append({"params": params, "summary": result["summary"], "account_equity": equity})
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
    parser.add_argument("--preflight", type=Path, help="Paper preflight (grid only: the instrument contract the replay uses)")
    parser.add_argument("--nautilus-python", help="Python with nautilus_trader (grid only)")
    args = parser.parse_args(argv)
    started = datetime.now(timezone.utc)
    write_result(args.output, {"schema_version": SCHEMA, "status": "running", "started_at": started.isoformat()})
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        replay = subprocess_replay(args.nautilus_python, args.preflight) if args.preflight and args.nautilus_python else None
        result = compare(request, client=DatafeedMarketClient(base_url=args.datafeed_url, timeout_seconds=60.0), config=dualtrack_config(), replay=replay,
                         replay_instrument=preflight_instrument(args.preflight) if args.preflight else None)
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
